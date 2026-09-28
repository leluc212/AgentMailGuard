"""Live-stack check for task 3.16: queries are embedded and reach the pgvector branch.

Run on the host after `make up` (reads .env like every host tool):

    make retrieval-gate

This is a wiring check. The default stack embeds with FakeEmbedder (hash-based vectors) and
pgvector applies no similarity floor, so any non-zero query vector returns the tenant's only
chunk: it proves queries are embedded and reach pgvector, not semantic quality.

Checks, stopping at the first failure:
  1. A knowledge document uploaded through the API is ingested to `active`.
  2. /v1/search/debug with a query sharing no word with the document finds the document
     through the vector branch alone (not degraded, lexical_count 0, a vector hit on it).
  3. The same holds for the billing email's own query (subject, body, intent, category), so
     the lexical branch cannot find the document for that email.
  4. The billing email reaches DRAFTED, and its CONTEXT_READY event reports
     retrieved_chunks_count >= 1: with check 3, the ai-worker found the document only through
     its embedded query.
Creates one throwaway organization, deletes it at the end, and deletes the uploaded file.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from uuid import UUID

import aio_pika
import asyncpg
import httpx
from stack_smoke import (
    SmokeFailure,
    build_mime,
    check_services,
    inject_email,
    seed_tenant,
    wait_for_state,
)

from packages.core.settings import AppSettings
from packages.core.storage import get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.domain.state_machine import JobState

API = "http://localhost:8000/v1"
TIMEOUT_S = 60.0
DOCUMENT = (
    "Aurora teapot calibration: rotate the blue valve clockwise until the gauge settles, "
    "then log the reading in the maintenance ledger."
)
# Shares no word with DOCUMENT, so the lexical branch cannot find it (checked below).
QUERY = "How should a sunrise kettle be tuned?"
EMAIL_SUBJECT = "Overdue payment failure on account"
EMAIL_BODY = "Our account balance is past due after a payment failure. Please advise."
EMAIL_INTENT = "overdue_payment"  # config/triage_rules.yaml, urgent-billing


async def upload_and_wait(http: httpx.AsyncClient, org_id: UUID) -> tuple[str, str | None]:
    """Upload the document; return its id and object key once it is `active`."""
    headers = {"X-Organization-ID": str(org_id)}
    resp = await http.post(
        f"{API}/knowledge/documents",
        headers=headers,
        files={"file": ("aurora.txt", DOCUMENT.encode(), "text/plain")},
        data={"title": "Aurora teapot calibration", "category": "billing"},
    )
    if resp.status_code != 202:
        raise SmokeFailure(f"upload returned {resp.status_code}: {resp.text}")
    document = resp.json()["document"]
    doc_id = str(document["id"])
    object_key = document.get("object_key")
    deadline = time.monotonic() + TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        got = await http.get(f"{API}/knowledge/documents/{doc_id}", headers=headers)
        status = got.json().get("status") if got.status_code == 200 else None
        if status == "active":
            return doc_id, object_key
        if status == "failed":
            break
        await asyncio.sleep(1.0)
    raise SmokeFailure(f"document {doc_id} ended in {status!r}, expected 'active'")


async def check_vector_only_hit(
    http: httpx.AsyncClient, org_id: UUID, doc_id: str, query: dict[str, Any]
) -> None:
    """Assert /search/debug finds the document through the vector branch alone."""
    resp = await http.post(
        f"{API}/search/debug",
        headers={"X-Organization-ID": str(org_id)},
        json={**query, "apply_rerank": False},
    )
    if resp.status_code != 200:
        raise SmokeFailure(f"/search/debug returned {resp.status_code}: {resp.text}")
    body = resp.json()
    ex = body["explanation"]
    hit_docs = {str(r["document_id"]) for r in body.get("vector_results", [])}
    if ex["retrieval_degraded"] or ex["lexical_count"] != 0 or doc_id not in hit_docs:
        raise SmokeFailure(
            f"expected a vector-only hit on document {doc_id}: {ex}, vector hits {sorted(hit_docs)}"
        )
    print(
        f"ok   /search/debug {sorted(query)} -> vector hit on the document, lexical_count 0, "
        f"query vector dim {body['constructed_query']['query_vector_dimension']}"
    )


async def check_ai_worker(
    settings: AppSettings, pool: asyncpg.Pool[Any], channel: Any, org_id: UUID, mailbox: UUID
) -> None:
    storage = get_storage_client(settings.object_storage)
    raw_keys: list[str] = []
    job_id = await inject_email(
        settings,
        pool,
        storage,
        channel,
        org_id,
        mailbox,
        build_mime("client@enterprise.example.com", EMAIL_SUBJECT, EMAIL_BODY),
        raw_keys,
    )
    await wait_for_state(pool, org_id, job_id, {JobState.DRAFTED.value})
    count = await pool.fetchval(
        "SELECT (payload->>'retrieved_chunks_count')::int FROM processing_event "
        "WHERE job_id = $1 AND state_to = 'CONTEXT_READY' ORDER BY created_at LIMIT 1",
        job_id,
    )
    for key in raw_keys:
        try:
            await storage.delete_object(settings.object_storage.bucket_raw_mime, key)
        except Exception as err:
            print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
    if not count:
        raise SmokeFailure(f"job {job_id} retrieved {count!r} chunks, expected >= 1")
    print(f"ok   billing email -> ai-worker retrieved {count} chunk(s) via the embedded query")


async def run() -> None:
    settings = AppSettings()
    await check_services(settings)
    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    org_id: UUID | None = None
    object_key: str | None = None
    try:
        channel = await connection.channel(on_return_raises=True)
        org_id, mailbox_id = await seed_tenant(pool)
        async with httpx.AsyncClient(timeout=10.0) as http:
            doc_id, object_key = await upload_and_wait(http, org_id)
            print(f"ok   document {doc_id} ingested -> active")
            await check_vector_only_hit(
                http, org_id, doc_id, {"query": QUERY, "category": "billing"}
            )
            await check_vector_only_hit(
                http,
                org_id,
                doc_id,
                {
                    "subject": EMAIL_SUBJECT,
                    "body_text": EMAIL_BODY,
                    "intent": EMAIL_INTENT,
                    "category": "billing",
                },
            )
        await check_ai_worker(settings, pool, channel, org_id, mailbox_id)
    finally:
        if org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", org_id)
        if object_key:
            try:
                storage = get_storage_client(settings.object_storage)
                await storage.delete_object(settings.object_storage.bucket_knowledge, object_key)
            except Exception as err:
                print(
                    f"warn could not delete knowledge object {object_key}: {err}", file=sys.stderr
                )
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("RETRIEVAL GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

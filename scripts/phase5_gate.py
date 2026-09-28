"""Live-stack check for the Phase 5 gate (specs/tasks.md 5.6; R13.3, R13.5, R16.1).

Run on the host by the owner, never under pytest (it needs a live model key, R24.5). The
stack must run the openai provider on the Gemini endpoint (task 5.0) and the mock embedder:

    # .env: LLM__PROVIDER=openai, LLM__OPENAI_API_KEY=<Gemini key>,
    #       LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai,
    #       LLM__FAST_MODEL / LLM__STRONG_MODEL / LLM__FALLBACK_MODEL, EMBEDDING__MOCK=true
    make up
    make phase5-gate

The check reads the host .env through AppSettings. Compose forwards the same keys into the
containers (task 5.0), so the host settings stand in for the containers' settings.

Checks, stopping at the first failure:
  1. The settings name a real model and the mock embedder. The key is never printed.
  2. The API is ready and every hosted queue has a consumer.
  3. The order-status procedure (the seeded "Order Status & Tracking Numbers" chunk) is uploaded
     once per ai-worker lane category and ingested to `active`. Retrieval filters documents by
     the triage category (packages/retrieval/query_builder.py:389), and the live triage decides
     the category, so every lane gets a copy.
  4. Alice's fixture email, from her address, reaches DRAFTED through
     normalize -> triage -> ai-worker.
  5. Its CONTEXT_READY event records customer FOUND, ORD-82915 FOUND, business data not
     degraded, and at least one retrieved chunk.
  6. The one draft states the exact seeded status (case-insensitive, whole word; an underscored
     status also matches its spaced form), cites a chunk of a procedure copy, and has no
     citation mismatch.
The procedure text does not contain the status (asserted by tests/unit/test_phase5_gate.py),
so a draft stating it took it from [BUSINESS DATA]. With EMBEDDING__MOCK=true the procedure
reaches the prompt through the vector branch (no similarity floor, one chunk per category): the
lexical branch ANDs the order number, which the procedure does not contain. That proves
wiring, not semantic retrieval quality.
Creates one throwaway organization and deletes it at the end, with its raw MIME and knowledge
objects.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from email.utils import formataddr
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import httpx
import stack_smoke
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
from packages.db.fixtures import BUSINESS_CUSTOMERS, BUSINESS_ORDERS, FIXTURE_EMAILS, KNOWLEDGE_DOCS
from packages.domain.state_machine import JobState
from services.ai_worker.main import resolve_lane_queues

API = "http://localhost:8000/v1"
INGEST_TIMEOUT_S = 60.0
# Two live model calls (triage may use the LLM stage, then the draft) on a free-tier endpoint.
DRAFT_TIMEOUT_S = 120.0
PROCEDURE_DOC_TITLE = "Acme Order Fulfillment & Tracking Guidelines"

ALICE = next(c for c in BUSINESS_CUSTOMERS if c["email"] == "alice.smith@clientcorp.com")
ORDER = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-82915")
EMAIL = next(
    f for f in FIXTURE_EMAILS if f.sender_email == ALICE["email"] and "82915" in f.body_text
)
PROCEDURE = next(
    chunk
    for doc in KNOWLEDGE_DOCS
    if doc.org_id == ALICE["organization_id"] and doc.title == PROCEDURE_DOC_TITLE
    for chunk in doc.chunks
    if chunk.chunk_index == 0
)


def check_settings(settings: AppSettings) -> None:
    """Fail unless a real model and the mock embedder are configured; never print the key."""
    if settings.llm.provider == "fake":
        raise SmokeFailure(
            "LLM__PROVIDER is 'fake': the gate needs a real model (set it up as in task 5.0)"
        )
    if not settings.embedding.mock:
        raise SmokeFailure("EMBEDDING__MOCK must be true: a live embedder is out of Phase 5 scope")
    print(
        f"ok   provider {settings.llm.provider} at {settings.llm.openai_base_url}; models "
        f"{settings.llm.fast_model} / {settings.llm.strong_model}; mock embedder"
    )


def lane_categories(settings: AppSettings) -> list[str]:
    """The categories of the lanes the ai-worker consumes (`email.<category>.<lane>`)."""
    return sorted({queue.split(".")[1] for queue in resolve_lane_queues(settings)})


def check_context(payload: dict[str, Any], reference: str) -> None:
    """The CONTEXT_READY payload records the customer and the order as FOUND, not degraded."""
    if payload.get("business_data_degraded") is not False:
        raise SmokeFailure(
            f"business data degraded or unrecorded: {payload.get('business_data_degraded')!r}"
        )
    if payload.get("customer_status") != "FOUND":
        raise SmokeFailure(f"customer_status {payload.get('customer_status')!r}, expected FOUND")
    rows = payload.get("business_fact_statuses") or []
    facts = [f for f in rows if f.get("reference") == reference]
    if [f.get("status") for f in facts] != ["FOUND"]:
        raise SmokeFailure(f"{reference} facts {facts}, expected one FOUND fact")
    if not payload.get("retrieved_chunks_count"):
        raise SmokeFailure(
            f"retrieved_chunks_count {payload.get('retrieved_chunks_count')!r}, expected >= 1"
        )


def check_draft(body: str, citations: Any, mismatch: bool, status: str, doc_ids: set[str]) -> str:
    """The draft states the seeded status and cites a procedure copy; returns the cited doc id."""
    forms = {status.lower(), status.lower().replace("_", " ")}
    if not any(re.search(rf"\b{re.escape(form)}\b", body.lower()) for form in forms):
        raise SmokeFailure(f"draft does not state the seeded status {status!r}: {body[:400]!r}")
    if mismatch:
        raise SmokeFailure("draft has a citation mismatch")
    parsed = json.loads(citations) if isinstance(citations, str) else (citations or [])
    cited = {str(c.get("document_id")) for c in parsed}
    hits = sorted(cited & doc_ids)
    if not hits:
        raise SmokeFailure(
            f"draft cites documents {sorted(cited)}, none of the procedure copies {sorted(doc_ids)}"
        )
    return hits[0]


async def seed_business(pool: asyncpg.Pool[Any], org_id: UUID) -> None:
    """Alice and her ORD-82915, copied from the demo fixtures into the throwaway org."""
    customer_id = uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO customer (id, organization_id, email, name, account_status, tier) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            customer_id,
            org_id,
            ALICE["email"],
            ALICE["name"],
            ALICE.get("account_status", "active"),
            ALICE.get("tier", "standard"),
        )
        await conn.execute(
            'INSERT INTO "order" (id, organization_id, customer_id, order_number, status, total) '
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid4(),
            org_id,
            customer_id,
            ORDER["order_number"],
            ORDER["status"],
            ORDER["total"],
        )
    print(f"ok   seeded {ALICE['email']} with {ORDER['order_number']} ({ORDER['status']})")


async def upload_procedure(
    http: httpx.AsyncClient, org_id: UUID, category: str
) -> tuple[str, str | None]:
    """Upload one procedure copy under `category`; return its id and object key once active."""
    headers = {"X-Organization-ID": str(org_id)}
    resp = await http.post(
        f"{API}/knowledge/documents",
        headers=headers,
        files={"file": ("order-status.txt", PROCEDURE.content.encode(), "text/plain")},
        data={"title": f"{PROCEDURE.title} ({category})", "category": category},
    )
    if resp.status_code != 202:
        raise SmokeFailure(f"upload returned {resp.status_code}: {resp.text}")
    document = resp.json()["document"]
    doc_id = str(document["id"])
    object_key = document.get("object_key")
    deadline = time.monotonic() + INGEST_TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        got = await http.get(f"{API}/knowledge/documents/{doc_id}", headers=headers)
        status = got.json().get("status") if got.status_code == 200 else None
        if status == "active":
            return doc_id, object_key
        if status == "failed":
            break
        await asyncio.sleep(1.0)
    raise SmokeFailure(f"document {doc_id} ({category}) ended in {status!r}, expected 'active'")


async def run() -> None:
    settings = AppSettings()
    check_settings(settings)
    await check_services(settings)
    stack_smoke.TIMEOUT_S = DRAFT_TIMEOUT_S  # wait_for_state reads it at call time
    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    storage = get_storage_client(settings.object_storage)
    org_id: UUID | None = None
    raw_keys: list[str] = []
    object_keys: list[str] = []
    try:
        channel = await connection.channel(on_return_raises=True)
        org_id, mailbox_id = await seed_tenant(pool)
        await seed_business(pool, org_id)

        doc_ids: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=10.0) as http:
            for category in lane_categories(settings):
                doc_id, object_key = await upload_procedure(http, org_id, category)
                doc_ids[doc_id] = category
                if object_key:
                    object_keys.append(object_key)
        print(f"ok   procedure ingested -> active for {sorted(doc_ids.values())}")

        job_id = await inject_email(
            settings,
            pool,
            storage,
            channel,
            org_id,
            mailbox_id,
            # Display-name From header, so the parseaddr split runs end to end (Review Focus 1).
            build_mime(
                formataddr((EMAIL.sender_name, EMAIL.sender_email)), EMAIL.subject, EMAIL.body_text
            ),
            raw_keys,
        )
        await wait_for_state(pool, org_id, job_id, {JobState.DRAFTED.value})
        print(f"ok   {EMAIL.subject!r} from {EMAIL.sender_email} -> ai-worker, job DRAFTED")

        raw_payload = await pool.fetchval(
            "SELECT payload::text FROM processing_event WHERE organization_id = $1 "
            "AND job_id = $2 AND state_to = 'CONTEXT_READY' ORDER BY created_at LIMIT 1",
            org_id,
            job_id,
        )
        if raw_payload is None:
            raise SmokeFailure(f"job {job_id} has no CONTEXT_READY event")
        payload = json.loads(raw_payload)
        check_context(payload, str(ORDER["order_number"]))
        print(
            f"ok   CONTEXT_READY: customer FOUND, {ORDER['order_number']} FOUND, not degraded, "
            f"{payload['retrieved_chunks_count']} chunk(s) retrieved"
        )

        rows = await pool.fetch(
            "SELECT body, citations, citation_mismatch, model_tier, escalation_reason "
            "FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
            org_id,
            job_id,
        )
        if len(rows) != 1:
            raise SmokeFailure(f"job {job_id} has {len(rows)} drafts, expected 1")
        draft = rows[0]
        hit = check_draft(
            draft["body"] or "",
            draft["citations"],
            bool(draft["citation_mismatch"]),
            str(ORDER["status"]),
            set(doc_ids),
        )
        print(
            f"ok   draft states {ORDER['status']!r} and cites the procedure copy filed under "
            f"{doc_ids[hit]!r}; tier {draft['model_tier']} ({draft['escalation_reason']})"
        )
        print(f"     draft: {draft['body']}")
    finally:
        if org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", org_id)
        for key in raw_keys:
            try:
                await storage.delete_object(settings.object_storage.bucket_raw_mime, key)
            except Exception as err:
                print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
        for key in object_keys:
            try:
                await storage.delete_object(settings.object_storage.bucket_knowledge, key)
            except Exception as err:
                print(f"warn could not delete knowledge object {key}: {err}", file=sys.stderr)
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("PHASE 5 GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

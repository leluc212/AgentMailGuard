"""Live-stack smoke check for the RA gate (RA.13; R24.7 partial, R20.1).

Run on the host after `make up` (reads .env like every host tool):

    make smoke

Checks, stopping at the first failure:
  1. The API is ready and every hosted queue has at least one consumer.
  2. A billing email injected into email.normalize is normalized, classified by rule and
     routed to email.billing.<lane>; the ai-worker drafts it (job DRAFTED, one draft).
  3. A no-reply newsletter reaches COMPLETED (early exit before any AI stage).
  4. A sync request naming another tenant's mailbox is dead-lettered with its reason.
Creates one throwaway organization and deletes it at the end. The routed billing job stays
in email.billing.<lane> until an AI worker exists (task 4.11 onward).
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import httpx
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractQueue

from packages.broker.envelope import JobEnvelope
from packages.core.settings import AppSettings
from packages.core.storage import ObjectKeyBuilder, StorageProtocol, get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState

HOSTED_QUEUES = ("mail.sync.requested", "email.normalize", "email.triage", "knowledge.ingest")
TIMEOUT_S = 45.0
API_READYZ = "http://localhost:8000/readyz"


class SmokeFailure(RuntimeError):  # noqa: N818
    """A gate check failed; the message says which and why."""


def build_mime(sender: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "support@smoke.example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain="smoke.example.com")
    msg.set_content(body)  # text/plain only: HTML offload targets an unprovisioned bucket (W4)
    return msg.as_bytes()


async def check_services(settings: AppSettings) -> None:
    async with httpx.AsyncClient(timeout=5.0) as http:
        ready = await http.get(API_READYZ)
        if ready.status_code != 200:
            raise SmokeFailure(f"api /readyz returned {ready.status_code}: {ready.text}")
        vhost = quote(settings.broker.vhost, safe="")
        resp = await http.get(
            f"http://{settings.broker.host}:15672/api/queues/{vhost}",
            auth=(settings.broker.user, settings.broker.password),
            params={"columns": "name,consumers"},
        )
        resp.raise_for_status()
        consumers = {q["name"]: q["consumers"] for q in resp.json()}
    missing = [q for q in HOSTED_QUEUES if consumers.get(q, 0) < 1]
    if missing:
        raise SmokeFailure(f"queues without a consumer: {missing}")
    print("ok   api ready; consumers on " + ", ".join(HOSTED_QUEUES))


async def seed_tenant(pool: asyncpg.Pool[Any]) -> tuple[UUID, UUID]:
    org_id, mailbox_id = uuid4(), uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"smoke-{org_id.hex[:8]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, status) "
            "VALUES ($1, $2, 'gmail', $3, 'active')",
            mailbox_id,
            org_id,
            f"smoke-{mailbox_id.hex[:8]}@smoke.example.com",
        )
    return org_id, mailbox_id


async def inject_email(
    settings: AppSettings,
    pool: asyncpg.Pool[Any],
    storage: StorageProtocol,
    channel: AbstractChannel,
    org_id: UUID,
    mailbox_id: UUID,
    mime: bytes,
    raw_keys: list[str],
) -> UUID:
    """Archive raw MIME, create the RECEIVED job and publish the normalize envelope,
    exactly as SyncOrchestrator does for a fetched message."""
    provider_message_id = f"smoke-{uuid4().hex}"
    bucket = settings.object_storage.bucket_raw_mime
    key = ObjectKeyBuilder.raw_mime(org_id, mailbox_id, provider_message_id)
    await storage.put_bytes(bucket=bucket, key=key, data=mime, content_type="message/rfc822")
    raw_keys.append(key)

    job_id = uuid4()
    idem = f"smoke:{org_id}:{provider_message_id}"
    await PostgresJobStore(pool).create_job(
        Job(
            id=job_id,
            organization_id=org_id,
            job_type="email_pipeline",
            state=JobState.RECEIVED.value,
            idempotency_key=idem,
        )
    )
    envelope = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=f"{idem}:normalize",
        organization_id=str(org_id),
        mailbox_id=str(mailbox_id),
        message_id=provider_message_id,
        job_type="normalize_email",
        payload={
            "job_id": str(job_id),
            "raw_object_key": key,
            "raw_bucket": bucket,
            "provider": "gmail",
            "provider_message_id": provider_message_id,
        },
    )
    exchange = await channel.get_exchange(settings.broker.exchange_email_process)
    await exchange.publish(envelope.to_message(), routing_key=settings.broker.queue_normalize)
    return job_id


async def wait_for_state(
    pool: asyncpg.Pool[Any], org_id: UUID, job_id: UUID, expected: set[str]
) -> str:
    deadline = time.monotonic() + TIMEOUT_S
    state: str | None = None
    while time.monotonic() < deadline:
        state = await pool.fetchval(
            "SELECT state FROM processing_job WHERE id=$1 AND organization_id=$2", job_id, org_id
        )
        if state in expected:
            return str(state)
        if state in {"FAILED", "DEAD_LETTER"}:
            break
        await asyncio.sleep(0.5)
    events = await pool.fetch(
        "SELECT event_type, state_from, state_to, payload FROM processing_event "
        "WHERE job_id=$1 ORDER BY created_at",
        job_id,
    )
    raise SmokeFailure(
        f"job {job_id} ended in {state!r}, expected {sorted(expected)}; "
        f"events: {[dict(e) for e in events]}"
    )


async def wait_for_job_message(queue: AbstractQueue, job_id: str) -> AbstractIncomingMessage:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        msg = await queue.get(no_ack=True, fail=False)
        if msg is None:
            await asyncio.sleep(0.2)
            continue
        if JobEnvelope.from_message(msg).job_id == job_id:
            return msg
    raise SmokeFailure(f"no message for job {job_id} on probe {queue.name} within {TIMEOUT_S}s")


async def run() -> None:
    settings = AppSettings()
    await check_services(settings)

    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    storage = get_storage_client(settings.object_storage)
    org_id: UUID | None = None
    raw_keys: list[str] = []
    try:
        channel = await connection.channel(on_return_raises=True)
        org_id, mailbox_id = await seed_tenant(pool)

        # 2. Billing email -> email.billing.<lane> -> ai-worker, job DRAFTED
        route_probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
        await route_probe.bind(settings.broker.exchange_email_route, routing_key="email.#")
        billing_job = await inject_email(
            settings,
            pool,
            storage,
            channel,
            org_id,
            mailbox_id,
            build_mime(
                "client@enterprise.example.com",
                "Urgent: Overdue payment failure on account",
                "Your account balance is past due with repeated payment failure. Please advise.",
            ),
            raw_keys,
        )
        routed = await wait_for_job_message(route_probe, str(billing_job))
        if not (routed.routing_key or "").startswith("email.billing."):
            raise SmokeFailure(f"billing email routed to {routed.routing_key!r}")
        await wait_for_state(pool, org_id, billing_job, {JobState.DRAFTED.value})
        async with pool.acquire() as conn:
            drafts = await conn.fetchval(
                "SELECT count(*) FROM generated_draft WHERE job_id = $1", billing_job
            )
        if drafts != 1:
            raise SmokeFailure(f"billing job {billing_job} has {drafts} drafts, expected 1")
        print(f"ok   billing email -> {routed.routing_key} -> ai-worker, job DRAFTED (1 draft)")

        # 3. No-reply newsletter -> COMPLETED (early exit)
        newsletter_job = await inject_email(
            settings,
            pool,
            storage,
            channel,
            org_id,
            mailbox_id,
            build_mime(
                "no-reply@news.example.com",
                "Your weekly product digest",
                "Here are this week's product updates. You are receiving this newsletter.",
            ),
            raw_keys,
        )
        await wait_for_state(pool, org_id, newsletter_job, {JobState.COMPLETED.value})
        print("ok   no-reply newsletter -> COMPLETED (early exit)")

        # 4. Cross-tenant sync request -> dead-lettered with its reason
        dlq_probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
        await dlq_probe.bind(
            settings.broker.exchange_dlx, routing_key=settings.broker.queue_mail_sync
        )
        sync_env = JobEnvelope(
            idempotency_key=f"smoke-sync-{uuid4()}",
            job_type="sync_mailbox",
            organization_id=str(uuid4()),
            mailbox_id=str(mailbox_id),
            payload={"provider": "gmail", "manual": True},
        )
        ingest = await channel.get_exchange(settings.broker.exchange_mail_ingest)
        await ingest.publish(sync_env.to_message(), routing_key=settings.broker.queue_mail_sync)
        dead = await wait_for_job_message(dlq_probe, sync_env.job_id)
        reason = str((dead.headers or {}).get("x-failure-reason", ""))
        if "does not belong" not in reason:
            raise SmokeFailure(f"sync request dead-lettered with unexpected reason: {reason!r}")
        print(f"ok   cross-tenant sync request -> email.dead_letter ({reason})")
    finally:
        if org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", org_id)
        for key in raw_keys:
            try:
                await storage.delete_object(settings.object_storage.bucket_raw_mime, key)
            except Exception as err:
                print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

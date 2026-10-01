"""Live-stack check for the Phase 4 gate (specs/tasks.md, after 4.13b).

Run on the host after `make up` (reads .env like every host tool). Each mode expects the
ai-worker to run with a matching router configuration; restart it between modes:

    uv run python scripts/phase4_gate.py                      # default router settings
    ROUTER_CONFIDENCE_THRESHOLD=1.0 docker compose up -d ai-worker
    uv run python scripts/phase4_gate.py --mode low-confidence
    ROUTER_FORCE_SINGLE_TIER=true docker compose up -d ai-worker
    uv run python scripts/phase4_gate.py --mode single-tier
    docker compose up -d ai-worker                            # back to the defaults

Modes and checks (all through the real path: raw MIME -> normalize -> triage -> ai-worker):
  default         a 12-message reply chain: the last message's job is DRAFTED with one
                  draft on a 12-message thread, the thread was summarized, and the draft's
                  citations are recorded without a mismatch; a one-message thread drafts
                  with no summarization (no thread_state row).
  low-confidence  every classification is below the threshold: the job escalates to the
                  high-capability tier exactly once (one escalated GENERATING event).
  single-tier     the cascade is bypassed: the draft records `single_tier_forced`.
Not covered live: a draft failing validation twice reaches the DLQ. The offline model always
answers validly, so that leg is proven on a real broker by
tests/integration/test_ai_worker_failure_routing_integration.py (4.13a).
Creates one throwaway organization and deletes it at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any
from uuid import UUID

import aio_pika
import asyncpg
from stack_smoke import (
    SmokeFailure,
    check_services,
    inject_email,
    seed_tenant,
    wait_for_state,
)

from packages.core.settings import AppSettings
from packages.core.storage import get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.domain.state_machine import JobState

THREAD_LENGTH = 12
DOMAIN = "gate.example.com"
CUSTOMER = "client@enterprise.example.com"
NOT_ESCALATIONS = {None, "none", "single_tier_forced"}


def build_mime(
    subject: str, body: str, *, message_id: str, parents: list[str] | None = None
) -> bytes:
    msg = EmailMessage()
    msg["From"] = CUSTOMER
    msg["To"] = f"support@{DOMAIN}"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = message_id
    if parents:
        msg["In-Reply-To"] = parents[-1]
        msg["References"] = " ".join(parents)
    msg.set_content(body)
    return msg.as_bytes()


class Gate:
    def __init__(self, settings: AppSettings, pool: asyncpg.Pool[Any]) -> None:
        self.settings = settings
        self.pool = pool
        self.storage = get_storage_client(settings.object_storage)
        self.raw_keys: list[str] = []
        self.org_id: UUID | None = None
        self.mailbox_id: UUID | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None

    async def send(self, mime: bytes) -> UUID:
        """Inject one email and wait until its job is DRAFTED (every gate email is actionable)."""
        assert self.org_id and self.mailbox_id and self.channel
        job_id = await inject_email(
            self.settings,
            self.pool,
            self.storage,
            self.channel,
            self.org_id,
            self.mailbox_id,
            mime,
            self.raw_keys,
        )
        await wait_for_state(self.pool, self.org_id, job_id, {JobState.DRAFTED.value})
        return job_id

    async def one_draft(self, job_id: UUID) -> asyncpg.Record:
        rows = await self.pool.fetch(
            "SELECT thread_id, body, citations, citation_mismatch, model_tier, "
            "escalation_reason FROM generated_draft WHERE job_id = $1",
            job_id,
        )
        if len(rows) != 1:
            raise SmokeFailure(f"job {job_id} has {len(rows)} drafts, expected 1")
        if not rows[0]["body"]:
            raise SmokeFailure(f"job {job_id} has an empty draft body")
        return rows[0]

    async def escalations(self, job_id: UUID) -> int:
        payloads = await self.pool.fetch(
            "SELECT payload->>'escalation_reason' AS reason FROM processing_event "
            "WHERE job_id = $1 AND state_to = 'GENERATING'",
            job_id,
        )
        return sum(1 for p in payloads if p["reason"] not in NOT_ESCALATIONS)

    async def single_message(self, subject: str, body: str) -> tuple[UUID, asyncpg.Record]:
        job_id = await self.send(build_mime(subject, body, message_id=make_msgid(domain=DOMAIN)))
        return job_id, await self.one_draft(job_id)


async def default_mode(gate: Gate) -> None:
    # Leg 1: a 12-message reply chain, drafted through the ai-worker.
    subject = "Payment failure on invoice 4471"
    ids: list[str] = []
    job_id: UUID | None = None
    for n in range(1, THREAD_LENGTH + 1):
        message_id = make_msgid(domain=DOMAIN)
        body = (
            f"Update {n}: our card payment failure on invoice 4471 happened again and the "
            "account balance now shows past due. Please advise on the billing charge."
        )
        job_id = await gate.send(
            build_mime(
                subject if n == 1 else f"Re: {subject}",
                body,
                message_id=message_id,
                parents=list(ids),
            )
        )
        ids.append(message_id)
    assert job_id is not None
    draft = await gate.one_draft(job_id)
    thread_id = draft["thread_id"]
    messages = await gate.pool.fetchval(
        "SELECT count(*) FROM email_message WHERE thread_id = $1", thread_id
    )
    if messages != THREAD_LENGTH:
        raise SmokeFailure(f"thread {thread_id} has {messages} messages, expected {THREAD_LENGTH}")
    state = await gate.pool.fetchrow(
        "SELECT version, summary FROM thread_state WHERE thread_id = $1", thread_id
    )
    if state is None or not state["summary"]:
        raise SmokeFailure(f"thread {thread_id} crossed the threshold but has no summary")
    if draft["citation_mismatch"]:
        raise SmokeFailure(f"job {job_id} draft has a citation mismatch")
    raw = draft["citations"]
    cited = len(json.loads(raw) if isinstance(raw, str) else raw)
    print(
        f"ok   {THREAD_LENGTH}-message thread -> ai-worker, job DRAFTED (1 draft); summary "
        f"v{state['version']}; tier {draft['model_tier']} ({draft['escalation_reason']}); "
        f"citations recorded: {cited}, mismatch false"
    )

    # Leg 3: a one-message thread makes no summarization call.
    job_id, draft = await gate.single_message(
        "Overdue payment failure on account 9020",
        "Our account balance is past due after a payment failure. Please advise.",
    )
    summarized = await gate.pool.fetchval(
        "SELECT count(*) FROM thread_state WHERE thread_id = $1", draft["thread_id"]
    )
    if summarized:
        raise SmokeFailure(f"short thread {draft['thread_id']} was summarized")
    print("ok   one-message thread -> DRAFTED with no summarization (no thread_state row)")


async def low_confidence_mode(gate: Gate) -> None:
    job_id, draft = await gate.single_message(
        "Payment failure on account 5530",
        "The payment failure left our account balance past due. Please advise.",
    )
    if draft["escalation_reason"] != "low_classification_confidence":
        raise SmokeFailure(
            f"draft escalation_reason {draft['escalation_reason']!r}; restart the ai-worker "
            "with ROUTER_CONFIDENCE_THRESHOLD=1.0"
        )
    count = await gate.escalations(job_id)
    if count != 1:
        raise SmokeFailure(f"job {job_id} escalated {count} times, expected exactly 1")
    print(
        f"ok   low-confidence job -> escalated once to {draft['model_tier']} "
        "(low_classification_confidence), DRAFTED"
    )


async def single_tier_mode(gate: Gate) -> None:
    job_id, draft = await gate.single_message(
        "Payment failure on account 7713",
        "A payment failure has left the account balance past due. Please advise.",
    )
    if draft["escalation_reason"] != "single_tier_forced":
        raise SmokeFailure(
            f"draft escalation_reason {draft['escalation_reason']!r}; restart the ai-worker "
            "with ROUTER_FORCE_SINGLE_TIER=true"
        )
    if await gate.escalations(job_id):
        raise SmokeFailure(f"job {job_id} counted an escalation under single-tier mode")
    print(
        f"ok   forced single tier -> job drafted on {draft['model_tier']} "
        "(single_tier_forced), no escalation counted"
    )


MODES = {
    "default": default_mode,
    "low-confidence": low_confidence_mode,
    "single-tier": single_tier_mode,
}


async def run(mode: str) -> None:
    settings = AppSettings()
    await check_services(settings)
    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    gate = Gate(settings, pool)
    try:
        gate.channel = await connection.channel(on_return_raises=True)
        gate.org_id, gate.mailbox_id = await seed_tenant(pool)
        await MODES[mode](gate)
    finally:
        if gate.org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", gate.org_id)
        for key in gate.raw_keys:
            try:
                await gate.storage.delete_object(settings.object_storage.bucket_raw_mime, key)
            except Exception as err:
                print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
        await connection.close()
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=sorted(MODES), default="default")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.mode))
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"PHASE 4 GATE ({args.mode}) OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

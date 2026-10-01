"""Live-stack check for the Phase 6 gate (specs/tasks.md 6.10; R17.1–R17.7, R16.6, R19.3).

Run on the host by the owner, never under pytest (it needs a live Gmail token, R24.5):

    # .env: GMAIL_ACCESS_TOKEN=<fresh token, docs/demo-runbook.md §3.2>
    # config/categories.yaml: dispatch_mode: send_reply on the billing category (gate only)
    make up
    make seed
    make connect-gmail ADDRESS=<test account>
    make phase6-gate            # then send the email it asks for, from another address

Checks, stopping at the first failure:
  1. GMAIL_ACCESS_TOKEN is set (never printed); the categories file routes billing to
     send_reply; the API is ready; every hosted queue and email.dispatch has a consumer.
  2. Exactly one mailbox connected by make connect-gmail exists in the demo tenant.
  3. The owner's email, whose subject carries this run's token, is pulled by
     POST /v1/mailboxes/{id}/resync and reaches DRAFTED through the pipeline.
  4. The draft is reviewable: listed by GET /v1/drafts?status=draft and readable by
     GET /v1/drafts/{id}; its category's dispatch_mode is send_reply.
  5. POST /v1/drafts/{id}/approve takes the job to COMPLETED; the draft is dispatched with a
     provider ref; exactly one outbound email_message replies to the original (R17.7).
  6. In Gmail, the thread holds the sent reply with our Message-ID, In-Reply-To and
     References naming the original, and exactly one "Re: " (R17.2).
  7. Replaying the captured dispatch message and approving again change nothing: same job
     state, outbound rows, Gmail thread size, state transitions and feedback rows (R19.3).
Nothing is deleted afterwards: the mail is real, and the demo tenant keeps the conversation.
Set billing back to create_draft and run make up after the gate (runbook §5.3).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email import message_from_bytes
from email.policy import default as default_policy
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import httpx
import stack_smoke
from connect_gmail import CREDENTIALS_REF, ConnectError, resolve_gmail_token
from dotenv import dotenv_values
from stack_smoke import SmokeFailure, check_services, wait_for_job_message, wait_for_state

from packages.adapters.exceptions import ProviderError
from packages.adapters.gmail import GmailProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.broker.routing import load_categories_from_yaml
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import DEMO_ORG_ID
from packages.domain import DispatchMode
from packages.domain.entities import Mailbox
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry

API = "http://localhost:8000/v1"
EMAIL_TIMEOUT_S = 600.0  # the owner sends the email by hand
RESYNC_EVERY_S = 10.0
DRAFT_TIMEOUT_S = 120.0
DISPATCH_TIMEOUT_S = 60.0
REPLAY_SETTLE_S = 15.0
GATE_BODY = (
    "Hello, my account shows a payment failure and the balance is now past due. What should I do?"
)


@dataclass(frozen=True)
class ReplaySnapshot:
    """Everything a second dispatch could change (R19.3)."""

    job_state: str
    outbound_rows: int
    thread_messages: int
    transitions: int
    feedback_rows: int


def gate_subject(token: str) -> str:
    """A subject the urgent-billing rule classifies as billing, carrying this run's token."""
    return f"Overdue payment failure on account [gate-{token}]"


def check_dispatch_mode(registry: TaxonomyRegistry, category: str) -> None:
    definition = registry.get(category)
    if definition is None:
        raise SmokeFailure(f"category {category!r} is not in config/categories.yaml")
    if definition.dispatch_mode != DispatchMode.SEND_REPLY:
        raise SmokeFailure(
            f"category {category!r} has dispatch_mode {definition.dispatch_mode!s}: set "
            f"dispatch_mode: send_reply on it in config/categories.yaml for the gate, then make up"
        )


def check_api_dispatch_mode(detail: Mapping[str, Any]) -> None:
    """The API reports the mode the dispatch-worker will use (both load the same YAML)."""
    mode = detail.get("dispatch_mode")
    if mode != DispatchMode.SEND_REPLY.value:
        raise SmokeFailure(
            f"GET /v1/drafts/{{id}} reports dispatch_mode {mode!r}, expected 'send_reply': the "
            "API has not loaded the edited config/categories.yaml; run make up"
        )


def check_dispatch_consumer(consumers: Mapping[str, int], queue: str) -> None:
    if consumers.get(queue, 0) < 1:
        raise SmokeFailure(f"no consumer on {queue}: is dispatch-worker healthy? (runbook §4)")


def find_listed_draft(page: Mapping[str, Any], draft_id: str) -> Mapping[str, Any]:
    for item in page.get("items") or []:
        if str(item.get("id")) == draft_id:
            return dict(item)
    raise SmokeFailure(f"draft {draft_id} is not in GET /v1/drafts?status=draft")


def check_outbound_row(
    row: Mapping[str, Any], *, original_rfc822_id: str, original_subject: str
) -> None:
    if not row.get("rfc822_message_id"):
        raise SmokeFailure("outbound email_message has no rfc822_message_id")
    if row.get("in_reply_to") != original_rfc822_id:
        raise SmokeFailure(
            f"outbound in_reply_to {row.get('in_reply_to')!r}, expected {original_rfc822_id!r}"
        )
    if row.get("subject") != f"Re: {original_subject}":
        raise SmokeFailure(f"outbound subject {row.get('subject')!r}, expected one 'Re: '")


def _raw_bytes(payload: bytes | str) -> bytes:
    """RawMessage.raw_payload is bytes from the Gmail adapter; a str is encoded as UTF-8."""
    return payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)


def _ids(value: object) -> list[str]:
    return [part.strip("<>") for part in str(value or "").split() if part.strip("<>")]


def check_gmail_thread(
    messages: Sequence[tuple[str, bytes]],
    *,
    sent_provider_id: str,
    reply_rfc822_id: str,
    original_rfc822_id: str,
    original_subject: str,
) -> None:
    """Gmail's copy of our reply is in the thread and threads onto the original (R17.2)."""
    raw = next((payload for pid, payload in messages if pid == sent_provider_id), None)
    if raw is None:
        raise SmokeFailure(
            f"Gmail thread has no message {sent_provider_id} (ids: {[p for p, _ in messages]})"
        )
    parsed = message_from_bytes(raw, policy=default_policy)
    got_id = str(parsed.get("Message-ID", "")).strip().strip("<>")
    if got_id != reply_rfc822_id:
        raise SmokeFailure(
            f"Gmail's copy has Message-ID {got_id!r}, not ours {reply_rfc822_id!r}: our stored "
            "id cannot thread the customer's next reply (research §8 Q1)"
        )
    if _ids(parsed.get("In-Reply-To")) != [original_rfc822_id]:
        raise SmokeFailure(f"In-Reply-To {parsed.get('In-Reply-To')!r}, expected the original")
    if original_rfc822_id not in _ids(parsed.get("References")):
        raise SmokeFailure(f"References {parsed.get('References')!r} lacks the original")
    if str(parsed.get("Subject", "")) != f"Re: {original_subject}":
        raise SmokeFailure(f"Gmail subject {parsed.get('Subject')!r}, expected one 'Re: '")


def check_replay(before: ReplaySnapshot, after: ReplaySnapshot) -> None:
    for field in ("job_state", "outbound_rows", "thread_messages", "transitions", "feedback_rows"):
        if getattr(before, field) != getattr(after, field):
            raise SmokeFailure(
                f"replay changed {field}: {getattr(before, field)!r} -> {getattr(after, field)!r}"
            )


async def queue_consumers(settings: AppSettings) -> dict[str, int]:
    vhost = quote(settings.broker.vhost, safe="")
    async with httpx.AsyncClient(timeout=5.0) as http:
        resp = await http.get(
            f"http://{settings.broker.host}:15672/api/queues/{vhost}",
            auth=(settings.broker.user, settings.broker.password),
            params={"columns": "name,consumers"},
        )
        resp.raise_for_status()
    return {q["name"]: int(q["consumers"]) for q in resp.json()}


async def connected_mailbox(pool: asyncpg.Pool[Any]) -> Mailbox:
    rows = await pool.fetch(
        "SELECT id, address FROM mailbox WHERE organization_id = $1 AND provider = 'gmail'"
        " AND credentials_ref = $2 AND status = 'active'",
        DEMO_ORG_ID,
        CREDENTIALS_REF,
    )
    if len(rows) != 1:
        raise SmokeFailure(
            f"{len(rows)} connected Gmail mailboxes in the demo tenant, expected 1: "
            "run make connect-gmail ADDRESS=<test account> (runbook §3.4)"
        )
    return Mailbox(
        id=rows[0]["id"],
        organization_id=DEMO_ORG_ID,
        provider="gmail",
        address=rows[0]["address"],
        credentials_ref=CREDENTIALS_REF,
    )


async def wait_for_owner_email(
    http: httpx.AsyncClient, pool: asyncpg.Pool[Any], mailbox: Mailbox, token: str
) -> asyncpg.Record:
    headers = {"X-Organization-ID": str(DEMO_ORG_ID)}
    deadline = time.monotonic() + EMAIL_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = await http.post(f"{API}/mailboxes/{mailbox.id}/resync", headers=headers, json={})
        if resp.status_code == 409:
            raise SmokeFailure(
                f"resync refused (409): {resp.text[:200]}. After a 401 the mailbox is "
                "needs_reauth: mint a token, make up, make connect-gmail again (runbook §7)"
            )
        if resp.status_code != 202:
            raise SmokeFailure(f"resync returned {resp.status_code}: {resp.text[:200]}")
        row = await pool.fetchrow(
            "SELECT id, thread_id, provider_message_id, rfc822_message_id, subject"
            " FROM email_message WHERE organization_id = $1 AND mailbox_id = $2"
            " AND direction = 'inbound' AND subject LIKE $3",
            DEMO_ORG_ID,
            mailbox.id,
            f"%[gate-{token}]%",
        )
        if row is not None:
            return row
        await asyncio.sleep(RESYNC_EVERY_S)
    raise SmokeFailure(f"no email with [gate-{token}] arrived within {EMAIL_TIMEOUT_S:.0f}s")


async def snapshot(
    pool: asyncpg.Pool[Any],
    adapter: GmailProviderAdapter,
    mailbox: Mailbox,
    job_id: UUID,
    draft_id: UUID,
    thread_id: UUID,
    provider_thread_id: str,
) -> ReplaySnapshot:
    org = DEMO_ORG_ID
    gmail_thread = await adapter.get_thread(mailbox, provider_thread_id)
    return ReplaySnapshot(
        job_state=str(
            await pool.fetchval(
                "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
                job_id,
                org,
            )
        ),
        outbound_rows=int(
            await pool.fetchval(
                "SELECT count(*) FROM email_message WHERE organization_id = $1"
                " AND thread_id = $2 AND direction = 'outbound'",
                org,
                thread_id,
            )
        ),
        thread_messages=len(gmail_thread.messages),
        transitions=int(
            await pool.fetchval(
                "SELECT count(*) FROM processing_event WHERE organization_id = $1"
                " AND job_id = $2 AND event_type = 'state_transition'",
                org,
                job_id,
            )
        ),
        feedback_rows=int(
            await pool.fetchval(
                "SELECT count(*) FROM feedback WHERE organization_id = $1 AND draft_id = $2",
                org,
                draft_id,
            )
        ),
    )


async def run() -> None:
    settings = AppSettings()
    gmail_token = resolve_gmail_token(os.environ, dotenv_values(".env"))
    print("ok   GMAIL_ACCESS_TOKEN is set (not printed)")
    registry = TaxonomyRegistry()
    load_categories_from_yaml(settings.routing.categories_config_path, registry)
    check_dispatch_mode(registry, "billing")
    print("ok   config/categories.yaml: billing dispatch_mode send_reply")
    await check_services(settings)
    check_dispatch_consumer(await queue_consumers(settings), settings.broker.queue_dispatch)
    print(f"ok   consumer on {settings.broker.queue_dispatch}")

    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    try:
        mailbox = await connected_mailbox(pool)
        print(f"ok   connected mailbox {mailbox.address} ({mailbox.id})")
        adapter = GmailProviderAdapter(access_token=gmail_token)
        token = uuid4().hex[:8]
        subject = gate_subject(token)
        print(
            f"\n>>> From ANOTHER address, send an email to {mailbox.address} now.\n"
            f">>> Subject: {subject}\n>>> Body:    {GATE_BODY}\n"
            f">>> Waiting up to {EMAIL_TIMEOUT_S / 60:.0f} minutes...\n"
        )
        headers = {"X-Organization-ID": str(DEMO_ORG_ID)}
        async with httpx.AsyncClient(timeout=15.0) as http:
            inbound = await wait_for_owner_email(http, pool, mailbox, token)
            print(f"ok   email {inbound['provider_message_id']} ingested into our thread")
            job_id = await pool.fetchval(
                "SELECT id FROM processing_job WHERE organization_id = $1 AND message_id = $2",
                DEMO_ORG_ID,
                inbound["id"],
            )
            if job_id is None:
                raise SmokeFailure(f"message {inbound['id']} has no processing job")
            stack_smoke.TIMEOUT_S = DRAFT_TIMEOUT_S
            await wait_for_state(pool, DEMO_ORG_ID, job_id, {JobState.DRAFTED.value})
            category = await pool.fetchval(
                "SELECT category FROM classification_result WHERE organization_id = $1"
                " AND message_id = $2 ORDER BY created_at DESC LIMIT 1",
                DEMO_ORG_ID,
                inbound["id"],
            )
            check_dispatch_mode(registry, str(category))
            draft_id = await pool.fetchval(
                "SELECT id FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
                DEMO_ORG_ID,
                job_id,
            )
            listed = await http.get(f"{API}/drafts", headers=headers, params={"status": "draft"})
            if listed.status_code != 200:
                raise SmokeFailure(f"GET /v1/drafts returned {listed.status_code}")
            find_listed_draft(listed.json(), str(draft_id))
            detail = await http.get(f"{API}/drafts/{draft_id}", headers=headers)
            if detail.status_code != 200:
                raise SmokeFailure(f"GET /v1/drafts/{draft_id} returned {detail.status_code}")
            check_api_dispatch_mode(detail.json())
            print(f"ok   job DRAFTED ({category}); draft {draft_id} listed, readable, send_reply")

            channel = await connection.channel(on_return_raises=True)
            probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
            await probe.bind(
                settings.broker.exchange_email_dispatch, routing_key=settings.broker.queue_dispatch
            )
            approved = await http.post(
                f"{API}/drafts/{draft_id}/approve",
                headers=headers,
                json={"reviewer": "phase6-gate"},
            )
            if not approved.is_success:
                raise SmokeFailure(f"approve returned {approved.status_code}: {approved.text}")
            captured = await wait_for_job_message(probe, str(job_id))
            stack_smoke.TIMEOUT_S = DISPATCH_TIMEOUT_S
            await wait_for_state(pool, DEMO_ORG_ID, job_id, {JobState.COMPLETED.value})
            draft = await pool.fetchrow(
                "SELECT status, provider_ref FROM generated_draft"
                " WHERE organization_id = $1 AND id = $2",
                DEMO_ORG_ID,
                draft_id,
            )
            if draft is None or draft["status"] != "dispatched" or not draft["provider_ref"]:
                raise SmokeFailure(f"draft after dispatch: {dict(draft) if draft else None}")
            outbound = await pool.fetch(
                "SELECT provider_message_id, rfc822_message_id, in_reply_to, subject"
                " FROM email_message WHERE organization_id = $1 AND thread_id = $2"
                " AND direction = 'outbound'",
                DEMO_ORG_ID,
                inbound["thread_id"],
            )
            if len(outbound) != 1:
                raise SmokeFailure(f"{len(outbound)} outbound messages in the thread, expected 1")
            check_outbound_row(
                dict(outbound[0]),
                original_rfc822_id=str(inbound["rfc822_message_id"]),
                original_subject=subject,
            )
            print("ok   approved -> COMPLETED; one outbound email_message replies to the original")

            provider_thread_id = await pool.fetchval(
                "SELECT provider_thread_id FROM email_thread"
                " WHERE organization_id = $1 AND id = $2",
                DEMO_ORG_ID,
                inbound["thread_id"],
            )
            gmail_thread = await adapter.get_thread(mailbox, str(provider_thread_id))
            check_gmail_thread(
                [(m.provider_message_id, _raw_bytes(m.raw_payload)) for m in gmail_thread.messages],
                sent_provider_id=str(outbound[0]["provider_message_id"]),
                reply_rfc822_id=str(outbound[0]["rfc822_message_id"]),
                original_rfc822_id=str(inbound["rfc822_message_id"]),
                original_subject=subject,
            )
            print(f"ok   Gmail thread {provider_thread_id} holds the threaded reply")

            before = await snapshot(
                pool,
                adapter,
                mailbox,
                job_id,
                draft_id,
                inbound["thread_id"],
                str(provider_thread_id),
            )
            exchange = await channel.get_exchange(settings.broker.exchange_email_dispatch)
            replay = JobEnvelope.from_message(captured)
            await exchange.publish(replay.to_message(), routing_key=settings.broker.queue_dispatch)
            again = await http.post(
                f"{API}/drafts/{draft_id}/approve",
                headers=headers,
                json={"reviewer": "phase6-gate"},
            )
            if not again.is_success:
                raise SmokeFailure(f"repeated approve returned {again.status_code}: {again.text}")
            await asyncio.sleep(REPLAY_SETTLE_S)
            after = await snapshot(
                pool,
                adapter,
                mailbox,
                job_id,
                draft_id,
                inbound["thread_id"],
                str(provider_thread_id),
            )
            check_replay(before, after)
            print(
                f"ok   replayed dispatch + repeated approve: nothing sent "
                f"(Gmail thread {after.thread_messages} messages, job {after.job_state})"
            )
    finally:
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except (SmokeFailure, ConnectError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    except ProviderError as exc:
        print(f"FAIL Gmail: {exc} (a 401 means the token expired: runbook §3.2)", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"FAIL HTTP {exc.response.status_code} from {exc.request.url}", file=sys.stderr)
        return 1
    print("PHASE 6 GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

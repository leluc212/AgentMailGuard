"""PostgresDispatchStore transactions (tasks 6.5, 6.6, 6.7; R17.3, R17.4, R17.7, R18.5, R19.2).

The claim and the finish are single transactions: key + queue_name + DISPATCHED together,
and provider ref + outbound email_message + thread + COMPLETED together; every query is
scoped by organization_id.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.dispatch import ClaimStatus, DispatchKeyConflictError, PostgresDispatchStore
from packages.db.draft import insert_draft
from packages.db.job import PostgresJobStore
from packages.domain import DispatchMode
from packages.domain.entities import EmailAddress, GeneratedDraft, Job, NormalizedMessage
from packages.domain.state_machine import JobState

QUEUE = "email.dispatch"


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


@dataclass(frozen=True)
class Seed:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    job_id: uuid.UUID
    draft_id: uuid.UUID


async def _seed(
    pool: asyncpg.Pool,
    *,
    state: JobState = JobState.DRAFTED,
    org_id: uuid.UUID | None = None,
    mailbox_id: uuid.UUID | None = None,
) -> Seed:
    org = org_id or uuid.uuid4()
    mbx = mailbox_id or uuid.uuid4()
    thread_id, msg_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        if org_id is None:
            await conn.execute(
                "INSERT INTO organization (id, name) VALUES ($1, $2)", org, f"org-{org.hex[:6]}"
            )
            await conn.execute(
                "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
                " VALUES ($1, $2, 'gmail', $3, 'Acme Support', 'active')",
                mbx,
                org,
                f"support-{mbx.hex[:6]}@acme.example",
            )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id,"
            " message_count, last_message_at) VALUES ($1, $2, $3, $4, 1, now())",
            thread_id,
            org,
            mbx,
            f"th-{thread_id.hex[:8]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                rfc822_message_id, direction, sender_email, sender_name, recipients,
                subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'inbound', 'alice@customer.example', 'Alice',
                      '[]', 'Where is order 82915?', 'Status of 82915?', now())
            """,
            msg_id,
            org,
            mbx,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            f"orig-{msg_id.hex[:8]}@customer.example",
        )
        for category, age in (("support", "1 hour"), ("billing", "0 seconds")):
            await conn.execute(
                f"""
                INSERT INTO classification_result (
                    id, organization_id, message_id, category, priority, reply_required,
                    retrieval_required, confidence, decided_by, created_at
                ) VALUES ($1, $2, $3, $4, 'normal', true, true, 0.9, 'rule',
                          now() - interval '{age}')
                """,
                uuid.uuid4(),
                org,
                msg_id,
                category,
            )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org,
            message_id=msg_id,
            thread_id=thread_id,
            state=state.value,
            idempotency_key=f"dstore-{uuid.uuid4()}",
        )
    )
    async with pool.acquire() as conn:
        draft = await insert_draft(
            conn,
            GeneratedDraft(
                organization_id=org,
                message_id=msg_id,
                thread_id=thread_id,
                job_id=job.id,
                body="Order ORD-82915 was dispatched on 24 September.",
                status="approved",
            ),
        )
    return Seed(org, mbx, thread_id, msg_id, uuid.UUID(str(job.id)), draft.id)


def _outbound(seed: Seed, provider_message_id: str) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid.uuid4(),
        thread_id=seed.thread_id,
        mailbox_id=seed.mailbox_id,
        organization_id=seed.org_id,
        provider="gmail",
        provider_message_id=provider_message_id,
        sender=EmailAddress("support@acme.example", "Acme Support"),
        received_at=datetime.now(UTC),
        rfc822_message_id="reply-1@acme.example",
        in_reply_to="orig-1@customer.example",
        recipients=[EmailAddress("alice@customer.example")],
        subject="Re: Where is order 82915?",
        body_text="Order ORD-82915 was dispatched on 24 September.",
        direction="outbound",
    )


async def _cleanup(pool: asyncpg.Pool, *org_ids: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organization WHERE id = ANY($1::uuid[])", list(org_ids))


async def test_load_is_tenant_scoped_and_reads_the_latest_category(pool: asyncpg.Pool) -> None:
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        ctx = await store.load(seed.org_id, seed.job_id)
        assert ctx is not None
        assert ctx.category == "billing"
        assert ctx.draft.id == seed.draft_id
        assert ctx.thread.provider_thread_id == f"th-{seed.thread_id.hex[:8]}"
        assert ctx.mailbox.address.startswith("support-")
        assert ctx.original.rfc822_message_id == f"orig-{seed.message_id.hex[:8]}@customer.example"
        assert await store.load(uuid.uuid4(), seed.job_id) is None
    finally:
        await _cleanup(pool, seed.org_id)


async def test_claim_sets_key_queue_and_dispatched_in_one_transaction(pool: asyncpg.Pool) -> None:
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        first = await store.claim(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            draft_id=seed.draft_id,
            idempotency_key="key-1",
            queue_name=QUEUE,
        )
        again = await store.claim(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            draft_id=seed.draft_id,
            idempotency_key="key-1",
            queue_name=QUEUE,
        )
        assert first.status is ClaimStatus.CLAIMED
        assert first.draft.dispatch_idempotency_key == "key-1"
        assert again.status is ClaimStatus.RESUMED
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                "SELECT state, queue_name, lease_expires_at FROM processing_job"
                " WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                seed.org_id,
            )
            dispatched_events = await conn.fetchval(
                "SELECT count(*) FROM processing_event WHERE job_id = $1 AND organization_id = $2"
                " AND state_to = 'DISPATCHED'",
                seed.job_id,
                seed.org_id,
            )
        assert job is not None
        assert (job["state"], job["queue_name"], job["lease_expires_at"]) == (
            "DISPATCHED",
            QUEUE,
            None,
        )
        assert dispatched_events == 1
    finally:
        await _cleanup(pool, seed.org_id)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (JobState.COMPLETED, ClaimStatus.ALREADY_COMPLETED),
        (JobState.GENERATING, ClaimStatus.NOT_DISPATCHABLE),
        (JobState.RETRY_PENDING, ClaimStatus.CLAIMED),
    ],
)
async def test_claim_by_job_state(
    pool: asyncpg.Pool, state: JobState, expected: ClaimStatus
) -> None:
    """COMPLETED acks, a job still generating is left alone, RETRY_PENDING is the replay edge."""
    seed = await _seed(pool, state=state)
    try:
        outcome = await PostgresDispatchStore(pool).claim(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            draft_id=seed.draft_id,
            idempotency_key=f"key-{state.value}",
            queue_name=QUEUE,
        )
        assert outcome.status is expected
        want = JobState.DISPATCHED.value if expected is ClaimStatus.CLAIMED else state.value
        assert outcome.job.state == want
    finally:
        await _cleanup(pool, seed.org_id)


async def test_one_key_cannot_be_claimed_by_two_drafts(pool: asyncpg.Pool) -> None:
    """R19.2: UNIQUE (dispatch_idempotency_key); the losing claim rolls back entirely."""
    first = await _seed(pool)
    second = await _seed(pool, org_id=first.org_id, mailbox_id=first.mailbox_id)
    store = PostgresDispatchStore(pool)
    try:
        await store.claim(
            organization_id=first.org_id,
            job_id=first.job_id,
            draft_id=first.draft_id,
            idempotency_key="shared-key",
            queue_name=QUEUE,
        )
        with pytest.raises(DispatchKeyConflictError):
            await store.claim(
                organization_id=second.org_id,
                job_id=second.job_id,
                draft_id=second.draft_id,
                idempotency_key="shared-key",
                queue_name=QUEUE,
            )
        async with pool.acquire() as conn:
            state = await conn.fetchval(
                "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
                second.job_id,
                second.org_id,
            )
        assert state == JobState.DRAFTED.value
    finally:
        await _cleanup(pool, first.org_id)


async def test_record_provider_draft_keeps_the_first_handle(pool: asyncpg.Pool) -> None:
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        first = await store.record_provider_draft(
            organization_id=seed.org_id,
            draft_id=seed.draft_id,
            provider_draft_id="d-1",
            provider_draft_message_id="m-1",
        )
        second = await store.record_provider_draft(
            organization_id=seed.org_id,
            draft_id=seed.draft_id,
            provider_draft_id="d-2",
            provider_draft_message_id="m-2",
        )
        assert (first.provider_draft_id, first.provider_draft_message_id) == ("d-1", "m-1")
        assert second.provider_draft_id == "d-1"
    finally:
        await _cleanup(pool, seed.org_id)


async def test_finish_send_reply_writes_one_outbound_row_and_updates_the_thread(
    pool: asyncpg.Pool,
) -> None:
    """R17.7: the outbound row and COMPLETED commit together; a second finish is a no-op."""
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        await store.claim(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            draft_id=seed.draft_id,
            idempotency_key="key-send",
            queue_name=QUEUE,
        )
        for _ in range(2):
            job = await store.finish(
                organization_id=seed.org_id,
                job_id=seed.job_id,
                draft_id=seed.draft_id,
                mode=DispatchMode.SEND_REPLY,
                provider_ref="sent-1",
                outbound=_outbound(seed, "sent-1"),
            )
            assert job.state == JobState.COMPLETED.value
        async with pool.acquire() as conn:
            outbound = await conn.fetch(
                "SELECT provider_message_id, rfc822_message_id, direction FROM email_message"
                " WHERE organization_id = $1 AND thread_id = $2 AND direction = 'outbound'",
                seed.org_id,
                seed.thread_id,
            )
            thread = await conn.fetchrow(
                "SELECT message_count, participants FROM email_thread"
                " WHERE id = $1 AND organization_id = $2",
                seed.thread_id,
                seed.org_id,
            )
            draft = await conn.fetchrow(
                "SELECT status, provider_ref FROM generated_draft"
                " WHERE id = $1 AND organization_id = $2",
                seed.draft_id,
                seed.org_id,
            )
        assert [dict(r) for r in outbound] == [
            {
                "provider_message_id": "sent-1",
                "rfc822_message_id": "reply-1@acme.example",
                "direction": "outbound",
            }
        ]
        assert thread is not None and thread["message_count"] == 2
        assert "alice@customer.example" in thread["participants"]
        assert draft is not None and (draft["status"], draft["provider_ref"]) == (
            "dispatched",
            "sent-1",
        )
    finally:
        await _cleanup(pool, seed.org_id)


async def test_finish_create_draft_writes_no_outbound_row(pool: asyncpg.Pool) -> None:
    """6.7: in create_draft mode the customer has received nothing, so nothing is recorded."""
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        await store.claim(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            draft_id=seed.draft_id,
            idempotency_key="key-draft",
            queue_name=QUEUE,
        )
        job = await store.finish(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            draft_id=seed.draft_id,
            mode=DispatchMode.CREATE_DRAFT,
            provider_ref="d-1",
            outbound=None,
        )
        assert job.state == JobState.COMPLETED.value
        assert job.result_ref == {
            "draft_id": str(seed.draft_id),
            "dispatch_mode": "create_draft",
            "provider_ref": "d-1",
        }
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM email_message"
                " WHERE organization_id = $1 AND direction = 'outbound'",
                seed.org_id,
            )
        assert count == 0
    finally:
        await _cleanup(pool, seed.org_id)


async def test_job_lock_admits_one_delivery_per_job_and_dies_with_its_session(
    pool: asyncpg.Pool,
) -> None:
    """R19.3: a second holder of the same job is refused without waiting; another job is not
    blocked; a terminated session (a killed worker) releases the lock."""
    org, job, other_job = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store = PostgresDispatchStore(pool)
    async with store.job_lock(org, job) as first:
        assert first is True
        async with store.job_lock(org, job) as second:
            assert second is False
        async with store.job_lock(org, other_job) as unrelated:
            assert unrelated is True
    async with store.job_lock(org, job) as again:
        assert again is True

    killed_pool = await create_pool_from_settings(AppSettings().database)
    killed = PostgresDispatchStore(killed_pool)
    holder = killed.job_lock(org, job)
    assert await holder.__aenter__() is True
    async with store.job_lock(org, job) as while_held:
        assert while_held is False
    killed_pool.terminate()  # the worker process dies: its session ends
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        async with store.job_lock(org, job) as after_kill:
            if after_kill:
                break
        assert asyncio.get_running_loop().time() < deadline, "lock survived its session"
        await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):  # the dead worker's context never exits cleanly
        await holder.__aexit__(None, None, None)


async def test_set_dispatch_queue_routes_a_failed_dispatch_but_not_a_generating_job(
    pool: asyncpg.Pool,
) -> None:
    """R18.7: a dispatch that fails before its claim still replays to the dispatch-worker."""
    drafted = await _seed(pool)
    generating = await _seed(pool, state=JobState.GENERATING)
    store = PostgresDispatchStore(pool)
    try:
        for seed in (drafted, generating):
            await pool.execute(
                "UPDATE processing_job SET queue_name = 'email.triage'"
                " WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                seed.org_id,
            )
            await store.set_dispatch_queue(
                organization_id=seed.org_id, job_id=seed.job_id, queue_name=QUEUE
            )
        queues = [
            await pool.fetchval(
                "SELECT queue_name FROM processing_job WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                seed.org_id,
            )
            for seed in (drafted, generating)
        ]
        assert queues == [QUEUE, "email.triage"]
        # Another tenant's id changes nothing.
        await store.set_dispatch_queue(
            organization_id=uuid.uuid4(), job_id=generating.job_id, queue_name=QUEUE
        )
    finally:
        await _cleanup(pool, drafted.org_id, generating.org_id)

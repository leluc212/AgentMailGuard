"""The dispatch-worker on a real broker and Postgres (tasks 6.5, 6.6, 6.7).

Review Focus (design §9, ADR-0009, R19.3): kill the worker after each dispatch step; the
redelivered job still ends with exactly one provider draft and at most one provider send.
Also: Retry-After keeps the job DISPATCHED and picks its ladder tier; a permanent provider
error dead-letters with the error kept; an operator replay goes RETRY_PENDING -> DISPATCHED
(never GENERATING); a republished dispatch after COMPLETED sends nothing; two deliveries of
one job at once draft and send once (the job lock defers one); a dispatch that dead-letters
before its claim still replays to the dispatch-worker. A killed worker runs on its own pool,
which the test terminates, so PostgreSQL frees its job lock as it would for a dead process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import (
    AppSettings,
    BrokerSettings,
    DispatchWorkerSettings,
    RetryLadderSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.dispatch import PostgresDispatchStore
from packages.db.draft import insert_draft
from packages.db.job import PostgresJobStore
from packages.dispatch.service import DispatchOutcome, DispatchService
from packages.domain import DispatchMode
from packages.domain.entities import GeneratedDraft, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics, get_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.dispatch_worker.consumer import DispatchConsumer
from services.dispatch_worker.main import build_consumer
from tests.integration.isolation import scratch_vhost
from tests.stubs.dispatch_fakes import RecordingFake, registry_with

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
SEND = DispatchMode.SEND_REPLY
DRAFT = DispatchMode.CREATE_DRAFT


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "dispatchsvc") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


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
    thread_id: uuid.UUID
    message_id: uuid.UUID
    job_id: uuid.UUID
    draft_id: uuid.UUID


async def _seed_approved(pool: asyncpg.Pool, *, provider_thread_id: str | None = "th-live") -> Seed:
    """An approved billing draft on a DRAFTED job (what POST approve leaves behind)."""
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Acme Support', 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@acme.example",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id,"
            " message_count, last_message_at) VALUES ($1, $2, $3, $4, 1, now())",
            thread_id,
            org_id,
            mbx_id,
            f"{provider_thread_id}-{thread_id.hex[:6]}" if provider_thread_id else None,
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                rfc822_message_id, direction, sender_email, sender_name, recipients,
                subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'inbound', 'alice@customer.example', 'Alice',
                      '[]', 'Where is order 82915?', 'What is the status of order 82915?', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            f"orig-{msg_id.hex[:8]}@customer.example",
        )
        await conn.execute(
            """
            INSERT INTO classification_result (
                id, organization_id, message_id, category, priority, reply_required,
                retrieval_required, confidence, decided_by
            ) VALUES ($1, $2, $3, 'billing', 'normal', true, true, 0.9, 'rule')
            """,
            uuid.uuid4(),
            org_id,
            msg_id,
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.DRAFTED.value,
            idempotency_key=f"dispatchsvc-{uuid.uuid4()}",
        )
    )
    async with pool.acquire() as conn:
        draft = await insert_draft(
            conn,
            GeneratedDraft(
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                job_id=job.id,
                subject="Re: Where is order 82915?",
                body="Order ORD-82915 was dispatched on 24 September.",
                status="approved",
            ),
        )
    return Seed(org_id, thread_id, msg_id, uuid.UUID(str(job.id)), draft.id)


async def _cleanup(pool: asyncpg.Pool, seed: Seed) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organization WHERE id = $1", seed.org_id)


async def _resources(broker: BrokerSettings, pool: asyncpg.Pool) -> WorkerResources:
    connection = await aio_pika.connect_robust(broker.url)
    return WorkerResources(
        settings=DispatchWorkerSettings(broker=broker, retry=FAST_RETRY),
        db_pool=pool,
        connection=connection,
        publisher=MessagePublisher(
            broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
        ),
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=create_pipeline_metrics(),
    )


def _service(
    pool: asyncpg.Pool,
    fake: RecordingFake,
    mode: DispatchMode,
    *,
    store: PostgresDispatchStore | None = None,
) -> DispatchService:
    return DispatchService(
        store=store or PostgresDispatchStore(pool),
        adapter_for=lambda _mailbox: fake,
        registry=registry_with(mode),
        confirm_recheck_delay_s=0,
    )


def _envelope(seed: Seed) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(seed.job_id),
        idempotency_key=f"dispatch-{seed.job_id}",
        job_type="dispatch",
        organization_id=str(seed.org_id),
        message_id=str(seed.message_id),
        thread_id=str(seed.thread_id),
    )


async def _publish(broker: BrokerSettings, seed: Seed, queue: str | None = None) -> None:
    routing_key = queue or broker.queue_dispatch
    exchange = broker.exchange_for_queue(routing_key)
    assert exchange is not None
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(exchange, routing_key, _envelope(seed))
    finally:
        await publisher.close()


async def _wait_for_state(pool: asyncpg.Pool, seed: Seed, state: JobState, timeout_s: float) -> Job:
    deadline = asyncio.get_running_loop().time() + timeout_s
    current: Job | None = None
    while asyncio.get_running_loop().time() < deadline:
        current = await PostgresJobStore(pool).get_job(seed.org_id, seed.job_id)
        if current is not None and current.state == state.value:
            return current
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"job {seed.job_id} ended {current.state if current else None}, expected {state.value}"
    )


async def _event_states(
    pool: asyncpg.Pool,
    seed: Seed,
    *,
    event_types: tuple[str, ...] = ("state_transition",),
) -> list[str]:
    """``state_to`` of the job's events in order. ``PostgresJobStore.replay_job`` writes its
    RETRY_PENDING event as ``operator_replay``, so a replay test asks for both types."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT state_to FROM processing_event WHERE job_id = $1 AND organization_id = $2"
            " AND event_type = ANY($3::text[]) ORDER BY id",
            seed.job_id,
            seed.org_id,
            list(event_types),
        )
    return [r["state_to"] for r in rows]


async def _outbound_count(pool: asyncpg.Pool, seed: Seed) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM email_message WHERE organization_id = $1 AND thread_id = $2"
                " AND direction = 'outbound'",
                seed.org_id,
                seed.thread_id,
            )
        )


async def _doomed_pool() -> asyncpg.Pool:
    """A pool for the worker a test kills. ``terminate()`` drops its sessions the way a dead
    process does, so PostgreSQL releases that worker's per-job advisory lock (R19.3)."""
    return await create_pool_from_settings(AppSettings().database)


async def _kill(res: WorkerResources, pool: asyncpg.Pool) -> None:
    """The kill: the broker sees the connection drop with the delivery unacked and requeues
    it; the database sees the sessions end and frees the job lock. The worker's task stays
    parked at its crash point; stop() on its closed connection would block, so it is left as
    is (same pattern as the 4.13b ai-worker kill test)."""
    await res.connection.close()
    pool.terminate()


async def _run_until(
    consumer: DispatchConsumer,
    res: WorkerResources,
    pool: asyncpg.Pool,
    seed: Seed,
    state: JobState,
) -> Job:
    await consumer.start()
    try:
        return await _wait_for_state(pool, seed, state, timeout_s=20)
    finally:
        await consumer.stop()
        await res.connection.close()


class _FinishCrashStore(PostgresDispatchStore):
    """Kills the worker after the provider draft is recorded, before step 5 commits."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(pool)
        self.armed = True
        self.reached = asyncio.Event()
        self._hang = asyncio.Event()

    async def finish(
        self,
        *,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        draft_id: uuid.UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        if self.armed:
            self.armed = False
            self.reached.set()
            await self._hang.wait()
        return await super().finish(
            organization_id=organization_id,
            job_id=job_id,
            draft_id=draft_id,
            mode=mode,
            provider_ref=provider_ref,
            outbound=outbound,
        )


class _RecordingService(DispatchService):
    """Records every dispatch outcome so a test can wait for a specific delivery."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.outcomes: list[DispatchOutcome] = []

    async def dispatch(self, *, organization_id: uuid.UUID, job_id: uuid.UUID) -> DispatchOutcome:
        outcome = await super().dispatch(organization_id=organization_id, job_id=job_id)
        self.outcomes.append(outcome)
        return outcome


async def test_create_draft_mode_completes_with_one_provider_draft(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    res = await _resources(broker, pool)
    try:
        consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
        await _publish(broker, seed)
        job = await _run_until(consumer, res, pool, seed, JobState.COMPLETED)
        assert job.queue_name == broker.queue_dispatch
        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == 0
        assert await _outbound_count(pool, seed) == 0
        assert (await _event_states(pool, seed))[-2:] == ["DISPATCHED", "COMPLETED"]
    finally:
        await _cleanup(pool, seed)


@pytest.mark.parametrize(
    ("crash_at", "mode"),
    [
        ("before_create_draft", DRAFT),
        ("before_create_draft", SEND),
        ("after_create_draft", DRAFT),  # provider holds the draft, its id is not recorded
        ("after_create_draft", SEND),
        ("before_send_draft", SEND),
        ("after_send_draft", SEND),
    ],
)
async def test_worker_killed_after_each_step_sends_exactly_once(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    crash_at: str,
    mode: DispatchMode,
) -> None:
    """R19.3 / ADR-0009 / tasks.md 6.5: kill -> broker redelivery -> exactly one provider
    draft (after_create_draft: the redelivery adopts it through find_draft) and at most one
    send."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.crash_at = crash_at
    pool_a = await _doomed_pool()
    try:
        res_a = await _resources(broker, pool_a)
        worker_a = build_consumer(res_a, service=_service(pool_a, fake, mode))
        await worker_a.start()
        await _publish(broker, seed)
        await asyncio.wait_for(fake.crash_reached.wait(), timeout=15)
        await _kill(res_a, pool_a)

        res_b = await _resources(broker, pool)
        worker_b = build_consumer(res_b, service=_service(pool, fake, mode))
        await _run_until(worker_b, res_b, pool, seed, JobState.COMPLETED)

        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == (1 if mode is SEND else 0)
        assert await _outbound_count(pool, seed) == (1 if mode is SEND else 0)
        if crash_at == "after_create_draft":
            assert fake.calls["find_draft"] == 1
    finally:
        await _cleanup(pool, seed)


async def test_worker_killed_before_finish_resumes_without_a_second_draft(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Crash after step 2 committed (draft handle stored), before step 5: reuse, never recreate."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    pool_a = await _doomed_pool()
    crashing = _FinishCrashStore(pool_a)
    try:
        res_a = await _resources(broker, pool_a)
        worker_a = build_consumer(res_a, service=_service(pool_a, fake, DRAFT, store=crashing))
        await worker_a.start()
        await _publish(broker, seed)
        await asyncio.wait_for(crashing.reached.wait(), timeout=15)
        await _kill(res_a, pool_a)

        res_b = await _resources(broker, pool)
        worker_b = build_consumer(res_b, service=_service(pool, fake, DRAFT))
        await _run_until(worker_b, res_b, pool, seed, JobState.COMPLETED)

        assert fake.calls["create_draft"] == 1
    finally:
        await _cleanup(pool, seed)


async def test_retry_after_keeps_the_job_dispatched_and_picks_its_tier(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """R17.5: a 429 with Retry-After 2.5 s goes to the 3 s tier; the job never leaves DISPATCHED."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.inject_rate_limit(retry_after=2.5)
    retries = get_metrics().retry_jobs_total.labels(queue=broker.queue_dispatch, tier="3s")
    before = retries._value.get()
    res = await _resources(broker, pool)
    try:
        consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
        await _publish(broker, seed)
        await _run_until(consumer, res, pool, seed, JobState.COMPLETED)
        assert retries._value.get() == before + 1
        states = await _event_states(pool, seed)
        assert JobState.RETRY_PENDING.value not in states
        assert states[-2:] == ["DISPATCHED", "COMPLETED"]
        assert fake.calls["create_draft"] == 1
    finally:
        await _cleanup(pool, seed)


async def test_permanent_error_dead_letters_then_operator_replay_completes(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """R17.5 / R18.7: DISPATCHED -> FAILED -> DEAD_LETTER with the error; replay -> DISPATCHED."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.inject_permanent_failure("400 Bad Request: invalid To header")
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
    await consumer.start()
    try:
        await _publish(broker, seed)
        dead = await _wait_for_state(pool, seed, JobState.DEAD_LETTER, timeout_s=20)
        assert dead.last_error is not None and "invalid To header" in dead.last_error
        dlq = await channel.declare_queue(broker.queue_dead_letter, passive=True)
        message = await dlq.get(no_ack=True, fail=True, timeout=5)
        assert "invalid To header" in str((message.headers or {}).get("x-failure-reason"))

        replayed, _ = await PostgresJobStore(pool).replay_job(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            payload={"operator_replay": True, "reason": "fixed recipient"},
        )
        assert replayed.queue_name == broker.queue_dispatch  # written by the claim
        await _publish(broker, seed, queue=replayed.queue_name)
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)

        states = await _event_states(
            pool, seed, event_types=("state_transition", "operator_replay")
        )
        assert JobState.GENERATING.value not in states
        replay_at = states.index(JobState.RETRY_PENDING.value)
        assert states[replay_at + 1 :] == ["DISPATCHED", "COMPLETED"]
        assert fake.calls["create_draft"] == 1
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, seed)


async def test_null_provider_thread_is_dead_lettered_without_provider_calls(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """6.3 / 6.6: a thread without a provider id cannot be replied to in-thread."""
    seed = await _seed_approved(pool, provider_thread_id=None)
    fake = RecordingFake()
    res = await _resources(broker, pool)
    try:
        consumer = build_consumer(res, service=_service(pool, fake, SEND))
        await _publish(broker, seed)
        dead = await _run_until(consumer, res, pool, seed, JobState.DEAD_LETTER)
        assert dead.last_error is not None and "MissingProviderThreadError" in dead.last_error
        assert sum(fake.calls.values()) == 0
    finally:
        await _cleanup(pool, seed)


async def test_republished_dispatch_after_completion_sends_nothing(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Phase 6 gate item 4 in CI form: replaying the dispatch job sends nothing further."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    service = _RecordingService(
        store=PostgresDispatchStore(pool),
        adapter_for=lambda _mailbox: fake,
        registry=registry_with(SEND),
        confirm_recheck_delay_s=0,
    )
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=service)
    await consumer.start()
    try:
        await _publish(broker, seed)
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)
        await _publish(broker, seed)
        deadline = asyncio.get_running_loop().time() + 20
        while len(service.outcomes) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
        assert service.outcomes == [DispatchOutcome.COMPLETED_SENT, DispatchOutcome.ALREADY_DONE]
        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == 1
        assert await _outbound_count(pool, seed) == 1
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, seed)


@pytest.mark.parametrize("mode", [DRAFT, SEND])
async def test_two_deliveries_of_one_job_at_once_draft_and_send_once(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool, mode: DispatchMode
) -> None:
    """R19.3: a double approve publishes two envelopes for one job; with prefetch > 1 both
    are delivered together. The job lock defers the second, which then finds COMPLETED."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.pause_at = "before_create_draft"  # hold delivery 1 inside the lock
    service = _RecordingService(
        store=PostgresDispatchStore(pool),
        adapter_for=lambda _mailbox: fake,
        registry=registry_with(mode),
        confirm_recheck_delay_s=0,
    )
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=service)
    assert consumer.prefetch_count > 1
    await consumer.start()
    try:
        await _publish(broker, seed)
        await _publish(broker, seed)
        await asyncio.wait_for(fake.paused.wait(), timeout=15)
        await asyncio.sleep(1.0)  # delivery 2 arrives, finds the lock busy, is deferred
        fake.resume.set()
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)
        deadline = asyncio.get_running_loop().time() + 20
        while len(service.outcomes) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
        assert sorted(service.outcomes) == sorted(
            [
                DispatchOutcome.COMPLETED_SENT if mode is SEND else DispatchOutcome.COMPLETED_DRAFT,
                DispatchOutcome.ALREADY_DONE,
            ]
        )
        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == (1 if mode is SEND else 0)
        assert await _outbound_count(pool, seed) == (1 if mode is SEND else 0)
        job = await PostgresJobStore(pool).get_job(seed.org_id, seed.job_id)
        assert job is not None and job.state == JobState.COMPLETED.value
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, seed)


async def test_key_conflict_dead_letters_and_the_replay_reaches_the_dispatch_worker(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """R18.7 / tasks.md 6.5: a dispatch that fails before its claim commits (the key rolls the
    claim back) still leaves queue_name = email.dispatch, so the replay never regenerates."""
    seed = await _seed_approved(pool)
    other = await _seed_approved(pool)
    fake = RecordingFake()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE processing_job SET queue_name = 'email.triage'"
            " WHERE id = $1 AND organization_id = $2",
            seed.job_id,
            seed.org_id,
        )
        # Another draft already holds this job's dispatch key, so the claim raises
        # DispatchKeyConflictError and rolls back (queue_name included).
        key = derive_idempotency_key(
            organization_id=seed.org_id,
            mailbox_id=await conn.fetchval(
                "SELECT mailbox_id FROM email_message WHERE id = $1", seed.message_id
            ),
            provider_message_id=f"prov-{seed.message_id.hex[:8]}",
            operation_type="dispatch",
        )
        await conn.execute(
            "UPDATE generated_draft SET dispatch_idempotency_key = $1 WHERE id = $2",
            key,
            other.draft_id,
        )
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
    await consumer.start()
    try:
        await _publish(broker, seed)
        dead = await _wait_for_state(pool, seed, JobState.DEAD_LETTER, timeout_s=20)
        assert dead.last_error is not None and "DispatchKeyConflictError" in dead.last_error
        assert dead.queue_name == broker.queue_dispatch
        assert sum(fake.calls.values()) == 0

        async with pool.acquire() as conn:  # the operator frees the key, then replays
            await conn.execute(
                "UPDATE generated_draft SET dispatch_idempotency_key = NULL WHERE id = $1",
                other.draft_id,
            )
        replayed, _ = await PostgresJobStore(pool).replay_job(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            payload={"operator_replay": True, "reason": "freed the dispatch key"},
        )
        assert replayed.queue_name == broker.queue_dispatch
        await _publish(broker, seed, queue=replayed.queue_name)
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)
        states = await _event_states(
            pool, seed, event_types=("state_transition", "operator_replay")
        )
        assert JobState.GENERATING.value not in states
        assert fake.calls["create_draft"] == 1
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, other)
        await _cleanup(pool, seed)

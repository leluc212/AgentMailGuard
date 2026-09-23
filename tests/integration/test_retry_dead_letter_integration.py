"""Live integration tests for retry ladder, exponential backoff, and dead-letter queue routing.

Verifies against real PostgreSQL 16 and RabbitMQ 3.13:
- R19.5: GENERATING -> RETRY_PENDING -> GENERATING state transitions.
- R19.6: FAILED -> DEAD_LETTER state transitions committed upon retry exhaustion.
- R3.5: DLQ messages carry x-original-routing-key, x-failure-reason, x-attempt, x-failed-at.
- R18.2: Support failure states RETRY_PENDING, FAILED, DEAD_LETTER in PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from packages.broker.consumer import BaseConsumer, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide asyncpg connection pool to the test database."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    """Provide a dedicated robust connection and channel for broker tests."""
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await conn.channel()
    yield channel
    if not channel.is_closed:
        await channel.close()
    if not conn.is_closed:
        await conn.close()


async def ensure_test_org(pool: asyncpg.Pool, org_id: UUID, name: str) -> None:
    """Ensure organization exists for foreign key constraints."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org_id,
            name,
        )


@pytest.mark.asyncio
async def test_live_retry_ladder_generating_to_retry_pending_and_recovery(
    db_pool: asyncpg.Pool,
    broker_channel: AbstractChannel,
) -> None:
    """Verify live PostgreSQL + RabbitMQ retry lifecycle (R19.5, R18.2).
    GENERATING -> RETRY_PENDING -> GENERATING.
    """
    b_settings = BrokerSettings()
    await setup_topology(broker_channel, b_settings)

    test_queue = "email.billing.priority"
    retry_q_name = "email.retry.30s"

    # Purge test queues
    q_billing = await broker_channel.get_queue(test_queue)
    await q_billing.purge()
    q_retry = await broker_channel.get_queue(retry_q_name)
    await q_retry.purge()

    org_id = uuid4()
    job_id = uuid4()
    await ensure_test_org(db_pool, org_id, f"test-org-{org_id}")

    job_store = PostgresJobStore(db_pool)

    # 1. Seed job in database in GENERATING state
    initial_job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:live:{job_id}",
        trace_id="trace-live-retry-1",
    )
    await job_store.create_job(initial_job)

    # 2. Set up consumer on email.billing.priority that simulates transient failure
    class FlakyWorker(BaseConsumer):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.fail_mode = True

        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            if self.fail_mode:
                raise TransientError("OpenAI 503 Service Unavailable")
            # Success mode
            return

    r_settings = RetryLadderSettings(
        tier_1_delay_s=30,
        tier_2_delay_s=300,
        tier_3_delay_s=1800,
        max_retries=3,
    )
    consumer = FlakyWorker(
        queue_name=test_queue,
        broker_settings=b_settings,
        retry_settings=r_settings,
        job_store=job_store,
    )
    await consumer.start()

    publisher = MessagePublisher(broker_settings=b_settings)
    await publisher.connect()

    try:
        envelope = JobEnvelope(
            job_id=str(job_id),
            idempotency_key=initial_job.idempotency_key,
            job_type="generate_reply",
            organization_id=str(org_id),
            attempt=0,
            trace_id=initial_job.trace_id,
        )

        # 3. Publish to email.route with key email.billing.priority
        await publisher.publish(
            exchange_name=b_settings.exchange_email_route,
            routing_key=test_queue,
            envelope=envelope,
        )

        # Wait for consumer to process transient failure
        await asyncio.sleep(1.0)

        # 4. Verify PostgreSQL state transitioned to RETRY_PENDING (R19.5)
        db_job = await job_store.get_job(org_id, job_id)
        assert db_job is not None
        assert db_job.state == JobState.RETRY_PENDING.value

        events = await job_store.list_events_for_job(org_id, job_id)
        assert events[-1].state_from == JobState.GENERATING.value
        assert events[-1].state_to == JobState.RETRY_PENDING.value
        assert events[-1].payload["attempt"] == 1
        assert events[-1].payload["delay_s"] == 30

        # 5. Verify message landed in email.retry.30s queue
        retry_msg = await q_retry.get(no_ack=True, timeout=2.0)
        assert retry_msg is not None

        retry_envelope = JobEnvelope.from_message(retry_msg)
        assert retry_envelope.job_id == str(job_id)
        assert retry_envelope.attempt == 1
        assert retry_msg.headers["x-original-routing-key"] == test_queue
        assert retry_msg.headers["x-original-exchange"] == b_settings.exchange_email_route
        assert "TransientError" in str(retry_msg.headers.get("x-failure-reason", ""))

        # 6. Re-deliver to consumer with fail_mode=False (simulating post-backoff retry)
        consumer.fail_mode = False
        await publisher.publish(
            exchange_name=b_settings.exchange_email_route,
            routing_key=test_queue,
            envelope=retry_envelope,
        )

        # Wait for consumer to process recovered delivery
        await asyncio.sleep(1.0)

        # 7. Verify PostgreSQL state transitioned RETRY_PENDING -> GENERATING (R19.5)
        recovered_job = await job_store.get_job(org_id, job_id)
        assert recovered_job is not None
        # Consumer recovered to GENERATING before executing
        recovery_events = [
            e
            for e in await job_store.list_events_for_job(org_id, job_id)
            if e.state_from == JobState.RETRY_PENDING.value
            and e.state_to == JobState.GENERATING.value
        ]
        assert len(recovery_events) == 1
        assert recovery_events[0].payload["resumed_at_attempt"] == 1

    finally:
        await consumer.stop()
        await publisher.close()


@pytest.mark.asyncio
async def test_live_exhausted_retries_transition_failed_and_dead_letter(
    db_pool: asyncpg.Pool,
    broker_channel: AbstractChannel,
) -> None:
    """Verify live retry exhaustion transitions FAILED -> DEAD_LETTER with headers (R19.6, R3.5).
    """
    b_settings = BrokerSettings()
    await setup_topology(broker_channel, b_settings)

    test_queue = "email.billing.priority"
    dlq_name = b_settings.queue_dead_letter

    q_billing = await broker_channel.get_queue(test_queue)
    await q_billing.purge()
    q_dlq = await broker_channel.get_queue(dlq_name)
    await q_dlq.purge()

    org_id = uuid4()
    job_id = uuid4()
    await ensure_test_org(db_pool, org_id, f"test-org-{org_id}")

    job_store = PostgresJobStore(db_pool)

    # 1. Seed job in database in GENERATING state
    initial_job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:exhaust:{job_id}",
        trace_id="trace-live-dlq-1",
    )
    await job_store.create_job(initial_job)

    class AlwaysFailingWorker(BaseConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            raise TransientError("External dependency permanent timeout")

    # Max retries = 2
    r_settings = RetryLadderSettings(
        max_retries=2,
        tier_1_delay_s=30,
        tier_2_delay_s=300,
        tier_3_delay_s=1800,
    )
    consumer = AlwaysFailingWorker(
        queue_name=test_queue,
        broker_settings=b_settings,
        retry_settings=r_settings,
        job_store=job_store,
    )
    await consumer.start()

    publisher = MessagePublisher(broker_settings=b_settings)
    await publisher.connect()

    try:
        # Publish envelope already at attempt=2 (which equals max_retries=2, so next is exhaustion)
        envelope = JobEnvelope(
            job_id=str(job_id),
            idempotency_key=initial_job.idempotency_key,
            job_type="generate_reply",
            organization_id=str(org_id),
            attempt=2,
            trace_id=initial_job.trace_id,
        )

        await publisher.publish(
            exchange_name=b_settings.exchange_email_route,
            routing_key=test_queue,
            envelope=envelope,
        )

        # Wait for consumer to process exhaustion
        await asyncio.sleep(1.0)

        # 2. Verify PostgreSQL state transitioned FAILED -> DEAD_LETTER (R19.6, R18.2)
        final_job = await job_store.get_job(org_id, job_id)
        assert final_job is not None
        assert final_job.state == JobState.DEAD_LETTER.value

        events = await job_store.list_events_for_job(org_id, job_id)
        transitions = [(e.state_from, e.state_to) for e in events if e.state_from is not None]
        assert (JobState.GENERATING.value, JobState.FAILED.value) in transitions
        assert (JobState.FAILED.value, JobState.DEAD_LETTER.value) in transitions

        # 3. Verify message routed to dead letter queue
        dlq_msg = await q_dlq.get(no_ack=True, timeout=2.0)
        assert dlq_msg is not None

        dlq_envelope = JobEnvelope.from_message(dlq_msg)
        assert dlq_envelope.job_id == str(job_id)
        assert dlq_envelope.attempt == 2

        # 4. Verify all DLQ headers preserved (R3.5)
        headers = dlq_msg.headers
        assert headers["x-original-routing-key"] == test_queue
        assert headers["x-original-exchange"] == b_settings.exchange_email_route
        assert "TransientError" in str(headers.get("x-failure-reason", ""))
        assert headers["x-attempt"] == 2
        assert "x-failed-at" in headers

    finally:
        await consumer.stop()
        await publisher.close()

"""Live integration tests for lease reaper and stuck job recovery (R19.8, design.md §9).

Verifies against real PostgreSQL 16 and RabbitMQ 3.13:
- R19.8: SELECT ... FOR UPDATE SKIP LOCKED concurrency safety across multiple workers.
- R19.8: Reclaim expired jobs in GENERATING to RETRY_PENDING when attempts remain.
- R19.6: Reclaim expired jobs with exhausted attempts to DEAD_LETTER.
- R18.4, R18.5: Atomically commit state transitions and ProcessingEvent records.
- RabbitMQ: Republish reaped jobs to retry exchange or dead-letter queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.lease_reaper import LeaseReaper
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings, LeaseReaperSettings
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.observability.metrics import create_pipeline_metrics

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
async def test_live_lease_reaper_concurrent_reap_skip_locked(db_pool: asyncpg.Pool) -> None:
    """Verify SELECT ... FOR UPDATE SKIP LOCKED guarantees zero contention and no double-reaping.

    Seeds 12 expired jobs in PostgreSQL. Runs 4 concurrent workers attempting to reap.
    Asserts each job is claimed and updated exactly once across all concurrent workers.
    """
    org_id = uuid4()
    await ensure_test_org(db_pool, org_id, "Concurrency Test Org")

    store = PostgresJobStore(db_pool)
    past_time = datetime.now(UTC) - timedelta(seconds=120)

    # Seed 12 jobs stuck past lease expiration
    job_ids: list[UUID] = []
    for _ in range(12):
        j = Job(
            organization_id=org_id,
            state=JobState.GENERATING.value,
            attempt=0,
            max_attempts=3,
            lease_expires_at=past_time,
            idempotency_key=f"lease-concurrent-{uuid4()}",
        )
        created, _ = await store.create_job(j)
        # Update lease_expires_at to past in DB
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE processing_job SET lease_expires_at = $1 WHERE id = $2",
                past_time,
                created.id,
            )
        job_ids.append(UUID(str(created.id)))

    # Launch 4 concurrent reaper tasks with batch_size=4
    async def worker_sweep() -> list[tuple[Job, Any, str]]:
        return await store.reap_expired_jobs(batch_size=4, organization_id=org_id)

    tasks = [worker_sweep() for _ in range(4)]
    results = await asyncio.gather(*tasks)

    # Flatten reaped results
    all_reaped_jobs = [item[0] for batch in results for item in batch]
    reaped_ids = [j.id for j in all_reaped_jobs]

    # Verify no duplicates across workers
    assert len(reaped_ids) == len(set(reaped_ids))
    assert set(reaped_ids) == set(job_ids)
    assert len(all_reaped_jobs) == 12

    # Verify state in database
    for j_id in job_ids:
        stored = await store.get_job(org_id, j_id)
        assert stored is not None
        assert stored.state == JobState.RETRY_PENDING.value
        assert stored.attempt == 1
        assert stored.lease_expires_at is None

        # Verify event history in DB (R18.4, R18.5)
        events = await store.list_events_for_job(org_id, j_id)
        # Initial event + retry event
        assert len(events) >= 2
        last_event = events[-1]
        assert last_event.state_from == JobState.GENERATING.value
        assert last_event.state_to == JobState.RETRY_PENDING.value


@pytest.mark.asyncio
async def test_live_lease_reaper_full_pipeline_retry_and_dlq(
    db_pool: asyncpg.Pool,
    broker_channel: AbstractChannel,
) -> None:
    """Verify live PostgreSQL and RabbitMQ integration for LeaseReaper service.

    - Reclaims retryable stuck job -> updates DB to RETRY_PENDING -> publishes to retry ladder.
    - Reclaims exhausted stuck job -> updates DB to DEAD_LETTER -> publishes to DLQ.
    - Leaves active job with valid lease untouched.
    """
    b_settings = BrokerSettings()
    await setup_topology(broker_channel, b_settings)

    org_id = uuid4()
    await ensure_test_org(db_pool, org_id, "Reaper Pipeline Org")

    store = PostgresJobStore(db_pool)
    metrics = create_pipeline_metrics()
    past_time = datetime.now(UTC) - timedelta(seconds=60)
    future_time = datetime.now(UTC) + timedelta(seconds=300)

    # 1. Job A: Stuck in GENERATING, attempt 0/3 (retryable)
    job_a = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=0,
        max_attempts=3,
        lease_expires_at=past_time,
        queue_name="email.support.normal",
        idempotency_key=f"reap-a-{uuid4()}",
    )
    created_a, _ = await store.create_job(job_a)

    # 2. Job B: Stuck in GENERATING, attempt 2/3 (exhausted)
    job_b = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=2,
        max_attempts=3,
        lease_expires_at=past_time,
        queue_name="email.billing.priority",
        idempotency_key=f"reap-b-{uuid4()}",
    )
    created_b, _ = await store.create_job(job_b)

    # 3. Job C: Active in GENERATING, future lease (should not be reaped)
    job_c = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=0,
        max_attempts=3,
        lease_expires_at=future_time,
        queue_name="email.sales.normal",
        idempotency_key=f"reap-c-{uuid4()}",
    )
    created_c, _ = await store.create_job(job_c)

    # Force past timestamp into database rows
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE processing_job SET lease_expires_at = $1 WHERE id IN ($2, $3)",
            past_time,
            created_a.id,
            created_b.id,
        )
        await conn.execute(
            "UPDATE processing_job SET lease_expires_at = $1 WHERE id = $2",
            future_time,
            created_c.id,
        )

    # Purge test queues before running assertions
    q_retry_pre = await broker_channel.get_queue("email.retry.30s", ensure=False)
    await q_retry_pre.purge()
    q_dlq_pre = await broker_channel.get_queue("email.dead_letter", ensure=False)
    await q_dlq_pre.purge()

    # Set up publisher and LeaseReaper
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await conn.channel()
    publisher = MessagePublisher(
        broker_settings=b_settings,
        connection=conn,
        channel=channel,
    )

    reaper = LeaseReaper(
        job_store=store,
        settings=LeaseReaperSettings(lease_timeout_s=30, batch_size=50),
        publisher=publisher,
        metrics=metrics,
    )

    # Execute single sweep restricted to test organization
    reaped = await reaper.reap_once(organization_id=org_id)
    reaped_ids = {str(item[0].id) for item in reaped}

    assert str(created_a.id) in reaped_ids
    assert str(created_b.id) in reaped_ids
    assert str(created_c.id) not in reaped_ids

    # Verify Job A state in DB
    db_job_a = await store.get_job(org_id, created_a.id)
    assert db_job_a is not None
    assert db_job_a.state == JobState.RETRY_PENDING.value
    assert db_job_a.attempt == 1

    # Verify Job B state in DB
    db_job_b = await store.get_job(org_id, created_b.id)
    assert db_job_b is not None
    assert db_job_b.state == JobState.DEAD_LETTER.value
    assert db_job_b.attempt == 3

    # Verify Job C state in DB
    db_job_c = await store.get_job(org_id, created_c.id)
    assert db_job_c is not None
    assert db_job_c.state == JobState.GENERATING.value
    assert db_job_c.lease_expires_at is not None

    # Verify RabbitMQ received messages:
    # 1. Job A in retry queue (email.retry.30s)
    retry_q = await channel.get_queue("email.retry.30s", ensure=False)
    retry_msg = await retry_q.get(no_ack=False)
    assert retry_msg is not None
    envelope_a = JobEnvelope.from_message(retry_msg)
    assert envelope_a.job_id == str(created_a.id)
    assert envelope_a.attempt == 1
    await retry_msg.ack()

    # 2. Job B in DLQ (email.dead_letter)
    dlq = await channel.get_queue("email.dead_letter", ensure=False)
    dlq_msg = await dlq.get(no_ack=False)
    assert dlq_msg is not None
    envelope_b = JobEnvelope.from_message(dlq_msg)
    assert envelope_b.job_id == str(created_b.id)
    assert envelope_b.attempt == 3
    assert dlq_msg.headers["x-original-routing-key"] == "email.billing.priority"
    await dlq_msg.ack()

    await channel.close()
    await conn.close()

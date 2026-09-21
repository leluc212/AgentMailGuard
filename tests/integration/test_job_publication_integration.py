"""Multi-tenant PostgreSQL and RabbitMQ integration tests for JobEnvelope and JobStore.

Verifies:
- Atomic creation of processing_job and processing_event rows in same transaction.
- Multi-tenant isolation across >= 3 organizations.
- Database UNIQUE constraint enforcement on idempotency_key.
- Real RabbitMQ persistent publication and round-trip consumption (R7.3, R18.1–R18.5, R19.2, R19.4).
"""

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Classification, Job
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


async def ensure_org(pool: asyncpg.Pool, org_id: UUID, name: str) -> None:
    """Ensure organization exists for foreign key constraints."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org_id,
            name,
        )


@pytest.mark.asyncio
async def test_postgres_job_store_multi_tenant_atomic_persistence(db_pool: asyncpg.Pool) -> None:
    """Verify >= 3 tenants isolation and atomic job + event persistence (R18.1–R18.5)."""
    job_store = PostgresJobStore(db_pool)

    # Seed >= 3 distinct organizations per GEMINI.md §8
    orgs = [uuid4() for _ in range(3)]
    for i, org_id in enumerate(orgs, start=1):
        await ensure_org(db_pool, org_id, f"Tenant {i} Org")

    job_ids: list[UUID] = []

    # 1. Create a job for each tenant in state RECEIVED
    for i, org_id in enumerate(orgs, start=1):
        j_id = uuid4()
        job_ids.append(j_id)
        key = derive_idempotency_key(org_id, uuid4(), f"provider_msg_{i}", "normalize")

        job = Job(
            id=j_id,
            organization_id=org_id,
            job_type="email_pipeline",
            state=JobState.RECEIVED.value,
            idempotency_key=key,
            trace_id=f"trace-tenant-{i}",
        )
        created, is_new = await job_store.create_job(
            job=job,
            initial_event_payload={"tenant_index": i},
        )
        assert is_new is True
        assert created.id == j_id
        assert created.state == "RECEIVED"

    # 2. Verify multi-tenant isolation: Tenant 1 cannot access Tenant 2's job
    assert await job_store.get_job(organization_id=orgs[0], job_id=job_ids[0]) is not None
    assert await job_store.get_job(organization_id=orgs[1], job_id=job_ids[0]) is None
    assert await job_store.get_job(organization_id=orgs[2], job_id=job_ids[0]) is None

    # 3. Transition Tenant 1 job to NORMALIZED and assert atomic processing_event
    updated, event = await job_store.transition_job_state(
        organization_id=orgs[0],
        job_id=job_ids[0],
        target_state=JobState.NORMALIZED,
        payload={"processed_bytes": 2048},
    )
    assert updated.state == "NORMALIZED"
    assert event.state_from == "RECEIVED"
    assert event.state_to == "NORMALIZED"

    # Verify directly via SQL that rows committed atomically
    async with db_pool.acquire() as conn:
        job_row = await conn.fetchrow(
            "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
            job_ids[0],
            orgs[0],
        )
        assert job_row is not None
        assert job_row["state"] == "NORMALIZED"

        event_rows = await conn.fetch(
            "SELECT state_from, state_to FROM processing_event WHERE job_id = $1 ORDER BY id ASC",
            job_ids[0],
        )
        assert len(event_rows) == 2
        assert event_rows[0]["state_to"] == "RECEIVED"
        assert event_rows[1]["state_from"] == "RECEIVED"
        assert event_rows[1]["state_to"] == "NORMALIZED"


@pytest.mark.asyncio
async def test_postgres_job_store_idempotency_deduplication(db_pool: asyncpg.Pool) -> None:
    """Verify database UNIQUE constraint enforcement on idempotency_key (R19.2, R19.4)."""
    job_store = PostgresJobStore(db_pool)
    org_id = uuid4()
    await ensure_org(db_pool, org_id, "Idempotency Test Org")

    key = derive_idempotency_key(org_id, uuid4(), "dup_msg_001", "normalize")
    original_id = uuid4()

    job1 = Job(
        id=original_id,
        organization_id=org_id,
        job_type="email_pipeline",
        state=JobState.RECEIVED.value,
        idempotency_key=key,
    )
    res1, is_new1 = await job_store.create_job(job1)
    assert is_new1 is True
    assert res1.id == original_id

    # Concurrent / repeated insert with identical idempotency_key
    job2 = Job(
        id=uuid4(),
        organization_id=org_id,
        job_type="email_pipeline",
        state=JobState.RECEIVED.value,
        idempotency_key=key,
    )
    res2, is_new2 = await job_store.create_job(job2)
    assert is_new2 is False
    assert res2.id == original_id  # Short-circuited with original record

    # Verify exactly ONE row exists in database for this key
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM processing_job WHERE idempotency_key = $1",
            key,
        )
        assert count == 1


@pytest.mark.asyncio
async def test_job_envelope_publication_and_broker_roundtrip(
    broker_channel: AbstractChannel,
) -> None:
    """Verify JobEnvelope publication to RabbitMQ with snapshot and correlation (R7.3, §7.3)."""
    settings = BrokerSettings()
    await setup_topology(broker_channel, settings, RetryLadderSettings())

    publisher = MessagePublisher(broker_settings=settings, channel=broker_channel)
    org_id = str(uuid4())
    job_id = str(uuid4())
    msg_id = str(uuid4())
    thd_id = str(uuid4())

    envelope = JobEnvelope(
        job_id=job_id,
        idempotency_key=f"routing:{org_id}:{msg_id}",
        job_type="generate_reply",
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thd_id,
        attempt=0,
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    )

    # Attach classification snapshot per R7.3
    classification = Classification(
        category="support",
        intent="password_reset",
        priority="priority",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=True,
        confidence=0.96,
        decided_by="ml",
    )
    enriched = envelope.with_classification(classification)

    # Purge triage queue for clean test isolation
    queue = await broker_channel.get_queue(settings.queue_triage)
    await queue.purge()

    # Publish to exchange_email_triage targeting queue_triage
    await publisher.publish(
        exchange_name=settings.exchange_email_triage,
        routing_key=settings.queue_triage,
        envelope=enriched,
    )

    # Consume directly from the queue to verify broker delivery
    incoming = await queue.get(timeout=5.0)
    assert incoming is not None

    async with incoming.process():
        received_envelope = JobEnvelope.from_message(incoming)
        assert received_envelope.job_id == job_id
        assert received_envelope.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert received_envelope.category == "support"
        assert received_envelope.priority == "priority"
        assert received_envelope.reply_required is True
        assert received_envelope.workflow_hint == "ai"
        assert received_envelope.classification["confidence"] == 0.96
        assert incoming.headers["category"] == "support"
        assert incoming.headers["priority"] == "priority"
        assert incoming.delivery_mode == aio_pika.DeliveryMode.PERSISTENT

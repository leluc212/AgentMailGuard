"""Live integration tests for Job timeline API and operator replay against PostgreSQL & RabbitMQ.

Requirements:
- R18.6: Job-timeline API returning ordered event history for a message from PostgreSQL.
- R18.7: Replaying DEAD_LETTER job from its last good state via operator endpoint.
- R23.2: Jobs and messages API endpoints.
- R23.6: Mandatory organization_id scoping and pagination on list endpoints.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread import PostgresThreadStore
from packages.domain.entities import EmailAddress, EmailThread, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from services.api.main import create_app

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide asyncpg connection pool to the live test database."""
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


@pytest.fixture
def api_app(db_pool: asyncpg.Pool, broker_channel: AbstractChannel) -> FastAPI:
    """Create test FastAPI application connected to live PostgreSQL and RabbitMQ."""
    app = create_app(lifespan_enabled=False)
    app.state.db_pool = db_pool
    b_cfg = BrokerSettings()
    app.state.publisher = MessagePublisher(broker_settings=b_cfg, channel=broker_channel)
    return app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _seed_org_mailbox_thread_message(
    pool: asyncpg.Pool,
    org_id: UUID,
    mbx_id: UUID,
    thd_id: UUID,
    msg_id: UUID,
) -> None:
    """Seed parent entities in live PostgreSQL database."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO organization (id, name)
            VALUES ($1, $2)
            ON CONFLICT (id) DO NOTHING;
            """,
            org_id,
            f"Org-{org_id}",
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, address, provider, status)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            f"support-{org_id}@example.com",
            "gmail",
            "active",
        )

    thd_store = PostgresThreadStore(pool)
    msg_store = PostgresMessageStore(pool)

    now = datetime.now(UTC)
    thread = EmailThread(
        id=thd_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        provider_thread_id=f"prov-thd-{thd_id}",
        subject_normalized="critical server downtime",
        participants=["dev@client.com", "support@example.com"],
        last_message_at=now,
        message_count=1,
    )
    await thd_store.create_thread(thread)

    msg = NormalizedMessage(
        message_id=msg_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thd_id,
        provider="gmail",
        provider_message_id=f"prov-{msg_id}",
        sender=EmailAddress(name="Dev", email="dev@client.com"),
        recipients=[EmailAddress(name="Support", email="support@example.com")],
        subject="Critical server downtime",
        subject_normalized="critical server downtime",
        body_text="Our servers are throwing 500 errors everywhere.",
        body_text_clean="Our servers are throwing 500 errors everywhere.",
        snippet="Our servers are throwing 500 errors everywhere.",
        received_at=now,
    )
    await msg_store.insert_message(msg)


@pytest.mark.asyncio
async def test_live_message_timeline_postgresql(
    db_pool: asyncpg.Pool, client: AsyncClient
) -> None:
    """Verify GET /v1/messages/{id}/timeline against live PostgreSQL with chronological events."""
    org_id = uuid4()
    mbx_id = uuid4()
    thd_id = uuid4()
    msg_id = uuid4()
    job_id = uuid4()

    await _seed_org_mailbox_thread_message(db_pool, org_id, mbx_id, thd_id, msg_id)

    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thd_id,
        job_type="generate_reply",
        state=JobState.RECEIVED.value,
        idempotency_key=f"idem-timeline-{job_id}",
    )
    await job_store.create_job(job)

    # Perform sequence of state transitions
    await job_store.transition_job_state(
        org_id, job_id, JobState.NORMALIZED, message_id=msg_id, thread_id=thd_id
    )
    await job_store.transition_job_state(
        org_id, job_id, JobState.CLASSIFIED, message_id=msg_id, thread_id=thd_id
    )
    await job_store.transition_job_state(
        org_id, job_id, JobState.QUEUED, message_id=msg_id, thread_id=thd_id
    )
    await job_store.transition_job_state(
        org_id, job_id, JobState.CONTEXT_READY, message_id=msg_id, thread_id=thd_id
    )
    await job_store.transition_job_state(
        org_id, job_id, JobState.GENERATING, message_id=msg_id, thread_id=thd_id
    )

    # Query message timeline API
    response = await client.get(
        f"/v1/messages/{msg_id}/timeline",
        headers={"X-Organization-ID": str(org_id)},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["message_id"] == str(msg_id)
    assert data["organization_id"] == str(org_id)
    assert data["current_state"] == "GENERATING"
    assert data["total_events"] == 6  # Initial created event + 5 transitions
    assert len(data["events"]) == 6

    # Verify chronological sequence
    assert [e["state_to"] for e in data["events"]] == [
        "RECEIVED",
        "NORMALIZED",
        "CLASSIFIED",
        "QUEUED",
        "CONTEXT_READY",
        "GENERATING",
    ]

    # Verify pagination on live PostgreSQL
    page_resp = await client.get(
        f"/v1/messages/{msg_id}/timeline?limit=2&offset=2",
        headers={"X-Organization-ID": str(org_id)},
    )
    assert page_resp.status_code == 200
    page_data = page_resp.json()
    assert page_data["total_events"] == 6
    assert len(page_data["events"]) == 2
    assert [e["state_to"] for e in page_data["events"]] == ["CLASSIFIED", "QUEUED"]


@pytest.mark.asyncio
async def test_live_operator_replay_postgresql_and_rabbitmq(
    db_pool: asyncpg.Pool,
    broker_channel: AbstractChannel,
    client: AsyncClient,
) -> None:
    """Verify live operator replay transitions DEAD_LETTER -> RETRY_PENDING and redelivers.

    Requirements: R18.7, R23.2.
    """
    org_id = uuid4()
    mbx_id = uuid4()
    thd_id = uuid4()
    msg_id = uuid4()
    job_id = uuid4()

    await _seed_org_mailbox_thread_message(db_pool, org_id, mbx_id, thd_id, msg_id)

    b_settings = BrokerSettings()
    await setup_topology(broker_channel, b_settings)

    # Purge destination queue to start clean
    target_queue_name = "email.support.normal"
    q = await broker_channel.get_queue(target_queue_name)
    await q.purge()

    # Seed job in DEAD_LETTER state
    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thd_id,
        job_type="generate_reply",
        state=JobState.DEAD_LETTER.value,
        attempt=5,
        max_attempts=5,
        queue_name=target_queue_name,
        last_error="5 consecutive LLM generation timeouts",
        idempotency_key=f"idem-replay-{job_id}",
    )
    await job_store.create_job(job)

    # Call replay endpoint
    replay_resp = await client.post(
        f"/v1/jobs/{job_id}/replay",
        headers={"X-Organization-ID": str(org_id)},
        json={"reset_attempts": True, "reason": "Operator cleared downstream outage"},
    )
    assert replay_resp.status_code == 200
    data = replay_resp.json()

    assert data["job_id"] == str(job_id)
    assert data["organization_id"] == str(org_id)
    assert data["previous_state"] == "DEAD_LETTER"
    assert data["new_state"] == "RETRY_PENDING"
    assert data["attempt"] == 0
    assert data["republished"] is True
    assert data["routing_key"] == target_queue_name

    # Verify state in live PostgreSQL database
    async with db_pool.acquire() as conn:
        job_row = await conn.fetchrow(
            "SELECT state, attempt, last_error FROM processing_job WHERE id = $1",
            job_id,
        )
        assert job_row is not None
        assert job_row["state"] == "RETRY_PENDING"
        assert job_row["attempt"] == 0
        assert job_row["last_error"] is None

        # Verify audit event in PostgreSQL
        ev_row = await conn.fetchrow(
            """
            SELECT event_type, state_from, state_to, payload
            FROM processing_event
            WHERE job_id = $1 AND event_type = 'operator_replay'
            """,
            job_id,
        )
        assert ev_row is not None
        assert ev_row["state_from"] == "DEAD_LETTER"
        raw_payload = ev_row["payload"]
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        assert payload["operator_replay"] is True
        assert payload["reason"] == "Operator cleared downstream outage"

    # Verify message in live RabbitMQ queue
    q_info = await q.declare()
    assert q_info.message_count is not None and q_info.message_count >= 1

    msg = await q.get(no_ack=False)
    assert msg is not None
    try:
        envelope = JobEnvelope.model_validate_json(msg.body)
        assert envelope.job_id == str(job_id)
        assert envelope.organization_id == str(org_id)
        assert envelope.attempt == 0
        assert envelope.payload["replayed"] is True
        assert envelope.payload["reason"] == "Operator cleared downstream outage"
    finally:
        await msg.ack()

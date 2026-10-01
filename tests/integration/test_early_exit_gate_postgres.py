"""PostgreSQL integration tests for the Early-Exit Gate (R6.5, R6.6, R18.3–R18.5).

Verifies:
1. Early-exit gate transitions job to COMPLETED in live PostgreSQL.
2. Processing events are durably recorded in processing_event table with audit payload.
3. Actionable jobs transition to QUEUED with correct retrieval_required flags.
4. Tenant isolation ensures jobs and events are strictly scoped by organization_id (R5.3).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Classification, Job
from packages.domain.state_machine import JobState
from services.triage_worker.gate import EarlyExitGate, GateAction


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide an asyncpg connection pool connected to live postgres."""
    settings = AppSettings()
    pool = await create_pool_from_settings(settings.database)
    try:
        yield pool
    finally:
        await pool.close()


async def seed_prerequisites(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    msg_id: uuid.UUID,
    thread_id: uuid.UUID,
) -> None:
    """Seed prerequisite organization, mailbox, thread, and message records."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Test Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            """
            INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO NOTHING;
            """,
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                'inbound', 'test@client.com', 'Test Sender',
                '[]', 'Test Subject', 'Body text', now()
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-msg-{msg_id.hex[:6]}",
        )


@pytest.mark.asyncio
async def test_postgres_early_exit_transitions_to_completed(db_pool: asyncpg.Pool) -> None:
    """Validate early exit on reply_required=False transitions to COMPLETED in PostgreSQL."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await seed_prerequisites(db_pool, org_id, mbx_id, msg_id, thread_id)
    store = PostgresJobStore(db_pool)
    gate = EarlyExitGate(job_store=store)

    # 1. Create job at NORMALIZED
    job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thread_id,
        state=JobState.NORMALIZED,
        idempotency_key=f"job-{job_id.hex}",
    )
    created_job, is_new = await store.create_job(job)
    assert is_new is True
    assert created_job.state == JobState.NORMALIZED

    # 2. Evaluate no-reply classification through gate
    classification = Classification(
        category="automated_notification",
        intent="system_alert",
        reply_required=False,
        retrieval_required=False,
        workflow_hint="none",
        confidence=0.99,
        decided_by="rule",
    )

    decision = await gate.evaluate_and_persist(created_job, classification)

    assert decision.action == GateAction.EARLY_EXIT
    assert decision.job.state == JobState.COMPLETED
    assert decision.should_embed is False
    assert decision.should_retrieve is False
    assert decision.should_generate is False

    # 3. Direct DB assertions
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, result_ref FROM processing_job WHERE id = $1 AND organization_id = $2;",
            job_id,
            org_id,
        )
        assert row is not None
        assert row["state"] == JobState.COMPLETED

        # Check telemetry events recorded
        events = await conn.fetch(
            """
            SELECT state_from, state_to, payload
            FROM processing_event
            WHERE job_id = $1
            ORDER BY id ASC;
            """,
            job_id,
        )
        assert len(events) >= 2  # NORMALIZED -> CLASSIFIED -> COMPLETED
        final_event = events[-1]
        assert final_event["state_from"] == JobState.CLASSIFIED
        assert final_event["state_to"] == JobState.COMPLETED


@pytest.mark.asyncio
async def test_postgres_actionable_queued_state_and_event(db_pool: asyncpg.Pool) -> None:
    """Validate actionable message transitions to QUEUED with selective RAG flags in PostgreSQL."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await seed_prerequisites(db_pool, org_id, mbx_id, msg_id, thread_id)
    store = PostgresJobStore(db_pool)
    gate = EarlyExitGate(job_store=store)

    job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thread_id,
        state=JobState.CLASSIFIED,
        idempotency_key=f"job-{job_id.hex}",
    )
    created_job, _ = await store.create_job(job)

    # Actionable email requiring response but skipping RAG (R6.6)
    classification = Classification(
        category="scheduling",
        intent="meeting_request",
        reply_required=True,
        retrieval_required=False,
        workflow_hint="ai",
        confidence=0.95,
        decided_by="ml",
    )

    decision = await gate.evaluate_and_persist(created_job, classification)

    assert decision.action == GateAction.PROCEED_NO_RAG
    assert decision.job.state == JobState.QUEUED
    assert decision.should_retrieve is False
    assert decision.should_generate is True

    # Direct DB assertions
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2;",
            job_id,
            org_id,
        )
        assert row is not None
        assert row["state"] == JobState.QUEUED


@pytest.mark.asyncio
async def test_postgres_early_exit_tenant_isolation(db_pool: asyncpg.Pool) -> None:
    """Verify tenant isolation: job operations must strictly scope by organization_id."""
    org1_id = uuid.uuid4()
    org2_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await seed_prerequisites(db_pool, org1_id, mbx_id, msg_id, thread_id)
    store = PostgresJobStore(db_pool)

    job = Job(
        id=job_id,
        organization_id=org1_id,
        message_id=msg_id,
        thread_id=thread_id,
        state=JobState.CLASSIFIED,
        idempotency_key=f"job-{job_id.hex}",
    )
    await store.create_job(job)

    # Tenant 2 attempts to fetch Tenant 1's job
    job_tenant2 = await store.get_job(organization_id=org2_id, job_id=job_id)
    assert job_tenant2 is None

    # Tenant 2 attempts to transition Tenant 1's job
    with pytest.raises(KeyError, match="not found for organization"):
        await store.transition_job_state(
            organization_id=org2_id,
            job_id=job_id,
            target_state=JobState.COMPLETED,
        )

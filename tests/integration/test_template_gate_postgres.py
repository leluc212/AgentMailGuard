"""PostgreSQL integration tests for deterministic template replies (R6.12–R6.15, R16.4).

Verifies in live PostgreSQL:
1. Template replies transition job to DRAFTED and atomically persist a generated_draft row (R6.13).
2. Processing events record the transition with template metadata and audit payload.
3. Missing templates fall back to workflow_hint='ai', transitioning to QUEUED with no draft (R6.14).
4. Multi-tenant isolation guarantees drafts cannot be accessed across organizations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.domain.entities import (
    Classification,
    Job,
)
from packages.domain.state_machine import JobState
from packages.domain.templates import (
    TemplateDefinition,
    TemplateRegistry,
)
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
    subject: str = "Test Subject",
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
                'inbound', 'client@example.com', 'Client User',
                '[]', $6, 'Please confirm my request', now()
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-msg-{msg_id.hex[:6]}",
            subject,
        )


@pytest.mark.asyncio
async def test_postgres_template_reply_persistence(db_pool: asyncpg.Pool) -> None:
    """R6.13: Test live PostgreSQL persistence of DRAFTED job, event, and generated_draft."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await seed_prerequisites(
        db_pool,
        org_id,
        mbx_id,
        msg_id,
        thread_id,
        subject="Urgent Order Inquiry",
    )

    job_store = PostgresJobStore(db_pool)
    draft_store = PostgresDraftStore(db_pool)

    registry = TemplateRegistry()
    registry.register(
        TemplateDefinition(
            id="ack-receipt-v1",
            version="v1",
            category="acknowledgement",
            intent="receipt_confirmation",
            subject="Re: {{ subject }}",
            body=(
                "Hello {{ sender_name }},\n\nWe received your inquiry regarding "
                "'{{ subject }}'. Order: {{ order_id }}"
            ),
        )
    )

    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=registry,
        draft_store=draft_store,
    )

    # 1. Insert job in NORMALIZED state
    initial_job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thread_id,
        state=JobState.NORMALIZED,
        idempotency_key=f"idem-{job_id}",
    )
    job, is_new = await job_store.create_job(initial_job)
    assert is_new is True

    # 2. Evaluate and persist gate decision
    cls = Classification(
        category="acknowledgement",
        intent="receipt_confirmation",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
        confidence=0.97,
        decided_by="rule",
    )

    msg_context = {
        "message_id": str(msg_id),
        "thread_id": str(thread_id),
        "subject": "Urgent Order Inquiry",
        "sender": {"name": "Client User", "email": "client@example.com"},
    }
    biz_data = {"order_id": "ORD-5555"}

    decision = await gate.evaluate_and_persist(
        job=job,
        classification=cls,
        message=msg_context,
        business_data=biz_data,
    )

    # 3. Verify in-memory decision
    assert decision.action == GateAction.TEMPLATE_REPLY
    assert decision.job.state == JobState.DRAFTED
    assert decision.should_retrieve is False
    assert decision.should_generate is False

    # 4. Verify job state in PostgreSQL
    stored_job = await job_store.get_job(org_id, job_id)
    assert stored_job is not None
    assert stored_job.state == JobState.DRAFTED

    # 5. Verify draft row in generated_draft table
    assert decision.rendered_draft is not None
    stored_draft = await draft_store.get_draft(decision.rendered_draft.id, org_id)
    assert stored_draft is not None
    assert stored_draft.subject == "Re: Urgent Order Inquiry"
    assert "Hello Client User" in stored_draft.body
    assert "Order: ORD-5555" in stored_draft.body
    assert stored_draft.status == "draft"
    assert stored_draft.model_name == "template"
    assert stored_draft.model_tier == "template"
    assert stored_draft.cost_estimate == 0.0
    assert stored_draft.input_tokens == 0
    assert stored_draft.output_tokens == 0
    assert stored_draft.citations == []

    # 6. Verify processing_event audit log
    events = await job_store.list_events_for_job(org_id, job_id)
    event_states = [e.state_to for e in events]
    assert JobState.CLASSIFIED in event_states
    assert JobState.DRAFTED in event_states


@pytest.mark.asyncio
async def test_postgres_template_fallback_to_ai(db_pool: asyncpg.Pool) -> None:
    """R6.14: When no template matches, gate must fall back to 'ai' and transition to QUEUED."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await seed_prerequisites(db_pool, org_id, mbx_id, msg_id, thread_id)

    job_store = PostgresJobStore(db_pool)
    draft_store = PostgresDraftStore(db_pool)

    # Empty registry -> No template will match
    empty_registry = TemplateRegistry()

    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=empty_registry,
        draft_store=draft_store,
    )

    initial_job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thread_id,
        state=JobState.NORMALIZED,
        idempotency_key=f"idem-{job_id}",
    )
    job, _ = await job_store.create_job(initial_job)

    # Classification requests template, but no template exists
    cls = Classification(
        category="billing",
        intent="refund_request",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=True,
        confidence=0.88,
        decided_by="ml",
    )

    decision = await gate.evaluate_and_persist(
        job=job,
        classification=cls,
        message={"subject": "Refund"},
    )

    # Must fall back to AI generation (never drop or block actionable mail)
    assert decision.action == GateAction.PROCEED_RAG
    assert decision.job.state == JobState.QUEUED
    assert decision.workflow_hint == "ai"
    assert decision.should_generate is True
    assert decision.should_retrieve is True

    # Stored job must be QUEUED
    stored_job = await job_store.get_job(org_id, job_id)
    assert stored_job is not None
    assert stored_job.state == JobState.QUEUED

    # No drafts must be created
    drafts = await draft_store.list_drafts_for_job(job_id, org_id)
    assert len(drafts) == 0


@pytest.mark.asyncio
async def test_postgres_template_draft_tenant_isolation(db_pool: asyncpg.Pool) -> None:
    """R5.3: Ensure drafts in generated_draft are strictly scoped by organization_id."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    mbx_a = uuid.uuid4()
    msg_a = uuid.uuid4()
    th_a = uuid.uuid4()
    job_a = uuid.uuid4()

    await seed_prerequisites(db_pool, org_a, mbx_a, msg_a, th_a)

    job_store = PostgresJobStore(db_pool)
    draft_store = PostgresDraftStore(db_pool)

    registry = TemplateRegistry()
    registry.register(
        TemplateDefinition(
            id="ack-v1",
            version="v1",
            category="acknowledgement",
            intent="receipt_confirmation",
            subject="Re: {{ subject }}",
            body="Hello {{ sender_name }}",
        )
    )

    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=registry,
        draft_store=draft_store,
    )

    job = Job(
        id=job_a,
        organization_id=org_a,
        message_id=msg_a,
        thread_id=th_a,
        state=JobState.CLASSIFIED,
        idempotency_key=f"idem-{job_a}",
    )
    job, _ = await job_store.create_job(job)

    cls = Classification(
        category="acknowledgement",
        intent="receipt_confirmation",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
    )

    decision = await gate.evaluate_and_persist(
        job=job,
        classification=cls,
        message={"subject": "Inquiry", "sender_name": "Org A User"},
    )

    assert decision.rendered_draft is not None
    draft_id = decision.rendered_draft.id

    # Org A can access the draft
    draft_for_a = await draft_store.get_draft(draft_id, org_a)
    assert draft_for_a is not None
    assert draft_for_a.id == draft_id

    # Org B CANNOT access the draft
    draft_for_b = await draft_store.get_draft(draft_id, org_b)
    assert draft_for_b is None

    # Listing by thread for Org B yields empty
    drafts_b = await draft_store.list_drafts_for_thread(th_a, org_b)
    assert len(drafts_b) == 0

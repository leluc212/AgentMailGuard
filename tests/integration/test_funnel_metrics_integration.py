"""PostgreSQL integration tests for funnel metrics and reconciliation (R6.10, R6.15, R21.4).

Verifies in a live PostgreSQL environment:
1. State transitions through EarlyExitGate persist to DB and atomically emit funnel metrics.
2. The Prometheus exposition payload contains all funnel counters alongside each other.
3. Mathematical reconciliation verifies zero residual bucket with live database state.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest
from prometheus_client import CollectorRegistry

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
from packages.observability.funnel import compute_funnel_reconciliation
from packages.observability.metrics import (
    create_pipeline_metrics,
    generate_metrics_payload,
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


async def seed_test_envelope(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    msg_id: uuid.UUID,
    thread_id: uuid.UUID,
    subject: str = "Funnel Test Email",
) -> None:
    """Seed prerequisite records in database."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Funnel Org {org_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Funnel Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            f"funnel-{org_id.hex[:6]}@example.com",
        )
        await conn.execute(
            """
            INSERT INTO email_thread (
                id, organization_id, mailbox_id, provider_thread_id, subject_normalized, status
            )
            VALUES ($1, $2, $3, $4, $5, 'open')
            ON CONFLICT (id) DO NOTHING;
            """,
            thread_id,
            org_id,
            mbx_id,
            f"th-funnel-{thread_id.hex[:6]}",
            subject.lower(),
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, subject, body_text,
                received_at
            )
            VALUES (
                $1, $2, $3, $4, $5, 'inbound', 'sender@corp.com', 'Sender', $6, 'Body text', now()
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"msg-funnel-{msg_id.hex[:6]}",
            subject,
        )


@pytest.mark.asyncio
async def test_funnel_metrics_integration_with_postgres(db_pool: asyncpg.Pool) -> None:
    """Verify live PostgreSQL gate transitions emit Prometheus counters with zero residual."""
    # 1. Setup isolated metrics registry
    reg = CollectorRegistry(auto_describe=True)
    metrics = create_pipeline_metrics(registry=reg)

    # 2. Setup templates and stores
    template_reg = TemplateRegistry()
    template_reg.register(
        TemplateDefinition(
            id="ack_std",
            category="acknowledgement",
            intent="receipt_confirmation",
            subject="Re: {{ subject }}",
            body="Thank you {{ sender.name }}, we received your email.",
            version="v1",
        )
    )

    job_store = PostgresJobStore(db_pool)
    draft_store = PostgresDraftStore(db_pool)
    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=template_reg,
        draft_store=draft_store,
        metrics=metrics,
    )

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()

    # Pre-seed 4 messages
    msg_ids = [uuid.uuid4() for _ in range(4)]
    th_ids = [uuid.uuid4() for _ in range(4)]
    for i in range(4):
        await seed_test_envelope(db_pool, org_id, mbx_id, msg_ids[i], th_ids[i], f"Email {i}")

    # Case 1: Early Exit (no reply required)
    j1_id = uuid.uuid4()
    job_1_entity = Job(
        id=j1_id,
        organization_id=org_id,
        message_id=msg_ids[0],
        thread_id=th_ids[0],
        state=JobState.NORMALIZED,
        idempotency_key=f"idem-{j1_id}",
    )
    await job_store.create_job(job_1_entity)
    cls_1 = Classification(
        category="marketing",
        intent="newsletter",
        priority="low",
        reply_required=False,
        workflow_hint="none",
        retrieval_required=False,
        confidence=0.99,
        decided_by="rules",
    )
    dec_1 = await gate.evaluate_and_persist(job=job_1_entity, classification=cls_1)
    assert dec_1.action == GateAction.EARLY_EXIT
    stored_j1 = await job_store.get_job(org_id, job_1_entity.id)
    assert stored_j1 is not None and stored_j1.state == JobState.COMPLETED

    # Case 2: Template Reply
    j2_id = uuid.uuid4()
    job_2_entity = Job(
        id=j2_id,
        organization_id=org_id,
        message_id=msg_ids[1],
        thread_id=th_ids[1],
        state=JobState.NORMALIZED,
        idempotency_key=f"idem-{j2_id}",
    )
    await job_store.create_job(job_2_entity)
    cls_2 = Classification(
        category="acknowledgement",
        intent="receipt_confirmation",
        priority="normal",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
        confidence=0.96,
        decided_by="rules",
    )
    msg_ctx = {"subject": "Test Message", "sender": {"name": "Alice"}}
    dec_2 = await gate.evaluate_and_persist(job=job_2_entity, classification=cls_2, message=msg_ctx)
    assert dec_2.action == GateAction.TEMPLATE_REPLY
    stored_j2 = await job_store.get_job(org_id, job_2_entity.id)
    assert stored_j2 is not None and stored_j2.state == JobState.DRAFTED
    assert dec_2.rendered_draft is not None
    stored_draft = await draft_store.get_draft(dec_2.rendered_draft.id, org_id)
    assert stored_draft is not None

    # Case 3: AI Generation with RAG
    j3_id = uuid.uuid4()
    job_3_entity = Job(
        id=j3_id,
        organization_id=org_id,
        message_id=msg_ids[2],
        thread_id=th_ids[2],
        state=JobState.NORMALIZED,
        idempotency_key=f"idem-{j3_id}",
    )
    await job_store.create_job(job_3_entity)
    cls_3 = Classification(
        category="technical_support",
        intent="bug_report",
        priority="high",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=True,
        confidence=0.91,
        decided_by="ml",
    )
    dec_3 = await gate.evaluate_and_persist(job=job_3_entity, classification=cls_3)
    assert dec_3.action == GateAction.PROCEED_RAG
    stored_j3 = await job_store.get_job(org_id, job_3_entity.id)
    assert stored_j3 is not None and stored_j3.state == JobState.QUEUED

    # Case 4: AI Generation without RAG
    j4_id = uuid.uuid4()
    job_4_entity = Job(
        id=j4_id,
        organization_id=org_id,
        message_id=msg_ids[3],
        thread_id=th_ids[3],
        state=JobState.NORMALIZED,
        idempotency_key=f"idem-{j4_id}",
    )
    await job_store.create_job(job_4_entity)
    cls_4 = Classification(
        category="general_inquiry",
        intent="greeting",
        priority="low",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=False,
        confidence=0.89,
        decided_by="ml",
    )
    dec_4 = await gate.evaluate_and_persist(job=job_4_entity, classification=cls_4)
    assert dec_4.action == GateAction.PROCEED_NO_RAG
    stored_j4 = await job_store.get_job(org_id, job_4_entity.id)
    assert stored_j4 is not None and stored_j4.state == JobState.QUEUED

    # 3. Simulate an AI generation completion for emails_generated_total
    metrics.emails_generated_total.labels(organization=str(org_id), model_tier="fast").inc()

    # 4. Verify Prometheus /metrics exposition payload (R21.4, design.md §10)
    payload_bytes, content_type = generate_metrics_payload(reg)
    payload_str = payload_bytes.decode("utf-8")

    assert "triage_funnel_outcomes_total" in payload_str
    assert "emails_early_exit_total" in payload_str
    assert "emails_templated_total" in payload_str
    assert "emails_generated_total" in payload_str
    assert 'template_id="ack_std"' in payload_str
    assert 'outcome="early_exit"' in payload_str
    assert 'outcome="template"' in payload_str
    assert 'outcome="ai_generation"' in payload_str

    # 5. Verify mathematical reconciliation with zero residual (R6.15)
    report = compute_funnel_reconciliation(metrics, organization=org_id)
    assert report.total_triaged == 4
    assert report.early_exit_count == 1
    assert report.template_count == 1
    assert report.ai_generation_count == 2
    assert report.ai_rag_count == 1
    assert report.ai_no_rag_count == 1
    assert report.residual_count == 0
    assert report.is_reconciled is True
    assert report.early_exit_ratio == 0.25
    assert report.template_ratio == 0.25
    assert report.ai_generation_ratio == 0.50
    assert report.rag_share_of_ai == 0.50

"""Live PostgreSQL integration tests for ContextBuilder orchestration (R14.8, R6.6, R18.1).

Verifies end-to-end:
- Live database thread assembly via PostgresMessageStore and PostgresThreadStateStore (Task 4.3).
- Live hybrid RAG search via PostgresSearchBackend and HybridRetriever (Task 3.8).
- R6.6: Conditional RAG gating (zero search calls when retrieval_required=False).
- R14.8: Strict fixed assembly order in ContextPackage.
- R18.1: Durable Job state transition from QUEUED to CONTEXT_READY with ProcessingEvent.
- Strict multi-tenant isolation across multiple organizations (GEMINI.md §8).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from packages.business.postgres import PostgresBusinessDataProvider
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import (
    ContextBuilder,
)
from packages.core.settings import AppSettings, SummarizationSettings
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.business import CustomerStatus, EntityType, FactStatus
from packages.domain.entities import (
    Classification,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
    ThreadState,
)
from packages.domain.state_machine import JobState
from packages.llm.profile import AgentProfileRegistry
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.query_builder import RetrievalQueryBuilder
from packages.retrieval.retriever import HybridRetriever


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide asyncpg pool connected to test database."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


def _pad_vec(vec: list[float] | None, dim: int = 1536) -> list[float] | None:
    if vec is None:
        return None
    v = [float(x) for x in vec]
    if len(v) < dim:
        v = v + [0.0] * (dim - len(v))
    elif len(v) > dim:
        v = v[:dim]
    return v


async def _seed_org(pool: asyncpg.Pool[Any], org_id: UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


async def _seed_mailbox(
    pool: asyncpg.Pool[Any],
    org_id: UUID,
    mbx_id: UUID,
    address: str = "support@example.com",
) -> None:
    await _seed_org(pool, org_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Support Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            address,
        )


async def _seed_thread(
    pool: asyncpg.Pool[Any],
    org_id: UUID,
    mbx_id: UUID,
    thread_id: UUID,
    subject: str = "Invoice Billing Question",
) -> None:
    await _seed_mailbox(pool, org_id, mbx_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_thread (
                id, organization_id, mailbox_id, subject_normalized, message_count, status
            )
            VALUES ($1, $2, $3, $4, 1, 'open')
            ON CONFLICT (id) DO NOTHING;
            """,
            thread_id,
            org_id,
            mbx_id,
            subject,
        )


async def _seed_message(pool: asyncpg.Pool[Any], msg: NormalizedMessage) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, subject, body_text_clean, received_at
            ) VALUES (
                $1, $2, $3, $4, $5, 'inbound', $6, $7, $8, $9, $10
            ) ON CONFLICT (id) DO NOTHING;
            """,
            msg.message_id,
            msg.organization_id,
            msg.mailbox_id,
            msg.thread_id,
            msg.provider_message_id,
            msg.sender.email,
            msg.sender.name,
            msg.subject,
            msg.body_text_clean or msg.body_text,
            msg.received_at,
        )


async def _seed_chunk(
    pool: asyncpg.Pool[Any],
    chunk_id: UUID,
    doc_id: UUID,
    org_id: UUID,
    content: str,
    category: str = "billing",
    external_id: str | None = None,
    embedding: list[float] | None = None,
) -> None:
    await _seed_org(pool, org_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO knowledge_document (id, organization_id, title, status, category, version)
            VALUES ($1, $2, $3, 'active', $4, 1)
            ON CONFLICT (id) DO NOTHING;
            """,
            doc_id,
            org_id,
            f"Doc {doc_id}",
            category,
        )
        await conn.execute(
            """
            INSERT INTO knowledge_chunk (
                id, document_id, organization_id, chunk_index, external_id,
                heading_path, section, category, content, metadata, version, content_tsv
            )
            VALUES (
                $1, $2, $3, 0, $4,
                '{}', 'Terms', $5, $6, '{"source": "test"}'::jsonb, 1, to_tsvector('english', $6)
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            chunk_id,
            doc_id,
            org_id,
            external_id or str(chunk_id),
            category,
            content,
        )
        if embedding is not None:
            vec = _pad_vec(embedding)
            await conn.execute(
                """
                INSERT INTO embedding_record (chunk_id, organization_id, model, dim, embedding)
                VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)
                ON CONFLICT (chunk_id) DO NOTHING;
                """,
                chunk_id,
                org_id,
                vec,
            )


@pytest.mark.asyncio
async def test_context_builder_postgres_end_to_end(db_pool: asyncpg.Pool[Any]) -> None:
    """Full end-to-end ContextBuilder execution against live PostgreSQL."""
    org_id = uuid4()
    mbx_id = uuid4()
    thread_id = uuid4()
    curr_msg_id = uuid4()
    earlier_msg_id = uuid4()
    chunk_id = uuid4()
    doc_id = uuid4()

    await _seed_thread(db_pool, org_id, mbx_id, thread_id)

    now = datetime.now(UTC)
    earlier_msg = NormalizedMessage(
        message_id=earlier_msg_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thread_id,
        provider="gmail",
        provider_message_id=f"prov-{earlier_msg_id}",
        sender=EmailAddress(email="alice@customer.com", name="Alice"),
        recipients=[EmailAddress(email="support@example.com")],
        subject="Invoice billing query",
        body_text="Hello, I received invoice INV-2026-999 yesterday.",
        body_text_clean="Hello, I received invoice INV-2026-999 yesterday.",
        received_at=now - timedelta(minutes=10),
    )
    curr_msg = NormalizedMessage(
        message_id=curr_msg_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thread_id,
        provider="gmail",
        provider_message_id=f"prov-{curr_msg_id}",
        sender=EmailAddress(email="alice@customer.com", name="Alice"),
        recipients=[EmailAddress(email="support@example.com")],
        subject="Re: Invoice billing query",
        body_text="Can you explain the payment terms for invoice INV-2026-999?",
        body_text_clean="Can you explain the payment terms for invoice INV-2026-999?",
        received_at=now,
    )
    await _seed_message(db_pool, earlier_msg)
    await _seed_message(db_pool, curr_msg)

    # Seed ThreadState in PostgreSQL
    thread_state_store = PostgresThreadStateStore(db_pool)
    state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Invoice Payment Terms",
        current_intent="Customer inquiring about payment terms for INV-2026-999",
        summary="Customer asked about invoice INV-2026-999 received yesterday.",
        open_questions=["What are the payment terms?"],
        summarized_through_message_id=earlier_msg_id,
        token_estimate=120,
        version=1,
    )
    await thread_state_store.save(state)

    # Seed knowledge chunk in PostgreSQL
    await _seed_chunk(
        pool=db_pool,
        chunk_id=chunk_id,
        doc_id=doc_id,
        org_id=org_id,
        content=(
            "Re: Invoice billing query regarding invoice_inquiry: "
            "Can you explain payment terms for invoice INV-2026-999? "
            "Invoice terms require Net 30 payment via wire transfer."
        ),
        category="billing",
        external_id="BILLING-DOC-TERMS",
    )

    # Seed Job in QUEUED state in PostgreSQL
    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=curr_msg_id,
        state=JobState.QUEUED,
        idempotency_key=f"job-{curr_msg_id}",
    )
    persisted_job, is_new = await job_store.create_job(job)
    assert is_new is True
    assert persisted_job.state == JobState.QUEUED

    # Instantiate live stores and ContextBuilder
    message_store = PostgresMessageStore(db_pool)
    thread_assembler = ThreadContextAssembler(
        message_store=message_store,
        thread_state_store=thread_state_store,
        settings=SummarizationSettings(keep_latest_messages=2),
    )
    search_backend = PostgresSearchBackend(db_pool)
    retriever = HybridRetriever(backend=search_backend)
    builder = ContextBuilder(
        thread_assembler=thread_assembler,
        retriever=retriever,
        query_builder=RetrievalQueryBuilder(),
        job_store=job_store,
    )

    classification = Classification(
        category="billing",
        intent="invoice_inquiry",
        retrieval_required=True,
    )

    pkg = await builder.build_context(
        job=job,
        message=curr_msg,
        classification=classification,
    )

    # 1. Verify ContextPackage content
    assert isinstance(pkg, ContextPackage)
    assert pkg.thread_summary == "Customer asked about invoice INV-2026-999 received yesterday."
    assert len(pkg.recent_messages) == 1
    assert pkg.recent_messages[0].message_id == earlier_msg_id
    assert pkg.current_message.message_id == curr_msg_id

    # 2. Verify Hybrid RAG retrieved knowledge
    assert len(pkg.retrieved_chunks) >= 1
    top_chunk = pkg.retrieved_chunks[0]
    assert "Net 30 payment" in top_chunk.content

    # 3. Verify strict fixed assembly order (R14.8)
    sections = pkg.get_ordered_sections()
    section_keys = [s[0] for s in sections]
    assert section_keys == [
        "agent_instructions",
        "category_instructions",
        "thread_summary",
        "recent_thread_messages",
        "current_email",
        "retrieved_knowledge",
    ]

    # 4. Verify durable Job state transition in PostgreSQL (R18.1)
    db_job = await job_store.get_job(org_id, job.id)
    assert db_job is not None
    assert db_job.state == JobState.CONTEXT_READY

    # 5. Verify ProcessingEvent telemetry stored in PostgreSQL (R18.4)
    events = await job_store.list_events_for_job(org_id, job.id)
    assert len(events) >= 2  # initial event from create_job + transition event
    transition_event = next(
        e for e in events if e.state_to in (JobState.CONTEXT_READY.value, "CONTEXT_READY")
    )
    assert transition_event.event_type == "state_transition"
    assert transition_event.payload.get("retrieval_performed") is True
    chunks_count = transition_event.payload.get("retrieved_chunks_count")
    assert isinstance(chunks_count, int) and chunks_count >= 1


@pytest.mark.asyncio
async def test_context_builder_postgres_skips_rag_when_retrieval_not_required(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """R6.6: Verify live pipeline skips RAG completely when retrieval_required=False."""
    org_id = uuid4()
    mbx_id = uuid4()
    thread_id = uuid4()
    msg_id = uuid4()

    await _seed_thread(db_pool, org_id, mbx_id, thread_id)

    curr_msg = NormalizedMessage(
        message_id=msg_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thread_id,
        provider="gmail",
        provider_message_id=f"prov-{msg_id}",
        sender=EmailAddress(email="bob@customer.com"),
        recipients=[EmailAddress(email="support@example.com")],
        subject="Thanks!",
        body_text="Thank you so much for the resolution!",
        received_at=datetime.now(UTC),
    )
    await _seed_message(db_pool, curr_msg)

    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=msg_id,
        state=JobState.QUEUED,
        idempotency_key=f"job-{msg_id}",
    )
    await job_store.create_job(job)

    thread_assembler = ThreadContextAssembler(
        message_store=PostgresMessageStore(db_pool),
        thread_state_store=PostgresThreadStateStore(db_pool),
    )
    search_backend = PostgresSearchBackend(db_pool)
    retriever = HybridRetriever(backend=search_backend)
    builder = ContextBuilder(
        thread_assembler=thread_assembler,
        retriever=retriever,
        query_builder=RetrievalQueryBuilder(),
        job_store=job_store,
    )

    classification = Classification(
        category="acknowledgement",
        intent="thank_you",
        retrieval_required=False,
    )

    pkg = await builder.build_context(
        job=job,
        message=curr_msg,
        classification=classification,
    )

    # RAG chunks empty
    assert len(pkg.retrieved_chunks) == 0

    # Job still transitions to CONTEXT_READY
    db_job = await job_store.get_job(org_id, job.id)
    assert db_job is not None
    assert db_job.state == JobState.CONTEXT_READY

    # Event records retrieval_performed = False
    events = await job_store.list_events_for_job(org_id, job.id)
    transition_event = next(
        e for e in events if e.state_to in (JobState.CONTEXT_READY.value, "CONTEXT_READY")
    )
    assert transition_event.payload.get("retrieval_performed") is False
    assert transition_event.payload.get("retrieved_chunks_count") == 0


@pytest.mark.asyncio
async def test_context_builder_postgres_tenant_isolation(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """GEMINI.md §8: Multi-tenant isolation asserting Org A never retrieves Org B chunks."""
    org_a = uuid4()
    org_b = uuid4()
    mbx_a = uuid4()
    thread_a = uuid4()
    msg_a = uuid4()

    await _seed_thread(db_pool, org_a, mbx_a, thread_a)

    curr_msg = NormalizedMessage(
        message_id=msg_a,
        organization_id=org_a,
        mailbox_id=mbx_a,
        thread_id=thread_a,
        provider="gmail",
        provider_message_id=f"prov-{msg_a}",
        sender=EmailAddress(email="client@tenant-a.com"),
        recipients=[EmailAddress(email="support@example.com")],
        subject="Confidential Policy",
        body_text="What is the confidential policy regarding refunds?",
        received_at=datetime.now(UTC),
    )
    await _seed_message(db_pool, curr_msg)

    # Seed chunk ONLY in Org B
    await _seed_chunk(
        pool=db_pool,
        chunk_id=uuid4(),
        doc_id=uuid4(),
        org_id=org_b,
        content="Confidential policy regarding refunds: strictly Org B internal only.",
        category="policy",
        external_id="POLICY-ORG-B",
    )

    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=uuid4(),
        organization_id=org_a,
        thread_id=thread_a,
        message_id=msg_a,
        state=JobState.QUEUED,
        idempotency_key=f"job-{msg_a}",
    )
    await job_store.create_job(job)

    thread_assembler = ThreadContextAssembler(
        message_store=PostgresMessageStore(db_pool),
        thread_state_store=PostgresThreadStateStore(db_pool),
    )
    search_backend = PostgresSearchBackend(db_pool)
    retriever = HybridRetriever(backend=search_backend)
    builder = ContextBuilder(
        thread_assembler=thread_assembler,
        retriever=retriever,
        query_builder=RetrievalQueryBuilder(),
        job_store=job_store,
    )

    classification = Classification(
        category="policy",
        intent="refund_policy",
        retrieval_required=True,
    )

    pkg = await builder.build_context(
        job=job,
        message=curr_msg,
        classification=classification,
    )

    # Must NOT retrieve Org B chunk
    assert len(pkg.retrieved_chunks) == 0


async def _seed_customer_with_order(
    pool: asyncpg.Pool[Any], org_id: UUID, *, email: str, order_number: str, status: str
) -> None:
    await _seed_org(pool, org_id)
    customer_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer (id, organization_id, email, name) VALUES ($1, $2, $3, $4);",
            customer_id,
            org_id,
            email,
            "Alice Example",
        )
        await conn.execute(
            'INSERT INTO "order" (id, organization_id, customer_id, order_number, status, total) '
            "VALUES ($1, $2, $3, $4, $5, $6);",
            uuid4(),
            org_id,
            customer_id,
            order_number,
            status,
            Decimal("120.00"),
        )


@pytest.mark.asyncio
async def test_context_builder_postgres_fetches_the_senders_typed_order(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """R13.3/R13.7 wiring: a typed order number in a general inquiry is looked up for the
    sender inside the tenant, rendered as section 7 and recorded in CONTEXT_READY."""
    org_id, other_org = uuid4(), uuid4()
    mbx_id, thread_id, msg_id = uuid4(), uuid4(), uuid4()
    await _seed_thread(db_pool, org_id, mbx_id, thread_id, subject="Order status")
    await _seed_customer_with_order(
        db_pool, org_id, email="alice@customer.com", order_number="ORD-82915", status="shipped"
    )
    # Same address and order number in another tenant must not leak (GEMINI.md §8).
    await _seed_customer_with_order(
        db_pool, other_org, email="alice@customer.com", order_number="ORD-82915", status="cancelled"
    )

    curr_msg = NormalizedMessage(
        message_id=msg_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thread_id,
        provider="gmail",
        provider_message_id=f"prov-{msg_id}",
        sender=EmailAddress(email="alice@customer.com", name="Alice"),
        recipients=[EmailAddress(email="support@example.com")],
        subject="Order status",
        body_text="Hi, what is the status of order 82915?",
        body_text_clean="Hi, what is the status of order 82915?",
        received_at=datetime.now(UTC),
    )
    await _seed_message(db_pool, curr_msg)

    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=msg_id,
        state=JobState.QUEUED,
        idempotency_key=f"job-{msg_id}",
    )
    await job_store.create_job(job)

    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(
            message_store=PostgresMessageStore(db_pool),
            thread_state_store=PostgresThreadStateStore(db_pool),
        ),
        job_store=job_store,
        business_data_provider=PostgresBusinessDataProvider(db_pool),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        business_timeout_ms=2000,
    )

    pkg = await builder.build_context(
        job=job,
        message=curr_msg,
        classification=Classification(category="general_inquiry", retrieval_required=False),
    )

    business = pkg.business_data
    assert business is not None
    assert business.customer_status == CustomerStatus.FOUND
    assert business.degraded is False
    order = next(
        f for f in business.facts if f.entity == EntityType.ORDER and f.reference == "ORD-82915"
    )
    assert order.status == FactStatus.FOUND
    assert dict(order.attributes)["status"] == "shipped"
    assert pkg.get_ordered_sections()[-1][0] == "business_data"

    events = await job_store.list_events_for_job(org_id, job.id)
    ready = next(e for e in events if e.state_to == JobState.CONTEXT_READY.value)
    assert ready.payload["customer_status"] == "FOUND"
    assert ready.payload["business_data_degraded"] is False
    assert {
        "entity": "order",
        "reference": "ORD-82915",
        "status": "FOUND",
        "reason": None,
    } in ready.payload["business_fact_statuses"]

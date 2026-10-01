"""Unit tests for Context Builder orchestration (R14.8, R6.6, R18.1).

Verifies:
- InstructionProvider protocol and DefaultInstructionProvider (R14.1, R14.8 static prefix).
- ContextBuilder gathers thread context, business data, and hybrid RAG.
- R6.6: Conditional hybrid RAG gating on retrieval_required.
- R14.8: Strict fixed assembly order in ContextPackage.
- R18.1: Job state transition from QUEUED to CONTEXT_READY.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import (
    ContextBuilder,
    DefaultInstructionProvider,
    InstructionProvider,
)
from packages.core.settings import SummarizationSettings
from packages.db.job import InMemoryJobStore
from packages.domain.entities import (
    Classification,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
    ThreadState,
)
from packages.domain.state_machine import JobState
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import RetrievalQuery
from packages.retrieval.query_builder import RetrievalQueryBuilder
from packages.retrieval.retriever import HybridRetriever, RetrievalResult


def _create_test_message(
    org_id: UUID,
    thread_id: UUID,
    msg_id: UUID | None = None,
    subject: str = "Test Subject",
    body: str = "Test Body",
    received_at: datetime | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=msg_id or uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=thread_id,
        provider="mock",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email="customer@example.com"),
        recipients=[EmailAddress(email="support@company.com")],
        subject=subject,
        body_text=body,
        body_text_clean=body,
        received_at=received_at or datetime.now(UTC),
    )


def test_instruction_provider_protocols_and_defaults() -> None:
    """Verify InstructionProvider returns static agent and category instructions (R14.1, R14.8)."""
    provider = DefaultInstructionProvider()
    assert isinstance(provider, InstructionProvider)

    # Billing category
    agent_inst, cat_inst = provider.get_instructions("billing")
    assert "enterprise AI assistant" in agent_inst
    assert "billing" in cat_inst.lower()

    # Support category
    _, support_inst = provider.get_instructions("support")
    assert "support" in support_inst.lower() or "diagnostic" in support_inst.lower()

    # Unknown category falls back cleanly
    _, fallback_inst = provider.get_instructions("custom_unseen_category")
    assert "custom_unseen_category" in fallback_inst


def test_build_context_with_retrieval_required_true() -> None:
    """Verify ContextBuilder gathers thread, invokes hybrid RAG, and emits 7 ordered sections."""
    org_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)

    msg1 = _create_test_message(
        org_id,
        thread_id,
        body="Earlier question",
        received_at=now - timedelta(minutes=5),
    )
    curr_msg = _create_test_message(
        org_id,
        thread_id,
        body="Current invoice question for INV-2026-001",
        received_at=now,
    )

    # Seed in-memory search backend with a relevant chunk
    backend = FakeSearchBackend()
    backend.add_chunk(
        chunk_id="CHUNK-INV-1",
        document_id="DOC-BILLING",
        organization_id=str(org_id),
        content="Invoice payment procedure and terms for INV-2026-001.",
        category="billing",
    )
    retriever = HybridRetriever(backend=backend)

    assembler = ThreadContextAssembler(settings=SummarizationSettings(keep_latest_messages=2))
    builder = ContextBuilder(
        thread_assembler=assembler,
        retriever=retriever,
        query_builder=RetrievalQueryBuilder(),
    )

    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=curr_msg.message_id,
        state=JobState.QUEUED,
    )
    classification = Classification(
        category="billing",
        intent="invoice_inquiry",
        retrieval_required=True,
    )

    pkg = asyncio.run(
        builder.build_context(
            job=job,
            message=curr_msg,
            classification=classification,
            thread_messages=[msg1, curr_msg],
        )
    )

    assert isinstance(pkg, ContextPackage)
    assert len(pkg.retrieved_chunks) >= 1
    assert pkg.retrieved_chunks[0].chunk_id == "CHUNK-INV-1"
    assert pkg.current_message.message_id == curr_msg.message_id
    assert len(pkg.recent_messages) == 1

    # Verify fixed assembly order (R14.8)
    sections = pkg.get_ordered_sections()
    section_names = [s[0] for s in sections]
    assert section_names == [
        "agent_instructions",
        "category_instructions",
        "recent_thread_messages",
        "current_email",
        "retrieved_knowledge",
    ]


def test_build_context_with_retrieval_required_false_skips_rag() -> None:
    """R6.6: When retrieval_required=False, hybrid RAG is skipped completely (zero search calls)."""
    org_id = uuid4()
    thread_id = uuid4()
    curr_msg = _create_test_message(org_id, thread_id, body="Thank you for your help!")

    # Spy backend to assert zero calls
    call_count = 0

    class SpyRetriever:
        async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
            nonlocal call_count
            call_count += 1
            return RetrievalResult(candidates=[])

    assembler = ThreadContextAssembler()
    builder = ContextBuilder(
        thread_assembler=assembler,
        retriever=SpyRetriever(),  # type: ignore[arg-type]
    )

    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=curr_msg.message_id,
        state=JobState.QUEUED,
    )
    classification = Classification(
        category="acknowledgement",
        intent="thank_you",
        retrieval_required=False,  # Skip RAG
    )

    pkg = asyncio.run(
        builder.build_context(
            job=job,
            message=curr_msg,
            classification=classification,
            thread_messages=[curr_msg],
        )
    )

    assert call_count == 0  # Assert ZERO retrieval calls made (R6.6)
    assert len(pkg.retrieved_chunks) == 0


def test_build_context_transitions_job_state_queued_to_context_ready() -> None:
    """R18.1: ContextBuilder transitions Job state from QUEUED to CONTEXT_READY."""
    org_id = uuid4()
    thread_id = uuid4()
    curr_msg = _create_test_message(org_id, thread_id)

    job_store = InMemoryJobStore()
    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=curr_msg.message_id,
        state=JobState.QUEUED,
    )
    asyncio.run(job_store.create_job(job))

    assembler = ThreadContextAssembler()
    builder = ContextBuilder(
        thread_assembler=assembler,
        job_store=job_store,
    )

    classification = Classification(category="support", retrieval_required=False)

    asyncio.run(
        builder.build_context(
            job=job,
            message=curr_msg,
            classification=classification,
            thread_messages=[curr_msg],
        )
    )

    # In-memory job state updated
    assert job.state == JobState.CONTEXT_READY

    # Durable store record updated
    persisted_job = asyncio.run(job_store.get_job(org_id, job.id))
    assert persisted_job is not None
    assert persisted_job.state == JobState.CONTEXT_READY


def test_build_context_with_thread_summary_wires_into_retrieval_query() -> None:
    """Verify thread summary from Task 4.3 is wired into RetrievalQueryBuilder."""
    org_id = uuid4()
    thread_id = uuid4()
    curr_msg = _create_test_message(org_id, thread_id, body="Need follow-up on earlier terms.")

    captured_query: RetrievalQuery | None = None

    class CaptureRetriever:
        async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
            nonlocal captured_query
            captured_query = query
            return RetrievalResult(candidates=[])

    assembler = ThreadContextAssembler()
    builder = ContextBuilder(
        thread_assembler=assembler,
        retriever=CaptureRetriever(),  # type: ignore[arg-type]
        query_builder=RetrievalQueryBuilder(),
    )

    thread_state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="License Negotiation",
        current_intent="Review enterprise tier discount",
        summary="Customer previously negotiated 20% discount on enterprise tier.",
        version=1,
    )

    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=curr_msg.message_id,
        state=JobState.QUEUED,
    )
    classification = Classification(
        category="sales",
        intent="discount_followup",
        retrieval_required=True,
    )

    asyncio.run(
        builder.build_context(
            job=job,
            message=curr_msg,
            classification=classification,
            thread_messages=[curr_msg],
            thread_state=thread_state,
        )
    )

    assert captured_query is not None
    # Retrieval query must contain the thread summary from Task 4.3
    assert "Customer previously negotiated 20% discount" in captured_query.semantic_text

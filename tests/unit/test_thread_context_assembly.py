"""Unit tests for Thread Context Assembly and token accounting (R8.5, R8.7, R8.8, H3).

Verifies:
- R8.5: Assembles context as summary + latest N messages + current email when summary exists.
- R8.2: Short threads without summary supply all messages verbatim.
- R8.7 / H3: Pre- vs post-compression token calculation and metric emission.
- ContextPackage conversion compatibility with design.md §5.4.
- Inbound email knowledge boundary isolation (R8.8).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from packages.context.assembly import AssembledThreadContext, ThreadContextAssembler
from packages.core.settings import SummarizationSettings
from packages.db.thread_state import InMemoryThreadStateStore
from packages.domain.entities import EmailAddress, NormalizedMessage, ThreadState
from packages.observability.metrics import create_pipeline_metrics


def _create_message(
    org_id: UUID,
    thread_id: UUID,
    msg_id: UUID | None = None,
    subject: str = "Test Subject",
    body: str = "Hello there",
    sender: str = "customer@example.com",
    received_at: datetime | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=msg_id or uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=thread_id,
        provider="mock",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email=sender),
        recipients=[EmailAddress(email="support@company.com")],
        subject=subject,
        body_text=body,
        body_text_clean=body,
        received_at=received_at or datetime.now(UTC),
    )


def test_assemble_short_thread_no_summary_verbatim_messages() -> None:
    """R8.2/R8.5: When no summary exists, all historical messages are supplied verbatim."""
    org_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)

    msg1 = _create_message(
        org_id,
        thread_id,
        body="Hi, I need help with invoice INV-100.",
        received_at=now - timedelta(minutes=10),
    )
    msg2 = _create_message(
        org_id,
        thread_id,
        body="Can you provide the invoice date?",
        sender="support@company.com",
        received_at=now - timedelta(minutes=5),
    )
    curr_msg = _create_message(
        org_id,
        thread_id,
        body="Sure, it was issued on September 15th.",
        received_at=now,
    )

    assembler = ThreadContextAssembler(settings=SummarizationSettings(keep_latest_messages=2))

    ctx = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=[msg1, msg2, curr_msg],
            thread_state=None,
        )
    )

    assert isinstance(ctx, AssembledThreadContext)
    assert ctx.has_summary is False
    assert ctx.summary is None
    assert len(ctx.recent_messages) == 2
    assert ctx.recent_messages[0].message_id == msg1.message_id
    assert ctx.recent_messages[1].message_id == msg2.message_id
    assert ctx.current_message.message_id == curr_msg.message_id
    assert ctx.tokens_saved == 0
    assert ctx.pre_compression_tokens == ctx.post_compression_tokens
    assert ctx.compression_ratio == 1.0


def test_assemble_long_thread_with_summary_latest_n_messages() -> None:
    """R8.5: When a summary exists, assemble summary + latest N messages + current email."""
    org_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)

    messages = [
        _create_message(
            org_id,
            thread_id,
            body=(
                f"Message {i}: Detailed discussion on enterprise license deployment "
                "with extensive setup instructions."
            ),
            received_at=now - timedelta(minutes=30 - i * 5),
        )
        for i in range(1, 6)
    ]
    curr_msg = _create_message(
        org_id,
        thread_id,
        body="Current message: Can we finalize the agreement?",
        received_at=now,
    )

    all_messages = messages + [curr_msg]

    thread_state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Enterprise License Setup",
        current_intent="Finalize agreement",
        summary="Customer discussed enterprise deployment with support across messages 1 to 5.",
        open_questions=["Can agreement be finalized?"],
        resolved_items=["Deployment topology chosen", "Pricing agreed"],
        summarized_through_message_id=UUID(str(messages[-1].message_id)),
        version=2,
    )

    metrics = create_pipeline_metrics()
    assembler = ThreadContextAssembler(
        settings=SummarizationSettings(keep_latest_messages=2),
        metrics=metrics,
    )

    ctx = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=all_messages,
            thread_state=thread_state,
        )
    )

    assert ctx.has_summary is True
    assert ctx.summary == thread_state.summary
    assert ctx.topic == "Enterprise License Setup"
    assert ctx.current_intent == "Finalize agreement"
    assert ctx.open_questions == ["Can agreement be finalized?"]
    assert ctx.resolved_items == ["Deployment topology chosen", "Pricing agreed"]

    # R8.5: latest N messages (default N=2)
    assert len(ctx.recent_messages) == 2
    assert ctx.recent_messages[0].message_id == messages[-2].message_id
    assert ctx.recent_messages[1].message_id == messages[-1].message_id
    assert ctx.current_message.message_id == curr_msg.message_id

    # R8.7: Tokens saved accounting for H3
    assert ctx.pre_compression_tokens > ctx.post_compression_tokens
    assert ctx.tokens_saved > 0
    assert ctx.tokens_saved == ctx.pre_compression_tokens - ctx.post_compression_tokens
    assert 0.0 < ctx.compression_ratio < 1.0

    # Metric increment verification
    val = metrics.tokens_saved_total.labels(organization=str(org_id))._value.get()
    assert val == ctx.tokens_saved


def test_assemble_single_message_thread() -> None:
    """A thread with only the incoming email has no history and zero tokens saved."""
    org_id = uuid4()
    thread_id = uuid4()
    curr_msg = _create_message(org_id, thread_id, body="Hello, first email ever.")

    assembler = ThreadContextAssembler()

    ctx = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=[curr_msg],
            thread_state=None,
        )
    )

    assert ctx.has_summary is False
    assert ctx.summary is None
    assert len(ctx.recent_messages) == 0
    assert ctx.tokens_saved == 0
    assert ctx.pre_compression_tokens == ctx.post_compression_tokens


def test_assemble_formatting_and_context_package_args() -> None:
    """Verifies prompt formatting and compatibility with ContextPackage kwargs."""
    org_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)

    msg1 = _create_message(
        org_id,
        thread_id,
        body="Earlier message",
        received_at=now - timedelta(minutes=5),
    )
    curr_msg = _create_message(org_id, thread_id, body="Latest question", received_at=now)

    thread_state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Billing Inquiry",
        current_intent="Check invoice",
        summary="Customer asked about invoice details.",
        open_questions=["What is the total?"],
        resolved_items=["Invoice exists"],
        version=1,
    )

    assembler = ThreadContextAssembler()
    ctx = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=[msg1, curr_msg],
            thread_state=thread_state,
        )
    )

    # Test format_for_prompt
    formatted = ctx.format_for_prompt()
    assert "=== THREAD SUMMARY ===" in formatted
    assert "Billing Inquiry" in formatted
    assert "Customer asked about invoice details." in formatted
    assert "=== RECENT MESSAGES ===" in formatted
    assert "Earlier message" in formatted
    assert "=== CURRENT EMAIL ===" in formatted
    assert "Latest question" in formatted

    # Test get_sections
    sections = ctx.get_sections()
    section_names = [s[0] for s in sections]
    assert section_names == ["thread_summary", "recent_thread_messages", "current_email"]

    # Test to_context_package_args
    pkg_args = ctx.to_context_package_args()
    assert pkg_args["current_message"] == curr_msg
    assert pkg_args["thread_summary"] == thread_state.summary
    assert len(pkg_args["recent_messages"]) == 1


def test_assemble_auto_fetch_from_stores() -> None:
    """Verifies automatic fetching of ThreadState from store when not explicitly passed."""
    org_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)

    state_store = InMemoryThreadStateStore()
    thread_state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Auto-fetch test",
        current_intent="Verify store fetch",
        summary="Store fetched successfully.",
        version=1,
    )
    asyncio.run(state_store.save(thread_state))

    curr_msg = _create_message(org_id, thread_id, body="Current message", received_at=now)

    assembler = ThreadContextAssembler(thread_state_store=state_store)

    ctx = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=[],
            thread_state=None,  # Should fetch from store
        )
    )

    assert ctx.has_summary is True
    assert ctx.summary == "Store fetched successfully."
    assert ctx.topic == "Auto-fetch test"


def test_inbound_email_strictly_isolated_from_knowledge_corpus() -> None:
    """R8.8: Inbound email content belongs strictly to the thread subsystem.

    Asserts that:
    1. ThreadContextAssembler interacts strictly with message/thread subsystems
       and has NO KnowledgeStore coupling.
    2. KnowledgeStore only accepts KnowledgeDocument entities and rejects message records.
    """
    from packages.db.knowledge import KnowledgeStore

    # ThreadContextAssembler has no knowledge_store attribute or parameter
    assembler = ThreadContextAssembler()
    assert not hasattr(assembler, "knowledge_store")

    # KnowledgeStore defines methods strictly for KnowledgeDocument and KnowledgeChunk
    assert hasattr(KnowledgeStore, "insert_document")
    assert hasattr(KnowledgeStore, "get_document")
    assert not hasattr(KnowledgeStore, "insert_message")
    assert not hasattr(KnowledgeStore, "get_message")


def test_h3_token_savings_progression() -> None:
    """R8.7 / R22.5: Verify H3 hypothesis - summarization keeps token consumption bounded.

    As thread grows from 3 to 10 messages:
    - Pre-compression tokens scale linearly with message count.
    - Post-compression tokens remain bounded.
    - Cumulative tokens saved increase monotonically.
    """
    org_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)

    assembler = ThreadContextAssembler(settings=SummarizationSettings(keep_latest_messages=2))

    # 1. Short thread (3 messages total): no summary
    short_history = [
        _create_message(
            org_id,
            thread_id,
            body=f"Paragraph {i}: Discussion on service tier.",
            received_at=now - timedelta(minutes=15 - i * 5),
        )
        for i in range(1, 3)
    ]
    curr_msg = _create_message(
        org_id,
        thread_id,
        body="Current message: Need clarification.",
        received_at=now,
    )

    ctx_short = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=short_history + [curr_msg],
            thread_state=None,
        )
    )
    assert ctx_short.tokens_saved == 0

    # 2. Long thread (10 messages total): with summary
    long_history = [
        _create_message(
            org_id,
            thread_id,
            body=(
                f"Long discussion message {i}: Lorem ipsum dolor sit amet, consectetur "
                "adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna. "
                "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris."
            ),
            received_at=now - timedelta(minutes=60 - i * 5),
        )
        for i in range(1, 10)
    ]

    thread_state_long = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Service Tier Configuration",
        current_intent="Clarify service tier requirements",
        summary="Customer and support discussed details, settling on standard deployment.",
        open_questions=["Need clarification on SLA"],
        resolved_items=["Pricing accepted", "Initial onboarding completed"],
        version=3,
    )

    ctx_long = asyncio.run(
        assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=curr_msg,
            thread_messages=long_history + [curr_msg],
            thread_state=thread_state_long,
        )
    )

    # In a 10-message thread, summarization yields significant token savings
    assert ctx_long.has_summary is True
    assert ctx_long.pre_compression_tokens > ctx_short.pre_compression_tokens * 2
    assert ctx_long.tokens_saved > 50
    assert ctx_long.compression_ratio < 0.70

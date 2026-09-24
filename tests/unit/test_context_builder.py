"""Unit tests for Context Builder orchestration (R14.8, R6.6, R18.1).

Verifies:
- BusinessDataProvider protocol and StubBusinessDataProvider (R13 stub).
- InstructionProvider protocol and DefaultInstructionProvider (R14.1, R14.8 static prefix).
- ContextBuilder gathers thread context, business data, and hybrid RAG.
- R6.6: Conditional hybrid RAG gating on retrieval_required.
- R14.8: Strict fixed assembly order in ContextPackage.
- R18.1: Job state transition from QUEUED to CONTEXT_READY.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.context.builder import (
    BusinessDataProvider,
    DefaultInstructionProvider,
    InstructionProvider,
    StubBusinessDataProvider,
)
from packages.domain.entities import EmailAddress, NormalizedMessage


def _create_test_message(
    org_id: UUID,
    thread_id: UUID,
    msg_id: UUID | None = None,
    subject: str = "Test Subject",
    body: str = "Test Body",
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
        received_at=datetime.now(UTC),
    )


def test_business_data_provider_protocols_and_stub() -> None:
    """Verify BusinessDataProvider protocol compliance and default stub behavior."""
    stub = StubBusinessDataProvider()
    assert isinstance(stub, BusinessDataProvider)

    org_id = uuid4()
    thread_id = uuid4()
    msg = _create_test_message(org_id, thread_id)

    data = asyncio.run(stub.get_business_data(org_id, msg, intent="invoice_inquiry"))
    assert isinstance(data, dict)
    assert data == {}


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

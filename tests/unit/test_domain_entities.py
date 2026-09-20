"""Unit tests for pure domain entities and fixed context package assembly order.

Requirements:
- R1.4: Provider-neutral domain objects.
- R5.2: Domain entity modeling.
- R14.8: Fixed assembly order for prompt-prefix caching.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.domain.entities import (
    AttachmentRef,
    Candidate,
    Checkpoint,
    Classification,
    ContextPackage,
    DraftRef,
    EmailAddress,
    Job,
    Mailbox,
    NormalizedMessage,
    OutboundReply,
    ProcessingEvent,
    RawMessage,
    RawThread,
    SentRef,
    Subscription,
    SyncResult,
    ThreadRef,
)


def test_email_address_formatting() -> None:
    """Verify string formatting of EmailAddress."""
    addr_with_name = EmailAddress(email="support@example.com", name="Support Team")
    assert str(addr_with_name) == "Support Team <support@example.com>"

    addr_without_name = EmailAddress(email="user@example.com")
    assert str(addr_without_name) == "user@example.com"


def test_attachment_ref_immutability() -> None:
    """Verify AttachmentRef is frozen and stores attributes."""
    att = AttachmentRef(
        filename="terms.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        object_key="attachments/org/msg/att/terms.pdf",
        checksum="sha256:abc",
    )
    assert att.filename == "terms.pdf"
    with pytest.raises(AttributeError):
        att.filename = "new.pdf"  # type: ignore[misc]


def test_normalized_message_creation() -> None:
    """Verify NormalizedMessage default fields and initialization."""
    msg_id = uuid4()
    thread_id = uuid4()
    org_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)

    msg = NormalizedMessage(
        message_id=msg_id,
        thread_id=thread_id,
        mailbox_id=mbx_id,
        organization_id=org_id,
        provider="gmail",
        provider_message_id="msg_123",
        sender=EmailAddress("client@example.com", "Client"),
        received_at=now,
        subject="Order Question",
        body_text="Hi, when will my order ship?",
        body_text_clean="Hi, when will my order ship?",
    )

    assert msg.message_id == msg_id
    assert msg.direction == "inbound"
    assert msg.attachments == []
    assert not msg.normalization_failed
    assert not msg.signature_stripped
    assert msg.flags == {"normalization_failed": False, "signature_stripped": False}

    contract = msg.to_contract_dict()
    assert contract["message_id"] == str(msg_id)
    assert contract["thread_id"] == str(thread_id)
    assert contract["provider"] == "gmail"
    assert contract["sender"] == {"name": "Client", "email": "client@example.com"}
    assert contract["body_text"] == "Hi, when will my order ship?"
    assert contract["body_text_clean"] == "Hi, when will my order ship?"
    assert contract["flags"] == {"normalization_failed": False, "signature_stripped": False}


def test_classification_defaults() -> None:
    """Verify Classification default fields and triage attributes."""
    c = Classification(category="technical_support", intent="reset_password")
    assert c.category == "technical_support"
    assert c.intent == "reset_password"
    assert c.priority == "normal"
    assert c.reply_required is True
    assert c.workflow_hint == "ai"
    assert c.retrieval_required is True
    assert c.confidence == 1.0


def test_candidate_search_scores() -> None:
    """Verify Candidate scoring and external id."""
    cand = Candidate(
        chunk_id="chunk-001",
        document_id="doc-100",
        content="Password reset instructions...",
        external_id="DOC-100-01",
        lexical_score=0.85,
        vector_score=0.92,
        fused_score=0.031,
    )
    assert cand.external_id == "DOC-100-01"
    assert cand.rerank_score is None


def test_context_package_fixed_assembly_order() -> None:
    """Verify ContextPackage strictly produces the 7 fixed assembly sections in order (R14.8)."""
    now = datetime.now(UTC)
    curr_msg = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="gmail",
        provider_message_id="curr-1",
        sender=EmailAddress("customer@example.com", "Bob"),
        received_at=now,
        body_text_clean="Please check order 8821.",
    )

    recent_msg = NormalizedMessage(
        message_id=uuid4(),
        thread_id=curr_msg.thread_id,
        mailbox_id=curr_msg.mailbox_id,
        organization_id=curr_msg.organization_id,
        provider="gmail",
        provider_message_id="prev-1",
        sender=EmailAddress("support@example.com", "Agent"),
        received_at=now,
        body_text_clean="We received your request.",
    )

    chunk = Candidate(
        chunk_id="chunk-9",
        document_id="doc-1",
        content="Shipping policy: Standard orders take 3-5 days.",
        external_id="SHIPPING-01",
    )

    pkg = ContextPackage(
        agent_instructions="You are an enterprise email assistant.",
        category_instructions="Category: Billing.",
        current_message=curr_msg,
        thread_summary="Customer inquiring about shipping schedule.",
        recent_messages=[recent_msg],
        retrieved_chunks=[chunk],
        business_data={"order_id": "8821", "status": "Shipped"},
    )

    sections = pkg.get_ordered_sections()
    section_names = [name for name, _ in sections]

    # Exactly match the 7 fixed sections in R14.8 order:
    expected_order = [
        "agent_instructions",
        "category_instructions",
        "thread_summary",
        "recent_thread_messages",
        "current_email",
        "retrieved_knowledge",
        "business_data",
    ]
    assert section_names == expected_order

    # Check content elements
    assert "enterprise email assistant" in sections[0][1]
    assert "Category: Billing" in sections[1][1]
    assert "shipping schedule" in sections[2][1]
    assert "We received your request" in sections[3][1]
    assert "Please check order 8821" in sections[4][1]
    assert "[CITATION: SHIPPING-01]" in sections[5][1]
    assert "[BUSINESS DATA]" in sections[6][1]
    assert "status: Shipped" in sections[6][1]


def test_job_and_processing_event_dataclasses() -> None:
    """Verify Job and ProcessingEvent creation and default attributes."""
    job = Job(organization_id="org_1", message_id="msg_1")
    assert job.state == "RECEIVED"
    assert job.attempt == 0
    assert job.max_attempts == 5

    event = ProcessingEvent(
        organization_id="org_1",
        job_id=job.id,
        state_from="RECEIVED",
        state_to="NORMALIZED",
        trace_id="tr-123",
    )
    assert event.state_from == "RECEIVED"
    assert event.state_to == "NORMALIZED"
    assert event.trace_id == "tr-123"


def test_mailbox_and_checkpoint_entities() -> None:
    """Verify Mailbox and Checkpoint domain entities."""
    mbx = Mailbox(
        id="mbx-1",
        organization_id="org-1",
        provider="gmail",
        address="user@company.com",
        display_name="User",
        credentials_ref="vault://tokens/gmail-1",
    )
    assert mbx.status == "active"
    assert mbx.credentials_ref == "vault://tokens/gmail-1"

    cp = Checkpoint(
        mailbox_id=mbx.id,
        history_id="12345",
        sync_state="idle",
    )
    assert cp.history_id == "12345"
    assert cp.pending_followup is False


def test_subscription_and_sync_result_entities() -> None:
    """Verify Subscription, RawMessage, and SyncResult domain objects."""
    now = datetime.now(UTC)
    sub = Subscription(
        mailbox_id="mbx-1",
        subscription_id="sub-abc",
        expires_at=now,
        provider="graph",
    )
    assert sub.subscription_id == "sub-abc"

    raw_msg = RawMessage(
        provider_message_id="p-msg-1",
        provider_thread_id="p-th-1",
        raw_payload=b"From: a@b.com\r\nSubject: Hi\r\n\r\nHello",
        internal_date=now,
    )
    assert raw_msg.provider_message_id == "p-msg-1"

    thread = RawThread(provider_thread_id="p-th-1", messages=[raw_msg])
    assert len(thread.messages) == 1

    result = SyncResult(
        messages=[raw_msg],
        new_checkpoint=Checkpoint(mailbox_id="mbx-1", history_id="67890"),
        requires_full_resync=False,
        has_more=False,
    )
    assert len(result.messages) == 1
    assert result.new_checkpoint.history_id == "67890"


def test_outbound_reply_and_references() -> None:
    """Verify OutboundReply, DraftRef, SentRef, and ThreadRef domain objects."""
    reply = OutboundReply(
        thread_id="th-1",
        mailbox_id="mbx-1",
        organization_id="org-1",
        to=[EmailAddress(email="dest@example.com")],
        body_text="Here is your requested update.",
        subject="Re: Update",
    )
    assert len(reply.to) == 1
    assert reply.body_text == "Here is your requested update."

    draft = DraftRef(provider_draft_id="d-100", provider_thread_id="th-1")
    assert draft.provider_draft_id == "d-100"

    sent = SentRef(provider_message_id="sent-100", provider_thread_id="th-1")
    assert sent.provider_message_id == "sent-100"

    tref = ThreadRef(provider_thread_id="th-1", message_count=3)
    assert tref.message_count == 3

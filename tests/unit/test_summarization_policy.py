"""Unit tests for conversation summarization policy and threshold triggers (R8.2, R8.3, R8.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.core.settings import SummarizationSettings
from packages.domain.entities import EmailAddress, NormalizedMessage, ThreadState
from packages.context.policy import SummarizationPolicy


def make_msg(idx: int, thread_id: UUID, org_id: UUID, body: str = "Test body") -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=thread_id,
        mailbox_id=uuid4(),
        organization_id=org_id,
        provider="gmail",
        provider_message_id=f"msg-{idx}",
        sender=EmailAddress(email="user@example.com"),
        received_at=datetime.now(UTC),
        body_text_clean=body,
    )


def test_summarization_policy_short_thread_no_summary() -> None:
    """Verify that short thread below thresholds does not trigger summarization (R8.2)."""
    settings = SummarizationSettings(min_messages_threshold=4, context_token_threshold=1500)
    policy = SummarizationPolicy(settings)
    thread_id, org_id = uuid4(), uuid4()

    messages = [make_msg(i, thread_id, org_id, "Short note") for i in range(2)]
    decision = policy.evaluate(messages=messages, current_state=None)

    assert not decision.should_summarize
    assert decision.reason == "below_threshold"


def test_summarization_policy_message_count_threshold_triggered() -> None:
    """Verify that exceeding min_messages_threshold triggers summarization (R8.3)."""
    settings = SummarizationSettings(min_messages_threshold=4, context_token_threshold=1500)
    policy = SummarizationPolicy(settings)
    thread_id, org_id = uuid4(), uuid4()

    messages = [make_msg(i, thread_id, org_id, "Short note") for i in range(5)]
    decision = policy.evaluate(messages=messages, current_state=None)

    assert decision.should_summarize
    assert decision.reason == "message_count_threshold_exceeded"


def test_summarization_policy_token_threshold_triggered() -> None:
    """Verify that exceeding token threshold triggers summarization (R8.3)."""
    settings = SummarizationSettings(min_messages_threshold=10, context_token_threshold=100)
    policy = SummarizationPolicy(settings)
    thread_id, org_id = uuid4(), uuid4()

    long_body = "This is a detailed paragraph with extensive explanations. " * 30
    messages = [make_msg(i, thread_id, org_id, long_body) for i in range(2)]
    decision = policy.evaluate(messages=messages, current_state=None)

    assert decision.should_summarize
    assert decision.reason == "token_threshold_exceeded"


def test_summarization_policy_already_summarized_skips_regeneration() -> None:
    """Verify that if already summarized through latest message, regeneration is skipped (R8.4)."""
    settings = SummarizationSettings(min_messages_threshold=3, context_token_threshold=1500)
    policy = SummarizationPolicy(settings)
    thread_id, org_id = uuid4(), uuid4()

    messages = [make_msg(i, thread_id, org_id, "Note") for i in range(5)]
    latest_id = messages[-1].message_id
    assert isinstance(latest_id, UUID)

    current_state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        summary="Existing summary",
        summarized_through_message_id=latest_id,
        version=1,
    )

    decision = policy.evaluate(messages=messages, current_state=current_state)
    assert not decision.should_summarize
    assert decision.reason == "already_summarized"

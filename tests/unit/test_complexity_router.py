"""Unit tests for EscalationReason, RoutingDecision, and action count heuristics.

References: R15.3, R15.4, R15.6, design.md §5.7.
"""

from dataclasses import FrozenInstanceError

import pytest

from packages.llm import EscalationReason, RoutingDecision, count_requested_actions
from packages.llm.protocol import ModelTier


def test_escalation_reason_values() -> None:
    """Verify all EscalationReason enum values adhere to specification (R15.3, R15.6)."""
    assert EscalationReason.NONE.value == "none"
    assert EscalationReason.LOW_CLASSIFICATION_CONFIDENCE.value == "low_classification_confidence"
    assert EscalationReason.COMPLEX_THREAD.value == "complex_thread"
    assert (
        EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE.value == "insufficient_retrieval_evidence"
    )
    assert EscalationReason.MULTIPLE_REQUESTED_ACTIONS.value == "multiple_requested_actions"
    assert EscalationReason.OVERSIZED_CONTEXT.value == "oversized_context"
    assert EscalationReason.SINGLE_TIER_FORCED.value == "single_tier_forced"
    assert EscalationReason("none") is EscalationReason.NONE

    # Verify enum length
    assert len(EscalationReason) == 7


def test_routing_decision_attributes() -> None:
    """Verify RoutingDecision default values and frozen immutability (R15.4)."""
    decision = RoutingDecision(tier=ModelTier.ROUTINE)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.model == ""
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE
    assert decision.details == {}

    # Verify immutability
    with pytest.raises(FrozenInstanceError):
        decision.tier = ModelTier.HIGH_CAPABILITY  # type: ignore[misc]

    # Verify custom attributes
    custom_decision = RoutingDecision(
        tier=ModelTier.HIGH_CAPABILITY,
        model="gpt-4o",
        is_escalated=True,
        escalation_reason=EscalationReason.MULTIPLE_REQUESTED_ACTIONS,
        details={"actions_count": 3},
    )
    assert custom_decision.tier == ModelTier.HIGH_CAPABILITY
    assert custom_decision.model == "gpt-4o"
    assert custom_decision.is_escalated is True
    assert custom_decision.escalation_reason == EscalationReason.MULTIPLE_REQUESTED_ACTIONS
    assert custom_decision.details == {"actions_count": 3}


def test_count_requested_actions_single_question() -> None:
    """Verify single question email counts as 1 requested action."""
    text = "Can you help me reset my password?"
    assert count_requested_actions(text) == 1


def test_count_requested_actions_multiple_questions() -> None:
    """Verify multiple questions in email count as >= 2 requested actions."""
    text = "Where is my invoice? Also, can you update my payment method?"
    assert count_requested_actions(text) == 2


def test_count_requested_actions_numbered_list() -> None:
    """Verify numbered list items are counted as distinct actions."""
    text = "Please: 1. Cancel order 2. Issue refund 3. Delete account"
    assert count_requested_actions(text) == 3


def test_count_requested_actions_bullet_list() -> None:
    """Verify bullet list items are counted as distinct actions."""
    text = "Please take care of: - Update address - Resend receipt"
    assert count_requested_actions(text) == 2


def test_count_requested_actions_transition_directives() -> None:
    """Verify transition phrases with action directives are counted."""
    text = "Please send the logs. Additionally, please call me tomorrow."
    assert count_requested_actions(text) == 2


def test_count_requested_actions_no_actions() -> None:
    """Verify polite closings and gratitude yield 0 requested actions."""
    text = "Thank you for the quick response. Have a great day!"
    assert count_requested_actions(text) == 0


def test_count_requested_actions_empty_or_whitespace() -> None:
    """Verify empty or whitespace strings yield 0 requested actions."""
    assert count_requested_actions("") == 0
    assert count_requested_actions("   \n\t  ") == 0


def test_count_requested_actions_multiline_formatting() -> None:
    """Verify multiline numbered and bullet lists are counted accurately."""
    numbered = "Please:\n1. Cancel order\n2. Issue refund\n3. Delete account"
    assert count_requested_actions(numbered) == 3

    bullets = "Please take care of:\n- Update address\n- Resend receipt"
    assert count_requested_actions(bullets) == 2

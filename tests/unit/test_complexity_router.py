"""Unit tests for EscalationReason, RoutingDecision, and ComplexityRouter.

References: R15.1-R15.6, design.md §5.7.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from packages.core.settings import ComplexityRouterSettings, LLMTiersSettings
from packages.domain.entities import (
    Candidate,
    Classification,
    ContextPackage,
    EmailAddress,
    NormalizedMessage,
)
from packages.llm import (
    ComplexityRouter,
    EscalationReason,
    RoutingDecision,
    count_requested_actions,
)
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


def test_count_requested_actions_polite_closing_with_directive() -> None:
    """Verify directives accompanied by gratitude are not dropped (review finding #1)."""
    assert count_requested_actions("Please send the invoice, thanks!") == 1
    assert count_requested_actions("Please reset my password, thank you.") == 1


def test_count_requested_actions_single_bullet_no_double_count() -> None:
    """Verify single bullet list items do not double-count (review finding #2)."""
    assert count_requested_actions("- Please update my address") == 1


def test_count_requested_actions_em_dash_in_prose() -> None:
    """Verify parenthetical dashes in prose are not treated as bullets (review finding #3)."""
    assert count_requested_actions("A cost-benefit analysis - not an audit - is needed.") == 0


def test_count_requested_actions_ticket_id_in_prose() -> None:
    """Verify ticket numbers with periods do not trigger numbered list false positive.

    References: review finding #3.
    """
    assert count_requested_actions("I am waiting on ticket 12345. Is there an update?") == 1


# --- Helpers for ComplexityRouter tests ---


def _make_sample_message(
    body: str = "Hello, please provide an update on my ticket.",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-001",
        sender=EmailAddress(email="customer@example.com", name="Customer"),
        subject="Ticket Update",
        subject_normalized="ticket update",
        body_text=body,
        body_text_clean=body,
        received_at=datetime.now(UTC),
    )


def _make_sample_context(
    body: str = "Hello, please provide an update on my ticket.",
    recent_messages: list[NormalizedMessage] | None = None,
    retrieved_chunks: list[Candidate] | None = None,
    thread_summary: str | None = None,
    agent_instructions: str = "You are an enterprise AI assistant.",
    category_instructions: str = "Provide general support.",
) -> ContextPackage:
    return ContextPackage(
        agent_instructions=agent_instructions,
        category_instructions=category_instructions,
        current_message=_make_sample_message(body=body),
        thread_summary=thread_summary,
        recent_messages=recent_messages or [],
        retrieved_chunks=retrieved_chunks or [],
    )


# --- ComplexityRouter test cases (R15.1-R15.6) ---


def test_default_routing_to_routine_tier() -> None:
    """Verify normal simple email defaults to routine tier with is_escalated=False (R15.2)."""
    router = ComplexityRouter()
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.90,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE


def test_escalation_trigger_low_confidence() -> None:
    """Verify classification confidence below threshold escalates (R15.3)."""
    router = ComplexityRouter()
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.60,  # default threshold: 0.75
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.LOW_CLASSIFICATION_CONFIDENCE
    assert decision.details["trigger"] == "low_classification_confidence"
    assert decision.details["confidence"] == 0.60


def test_escalation_trigger_complex_thread_messages() -> None:
    """Verify recent messages count >= threshold triggers escalation (R15.3)."""
    router = ComplexityRouter()
    recent = [_make_sample_message(f"Previous message {i}") for i in range(5)]
    context = _make_sample_context(recent_messages=recent)
    classification = Classification(
        category="support",
        confidence=0.90,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.COMPLEX_THREAD
    assert decision.details["message_count"] == 5


def test_escalation_trigger_complex_thread_tokens() -> None:
    """Verify thread token count >= threshold triggers escalation (R15.3)."""
    router = ComplexityRouter()
    # 2200 words (~2900 tokens > 2000 default threshold)
    long_thread_text = "word " * 2200
    long_msg = _make_sample_message(long_thread_text)
    context = _make_sample_context(recent_messages=[long_msg])
    classification = Classification(
        category="support",
        confidence=0.90,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.COMPLEX_THREAD
    assert decision.details["thread_tokens"] >= 2000


def test_escalation_trigger_insufficient_retrieval_zero_chunks() -> None:
    """Verify retrieval_required=True with 0 retrieved chunks escalates (R15.3)."""
    router = ComplexityRouter()
    context = _make_sample_context(retrieved_chunks=[])
    classification = Classification(
        category="billing",
        confidence=0.90,
        retrieval_required=True,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE


def test_escalation_trigger_insufficient_retrieval_low_score() -> None:
    """Verify qualifying chunks count below min_retrieved_chunks escalates (R15.3)."""
    router = ComplexityRouter()
    # 1 chunk with rerank_score 0.30 (< min_relevance_score 0.50, min_retrieved_chunks is 2)
    low_chunk = Candidate(
        chunk_id="c1",
        document_id="d1",
        content="Irrelevant info",
        rerank_score=0.30,
    )
    context = _make_sample_context(retrieved_chunks=[low_chunk])
    classification = Classification(
        category="billing",
        confidence=0.90,
        retrieval_required=True,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE


def test_retrieval_sufficient_does_not_escalate() -> None:
    """Verify sufficient retrieved chunks with high scores avoid escalation (R15.3)."""
    router = ComplexityRouter()
    chunk1 = Candidate(chunk_id="c1", document_id="d1", content="KB 1", rerank_score=0.85)
    chunk2 = Candidate(chunk_id="c2", document_id="d2", content="KB 2", rerank_score=0.90)
    context = _make_sample_context(retrieved_chunks=[chunk1, chunk2])
    classification = Classification(
        category="billing",
        confidence=0.90,
        retrieval_required=True,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE


def test_escalation_trigger_multiple_requested_actions() -> None:
    """Verify multiple requested actions (>= 2) triggers escalation (R15.3)."""
    router = ComplexityRouter()
    body = "Where is my invoice? Also, can you update my payment method?"
    context = _make_sample_context(body=body)
    classification = Classification(
        category="billing",
        confidence=0.90,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.MULTIPLE_REQUESTED_ACTIONS
    assert decision.details["actions_count"] == 2


def test_escalation_trigger_oversized_context() -> None:
    """Verify total context tokens exceeding threshold triggers escalation (R15.3)."""
    router = ComplexityRouter()
    # Generate long instructions that push total context tokens > 3500
    long_instructions = "Context instructions line with information. " * 700  # ~4200 tokens > 3500
    context = _make_sample_context(agent_instructions=long_instructions)
    classification = Classification(
        category="support",
        confidence=0.90,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.OVERSIZED_CONTEXT
    assert decision.details["total_context_tokens"] > 3500


def test_max_escalation_cap_prevents_escalation() -> None:
    """Verify escalation is suppressed when escalations_performed >= max cap (R15.5)."""
    router = ComplexityRouter()
    context = _make_sample_context()
    # Would trigger low confidence escalation if not capped
    classification = Classification(
        category="support",
        confidence=0.40,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification, escalations_performed=1)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE
    assert decision.details["escalation_suppressed"] is True
    assert decision.details["escalations_performed"] == 1


def test_force_single_tier_override() -> None:
    """Verify force_single_tier bypasses cascade and returns single_tier_override (R15.6)."""
    settings = ComplexityRouterSettings(
        force_single_tier=True,
        single_tier_override="high_capability",
    )
    router = ComplexityRouter(settings=settings)
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.95,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.SINGLE_TIER_FORCED
    assert decision.details.get("forced") is True


def test_disabled_router() -> None:
    """Verify disabled router routes to routine tier without escalating (R15.1)."""
    settings = ComplexityRouterSettings(enabled=False)
    router = ComplexityRouter(settings=settings)
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.30,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE
    assert decision.details.get("router_enabled") is False


def test_escalation_trigger_order_priority() -> None:
    """Verify triggers are evaluated in deterministic priority order (R15.3)."""
    router = ComplexityRouter()
    # Both low confidence (< 0.75) and multiple actions (>= 2) are present
    body = "Where is my refund? And can you cancel my account?"
    context = _make_sample_context(body=body)
    classification = Classification(
        category="billing",
        confidence=0.50,  # Trigger 1
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    # Trigger 1 takes precedence over Trigger 4
    assert decision.escalation_reason == EscalationReason.LOW_CLASSIFICATION_CONFIDENCE


def test_model_resolution_from_llm_tiers_settings() -> None:
    """Verify concrete model resolution from LLMTiersSettings."""
    tiers_settings = LLMTiersSettings(
        fast_model="gpt-4o-mini",
        strong_model="gpt-4o",
    )
    router = ComplexityRouter(tiers_settings=tiers_settings)
    context = _make_sample_context()

    # Routine decision resolves fast_model
    decision_routine = router.route(
        context,
        classification=Classification(category="faq", confidence=0.90, retrieval_required=False),
    )
    assert decision_routine.tier == ModelTier.ROUTINE
    assert decision_routine.model == "gpt-4o-mini"

    # Escalated decision resolves strong_model
    decision_escalated = router.route(
        context,
        classification=Classification(category="faq", confidence=0.50, retrieval_required=False),
    )
    assert decision_escalated.tier == ModelTier.HIGH_CAPABILITY
    assert decision_escalated.model == "gpt-4o"


def test_metrics_recording_on_escalation() -> None:
    """Verify Prometheus counter is incremented on escalation when metrics provided (R15.4)."""
    mock_metrics = MagicMock()
    router = ComplexityRouter(metrics=mock_metrics)
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.50,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.is_escalated is True
    mock_metrics.model_escalations_total.labels.assert_called_once_with(
        reason=EscalationReason.LOW_CLASSIFICATION_CONFIDENCE.value,
        tier=ModelTier.HIGH_CAPABILITY.value,
    )
    mock_metrics.model_escalations_total.labels.return_value.inc.assert_called_once()


def test_insufficient_retrieval_ignored_when_retrieval_not_required() -> None:
    """Verify 0 retrieved chunks does not trigger escalation if retrieval_required is False."""
    router = ComplexityRouter()
    context = _make_sample_context(retrieved_chunks=[])
    classification = Classification(
        category="support",
        confidence=0.90,
        retrieval_required=False,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE


def test_candidate_score_fallback_resolution() -> None:
    """Verify fallback score resolution across fused, vector, lexical, and metadata."""
    router = ComplexityRouter()
    # Chunks without rerank_score: 1 with vector_score, 1 with metadata["score"]
    chunk1 = Candidate(
        chunk_id="c1",
        document_id="d1",
        content="Chunk 1",
        vector_score=0.85,
    )
    chunk2 = Candidate(
        chunk_id="c2",
        document_id="d2",
        content="Chunk 2",
        metadata={"score": 0.75},
    )
    context = _make_sample_context(retrieved_chunks=[chunk1, chunk2])
    classification = Classification(
        category="billing",
        confidence=0.90,
        retrieval_required=True,
    )

    decision = router.route(context, classification=classification)

    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE

"""Integration tests for ComplexityRouter, SinglePassGenerator, BudgetedLLMProvider, and Metrics.

References: R15.1-R15.6, R14.3, R14.4, R14.9, R21.4, design.md §5.7.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
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
    AgentProfileRegistry,
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallKind,
    ComplexityRouter,
    EscalationReason,
    FakeLLMProvider,
    GenerationResult,
    ModelTier,
    SinglePassGenerator,
)
from packages.observability.metrics import (
    create_pipeline_metrics,
    generate_metrics_payload,
)


def _make_sample_message(
    body: str = "How do I reset my password?",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-001",
        sender=EmailAddress(email="customer@example.com", name="Alice Customer"),
        subject="Password Reset Help",
        subject_normalized="password reset help",
        body_text=body,
        body_text_clean=body,
        received_at=datetime.now(UTC),
    )


def _make_sample_context(
    body: str = "How do I reset my password?",
    recent_messages: list[NormalizedMessage] | None = None,
    retrieved_chunks: list[Candidate] | None = None,
    thread_summary: str | None = None,
    agent_instructions: str = "You are an enterprise AI assistant.",
    category_instructions: str = "Address technical support questions.",
) -> ContextPackage:
    if retrieved_chunks is not None:
        chunks = retrieved_chunks
    else:
        chunks = [
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-kb-01",
                content="To reset password, go to settings and click Reset Password.",
                external_id="KB-PWD-01",
                rerank_score=0.88,
            ),
            Candidate(
                chunk_id="chunk-2",
                document_id="doc-kb-02",
                content="Alternatively, contact an administrator to assist with password resets.",
                external_id="KB-PWD-02",
                rerank_score=0.85,
            ),
        ]
    return ContextPackage(
        agent_instructions=agent_instructions,
        category_instructions=category_instructions,
        current_message=_make_sample_message(body=body),
        thread_summary=thread_summary,
        recent_messages=recent_messages or [],
        retrieved_chunks=chunks,
        business_data={"customer_tier": "gold"},
    )


@pytest.fixture
def profile_registry() -> AgentProfileRegistry:
    return AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")


@pytest.fixture
def canned_reply() -> dict[str, Any]:
    return {
        "action": "reply",
        "draft": "Hello Alice, you can reset your password from the account settings page.",
        "confidence": 0.95,
        "knowledge_chunks": ["chunk-1"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }


@pytest.fixture
def tiers_settings() -> LLMTiersSettings:
    return LLMTiersSettings(
        routine_model="gpt-4o-mini",
        high_capability_model="gpt-4o",
    )


@pytest.mark.asyncio
async def test_routine_flow_integration(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify routine email routes to ROUTINE tier and generates draft (R15.1-R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.95,
        retrieval_required=True,
    )
    tracker = CallBudgetTracker(job_id="job-routine-001")

    # 1. Routing decision
    decision = router.route(context, classification=classification, escalations_performed=0)
    assert decision.tier == ModelTier.ROUTINE
    assert decision.model == "gpt-4o-mini"
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE

    # 2. Generation execution
    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    # 3. Assertions on generation result and call budget
    assert isinstance(result, GenerationResult)
    assert result.tier == ModelTier.ROUTINE
    assert result.escalation_reason == "none"
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)

    # Verify provider recorded call with routine tier
    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.ROUTINE

    # Verify no escalation metric was recorded
    payload, _ = generate_metrics_payload(metrics.registry)
    payload_str = payload.decode("utf-8")
    assert "model_escalations_total{" not in payload_str


@pytest.mark.asyncio
async def test_escalated_flow_low_confidence(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify low confidence escalates to HIGH_CAPABILITY and increments metric (R15.3-R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(confidence_threshold=0.75),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.60,  # below 0.75 threshold
        retrieval_required=True,
    )
    tracker = CallBudgetTracker(job_id="job-escalated-confidence")

    # 1. Routing decision
    decision = router.route(context, classification=classification, escalations_performed=0)
    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.model == "gpt-4o"
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.LOW_CLASSIFICATION_CONFIDENCE

    # Verify metric incremented
    metric_val = metrics.model_escalations_total.labels(
        reason="low_classification_confidence",
        tier="high_capability",
    )._value.get()
    assert metric_val == 1.0

    # 2. Generation execution with escalated tier and reason
    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    # 3. Assertions
    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "low_classification_confidence"
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)

    # Exactly 1 generation call in provider, using high_capability tier
    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.HIGH_CAPABILITY


@pytest.mark.asyncio
async def test_escalated_flow_multiple_actions(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify multiple actions email escalates and preserves 1-call budget (R15.3-R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(multiple_actions_threshold=2),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    multi_action_body = "Please: 1. Cancel order 2. Issue refund 3. Delete account"
    context = _make_sample_context(body=multi_action_body)
    classification = Classification(
        category="support",
        confidence=0.90,
        retrieval_required=False,
    )
    tracker = CallBudgetTracker(job_id="job-escalated-multi-actions")

    # 1. Routing decision
    decision = router.route(context, classification=classification)
    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.MULTIPLE_REQUESTED_ACTIONS

    # Metric incremented
    metric_val = metrics.model_escalations_total.labels(
        reason="multiple_requested_actions",
        tier="high_capability",
    )._value.get()
    assert metric_val == 1.0

    # 2. Generation execution
    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    # 3. Assertions
    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "multiple_requested_actions"
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)
    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.HIGH_CAPABILITY


@pytest.mark.asyncio
async def test_max_escalation_cap_suppresses_escalation(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify prior escalation suppresses further escalation, keeping routine tier (R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(max_escalations_per_job=1),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    # Low confidence triggers escalation normally, but escalations_performed=1 suppresses it
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.50,
        retrieval_required=True,
    )
    tracker = CallBudgetTracker(job_id="job-escalation-capped")

    decision = router.route(context, classification=classification, escalations_performed=1)
    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is False
    assert decision.escalation_reason == EscalationReason.NONE
    assert decision.details["escalation_suppressed"] is True
    assert decision.details["escalations_performed"] == 1

    # Generation invoked once
    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.ROUTINE
    assert result.escalation_reason == "none"
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.ROUTINE


@pytest.mark.asyncio
async def test_ablation_switch_force_single_tier(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify force_single_tier switch forces specified tier even when triggers fire (R15.6)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(
            force_single_tier=True,
            single_tier_override="routine",
        ),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    # Low confidence would trigger escalation, but single_tier_override is routine
    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.40,
        retrieval_required=False,
    )
    tracker = CallBudgetTracker(job_id="job-ablation-routine")

    decision = router.route(context, classification=classification)
    assert decision.tier == ModelTier.ROUTINE
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.SINGLE_TIER_FORCED
    assert decision.details["forced"] is True

    # Metric recorded for single_tier_forced
    metric_val = metrics.model_escalations_total.labels(
        reason="single_tier_forced",
        tier="routine",
    )._value.get()
    assert metric_val == 1.0

    # Generation executed once with forced routine tier
    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.ROUTINE
    assert result.escalation_reason == "single_tier_forced"
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)


@pytest.mark.asyncio
async def test_ablation_switch_force_single_tier_high_capability(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify force_single_tier switch can force high_capability tier on routine input (R15.6)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(
            force_single_tier=True,
            single_tier_override="high_capability",
        ),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    context = _make_sample_context()
    classification = Classification(
        category="support",
        confidence=0.99,
        retrieval_required=False,
    )
    tracker = CallBudgetTracker(job_id="job-ablation-high-capability")

    decision = router.route(context, classification=classification)
    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.is_escalated is True
    assert decision.escalation_reason == EscalationReason.SINGLE_TIER_FORCED

    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "single_tier_forced"
    assert tracker.count(CallKind.GENERATE) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.HIGH_CAPABILITY


@pytest.mark.asyncio
async def test_escalated_flow_complex_thread(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify complex thread trigger escalates and succeeds with single call (R15.3, R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(thread_messages_threshold=5),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    recent = [_make_sample_message(f"Message step {i}") for i in range(5)]
    context = _make_sample_context(recent_messages=recent)
    classification = Classification(category="support", confidence=0.90, retrieval_required=False)
    tracker = CallBudgetTracker(job_id="job-escalated-complex-thread")

    decision = router.route(context, classification=classification)
    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.escalation_reason == EscalationReason.COMPLEX_THREAD

    metric_val = metrics.model_escalations_total.labels(
        reason="complex_thread",
        tier="high_capability",
    )._value.get()
    assert metric_val == 1.0

    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "complex_thread"
    assert tracker.count(CallKind.GENERATE) == 1


@pytest.mark.asyncio
async def test_escalated_flow_insufficient_retrieval_evidence(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify insufficient retrieval evidence escalates tier (R15.3, R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    # Retrieval required but 0 chunks returned
    context = _make_sample_context(retrieved_chunks=[])
    classification = Classification(category="support", confidence=0.90, retrieval_required=True)
    tracker = CallBudgetTracker(job_id="job-escalated-retrieval")

    decision = router.route(context, classification=classification)
    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.escalation_reason == EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE

    metric_val = metrics.model_escalations_total.labels(
        reason="insufficient_retrieval_evidence",
        tier="high_capability",
    )._value.get()
    assert metric_val == 1.0

    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "insufficient_retrieval_evidence"
    assert tracker.count(CallKind.GENERATE) == 1


@pytest.mark.asyncio
async def test_escalated_flow_oversized_context(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify oversized context triggers escalation (R15.3, R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(context_tokens_threshold=3500),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    long_instructions = "Context instructions line with information. " * 700
    context = _make_sample_context(agent_instructions=long_instructions)
    classification = Classification(category="support", confidence=0.90, retrieval_required=False)
    tracker = CallBudgetTracker(job_id="job-escalated-oversized")

    decision = router.route(context, classification=classification)
    assert decision.tier == ModelTier.HIGH_CAPABILITY
    assert decision.escalation_reason == EscalationReason.OVERSIZED_CONTEXT

    metric_val = metrics.model_escalations_total.labels(
        reason="oversized_context",
        tier="high_capability",
    )._value.get()
    assert metric_val == 1.0

    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "oversized_context"
    assert tracker.count(CallKind.GENERATE) == 1


@pytest.mark.asyncio
async def test_end_to_end_with_budgeted_llm_provider_wrapper(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
    tiers_settings: LLMTiersSettings,
) -> None:
    """Verify BudgetedLLMProvider wrapper preserves budget tracker (R14.9, R15.5)."""
    metrics = create_pipeline_metrics()
    router = ComplexityRouter(
        settings=ComplexityRouterSettings(),
        tiers_settings=tiers_settings,
        metrics=metrics,
    )
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    tracker = CallBudgetTracker(job_id="job-budgeted-wrapper", metrics=metrics)
    budgeted_provider = BudgetedLLMProvider(fake_llm, tracker=tracker, metrics=metrics)

    generator = SinglePassGenerator(
        llm_provider=budgeted_provider,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    context = _make_sample_context()
    classification = Classification(category="support", confidence=0.95, retrieval_required=True)

    decision = router.route(context, classification=classification)
    result = await generator.generate_draft(
        context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=decision.tier,
        escalation_reason=decision.escalation_reason.value,
    )

    assert result.tier == ModelTier.ROUTINE
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)

    # Attempting a second generation call with the same tracker must fail with
    # CallBudgetExceededError (R15.5, R14.9)
    with pytest.raises(CallBudgetExceededError):
        await generator.generate_draft(
            context,
            category="support",
            budget_tracker=tracker,
            escalated_tier=decision.tier,
            escalation_reason=decision.escalation_reason.value,
        )

    # Provider still only saw 1 execution
    assert len(fake_llm.recorded_calls) == 1


def test_llm_tiers_settings_routine_and_high_capability_aliases() -> None:
    """Verify LLMTiersSettings binds routine_model and high_capability_model aliases (R15.1)."""
    # 1. Instantiation via aliases
    tiers = LLMTiersSettings(
        routine_model="custom-routine-mini",
        high_capability_model="custom-high-capability-v1",
    )
    assert tiers.fast_model == "custom-routine-mini"
    assert tiers.routine_model == "custom-routine-mini"
    assert tiers.strong_model == "custom-high-capability-v1"
    assert tiers.high_capability_model == "custom-high-capability-v1"

    # 2. Router model resolution from aliases
    router = ComplexityRouter(tiers_settings=tiers)
    assert router._resolve_model(ModelTier.ROUTINE) == "custom-routine-mini"
    assert router._resolve_model(ModelTier.HIGH_CAPABILITY) == "custom-high-capability-v1"

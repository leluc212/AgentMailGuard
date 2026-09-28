"""Unit tests for SinglePassGenerator draft synthesis (R14.3, R14.4, R14.6, R15.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    NormalizedMessage,
)
from packages.llm import (
    AgentProfile,
    AgentProfileRegistry,
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallKind,
    ContextPolicy,
    FakeLLMProvider,
    GenerationResult,
    LLMTimeoutError,
    ModelTier,
    SinglePassGenerator,
)
from packages.observability.metrics import create_pipeline_metrics


def _create_sample_context() -> ContextPackage:
    msg = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-001",
        sender=EmailAddress(email="customer@example.com", name="Alice Customer"),
        subject="How do I reset my password?",
        subject_normalized="How do I reset my password?",
        body_text="I forgot my password, how can I reset it?",
        body_text_clean="I forgot my password, how can I reset it?",
        received_at=datetime.now(UTC),
    )
    chunk = Candidate(
        chunk_id="chunk-1",
        document_id="doc-kb-01",
        content="To reset password, go to settings and click Reset Password.",
        external_id="KB-PWD-01",
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=msg,
        retrieved_chunks=[chunk],
    )


@pytest.fixture
def sample_context() -> ContextPackage:
    return _create_sample_context()


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


@pytest.mark.asyncio
async def test_single_pass_generation_normal_flow(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify standard single-pass generation makes 1 LLM call (R14.3, R14.4, R14.6)."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(sample_context, category="support")

    assert isinstance(result, GenerationResult)
    assert len(fake_llm.recorded_calls) == 1
    assert result.budget_tracker.count(CallKind.GENERATE) == 1
    assert result.budget_tracker.total_calls == 1
    assert result.prompt_version == "support.v1"
    assert result.tier == ModelTier.ROUTINE
    assert result.content["action"] == "reply"
    assert result.content["draft"] == canned_reply["draft"]
    assert result.content["confidence"] == 0.95
    assert result.model == "fake-fast-model"
    assert result.escalation_reason is None
    assert result.input_tokens > 0
    assert result.output_tokens > 0


@pytest.mark.asyncio
async def test_tier_escalation_replaces_generation_call(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify tier escalation replaces the generation call rather than adding one (R15.5)."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    tracker = CallBudgetTracker(job_id="job-escalated")
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(
        sample_context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=ModelTier.HIGH_CAPABILITY,
        escalation_reason="low_confidence",
    )

    # Exactly 1 call was recorded in fake provider
    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.HIGH_CAPABILITY

    # Result reflects escalated tier and reason
    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "low_confidence"

    # Call tracker has exactly 1 generation call, proving escalation replaced the call
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    assert result.budget_tracker is tracker


@pytest.mark.asyncio
async def test_tier_escalation_with_string_tier(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify tier escalation works when passing tier as a string."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(
        sample_context,
        category="support",
        escalated_tier="high_capability",
        escalation_reason="manual_override",
    )

    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "manual_override"


@pytest.mark.asyncio
async def test_second_generation_call_prevention(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify pre-existing generation call raises CallBudgetExceededError (R14.9)."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    tracker = CallBudgetTracker(job_id="job-second-gen")
    tracker.record_call(CallKind.GENERATE, model="fake-model", tier="routine")
    assert tracker.count(CallKind.GENERATE) == 1

    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    with pytest.raises(CallBudgetExceededError):
        await generator.generate_draft(
            sample_context,
            category="support",
            budget_tracker=tracker,
        )

    # Fake LLM was never called because budget check stopped it before execution
    assert len(fake_llm.recorded_calls) == 0


@pytest.mark.asyncio
async def test_metric_emission(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify Prometheus metrics are emitted after successful generation."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(sample_context, category="support")

    # llm_calls_total counter incremented for generate
    assert metrics.llm_calls_total.labels(kind="generate", model=result.model)._value.get() == 1.0

    # Token counters incremented
    assert metrics.input_tokens_total.labels(model=result.model, tier="routine")._value.get() > 0.0
    assert metrics.output_tokens_total.labels(model=result.model, tier="routine")._value.get() > 0.0

    # generation_latency_ms recorded
    latency_samples = metrics.generation_latency_ms.labels(
        model=result.model, tier="routine"
    )._sum.get()
    assert latency_samples >= 0.0

    # llm_calls_per_job histogram recorded generate call
    generate_sample = metrics.llm_calls_per_job.labels(kind="generate")._sum.get()
    assert generate_sample >= 1.0


@pytest.mark.asyncio
async def test_single_pass_generation_custom_profile(
    sample_context: ContextPackage,
    canned_reply: dict[str, Any],
) -> None:
    """Verify SinglePassGenerator works with an explicitly passed AgentProfile."""
    custom_profile = AgentProfile(
        profile="custom_support",
        knowledge_domain="support",
        response_style="concise",
        model_tier=ModelTier.ROUTINE,
        context_policy=ContextPolicy.THREAD_PLUS_RAG,
        prompt_template="prompts/support.v1.j2",
        output_schema="schemas/reply.v1.json",
        prompt_version="v1",
        categories=["support"],
    )
    registry = AgentProfileRegistry(profiles=[custom_profile])
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=registry,
    )

    result = await generator.generate_draft(sample_context, profile=custom_profile)

    assert result.profile.profile == "custom_support"
    assert result.prompt_version == "v1"
    assert result.content["action"] == "reply"


@pytest.mark.asyncio
async def test_single_pass_generation_with_pre_existing_budgeted_provider(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify SinglePassGenerator works when initialized with an already BudgetedLLMProvider."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    tracker = CallBudgetTracker(job_id="job-existing-budgeted")
    budgeted_provider = BudgetedLLMProvider(fake_llm, tracker=tracker)

    generator = SinglePassGenerator(
        llm_provider=budgeted_provider,
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(sample_context, category="support")

    assert result.budget_tracker is tracker
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    assert len(fake_llm.recorded_calls) == 1


@pytest.mark.asyncio
async def test_single_pass_generation_temperature_and_max_tokens_forwarding(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify temperature and max_tokens parameters are passed down to provider."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    await generator.generate_draft(
        sample_context,
        category="support",
        temperature=0.7,
        max_tokens=512,
    )

    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["temperature"] == 0.7
    assert fake_llm.recorded_calls[0]["max_tokens"] == 512


@pytest.mark.asyncio
async def test_single_pass_generation_fallback_profile(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify unknown category falls back to default profile."""
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(sample_context, category="unmapped_mystery_category")

    assert result.profile.profile == "general_inquiry"
    assert result.prompt_version == "general.v1"


@pytest.mark.asyncio
async def test_single_pass_generation_error_propagation(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
) -> None:
    """Verify provider exceptions bubble up appropriately."""
    fake_llm = FakeLLMProvider(error_to_raise=LLMTimeoutError("Request timed out"))
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    with pytest.raises(LLMTimeoutError, match="Request timed out"):
        await generator.generate_draft(sample_context, category="support")

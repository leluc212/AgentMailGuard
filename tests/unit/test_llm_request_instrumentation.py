"""Every model request records context size, tokens and cost exactly once (R11.7, R21.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage
from packages.llm import (
    AgentProfileRegistry,
    FakeLLMProvider,
    InstrumentedLLMProvider,
    SinglePassGenerator,
)
from packages.llm.budget import CallKind
from packages.llm.protocol import ChatMessage, ModelTier
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics

PRICES = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
REPLY = {
    "action": "reply",
    "draft": "Hello Alice, open Settings and choose Reset Password.",
    "confidence": 0.95,
    "knowledge_chunks": ["DOC-125-08"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


def _context() -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="prov-1",
        sender=EmailAddress(email="alice@example.com", name="Alice"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=[
            Candidate(
                chunk_id="c1",
                document_id="d1",
                content="Reset via Settings.",
                external_id="DOC-125-08",
            )
        ],
    )


def _count(m: PipelineMetrics, kind: str, tier: str) -> float:
    return (
        m.registry.get_sample_value("llm_context_tokens_count", {"kind": kind, "tier": tier}) or 0
    )


def _tokens(m: PipelineMetrics, name: str, model: str, tier: str) -> float:
    return m.registry.get_sample_value(name, {"model": model, "tier": tier}) or 0


def _generator(provider: FakeLLMProvider, m: PipelineMetrics) -> SinglePassGenerator:
    return SinglePassGenerator(
        llm_provider=provider,
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        metrics=m,
        price_table=PRICES,
    )


async def test_generation_tokens_are_counted_once_per_request() -> None:
    """Review Focus 2: the generator no longer counts tokens itself."""
    m = create_pipeline_metrics()
    result = await _generator(FakeLLMProvider(default_response=REPLY), m).generate_draft(
        _context(), category="technical_support"
    )

    tier = str(result.tier)
    assert _count(m, "generate", tier) == 1
    assert _tokens(m, "input_tokens_total", result.model, tier) == result.input_tokens
    assert _tokens(m, "output_tokens_total", result.model, tier) == result.output_tokens
    assert m.registry.get_sample_value(
        "estimated_ai_cost_total", {"model": result.model, "tier": tier}
    ) == pytest.approx((result.input_tokens * 0.15 + result.output_tokens * 0.60) / 1e6, abs=1e-6)


async def test_repair_request_is_recorded_under_its_own_kind() -> None:
    m = create_pipeline_metrics()
    provider = FakeLLMProvider(canned_responses=[{"action": "reply"}, dict(REPLY)])
    result = await _generator(provider, m).generate_draft(_context(), category="technical_support")

    tier = str(result.tier)
    assert _count(m, "generate", tier) == 1
    assert _count(m, "repair", tier) == 1
    assert _tokens(m, "input_tokens_total", result.model, tier) == result.input_tokens


async def test_failed_call_records_context_but_no_tokens() -> None:
    """Review Focus 1: the request is recorded and the provider's error reaches the caller."""
    m = create_pipeline_metrics()
    provider = FakeLLMProvider(error_to_raise=TimeoutError("upstream timeout"))

    with pytest.raises(TimeoutError, match="upstream timeout"):
        await _generator(provider, m).generate_draft(_context(), category="technical_support")

    total = sum(
        s.value
        for metric in m.llm_context_tokens.collect()
        for s in metric.samples
        if s.name == "llm_context_tokens_count" and s.labels["kind"] == "generate"
    )
    assert total == 1
    assert not [
        s for metric in m.input_tokens_total.collect() for s in metric.samples if s.value > 0
    ]


async def test_instrumented_provider_records_fixed_kind_requests() -> None:
    m = create_pipeline_metrics()
    inner = FakeLLMProvider()
    provider = InstrumentedLLMProvider(inner, kind=CallKind.TRIAGE, metrics=m, price_table=PRICES)

    result = await provider.generate(
        messages=[ChatMessage(role="user", content="classify me")], tier=ModelTier.FAST
    )

    assert provider.provider is inner
    assert _count(m, "triage", "fast") == 1
    assert _tokens(m, "input_tokens_total", result.model, "fast") == result.input_tokens


async def test_instrumented_provider_forwards_aclose() -> None:
    closed: list[bool] = []

    class _Closable(FakeLLMProvider):
        async def aclose(self) -> None:
            closed.append(True)

    await InstrumentedLLMProvider(_Closable(), kind=CallKind.TRIAGE).aclose()
    assert closed == [True]


async def test_instrumented_provider_propagates_errors_unchanged() -> None:
    boom = RuntimeError("provider down")
    provider = InstrumentedLLMProvider(
        FakeLLMProvider(error_to_raise=boom), kind=CallKind.SUMMARIZE, metrics=object()
    )
    with pytest.raises(RuntimeError) as excinfo:
        await provider.generate(messages=[ChatMessage(role="user", content="x")])
    assert excinfo.value is boom


async def test_budgeted_and_instrumented_providers_share_one_recording_call_site() -> None:
    """A future change to the recording sequence must land in one place, not two.

    BudgetedLLMProvider.generate and InstrumentedLLMProvider.generate must both delegate to
    the same `instrumented_call` helper rather than each carrying their own copy of the
    context-token-count + record_inference sequence, which could otherwise drift apart.
    Reads via `vars()` rather than attribute access, since `instrumented_call` is imported
    (not defined) in both `budget` and `instrumented`, and is not meant to be re-exported.
    """
    from packages.llm import budget as budget_module
    from packages.llm import inference_metrics
    from packages.llm import instrumented as instrumented_module

    assert vars(budget_module)["instrumented_call"] is inference_metrics.instrumented_call
    assert vars(instrumented_module)["instrumented_call"] is inference_metrics.instrumented_call

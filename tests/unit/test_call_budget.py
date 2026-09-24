"""Unit tests for CallBudgetTracker, BudgetedLLMProvider, and invariants (R14.9, §5.7)."""

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import CollectorRegistry

from packages.llm import (
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallBudgetViolationError,
    CallKind,
    CallRecord,
    ChatMessage,
    FakeLLMProvider,
    LLMError,
    ModelTier,
)
from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload


def test_call_kind_enum_values() -> None:
    """Verify CallKind members and string values."""
    assert CallKind.TRIAGE.value == "triage"
    assert CallKind.SUMMARIZE.value == "summarize"
    assert CallKind.GENERATE.value == "generate"
    assert CallKind.REPAIR.value == "repair"


def test_call_record_attributes_and_immutability() -> None:
    """Verify CallRecord fields, defaults, and frozen behavior."""
    record = CallRecord(
        kind=CallKind.GENERATE,
        model="gpt-4o",
        tier="strong",
        tokens_in=100,
        tokens_out=50,
    )
    assert record.kind == CallKind.GENERATE
    assert record.model == "gpt-4o"
    assert record.tier == "strong"
    assert record.tokens_in == 100
    assert record.tokens_out == 50
    assert record.timestamp > 0.0

    with pytest.raises(FrozenInstanceError):
        record.tokens_in = 200  # type: ignore[misc]


def test_call_budget_tracker_initial_state() -> None:
    """Verify tracker starts with zeroed counts and empty history."""
    tracker = CallBudgetTracker(job_id="job-123")
    assert tracker.job_id == "job-123"
    assert tracker.total_calls == 0
    assert tracker.count(CallKind.TRIAGE) == 0
    assert tracker.count(CallKind.SUMMARIZE) == 0
    assert tracker.count(CallKind.GENERATE) == 0
    assert tracker.count(CallKind.REPAIR) == 0
    assert tracker.get_counts() == {
        "triage": 0,
        "summarize": 0,
        "generate": 0,
        "repair": 0,
        "total": 0,
    }


def test_call_budget_tracker_constants() -> None:
    """Verify budget limits match R14.9 and design.md §5.7."""
    assert CallBudgetTracker.MAX_TRIAGE == 1
    assert CallBudgetTracker.MAX_SUMMARIZE == 1
    assert CallBudgetTracker.MAX_GENERATE == 1
    assert CallBudgetTracker.MAX_REPAIR == 1
    assert CallBudgetTracker.BUDGET_CEILING == 4


def test_call_budget_tracker_permitted_ceiling_case() -> None:
    """Verify ceiling case of exactly 1 call per kind (total = 4)."""
    tracker = CallBudgetTracker()

    rec_triage = tracker.record_call(
        CallKind.TRIAGE, model="fast-m", tier="fast", tokens_in=10, tokens_out=5
    )
    rec_sum = tracker.record_call(
        CallKind.SUMMARIZE, model="fast-m", tier="fast", tokens_in=20, tokens_out=10
    )
    rec_gen = tracker.record_call(
        CallKind.GENERATE, model="strong-m", tier="strong", tokens_in=100, tokens_out=50
    )
    rec_rep = tracker.record_call(
        CallKind.REPAIR, model="strong-m", tier="strong", tokens_in=110, tokens_out=40
    )

    assert tracker.total_calls == 4
    assert tracker.count(CallKind.TRIAGE) == 1
    assert tracker.count(CallKind.SUMMARIZE) == 1
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.count(CallKind.REPAIR) == 1
    assert tracker.get_counts() == {
        "triage": 1,
        "summarize": 1,
        "generate": 1,
        "repair": 1,
        "total": 4,
    }
    assert len(tracker.calls) == 4
    assert tracker.calls == [rec_triage, rec_sum, rec_gen, rec_rep]

    # Passes assertion
    tracker.assert_generation_budget(require_generation=True)


@pytest.mark.parametrize(
    ("kind"),
    [
        CallKind.TRIAGE,
        CallKind.SUMMARIZE,
        CallKind.GENERATE,
        CallKind.REPAIR,
    ],
)
def test_call_budget_tracker_per_kind_exceeded(kind: CallKind) -> None:
    """Verify that a 2nd call of any single kind raises CallBudgetExceededError."""
    tracker = CallBudgetTracker()
    tracker.record_call(kind)

    with pytest.raises(CallBudgetExceededError) as exc_info:
        tracker.record_call(kind)

    assert issubclass(CallBudgetExceededError, LLMError)
    assert kind.value in str(exc_info.value)
    assert tracker.count(kind) == 1
    assert tracker.total_calls == 1


def test_call_budget_tracker_ceiling_exceeded() -> None:
    """Verify attempting a 5th call when all 4 slots are filled raises CallBudgetExceededError."""
    tracker = CallBudgetTracker()
    tracker.record_call(CallKind.TRIAGE)
    tracker.record_call(CallKind.SUMMARIZE)
    tracker.record_call(CallKind.GENERATE)
    tracker.record_call(CallKind.REPAIR)

    assert tracker.total_calls == 4

    with pytest.raises(CallBudgetExceededError) as exc_info:
        tracker.check_can_call(CallKind.GENERATE)

    assert "ceiling" in str(exc_info.value).lower() or "budget" in str(exc_info.value).lower()


def test_assert_generation_budget_validation() -> None:
    """Verify assert_generation_budget invariants."""
    tracker = CallBudgetTracker()

    # 0 generate calls with require_generation=True raises CallBudgetViolationError
    with pytest.raises(CallBudgetViolationError) as exc_info:
        tracker.assert_generation_budget(require_generation=True)
    assert issubclass(CallBudgetViolationError, LLMError)
    assert "generation" in str(exc_info.value).lower()

    # 0 generate calls with require_generation=False succeeds (e.g. early exit)
    tracker.assert_generation_budget(require_generation=False)

    # 1 generate call succeeds
    tracker.record_call(CallKind.GENERATE)
    tracker.assert_generation_budget(require_generation=True)
    tracker.assert_generation_budget(require_generation=False)


def test_export_metrics_with_real_pipeline_metrics() -> None:
    """Verify export_metrics records labeled histogram metrics to real registry (R14.10)."""
    reg = CollectorRegistry(auto_describe=True)
    metrics = create_pipeline_metrics(registry=reg)

    tracker = CallBudgetTracker()
    tracker.record_call(CallKind.TRIAGE)
    tracker.record_call(CallKind.GENERATE)
    tracker.export_metrics(metrics)

    payload, _ = generate_metrics_payload(reg)
    payload_str = payload.decode("utf-8")
    assert 'llm_calls_per_job_bucket{kind="triage"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="generate"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="total"' in payload_str


def test_export_metrics_fallback_when_unlabelled() -> None:
    """Verify export_metrics falls back gracefully if histogram does not support labels."""
    mock_metrics = MagicMock()
    mock_metrics.llm_calls_per_job.labels.side_effect = ValueError("No label names were set")

    tracker = CallBudgetTracker()
    tracker.record_call(CallKind.GENERATE)
    tracker.export_metrics(mock_metrics)

    mock_metrics.llm_calls_per_job.observe.assert_called_once_with(1)


def test_export_metrics_with_kind_labeled_histogram() -> None:
    """Verify export_metrics observes per-kind and total labels when supported (R14.10)."""
    mock_metrics = MagicMock()
    tracker = CallBudgetTracker()
    tracker.record_call(CallKind.TRIAGE)
    tracker.record_call(CallKind.GENERATE)

    tracker.export_metrics(mock_metrics)

    # Verify labels was called with each kind and with total
    mock_metrics.llm_calls_per_job.labels.assert_any_call(kind="triage")
    mock_metrics.llm_calls_per_job.labels.assert_any_call(kind="summarize")
    mock_metrics.llm_calls_per_job.labels.assert_any_call(kind="generate")
    mock_metrics.llm_calls_per_job.labels.assert_any_call(kind="repair")
    mock_metrics.llm_calls_per_job.labels.assert_any_call(kind="total")


@pytest.mark.asyncio
async def test_budgeted_llm_provider_common_case() -> None:
    """Verify common case of 1 generation call via BudgetedLLMProvider."""
    fake = FakeLLMProvider()
    budgeted = BudgetedLLMProvider(provider=fake)

    assert budgeted.tracker.total_calls == 0
    messages = [ChatMessage(role="user", content="Hello")]

    result = await budgeted.generate(messages=messages, tier=ModelTier.FAST)

    assert result is not None
    assert budgeted.tracker.total_calls == 1
    assert budgeted.tracker.count(CallKind.GENERATE) == 1
    call_record = budgeted.tracker.calls[0]
    assert call_record.kind == CallKind.GENERATE
    assert call_record.model == "fake-fast-model"
    assert call_record.tier == ModelTier.FAST
    assert call_record.tokens_in > 0
    assert call_record.tokens_out > 0


@pytest.mark.asyncio
async def test_budgeted_llm_provider_explicit_call_kind() -> None:
    """Verify passing explicit call_kind records under that kind."""
    fake = FakeLLMProvider()
    budgeted = BudgetedLLMProvider(provider=fake)
    messages = [ChatMessage(role="user", content="Classify")]

    result = await budgeted.generate(messages=messages, call_kind=CallKind.TRIAGE)

    assert result is not None
    assert budgeted.tracker.total_calls == 1
    assert budgeted.tracker.count(CallKind.TRIAGE) == 1
    assert budgeted.tracker.count(CallKind.GENERATE) == 0


@pytest.mark.asyncio
async def test_budgeted_llm_provider_exceeded_error_prevents_invocation() -> None:
    """Verify exceeding call budget raises CallBudgetExceededError without inner call."""
    fake = FakeLLMProvider()
    budgeted = BudgetedLLMProvider(provider=fake)
    messages = [ChatMessage(role="user", content="Generate")]

    # First call succeeds
    await budgeted.generate(messages=messages)
    assert len(fake.recorded_calls) == 1

    # Second call of kind GENERATE fails before inner provider invocation
    with pytest.raises(CallBudgetExceededError):
        await budgeted.generate(messages=messages)

    # Provider was NOT invoked for the rejected 2nd call
    assert len(fake.recorded_calls) == 1
    assert budgeted.tracker.count(CallKind.GENERATE) == 1
    assert budgeted.tracker.total_calls == 1


@pytest.mark.asyncio
async def test_budgeted_llm_provider_ceiling_workflow() -> None:
    """Verify executing all 4 allowed kinds succeeds, and 5th call is rejected."""
    fake = FakeLLMProvider()
    budgeted = BudgetedLLMProvider(provider=fake)
    messages = [ChatMessage(role="user", content="Ping")]

    await budgeted.generate(messages=messages, call_kind=CallKind.TRIAGE)
    await budgeted.generate(messages=messages, call_kind=CallKind.SUMMARIZE)
    await budgeted.generate(messages=messages, call_kind=CallKind.GENERATE)
    await budgeted.generate(messages=messages, call_kind=CallKind.REPAIR)

    assert budgeted.tracker.total_calls == 4
    budgeted.tracker.assert_generation_budget(require_generation=True)

    with pytest.raises(CallBudgetExceededError):
        await budgeted.generate(messages=messages, call_kind=CallKind.GENERATE)


@pytest.mark.asyncio
async def test_budgeted_llm_provider_escalated_tier_replaces_call() -> None:
    """Verify generation with escalated ModelTier.HIGH_CAPABILITY records properly."""
    fake = FakeLLMProvider()
    budgeted = BudgetedLLMProvider(provider=fake)
    messages = [ChatMessage(role="user", content="Escalated generation")]

    result = await budgeted.generate(messages=messages, tier=ModelTier.HIGH_CAPABILITY)

    assert result is not None
    assert budgeted.tracker.total_calls == 1
    assert budgeted.tracker.count(CallKind.GENERATE) == 1
    call_record = budgeted.tracker.calls[0]
    assert call_record.kind == CallKind.GENERATE
    assert call_record.tier == ModelTier.HIGH_CAPABILITY
    assert call_record.model == "fake-strong-model"


@pytest.mark.asyncio
async def test_budgeted_llm_provider_aclose_delegation() -> None:
    """Verify aclose delegates to inner provider if available."""
    mock_provider = MagicMock()
    mock_provider.aclose = AsyncMock()
    budgeted = BudgetedLLMProvider(provider=mock_provider)

    await budgeted.aclose()
    mock_provider.aclose.assert_awaited_once()

    # Provider without aclose should complete without error
    fake = FakeLLMProvider()
    budgeted_fake = BudgetedLLMProvider(provider=fake)
    await budgeted_fake.aclose()

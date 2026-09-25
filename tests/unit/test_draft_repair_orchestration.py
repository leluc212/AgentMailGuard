"""Integration tests for draft schema validation and repair orchestration (R16.1-R16.3).

Covers the validate -> repair once -> fail into retry/DLQ path of SinglePassGenerator
described in design.md §5.7. An unvalidated draft is never returned to the caller,
so it can never be persisted.
"""

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
    AgentProfileRegistry,
    CallBudgetTracker,
    CallKind,
    DraftReplyPayload,
    FakeLLMProvider,
    LLMSchemaValidationError,
    LLMTimeoutError,
    SinglePassGenerator,
    UnvalidatedDraftError,
)
from packages.observability.metrics import (
    create_pipeline_metrics,
    generate_metrics_payload,
)


def _create_sample_context() -> ContextPackage:
    msg = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-repair-001",
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
        business_data={"customer_tier": "gold"},
    )


@pytest.fixture
def sample_context() -> ContextPackage:
    return _create_sample_context()


@pytest.fixture
def profile_registry() -> AgentProfileRegistry:
    return AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")


@pytest.fixture
def conformant_reply() -> dict[str, Any]:
    """A payload satisfying every field required by R16.1."""
    return {
        "action": "reply",
        "draft": "Hello Alice, you can reset your password from the account settings page.",
        "confidence": 0.95,
        "knowledge_chunks": ["chunk-1"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }


@pytest.fixture
def malformed_reply() -> dict[str, Any]:
    """A payload missing the required thread_summary_updated and model_tier fields."""
    return {
        "action": "reply",
        "draft": "Hello Alice, you can reset your password from the account settings page.",
        "confidence": 0.95,
        "knowledge_chunks": ["chunk-1"],
    }


def _metrics_payload(metrics: Any) -> str:
    payload, _ = generate_metrics_payload(metrics.registry)
    return str(payload.decode("utf-8"))


@pytest.mark.asyncio
async def test_conformant_first_attempt_needs_no_repair(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    conformant_reply: dict[str, Any],
) -> None:
    """A schema-conformant first response validates with zero repair calls (R16.2)."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(default_response=conformant_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(sample_context, category="support")

    assert len(fake_llm.recorded_calls) == 1
    assert result.budget_tracker.count(CallKind.GENERATE) == 1
    assert result.budget_tracker.count(CallKind.REPAIR) == 0
    assert result.is_repaired is False
    assert result.repair_attempts == 0
    assert isinstance(result.validated_payload, DraftReplyPayload)
    assert result.validated_payload.action == "reply"
    assert result.validated_payload.model_tier == "routine"
    assert result.content == conformant_reply

    payload = _metrics_payload(metrics)
    assert 'draft_validation_failures_total{stage="initial"}' not in payload
    assert 'draft_repairs_total{status="succeeded"}' not in payload


@pytest.mark.asyncio
async def test_malformed_response_is_repaired_in_one_retry(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    malformed_reply: dict[str, Any],
    conformant_reply: dict[str, Any],
) -> None:
    """A schema failure triggers exactly one repair retry that succeeds (R16.3)."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(canned_responses=[malformed_reply, conformant_reply])
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(sample_context, category="support")

    # Exactly two provider calls: one generation, one repair. Ceiling is 4 (R14.9).
    assert len(fake_llm.recorded_calls) == 2
    assert result.budget_tracker.count(CallKind.GENERATE) == 1
    assert result.budget_tracker.count(CallKind.REPAIR) == 1
    assert result.budget_tracker.total_calls == 2

    assert result.is_repaired is True
    assert result.repair_attempts == 1
    assert result.content == conformant_reply
    assert isinstance(result.validated_payload, DraftReplyPayload)
    assert result.validated_payload.model_tier == "routine"

    # The repair call carries the repair instruction plus the rejected output.
    repair_messages = fake_llm.recorded_calls[1]["messages"]
    assert len(repair_messages) > len(fake_llm.recorded_calls[0]["messages"])
    assert repair_messages[-1].role == "user"
    assert "failed schema validation" in repair_messages[-1].content
    assert repair_messages[-2].role == "assistant"

    # Token accounting covers both calls so cost reporting reflects real spend (R16.4).
    assert result.input_tokens == sum(c.tokens_in for c in result.budget_tracker.calls)
    assert result.output_tokens == sum(c.tokens_out for c in result.budget_tracker.calls)

    payload = _metrics_payload(metrics)
    assert 'draft_validation_failures_total{stage="initial"} 1.0' in payload
    assert 'draft_repairs_total{status="succeeded"} 1.0' in payload
    assert 'draft_validation_failures_total{stage="repair"}' not in payload


@pytest.mark.asyncio
async def test_second_failure_raises_unvalidated_draft_error(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    malformed_reply: dict[str, Any],
) -> None:
    """Failing validation twice fails the job instead of returning a draft (R16.3)."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(canned_responses=[malformed_reply, dict(malformed_reply)])
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    with pytest.raises(UnvalidatedDraftError):
        await generator.generate_draft(sample_context, category="support")

    # One generation, one repair, then the job fails. No third attempt.
    assert len(fake_llm.recorded_calls) == 2

    payload = _metrics_payload(metrics)
    assert 'draft_validation_failures_total{stage="initial"} 1.0' in payload
    assert 'draft_validation_failures_total{stage="repair"} 1.0' in payload
    assert 'draft_repairs_total{status="failed"} 1.0' in payload
    assert 'draft_repairs_total{status="succeeded"}' not in payload


@pytest.mark.asyncio
async def test_spent_repair_budget_blocks_the_retry(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    malformed_reply: dict[str, Any],
) -> None:
    """An already-spent repair budget fails the job rather than exceeding it (R14.9, R16.3)."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(default_response=malformed_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    tracker = CallBudgetTracker(job_id="job-repair-spent")
    tracker.record_call(CallKind.REPAIR, model="fake-fast-model")

    with pytest.raises(UnvalidatedDraftError):
        await generator.generate_draft(
            sample_context,
            category="support",
            budget_tracker=tracker,
        )

    # Only the generation call reached the provider; the repair was never attempted.
    assert len(fake_llm.recorded_calls) == 1
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.count(CallKind.REPAIR) == 1

    payload = _metrics_payload(metrics)
    assert 'draft_validation_failures_total{stage="initial"} 1.0' in payload
    assert 'draft_repairs_total{status="failed"} 1.0' in payload


def _raise_then_return(
    error: Exception,
    then: dict[str, Any],
) -> Any:
    """Build a responder that raises on its first call and returns `then` afterwards."""
    state = {"calls": 0}

    def responder(
        messages: list[Any],
        schema: dict[str, Any] | None,
        tier: Any,
    ) -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] == 1:
            raise error
        return then

    return responder


def _return_then_raise(
    first: dict[str, Any],
    error: Exception,
) -> Any:
    """Build a responder that returns `first` once and then raises."""
    state = {"calls": 0}

    def responder(
        messages: list[Any],
        schema: dict[str, Any] | None,
        tier: Any,
    ) -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] == 1:
            return first
        raise error

    return responder


@pytest.mark.asyncio
async def test_provider_parse_failure_is_repaired(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    conformant_reply: dict[str, Any],
) -> None:
    """An unparseable provider response is a validation failure and is repaired (R16.3).

    Both real providers raise LLMSchemaValidationError themselves when the model returns
    text that is not a structured object (truncation, refusal, or an endpoint that ignores
    the json_schema directive). That is the dominant real-world schema failure, so it must
    reach the repair path rather than escaping the generator.
    """
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(
        responder=_raise_then_return(
            LLMSchemaValidationError(
                "Failed to parse structured JSON response",
                raw_content='Sure! Here is the reply: "Dear Alice, ..."',
            ),
            conformant_reply,
        )
    )
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(sample_context, category="support")

    assert result.is_repaired is True
    assert result.repair_attempts == 1
    assert result.content == conformant_reply

    # The failed generation still cost a call, so the budget must account for it (R14.9).
    assert result.budget_tracker.count(CallKind.GENERATE) == 1
    assert result.budget_tracker.count(CallKind.REPAIR) == 1

    # The repair prompt shows the model the raw text it actually emitted.
    repair_messages = fake_llm.recorded_calls[1]["messages"]
    assert any("Sure! Here is the reply" in m.content for m in repair_messages)

    payload = _metrics_payload(metrics)
    assert 'draft_validation_failures_total{stage="initial"} 1.0' in payload
    assert 'draft_repairs_total{status="succeeded"} 1.0' in payload


@pytest.mark.asyncio
async def test_provider_error_during_repair_counts_a_failed_repair(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    malformed_reply: dict[str, Any],
) -> None:
    """A repair attempt that dies in the provider is still counted as a failed repair."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(
        responder=_return_then_raise(malformed_reply, LLMTimeoutError("repair timed out"))
    )
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    with pytest.raises(LLMTimeoutError, match="repair timed out"):
        await generator.generate_draft(sample_context, category="support")

    payload = _metrics_payload(metrics)
    assert 'draft_validation_failures_total{stage="initial"} 1.0' in payload
    assert 'draft_repairs_total{status="failed"} 1.0' in payload


@pytest.mark.asyncio
async def test_failed_job_still_exports_budget_and_token_metrics(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    malformed_reply: dict[str, Any],
) -> None:
    """A job that dies in validation still reports the calls and tokens it burned (R14.10).

    The per-job call histogram exists so the R14.9 budget is observable rather than
    assumed; a job that spent two calls and failed is exactly the job an operator needs
    to see, so the failure path must not skip the metrics block.
    """
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(canned_responses=[malformed_reply, dict(malformed_reply)])
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    with pytest.raises(UnvalidatedDraftError):
        await generator.generate_draft(sample_context, category="support")

    payload = _metrics_payload(metrics)
    assert 'llm_calls_per_job_count{kind="generate"} 1.0' in payload
    assert 'llm_calls_per_job_count{kind="repair"} 1.0' in payload
    assert "input_tokens_total" in payload
    assert "output_tokens_total" in payload


@pytest.mark.asyncio
async def test_repair_call_is_exported_as_a_repair_kind_call(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
    malformed_reply: dict[str, Any],
    conformant_reply: dict[str, Any],
) -> None:
    """The repair call is visible in llm_calls_total and the per-job histogram (R14.10)."""
    metrics = create_pipeline_metrics()
    fake_llm = FakeLLMProvider(canned_responses=[malformed_reply, conformant_reply])
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    await generator.generate_draft(sample_context, category="support")

    payload = _metrics_payload(metrics)
    assert 'llm_calls_total{kind="generate",model="fake-fast-model"} 1.0' in payload
    assert 'llm_calls_total{kind="repair",model="fake-fast-model"} 1.0' in payload
    assert 'llm_calls_per_job_count{kind="repair"} 1.0' in payload


@pytest.mark.asyncio
async def test_content_is_the_validated_payload_not_the_raw_response(
    sample_context: ContextPackage,
    profile_registry: AgentProfileRegistry,
) -> None:
    """GenerationResult.content carries validated values, so what is persisted was checked.

    Pydantic coerces in its default lax mode, so a model that returns confidence as the
    string "0.95" validates successfully. Returning the raw dict would hand a caller the
    string to persist while the validator had already normalized it to a float (R16.2).
    """
    loosely_typed_reply = {
        "action": "reply",
        "draft": "Hello Alice, you can reset your password from the account settings page.",
        "confidence": "0.95",
        "knowledge_chunks": ["chunk-1"],
        "thread_summary_updated": "false",
        "model_tier": "routine",
    }
    fake_llm = FakeLLMProvider(default_response=loosely_typed_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(sample_context, category="support")

    assert result.content["confidence"] == 0.95
    assert isinstance(result.content["confidence"], float)
    assert result.content["thread_summary_updated"] is False
    assert result.validated_payload is not None
    assert result.content == result.validated_payload.model_dump()

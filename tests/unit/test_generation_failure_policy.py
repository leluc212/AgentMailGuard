"""Generation failure routing: retry, dead-letter or drop (4.13a; closes 4.9)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from packages.broker.consumer import FatalError
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.drafts import UnpersistableDraftError
from packages.llm.protocol import (
    CallProvenance,
    LLMProviderMismatchError,
    LLMResponseError,
    LLMResult,
    LLMTimeoutError,
)
from packages.llm.validation import DraftSchemaContractError, UnvalidatedDraftError
from services.ai_worker.failure_policy import (
    DRAFTED_OR_LATER,
    Disposition,
    classify_generation_failure,
)


@pytest.mark.parametrize("state", sorted(DRAFTED_OR_LATER))
def test_illegal_transition_for_drafted_job_is_dropped(state: str) -> None:
    exc = IllegalStateTransitionError(state, JobState.GENERATING)
    decision = classify_generation_failure(exc, job_state=state)
    assert decision.disposition is Disposition.ACK_DROP
    assert state in decision.reason


def test_illegal_transition_for_undrafted_job_is_dead_lettered() -> None:
    """Review Focus 5: a state-machine violation that is not a redelivery is not dropped."""
    exc = IllegalStateTransitionError(JobState.CLASSIFIED, JobState.GENERATING)
    decision = classify_generation_failure(exc, job_state=JobState.CLASSIFIED.value)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert "IllegalStateTransitionError" in decision.reason


@pytest.mark.parametrize(
    "exc",
    [
        UnvalidatedDraftError("draft failed schema validation after one repair retry"),
        DraftSchemaContractError("profile schema narrows the action enum"),
        UnpersistableDraftError("message has no thread"),
        FatalError("job not found"),
    ],
)
def test_permanent_failures_are_dead_lettered(exc: Exception) -> None:
    decision = classify_generation_failure(exc, job_state=None)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert type(exc).__name__ in decision.reason


@pytest.mark.parametrize(
    "exc",
    [
        LLMTimeoutError("upstream timeout"),
        LLMResponseError("HTTP 529 overloaded"),
        ConnectionError("reset by peer"),
        RuntimeError("something unexpected"),
    ],
)
def test_transient_and_unknown_failures_are_retried(exc: Exception) -> None:
    decision = classify_generation_failure(exc, job_state=None)
    assert decision.disposition is Disposition.RETRY


def _mismatch() -> LLMProviderMismatchError:
    return LLMProviderMismatchError(
        "provider_mismatch: served by 'Together', pinned to ['phala'] (model qwen/qwen-2.5-7b)",
        expected=("phala",),
        served="Together",
        provenance=CallProvenance(requested_model="qwen/qwen-2.5-7b-instruct"),
    )


def test_provider_mismatch_is_dead_lettered_with_its_marker() -> None:
    """A wrong provider is deterministic: a retry is billed again and fails the same way."""
    decision = classify_generation_failure(_mismatch(), job_state=None)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert "provider_mismatch" in decision.reason


@pytest.mark.parametrize("source", ["openrouter_credits", "openrouter_key_limit", None])
def test_a_402_that_is_not_the_in_flight_budget_is_dead_lettered(source: str | None) -> None:
    """No credit or a key limit does not clear by waiting 30 s, 5 m and 30 m."""
    exc = LLMResponseError("HTTP 402", status_code=402, limit_source=source)
    assert classify_generation_failure(exc, job_state=None).disposition is Disposition.DEAD_LETTER


def test_the_transient_in_flight_402_is_still_retried() -> None:
    exc = LLMResponseError("HTTP 402", status_code=402, limit_source="openrouter_in_flight_budget")
    assert classify_generation_failure(exc, job_state=None).disposition is Disposition.RETRY


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_other_http_errors_are_still_retried(status: int) -> None:
    exc = LLMResponseError(f"HTTP {status}", status_code=status)
    assert classify_generation_failure(exc, job_state=None).disposition is Disposition.RETRY


def test_truncated_draft_reason_names_max_tokens() -> None:
    """Review Focus 3."""
    exc = UnvalidatedDraftError("invalid after repair", finish_reason="length")
    decision = classify_generation_failure(exc, job_state=None)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert "truncated at max_tokens" in decision.reason
    assert "finish_reason=length" in decision.reason


def test_untruncated_draft_reason_does_not_mention_max_tokens() -> None:
    exc = UnvalidatedDraftError("invalid after repair", finish_reason="stop")
    assert "max_tokens" not in classify_generation_failure(exc, job_state=None).reason


class _TruncatingProvider(FakeLLMProvider):
    """Fake provider whose every response reports a length stop."""

    async def generate(self, **kwargs: Any) -> LLMResult:
        result = await super().generate(**kwargs)
        return replace(result, raw_finish_reason="length")


async def test_generator_attaches_finish_reason_to_unvalidated_draft() -> None:
    invalid = {"action": "reply"}
    generator = SinglePassGenerator(
        llm_provider=_TruncatingProvider(canned_responses=[invalid, dict(invalid)]),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    from datetime import UTC, datetime
    from uuid import uuid4

    from packages.domain.entities import ContextPackage, EmailAddress, NormalizedMessage

    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="p-1",
        sender=EmailAddress(email="a@example.com"),
        received_at=datetime.now(UTC),
        subject="Hello",
        body_text="Question",
        body_text_clean="Question",
    )
    context = ContextPackage(
        agent_instructions="a", category_instructions="c", current_message=message
    )

    with pytest.raises(UnvalidatedDraftError) as excinfo:
        await generator.generate_draft(context, category="support")

    assert excinfo.value.finish_reason == "length"


@pytest.mark.parametrize(
    "state",
    [
        JobState.QUEUED.value,
        JobState.CONTEXT_READY.value,
        JobState.GENERATING.value,
        JobState.RETRY_PENDING.value,
        None,
    ],
)
def test_illegal_transition_for_in_flight_job_is_retried(state: str | None) -> None:
    """A duplicate racing a live delivery, or a skipped recovery, must not kill the job."""
    exc = IllegalStateTransitionError(state or JobState.QUEUED, JobState.GENERATING)
    assert classify_generation_failure(exc, job_state=state).disposition is Disposition.RETRY


def test_illegal_transition_for_dead_lettered_job_is_dropped() -> None:
    """A stale delivery for a job already in the DLQ is not dead-lettered a second time."""
    exc = IllegalStateTransitionError(JobState.DEAD_LETTER, JobState.GENERATING)
    decision = classify_generation_failure(exc, job_state=JobState.DEAD_LETTER.value)
    assert decision.disposition is Disposition.ACK_DROP


@pytest.mark.parametrize(
    "state",
    [JobState.RECEIVED.value, JobState.NORMALIZED.value, JobState.FAILED.value],
)
def test_illegal_transition_for_unroutable_job_is_dead_lettered(state: str) -> None:
    exc = IllegalStateTransitionError(state, JobState.GENERATING)
    decision = classify_generation_failure(exc, job_state=state)
    assert decision.disposition is Disposition.DEAD_LETTER


async def test_unparseable_truncated_output_still_names_max_tokens() -> None:
    """Real truncation cuts the JSON off, so it fails parsing, not validation (I3)."""
    from packages.llm.protocol import LLMSchemaValidationError

    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(
            error_to_raise=LLMSchemaValidationError(
                "Failed to parse structured JSON",
                raw_content='{"action": "re',
                finish_reason="length",
            )
        ),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    from datetime import UTC, datetime
    from uuid import uuid4

    from packages.domain.entities import ContextPackage, EmailAddress, NormalizedMessage

    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="p-2",
        sender=EmailAddress(email="a@example.com"),
        received_at=datetime.now(UTC),
        subject="Hello",
        body_text="Question",
        body_text_clean="Question",
    )
    context = ContextPackage(
        agent_instructions="a", category_instructions="c", current_message=message
    )

    with pytest.raises(UnvalidatedDraftError) as excinfo:
        await generator.generate_draft(context, category="support")

    assert excinfo.value.finish_reason == "length"
    decision = classify_generation_failure(excinfo.value, job_state=None)
    assert "truncated at max_tokens" in decision.reason

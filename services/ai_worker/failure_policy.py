"""Generation failure routing for the AI worker (task 4.13a; closes 4.9's retry/DLQ item).

Retry only transient faults; send non-transient ones straight to the dead-letter queue with
their reason; acknowledge and drop redeliveries of jobs that are already drafted. Sources:
Microsoft Retry pattern ("cancel" non-transient faults; decide retries where the full context
is understood), NServiceBus recoverability (unrecoverable exceptions skip retries), OpenAI
Structured Outputs (non-matching output comes from refusals or max_tokens truncation).
Lives in the worker so ``packages.llm`` never depends on broker types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from packages.broker.consumer import FatalError
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.drafts import UnpersistableDraftError
from packages.llm.protocol import (
    LLMProviderMismatchError,
    LLMQuotaExhaustedError,
    LLMResponseError,
)
from packages.llm.validation import DraftSchemaContractError, UnvalidatedDraftError


class Disposition(StrEnum):
    """What the consumer does with a delivery whose generation failed."""

    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    ACK_DROP = "ack_drop"


@dataclass(frozen=True)
class FailureDecision:
    """A disposition and the human-readable reason recorded with it."""

    disposition: Disposition
    reason: str


DRAFTED_OR_LATER: frozenset[str] = frozenset(
    {JobState.DRAFTED.value, JobState.DISPATCHED.value, JobState.COMPLETED.value}
)
"""Job states in which a new delivery is a redelivery, not a failure."""

IN_FLIGHT_OR_RECOVERING: frozenset[str] = frozenset(
    {
        JobState.QUEUED.value,
        JobState.CONTEXT_READY.value,
        JobState.GENERATING.value,
        JobState.RETRY_PENDING.value,
    }
)
"""States in which a state error means another delivery owns the job, or a recovery write was
skipped; the retry ladder resolves both (the job drafts, or the next delivery recovers it)."""

TRUNCATION_FINISH_REASONS: frozenset[str] = frozenset({"length", "max_tokens", "max_output_tokens"})
"""Provider stop reasons meaning the output was cut off at the token limit."""

_PERMANENT: tuple[type[BaseException], ...] = (
    DraftSchemaContractError,
    UnpersistableDraftError,
    FatalError,
    LLMProviderMismatchError,
    # A used-up quota, balance, spend limit or daily cap (HTTP 429): OpenAI's error-code guide
    # says retrying does not restore access, so the ladder's 30 s to 30 min tiers would only
    # hold the job (packages/core/provider_limits.py). A per-minute 429 is still retried.
    LLMQuotaExhaustedError,
)

_TRANSIENT_402_SOURCE = "openrouter_in_flight_budget"
"""The one OpenRouter 402 that clears by itself (in-flight spend drains); every other 402 is
an account limit (no credit, a key limit, or an unnamed source) that waiting does not lift."""


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def classify_generation_failure(exc: BaseException, *, job_state: str | None) -> FailureDecision:
    """Map a generation failure (and, for state errors, the job's state) to a disposition.

    Unknown exceptions are retried: failing safe still reaches the DLQ at ``max_retries``.
    """
    if isinstance(exc, IllegalStateTransitionError):
        if job_state in DRAFTED_OR_LATER or job_state == JobState.DEAD_LETTER.value:
            return FailureDecision(
                Disposition.ACK_DROP, f"job already {job_state}; redelivery dropped"
            )
        if job_state is None or job_state in IN_FLIGHT_OR_RECOVERING:
            return FailureDecision(Disposition.RETRY, _describe(exc))
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    if isinstance(exc, UnvalidatedDraftError):
        reason = _describe(exc)
        if exc.finish_reason in TRUNCATION_FINISH_REASONS:
            reason = (
                f"{reason} [truncated at max_tokens (finish_reason={exc.finish_reason}); "
                "raise max_tokens before replaying]"
            )
        return FailureDecision(Disposition.DEAD_LETTER, reason)
    if isinstance(exc, _PERMANENT):
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    if (
        isinstance(exc, LLMResponseError)
        and exc.status_code == 402
        and exc.limit_source != _TRANSIENT_402_SOURCE
    ):
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    return FailureDecision(Disposition.RETRY, _describe(exc))

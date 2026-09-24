"""Call budget tracker, budgeted LLM provider wrapper, and invocation invariants.

Implements R14.9 and design.md §5.7:
Enforces hard upper bounds on model invocations per email processing job:
≤1 triage-LLM call (stage 3 only), ≤1 thread-summarization call (threshold-triggered only),
exactly 1 generation call, and ≤1 schema-repair retry. Ceiling is 4 calls, common case is 1.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from packages.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResult,
    ModelTier,
)

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics


class CallKind(StrEnum):
    """Categories of LLM calls permitted in the pipeline lifecycle (R14.9)."""

    TRIAGE = "triage"
    SUMMARIZE = "summarize"
    GENERATE = "generate"
    REPAIR = "repair"


@dataclass(frozen=True)
class CallRecord:
    """Immutable audit record of a completed LLM invocation."""

    kind: CallKind
    model: str = ""
    tier: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    timestamp: float = field(default_factory=time.time)


class CallBudgetExceededError(LLMError):
    """Raised when attempting an LLM call exceeding per-kind or total budget bounds (R14.9)."""


class CallBudgetViolationError(LLMError):
    """Raised when a completed job violates post-execution budget assertions (R14.9)."""


class CallBudgetTracker:
    """Per-job tracker enforcing the 4-call budget ceiling and invariants (R14.9, §5.7)."""

    MAX_TRIAGE: int = 1
    MAX_SUMMARIZE: int = 1
    MAX_GENERATE: int = 1
    MAX_REPAIR: int = 1
    BUDGET_CEILING: int = 4

    def __init__(self, job_id: str | None = None) -> None:
        self.job_id = job_id
        self._calls: list[CallRecord] = []
        self._counts: dict[CallKind, int] = dict.fromkeys(CallKind, 0)

    @property
    def calls(self) -> list[CallRecord]:
        """Return a copy of all recorded calls."""
        return list(self._calls)

    @property
    def total_calls(self) -> int:
        """Total number of model invocations recorded across all kinds."""
        return sum(self._counts.values())

    def count(self, kind: CallKind) -> int:
        """Return the number of calls recorded for a specific CallKind."""
        return self._counts.get(kind, 0)

    def _max_for_kind(self, kind: CallKind) -> int:
        match kind:
            case CallKind.TRIAGE:
                return self.MAX_TRIAGE
            case CallKind.SUMMARIZE:
                return self.MAX_SUMMARIZE
            case CallKind.GENERATE:
                return self.MAX_GENERATE
            case CallKind.REPAIR:
                return self.MAX_REPAIR
            case _:
                return 1

    def check_can_call(self, kind: CallKind) -> None:
        """Verify that invoking kind will not breach per-kind limits or the budget ceiling.

        Raises:
            CallBudgetExceededError: If the call would violate budget limits.
        """
        job_ctx = f" for job {self.job_id}" if self.job_id else ""
        if self.total_calls >= self.BUDGET_CEILING:
            raise CallBudgetExceededError(
                f"LLM call budget ceiling exceeded{job_ctx}: cannot execute {kind.value!r} call "
                f"(total calls {self.total_calls} >= ceiling {self.BUDGET_CEILING})"
            )

        max_allowed = self._max_for_kind(kind)
        current_count = self._counts.get(kind, 0)
        if current_count >= max_allowed:
            raise CallBudgetExceededError(
                f"LLM call kind budget exceeded{job_ctx}: cannot execute {kind.value!r} call "
                f"(kind count {current_count} >= limit {max_allowed})"
            )

    def record_call(
        self,
        kind: CallKind,
        model: str = "",
        tier: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> CallRecord:
        """Validate budget and record a completed invocation.

        Raises:
            CallBudgetExceededError: If adding this call exceeds limits.
        """
        self.check_can_call(kind)
        record = CallRecord(
            kind=kind,
            model=model,
            tier=tier,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._calls.append(record)
        self._counts[kind] = self._counts.get(kind, 0) + 1
        return record

    def assert_generation_budget(self, require_generation: bool = True) -> None:
        """Assert final budget constraints for the completed job.

        Parameters:
            require_generation: If True, asserts exactly 1 generation call occurred.
                                Set to False for early-exit jobs (templates / canned acks).

        Raises:
            CallBudgetExceededError: If any call kind or total calls exceeded limits.
            CallBudgetViolationError: If required generation call was omitted.
        """
        job_ctx = f" for job {self.job_id}" if self.job_id else ""
        if self.total_calls > self.BUDGET_CEILING:
            raise CallBudgetExceededError(
                f"Job{job_ctx} exceeded budget ceiling: {self.total_calls} > {self.BUDGET_CEILING}"
            )

        for kind in CallKind:
            max_allowed = self._max_for_kind(kind)
            current_count = self._counts.get(kind, 0)
            if current_count > max_allowed:
                raise CallBudgetExceededError(
                    f"Job{job_ctx} exceeded {kind.value!r} limit: {current_count} > {max_allowed}"
                )

        if require_generation and self.count(CallKind.GENERATE) != 1:
            raise CallBudgetViolationError(
                f"Job{job_ctx} failed generation requirement: expected exactly 1 generation call, "
                f"found {self.count(CallKind.GENERATE)}"
            )

    def get_counts(self) -> dict[str, int]:
        """Return a dictionary summary of call counts by kind and total."""
        return {
            "triage": self.count(CallKind.TRIAGE),
            "summarize": self.count(CallKind.SUMMARIZE),
            "generate": self.count(CallKind.GENERATE),
            "repair": self.count(CallKind.REPAIR),
            "total": self.total_calls,
        }

    def export_metrics(self, metrics: PipelineMetrics | Any) -> None:
        """Export call counts into Prometheus metrics (R21.4)."""
        if metrics is None or not hasattr(metrics, "llm_calls_per_job"):
            return
        histogram = metrics.llm_calls_per_job
        try:
            for kind in CallKind:
                histogram.labels(kind=kind.value).observe(self.count(kind))
            histogram.labels(kind="total").observe(self.total_calls)
        except (ValueError, TypeError, AttributeError):
            histogram.observe(self.total_calls)


class BudgetedLLMProvider(LLMProvider):
    """Decorator wrapping an LLMProvider to enforce per-job call budgets (R14.9)."""

    def __init__(
        self,
        provider: LLMProvider,
        tracker: CallBudgetTracker | None = None,
        default_kind: CallKind = CallKind.GENERATE,
    ) -> None:
        self.provider = provider
        self._tracker = tracker if tracker is not None else CallBudgetTracker()
        self.default_kind = default_kind

    @property
    def tracker(self) -> CallBudgetTracker:
        """Return the underlying CallBudgetTracker instance."""
        return self._tracker

    async def aclose(self) -> None:
        """Close provider resources if inner provider supports aclose."""
        aclose_fn = getattr(self.provider, "aclose", None)
        if callable(aclose_fn):
            res = aclose_fn()
            if inspect.isawaitable(res):
                await res

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
        call_kind: CallKind | None = None,
        **params: Any,
    ) -> LLMResult:
        """Execute model generation while enforcing budget checks and accounting."""
        kind = call_kind or self.default_kind
        self.tracker.check_can_call(kind)
        result = await self.provider.generate(
            messages=messages,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            **params,
        )
        self.tracker.record_call(
            kind,
            model=result.model,
            tier=str(result.tier),
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
        )
        return result

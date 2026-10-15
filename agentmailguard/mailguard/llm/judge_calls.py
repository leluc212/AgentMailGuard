"""Bookkeeping shared by the five guard judges (ADR-0015 decisions 5 and 12).

* **Call budget.** Every judge records one call per invocation (a schema repair inside
  ``call_structured`` is part of the same call). The pipeline opens a counter per step
  (``counting_guard_calls``) and adds it to ``GuardReport.metadata["guard_calls"]``
  (``{stage: count}``). A job may use at most ``MAX_GUARD_CALLS_PER_JOB`` calls: two in the
  Inbound Guard, one doc chunk judge, one doc set judge, one output judge.
* **Fail-closed judges.** ``judge_failure`` marks a failed or invalid judge answer as a visible
  fallback (``mark_llm_fallback``) and, with ``MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR`` on, returns
  the verdict error ``judge_invalid:<stage>`` that the policy's fail-closed rule (P00) turns into
  human review. A judge that is not configured never gets here: it still degrades silently.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from mailguard.llm.structured import mark_llm_fallback

JudgeStage = Literal["inbound_scan", "inbound_extract", "doc_chunk", "doc_set", "output"]
JUDGE_STAGES: tuple[JudgeStage, ...] = (
    "inbound_scan",
    "inbound_extract",
    "doc_chunk",
    "doc_set",
    "output",
)
MAX_GUARD_CALLS_PER_JOB = 5
"""Ceiling on guard model calls per job (decision 12); the production default enables two."""

JUDGE_INVALID_PREFIX = "judge_invalid:"

_CALLS: ContextVar[Counter[str] | None] = ContextVar("mailguard_guard_calls", default=None)


@contextmanager
def counting_guard_calls() -> Iterator[Counter[str]]:
    """Count the judge calls made inside the block (per asyncio context, so concurrent jobs
    sharing one pipeline never mix their counts)."""
    counter: Counter[str] = Counter()
    token = _CALLS.set(counter)
    try:
        yield counter
    finally:
        _CALLS.reset(token)


def record_guard_call(stage: JudgeStage) -> None:
    """One judge call of ``stage``; a no-op outside ``counting_guard_calls``."""
    counter = _CALLS.get()
    if counter is not None:
        counter[stage] += 1


def merge_guard_calls(previous: Any, calls: Counter[str]) -> dict[str, int]:
    """``previous`` (a report's ``guard_calls``) plus ``calls``, every stage listed in order."""
    merged: Counter[str] = Counter(previous if isinstance(previous, dict) else {})
    merged.update(calls)
    return {stage: int(merged[stage]) for stage in JUDGE_STAGES}


def judge_failure(
    metadata: dict[str, Any], exc: BaseException, *, stage: JudgeStage, fail_closed: bool
) -> str | None:
    """Mark a failed judge answer; return the verdict error when the judge fails closed."""
    mark_llm_fallback(metadata, exc)
    if not fail_closed:
        return None
    metadata["judge_invalid"] = stage
    return f"{JUDGE_INVALID_PREFIX}{stage}"


__all__ = [
    "JUDGE_INVALID_PREFIX",
    "JUDGE_STAGES",
    "MAX_GUARD_CALLS_PER_JOB",
    "JudgeStage",
    "counting_guard_calls",
    "judge_failure",
    "merge_guard_calls",
    "record_guard_call",
]

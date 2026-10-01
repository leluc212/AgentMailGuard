"""The route of a benchmark run: what its meta records, which failures are the route's, when
the run stops.

Task 7.29 (the full-cloud route, ADR-0014; built as work package R4). A run whose model is
served through OpenRouter pins ONE provider with fallbacks off. What follows is pure and testable
without a network:

- the run meta records the pin (``route_meta``), so a resume under another pin is refused;
- every call's provenance (the served provider, the attempt, the generation id, the cost) is
  read back from the result rows (``provenance_summary``);
- a pinned run stops after three consecutive provider mismatches or "no provider" errors; EVERY
  run, OpenAI and Gemini included, stops at once when the account has no credit or a quota, a
  balance, a spend limit or a daily cap is used up (``RouteBreaker``; owner decision 2026-10-01,
  ADR-0014): continuing would only fill the result file with error rows. A per-minute rate limit
  never stops a run; it is backed off and retried. Cases already recorded stay; a resume runs the
  rest. The runner then exits ``ROUTE_STOP_EXIT``, and the benchmark kit stops the whole campaign.
- a guard LLM call that failed on the route or the service itself (``guard_call_failure``: HTTP
  402, 404, 408, 409, 429 or 5xx, a timeout, a connection error, a router error in an HTTP 200
  body) makes its case an error row (``guard_route_failure``), not a scored fallback (owner
  decision 2026-10-01). A model that answers badly (no JSON, the wrong fields, a refusal) is the
  guard's own behaviour and stays a scored fallback.

A call another provider served is an error of its case (``packages/llm/client.py`` raises for
rag-email's calls; ``counting.CountingProvider`` records a violation for the guard's), never a
defence.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from evaluation.mailguard_bench.scoring import GUARD_ROUTE_FAILURE_KIND
from packages.core.provider_limits import (
    QUOTA_MARKER,
    quota_exhaustion,
    quota_in_text,
    quota_note,
)
from packages.core.settings import LLMTiersSettings, ProviderRouting
from packages.llm.provenance import MISMATCH_MARKER, check_pinned_route

STOP_AFTER = 3
"""Consecutive provider mismatches or no-provider errors after which a pinned run stops."""
GUARD_ROUTE_FAILURE = GUARD_ROUTE_FAILURE_KIND
"""The ``error.kind`` of a row a guard LLM call's route or service failure kept out of the
scores, and the marker its recorded failures start with."""
ROUTE_FAILURE_STATUSES = frozenset({402, 404, 408, 409, 429})
"""The HTTP statuses (with every 5xx) of a guard call that failed on the route or the service."""
IMMEDIATE_STOPS = ("no_credit", QUOTA_MARKER)
"""Route failures that stop any run at once: waiting does not lift them."""
PINNED_STREAKS = ("provider_mismatch", "no_provider")
"""Route failures that stop a pinned run after ``STOP_AFTER`` in a row; an unpinned run (OpenAI
directly) has no pinned provider to lose, and its 5xx rows are retried like any error row."""
EMPTY_CHOICES = "Empty choices from OpenAI: "
"""How the guard's OpenAI provider reports a body without ``choices`` (``repr`` of the body)."""
_GUARD_HTTP = re.compile(r"\b(?:OpenAI|Ollama) HTTP (\d{3})\b")
_GUARD_TIMEOUT = re.compile(r"\brequest timed out after\b")
_GUARD_TRANSPORT = re.compile(r"\b(?:OpenAI|Ollama) transport error\b")
"""The guard providers' own messages (``mailguard/llm/openai_provider.py``,
``ollama_provider.py``), read only when no transport exception is chained."""
ROUTE_STOP_EXIT = 3
"""The exit status of a runner the breaker stopped (``STOP <config>``), distinct from a failure's
1: the benchmark kit stops the whole campaign on it, because the next config and the retry pass
would only hit the same route."""

_NO_PROVIDER = re.compile(r"(?:LLM request failed with status|HTTP)\s*:?\s*(?:404|502|503)\b")
_NO_CREDIT = re.compile(r"(?:LLM request failed with status|HTTP)\s*:?\s*402\b")
_TRANSIENT_402 = "openrouter_in_flight_budget"


def route_meta(llm: LLMTiersSettings) -> dict[str, Any] | None:
    """The route facts a routed run adds to its ``generation`` meta; None when not routed."""
    if llm.openai_provider_routing is None:
        return None
    return {
        "provider_routing": llm.openai_provider_routing.request_object(),
        "response_metadata": llm.openai_response_metadata,
    }


def pin_problem(
    routing: Mapping[str, Any] | None, provenance: Mapping[str, Any] | None
) -> str | None:
    """Why a call does not honour the route its provider pinned, or None.

    ``routing`` is the ``provider`` object the provider sent (None: not pinned, nothing to
    check) and ``provenance`` what the call's response said (None: it said nothing, which a pin
    cannot accept). Used for the guard's judges, whose provider (AgentMailGuard's) does not
    judge the match itself, and by the guard probe.
    """
    if not routing:
        return None
    return check_pinned_route(provenance or {}, ProviderRouting.model_validate(dict(routing)))


def expected_guard_route(llm: LLMTiersSettings) -> dict[str, Any] | None:
    """The ``provider`` object the guard's judges must send in this run; None when not routed."""
    routing = llm.openai_provider_routing
    return None if routing is None else routing.request_object()


def guard_pin_problem(provider: object, expected: Mapping[str, Any] | None) -> str | None:
    """Why the guard's provider would not honour the run's pin, or None.

    A guard commit without provider routing (1a3ef62, the v2 pin before ADR-0014) ignores the
    keys of ``guard_models.yaml`` it does not know: its provider would send no pin and report no
    provider, and the run would look pinned while the judges ran on whatever OpenRouter picked.
    So a routed run compares what the provider is configured to send with what the run pins,
    before any call.
    """
    if expected is None:
        return None
    actual = getattr(provider, "provider_routing", None)
    if actual != dict(expected):
        return (
            f"the guard's provider for this model sends provider routing {actual!r}, the run "
            f"pins {dict(expected)!r}: the guard commit lacks provider routing support "
            "(use the guard commit of the OpenRouter route) or guard_models.yaml disagrees "
            "with the model profile"
        )
    if getattr(provider, "response_metadata", False) is not True:
        return "the guard's provider does not ask for the router's metadata (response_metadata)"
    return None


def route_failure(record: Mapping[str, Any]) -> str | None:
    """Why an error row is a route failure, or None (an ok row, or an error of another kind).

    ``quota_exhausted``: a quota, a balance, a spend limit or a daily cap is used up, in any
    model call or embedding the row names (``packages.core.provider_limits``).
    ``provider_mismatch``: a call another provider served (rag-email's or a guard judge's).
    ``no_provider``: HTTP 404, 502 or 503 from the router with fallbacks off: the pinned provider
    is gone. ``no_credit``: HTTP 402 other than the transient in-flight budget, which the
    back-off retries.
    """
    if record.get("status") != "error":
        return None
    error = record.get("error")
    if not isinstance(error, Mapping):
        return None
    text = f"{error.get('kind') or ''} {error.get('message') or ''}"
    if QUOTA_MARKER in text:
        return QUOTA_MARKER
    if MISMATCH_MARKER in text or "LLMProviderMismatchError" in text:
        return "provider_mismatch"
    if _NO_PROVIDER.search(text):
        return "no_provider"
    if _NO_CREDIT.search(text) and _TRANSIENT_402 not in text:
        return "no_credit"
    return None


def _is_route_status(status: int | None) -> bool:
    return status is not None and (status in ROUTE_FAILURE_STATUSES or 500 <= status <= 599)


def _status_note(status: int | None, body: Any) -> str:
    """``HTTP N[ (limit_source)][ (quota_exhausted: q)]``: what a route classifier reads first."""
    note = f"HTTP {status}"
    error = body.get("error") if isinstance(body, Mapping) else None
    metadata = error.get("metadata") if isinstance(error, Mapping) else None
    source = metadata.get("limit_source") if isinstance(metadata, Mapping) else None
    if isinstance(source, str) and source:
        note += f" ({source})"
    quota = quota_exhaustion(status, body)
    if quota is not None:
        note += f" ({quota_note(quota)})"
    return note


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _body_error_note(error: Mapping[str, Any], body: Any) -> str:
    code = error.get("code")
    status = code if isinstance(code, int) and not isinstance(code, bool) else None
    head = "a router error in an HTTP 200 body"
    return f"{head} ({_status_note(status, body)})" if status is not None else head


def _empty_choices_error(text: str) -> str | None:
    """The router error a guard provider reported as a body without ``choices``, or None.

    AgentMailGuard's OpenAI provider raises ``Empty choices from OpenAI: <repr of the body>``,
    which is the only place the body survives; a body with no ``error`` object is not a route
    failure (an odd answer is the model's).
    """
    if not text.startswith(EMPTY_CHOICES):
        return None
    try:
        body = ast.literal_eval(text.removeprefix(EMPTY_CHOICES))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    error = body.get("error") if isinstance(body, Mapping) else None
    return _body_error_note(error, body) if isinstance(error, Mapping) else None


def guard_call_failure(exc: BaseException) -> str | None:
    """Why a guard LLM call that raised failed on the route or the service; None if it did not.

    The guard's providers chain the transport's exception, so the cause chain says what happened:
    an HTTP 402, 404, 408, 409, 429 or 5xx answer (its body read for OpenRouter's
    ``limit_source`` and a used-up quota), a timeout, a connection error, or a router error the
    provider found in an HTTP 200 body. Anything else (a 400, a 401) returns None, as does a
    model's bad answer, which never reaches the provider as an exception.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            status = current.response.status_code
            if not _is_route_status(status):
                return None
            return _status_note(status, _json_body(current.response))
        if isinstance(current, httpx.TimeoutException) or type(current).__name__ in (
            "LLMTimeoutError",
            "TimeoutError",
        ):
            return "timed out"
        if isinstance(current, httpx.RequestError):
            return "a connection error"
        found = _empty_choices_error(str(current))
        if found is not None:
            return found
        current = current.__cause__ or current.__context__
    return _guard_message_failure(str(exc))


def _guard_message_failure(text: str) -> str | None:
    """The route failure a guard provider's message names when nothing is chained, or None."""
    status = _GUARD_HTTP.search(text)
    if status is not None:
        code = int(status.group(1))
        return f"HTTP {code}" if _is_route_status(code) else None
    if _GUARD_TIMEOUT.search(text):
        return "timed out"
    if _GUARD_TRANSPORT.search(text):
        return "a connection error"
    return None


def guard_result_failure(result: Any) -> str | None:
    """A router error an HTTP 200 answer to a guard call carries (an ``error`` object, or
    OpenRouter's ``finish_reason`` ``error``); None for an answer, good or bad."""
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, Mapping):
        choices = raw.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        for error in (raw.get("error"), first.get("error") if isinstance(first, Mapping) else None):
            if isinstance(error, Mapping):
                return _body_error_note(error, raw)
    if getattr(result, "raw_finish_reason", None) == "error":
        return "a router error in an HTTP 200 body (finish_reason error)"
    return None


def describe_guard_failure(note: str, detail: str) -> str:
    """The recorded line of one guard call's route failure: marker first, then the facts."""
    return f"{GUARD_ROUTE_FAILURE}: a guard LLM call failed on the route: {note}: {detail[:300]}"


def stop_advice(reason: str) -> str:
    """What to do before the same command runs again, for the reason a run stopped."""
    if reason.startswith((QUOTA_MARKER, "no_credit")):
        return (
            "the provider's credit, quota, spend limit or daily cap is used up (a daily cap resets "
            "the next day); restore it"
        )
    return "once the route serves again"


def stop_message(config: str, reason: str) -> str:
    """The line a run prints when the breaker stopped it, with how to go on.

    The rows that tripped the stop are ``error`` rows, and a resume skips every recorded case,
    so only ``--retry-errors`` runs those cases again. The kit (``make bench-run``) already passes
    it to every runner it starts and has no such option, so the line says which command needs it:
    a teammate reading it under the kit must rerun the kit's command unchanged.
    """
    return (
        f"STOP {config}: {reason}; {stop_advice(reason)}, then rerun: under the kit, the same "
        "`make bench-run` command unchanged (it already retries error rows; other models' runs "
        "may run in between); a runner started by hand, its command with --retry-errors (the "
        "rows that tripped the stop are error rows, and a plain resume skips them)"
    )


class RouteBreaker:
    """Trips at the first used-up quota or ``no_credit``, and, on a pinned route, after ``limit``
    provider mismatches or no-provider errors in a row.

    ``routed`` is whether the run pins an OpenRouter provider. Every run stops on a used-up quota
    or credit (owner decision 2026-10-01); only a pinned run stops on a streak, because only it
    has a pinned provider that can be gone or replaced.
    """

    def __init__(self, limit: int = STOP_AFTER, *, routed: bool = True) -> None:
        self.limit = limit
        self.routed = routed
        self.tripped: str | None = None
        self._streak = 0
        self._last: str | None = None

    def observe(self, record: Mapping[str, Any]) -> None:
        """Count one finished row: a route failure extends the streak, any other row resets it."""
        reason = route_failure(record)
        if reason is None or (reason in PINNED_STREAKS and not self.routed):
            self._streak = 0
            return
        self._streak += 1
        self._last = reason
        if self.tripped is not None:
            return
        if reason == QUOTA_MARKER:
            self.tripped = (
                f"{QUOTA_MARKER}: {_quota_detail(record)}; a provider's quota, balance, spend "
                "limit or daily cap is used up"
            )
        elif reason == "no_credit":
            self.tripped = "no_credit: the account has no credit (HTTP 402); fund it and resume"
        elif self._streak >= self.limit:
            self.tripped = f"{self._streak} consecutive {reason} errors; the route is not serving"


def _quota_detail(record: Mapping[str, Any]) -> str:
    """What ran out, as the row's error names it (``quota_exhausted: <what>``)."""
    error = record.get("error")
    message = str(error.get("message") or "") if isinstance(error, Mapping) else ""
    return quota_in_text(message) or "unknown"


def _calls(row: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    result = row.get("result")
    if not isinstance(result, Mapping):
        return
    for role, key in (("generation", "generation"), ("guard", "guard_llm")):
        block = result.get(key)
        calls = block.get("provenance") if isinstance(block, Mapping) else None
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, Mapping):
                    yield role, call


def provenance_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Totals over every recorded call's provenance; None when no row carries any (not routed).

    ``fallback_attempts`` counts calls the router served after a fallback (attempt above 1) and
    ``unverified`` calls whose response named no provider: both should be zero in a pinned run,
    where each is an error row rather than a scored one.
    """
    by_provider: Counter[str] = Counter()
    by_model: Counter[str] = Counter()
    by_role: Counter[str] = Counter()
    fallback_attempts = unverified = prompt = completion = 0
    cost = 0.0
    for row in rows:
        for role, call in _calls(row):
            by_role[role] += 1
            served = call.get("served_provider")
            by_provider[str(served) if served else "unknown"] += 1
            by_model[str(call.get("requested_model") or "unknown")] += 1
            attempt = call.get("attempt")
            if isinstance(attempt, int) and attempt > 1:
                fallback_attempts += 1
            if not served:
                unverified += 1
            prompt += int(call.get("prompt_tokens") or 0)
            completion += int(call.get("completion_tokens") or 0)
            call_cost = call.get("cost")
            if isinstance(call_cost, int | float):
                cost += float(call_cost)
    if not by_role:
        return None
    return {
        "calls": sum(by_role.values()),
        "by_role": dict(by_role),
        "by_provider": dict(by_provider),
        "by_model": dict(by_model),
        "fallback_attempts": fallback_attempts,
        "unverified": unverified,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cost_usd": cost,
    }

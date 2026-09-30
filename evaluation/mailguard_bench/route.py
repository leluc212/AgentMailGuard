"""The OpenRouter route of a benchmark run: what its meta records and when the run stops.

Work package R4 (parked; ADR-0012 decision 9). A run whose model is served through OpenRouter
pins ONE provider with fallbacks off. Three things follow, all pure and testable without a
network:

- the run meta records the pin (``route_meta``), so a resume under another pin is refused;
- every call's provenance (the served provider, the attempt, the generation id, the cost) is
  read back from the result rows (``provenance_summary``);
- the run stops after three consecutive provider mismatches or "no provider" errors, and at once
  when the account has no credit (``RouteBreaker``): continuing would only fill the result file
  with error rows. Cases already recorded stay; a resume runs the rest.

A call another provider served is an error of its case (``packages/llm/client.py`` raises for
rag-email's calls; ``counting.CountingProvider`` records a violation for the guard's), never a
defence.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from packages.core.settings import LLMTiersSettings, ProviderRouting
from packages.llm.provenance import MISMATCH_MARKER, check_pinned_route

STOP_AFTER = 3
"""Consecutive provider mismatches or no-provider errors after which the run stops."""

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

    A guard commit without provider routing (1a3ef62, before this route) ignores the keys of
    ``guard_models.yaml`` it does not know: its provider would send no pin and report no provider,
    and the run would look pinned while the judges ran on whatever OpenRouter picked. So a routed
    run compares what the provider is configured to send with what the run pins, before any call.
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
    if MISMATCH_MARKER in text or "LLMProviderMismatchError" in text:
        return "provider_mismatch"
    if _NO_PROVIDER.search(text):
        return "no_provider"
    if _NO_CREDIT.search(text) and _TRANSIENT_402 not in text:
        return "no_credit"
    return None


class RouteBreaker:
    """Trips after ``limit`` route failures in a row, or at the first ``no_credit``."""

    def __init__(self, limit: int = STOP_AFTER) -> None:
        self.limit = limit
        self.tripped: str | None = None
        self._streak = 0
        self._last: str | None = None

    def observe(self, record: Mapping[str, Any]) -> None:
        """Count one finished row: a route failure extends the streak, any other row resets it."""
        reason = route_failure(record)
        if reason is None:
            self._streak = 0
            return
        self._streak += 1
        self._last = reason
        if self.tripped is not None:
            return
        if reason == "no_credit":
            self.tripped = "no_credit: the account has no credit (HTTP 402); fund it and resume"
        elif self._streak >= self.limit:
            self.tripped = f"{self._streak} consecutive {reason} errors; the route is not serving"


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

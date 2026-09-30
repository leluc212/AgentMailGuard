"""Call/token counter around the guard-side LLM provider (task 7.19, spec §4b overhead).

Wraps the provider AgentMailGuard's own ModelRegistry built, so the report can give
guard-judge calls and tokens per email apart from the generation call. It also records every
exception, because the guard's L2 stage swallows LLM failures ("heuristic result kept") and
leaves no verdict error behind. It forwards calls unchanged: no retry, no filtering, no
defence logic (ADR-0010).

One provider is shared by every case in flight (``--concurrency 2``), so the counters are
per case, not per provider: ``begin_case()`` puts a fresh tally in the current asyncio
context. ``run_cases`` runs each case in its own task (``asyncio.gather``), each task has
its own context copy, and tasks the guard spawns inside ``pipeline.run`` inherit the same
tally object. Case B starting can therefore never wipe case A's recorded errors, and A's
429 or tokens are never attributed to B.
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from evaluation.mailguard_bench.route import pin_problem
from packages.llm.provenance import MISMATCH_MARKER


@dataclass
class _Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    route_violations: list[str] = field(default_factory=list)


class CountingProvider:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._tally: contextvars.ContextVar[_Tally] = contextvars.ContextVar(
            f"guard_llm_tally_{id(self)}"
        )

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "_tally"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def begin_case(self) -> None:
        """Start a fresh tally for the case running in the current asyncio context."""
        self._tally.set(_Tally())

    def _current(self) -> _Tally:
        tally = self._tally.get(None)
        if tally is None:  # a call outside any case (e.g. a probe): count it on its own
            tally = _Tally()
            self._tally.set(tally)
        return tally

    async def generate(self, **kwargs: Any) -> Any:
        tally = self._current()
        tally.calls += 1
        try:
            result = await self.inner.generate(**kwargs)
        except Exception as exc:
            tally.errors.append(f"{type(exc).__name__}: {exc}")
            raise
        tally.input_tokens += int(getattr(result, "input_tokens", 0) or 0)
        tally.output_tokens += int(getattr(result, "output_tokens", 0) or 0)
        self._record_provenance(tally, result)
        return result

    def _record_provenance(self, tally: _Tally, result: Any) -> None:
        """Keep which provider served the call, and note a call that broke the pin.

        A violation is recorded and not raised: an exception would only make the judge stage fall
        back to its cheap verdict, and a fallback row is scored. The case executor turns a
        recorded violation into an error of the case, never a defence.
        """
        provenance = getattr(result, "provenance", None)
        call: dict[str, Any] = {}
        if provenance is not None:
            call = dict(provenance.to_dict() if hasattr(provenance, "to_dict") else provenance)
            tally.provenance.append(call)
        # The route is the wrapped provider's own pin, read on each call (a test swaps ``inner``):
        # the guard's model spec is what sent it, so it is what the served provider must match.
        routing = getattr(self.inner, "provider_routing", None)
        problem = pin_problem(routing if isinstance(routing, Mapping) else None, call)
        if problem is not None:
            tally.route_violations.append(f"{MISMATCH_MARKER}: guard judge call {problem}")

    def snapshot(self) -> dict[str, Any]:
        """This case's counts (the current context's tally)."""
        tally = self._current()
        model = getattr(self.inner, "model", None) or getattr(self.inner, "model_name", None)
        snapshot: dict[str, Any] = {
            "model": str(model or ""),
            "calls": tally.calls,
            "input_tokens": tally.input_tokens,
            "output_tokens": tally.output_tokens,
            "errors": list(tally.errors),
        }
        # Only a routed provider (OpenRouter) has these: an unrouted snapshot keeps its v1 shape.
        if tally.provenance:
            snapshot["provenance"] = list(tally.provenance)
        if tally.route_violations:
            snapshot["route_violations"] = list(tally.route_violations)
        return snapshot

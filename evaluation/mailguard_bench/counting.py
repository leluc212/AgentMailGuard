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
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)


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
        return result

    def snapshot(self) -> dict[str, Any]:
        """This case's counts (the current context's tally)."""
        tally = self._current()
        model = getattr(self.inner, "model", None) or getattr(self.inner, "model_name", None)
        return {
            "model": str(model or ""),
            "calls": tally.calls,
            "input_tokens": tally.input_tokens,
            "output_tokens": tally.output_tokens,
            "errors": list(tally.errors),
        }

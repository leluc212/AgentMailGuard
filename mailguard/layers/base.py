"""Shared layer helpers: timing, fail-closed error verdicts, text utilities."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mailguard.contracts.verdict import Finding, LayerName, LayerVerdict, Severity, ThreatType


@dataclass
class Stopwatch:
    start: float
    elapsed_ms: int = 0

    def stop(self) -> int:
        self.elapsed_ms = max(1, int((time.perf_counter() - self.start) * 1000))
        return self.elapsed_ms


@contextmanager
def timed() -> Iterator[Stopwatch]:
    sw = Stopwatch(start=time.perf_counter())
    try:
        yield sw
    finally:
        sw.stop()


@runtime_checkable
class GuardLayer(Protocol):
    """Every layer exposes a stable ``name``; the inspect signature differs per layer."""

    name: LayerName


def error_verdict(layer: LayerName, exc: BaseException, *, fail_closed: bool) -> LayerVerdict:
    """Build the verdict for an internal error. Fail-closed means HIGH severity."""
    severity = Severity.HIGH if fail_closed else Severity.NONE
    return LayerVerdict(
        layer=layer,
        severity=severity,
        score=0.9 if fail_closed else 0.0,
        decided_by="error",
        error=f"{type(exc).__name__}: {exc}",
        findings=[
            Finding(
                layer=layer,
                threat_type=ThreatType.INTERNAL_ERROR,
                severity=severity,
                score=0.9 if fail_closed else 0.0,
                detector="heuristic",
                rationale="Layer raised an exception; policy is fail-closed"
                if fail_closed
                else "Layer raised an exception; policy is fail-open",
            )
        ],
    )


_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def excerpt(text: str, start: int, end: int, radius: int = 60, limit: int = 300) -> str:
    lo, hi = max(0, start - radius), min(len(text), end + radius)
    out = text[lo:hi].replace("\n", " ")
    return out[:limit]


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) used for budgets; never for billing."""
    return max(1, len(text) // 4)

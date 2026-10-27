"""Score/threshold formatting (Requirement 15; 4.1, 4.2).

Pure helpers that render a score next to the threshold the guard compared it with, e.g.
``"0.91 / block ≥ 0.85, flag ≥ 0.50"``. Thresholds always come from the ``Thresholds`` of the
displayed Run (Requirement 15.5); these helpers never substitute defaults.

Rounding: half away from zero (``decimal.ROUND_HALF_UP``) applied to the shortest decimal
representation of the float (``repr``), so ``0.125 -> "0.13"`` and ``0.285 -> "0.29"`` as a
reader would expect, instead of binary-float artefacts.

Missing thresholds (Requirement 15.6): for the L1 verdict, which has two thresholds, only the
present ones are shown (``"0.91 / block ≥ 0.85"``); when both are absent the score is followed by
``"(no threshold)"``. A missing score renders as ``"—"``.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from services.guard_playground.core.schema import Thresholds

DASH = "—"
NO_THRESHOLD = "(no threshold)"
_TWO = Decimal("0.01")
_ONE = Decimal("0.1")


def _round(x: float, quantum: Decimal) -> Decimal:
    return Decimal(repr(float(x))).quantize(quantum, rounding=ROUND_HALF_UP)


def _missing(x: float | None) -> bool:
    return x is None or not math.isfinite(x)


def fmt_score(x: float | None) -> str:
    """Score with 2 decimals, clamped to [0, 1]; ``"—"`` when missing."""
    if _missing(x):
        return DASH
    return f"{_round(min(1.0, max(0.0, x)), _TWO):.2f}"


def fmt_threshold(x: float | None) -> str:
    """Threshold with 2 decimals; ``"—"`` when missing."""
    if _missing(x):
        return DASH
    return f"{_round(x, _TWO):.2f}"


def plain_score(score: float | None) -> str:
    """Score without a threshold (every L4 score): ``"0.40 (no threshold)"``."""
    if _missing(score):
        return DASH
    return f"{fmt_score(score)} {NO_THRESHOLD}"


def _with(score: float | None, parts: list[str]) -> str:
    if _missing(score):
        return DASH
    if not parts:
        return plain_score(score)
    return f"{fmt_score(score)} / {', '.join(parts)}"


def _part(name: str, value: float | None) -> list[str]:
    return [] if _missing(value) else [f"{name} ≥ {fmt_threshold(value)}"]


def l1_score(score: float | None, th: Thresholds) -> str:
    """L1 verdict score: ``"0.91 / block ≥ 0.85, flag ≥ 0.50"`` (Requirement 15.1)."""
    return _with(score, _part("block", th.block) + _part("flag", th.flag))


def strip_score(score: float | None, th: Thresholds) -> str:
    """Stripped segment score: ``"0.62 / strip ≥ 0.50"`` (Requirement 15.2)."""
    return _with(score, _part("strip", th.strip))


def quarantine_score(score: float | None, th: Thresholds) -> str:
    """L2 verdict or KB chunk score: ``"0.74 / quarantine ≥ 0.70"`` (Requirement 15.3)."""
    return _with(score, _part("quarantine", th.quarantine))


def layer_score(layer_name: str, score: float | None, th: Thresholds) -> str:
    """Verdict-level score for a layer row; unknown layers have no threshold."""
    if layer_name == "inbound_guard":
        return l1_score(score, th)
    if layer_name == "doc_scanner":
        return quarantine_score(score, th)
    return plain_score(score)


def pct1(x: float | None) -> str:
    """Ratio in [0, 1] as a percentage with 1 decimal: ``0.125 -> "12.5%"``."""
    if _missing(x):
        return DASH
    value = Decimal(repr(float(min(1.0, max(0.0, x))))) * 100
    return f"{value.quantize(_ONE, rounding=ROUND_HALF_UP):.1f}%"

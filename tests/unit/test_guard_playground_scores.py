"""Score/threshold formatting (task 3.4; Requirements 15.1–15.6, 4.1, 4.2)."""

from __future__ import annotations

import pytest

from services.guard_playground.core.schema import Thresholds
from services.guard_playground.core.scores import (
    fmt_score,
    fmt_threshold,
    l1_score,
    layer_score,
    pct1,
    plain_score,
    quarantine_score,
    strip_score,
)

DEFAULTS = Thresholds(block=0.85, flag=0.5, strip=0.5, quarantine=0.7)
NONE = Thresholds(block=None, flag=None, strip=None, quarantine=None)


def test_spec_examples() -> None:
    assert l1_score(0.91, DEFAULTS) == "0.91 / block ≥ 0.85, flag ≥ 0.50"
    assert strip_score(0.62, DEFAULTS) == "0.62 / strip ≥ 0.50"
    assert quarantine_score(0.74, DEFAULTS) == "0.74 / quarantine ≥ 0.70"
    assert plain_score(0.4) == "0.40 (no threshold)"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.125, "0.13"),
        (0.005, "0.01"),
        (0.004999, "0.00"),
        (0.285, "0.29"),
        (0.0, "0.00"),
        (1.0, "1.00"),
        (1.7, "1.00"),
        (-0.2, "0.00"),
        (None, "—"),
        (float("nan"), "—"),
    ],
)
def test_fmt_score_rounds_half_up_and_clamps(value: float | None, expected: str) -> None:
    assert fmt_score(value) == expected


def test_fmt_threshold() -> None:
    assert fmt_threshold(0.5) == "0.50"
    assert fmt_threshold(0.125) == "0.13"
    assert fmt_threshold(None) == "—"


def test_missing_thresholds() -> None:
    assert l1_score(0.91, NONE) == "0.91 (no threshold)"
    assert strip_score(0.62, NONE) == "0.62 (no threshold)"
    assert quarantine_score(0.74, NONE) == "0.74 (no threshold)"
    # Only one L1 threshold present: show the present one.
    assert l1_score(0.91, NONE.model_copy(update={"flag": 0.5})) == "0.91 / flag ≥ 0.50"
    assert l1_score(0.91, NONE.model_copy(update={"block": 0.85})) == "0.91 / block ≥ 0.85"


def test_missing_score() -> None:
    assert strip_score(None, DEFAULTS) == "—"
    assert l1_score(None, DEFAULTS) == "—"
    assert plain_score(None) == "—"


def test_layer_score_dispatch() -> None:
    assert layer_score("inbound_guard", 0.91, DEFAULTS) == "0.91 / block ≥ 0.85, flag ≥ 0.50"
    assert layer_score("doc_scanner", 0.74, DEFAULTS) == "0.74 / quarantine ≥ 0.70"
    assert layer_score("output_guard", 0.4, DEFAULTS) == "0.40 (no threshold)"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.125, "12.5%"),
        (1 / 3, "33.3%"),
        (0.00125, "0.1%"),
        (0.0, "0.0%"),
        (1.0, "100.0%"),
        (None, "—"),
    ],
)
def test_pct1(value: float | None, expected: str) -> None:
    assert pct1(value) == expected

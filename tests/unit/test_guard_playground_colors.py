"""Outcome_Banner colours (decision D11; Requirements 12.4, 10.7).

Reads the real playground.css and checks every banner's text/background contrast is at least
4.5:1 (WCAG relative luminance) and that no green background is used.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import services.guard_playground as pg

CSS = (Path(pg.__file__).resolve().parent / "static" / "playground.css").read_text(encoding="utf-8")
EXPECTED = {
    "block": "#8b0000",
    "quarantine": "#4a148c",
    "review": "#e66100",
    "draft": "#1a5fb4",
    "send": "#006d77",
    "none": "#eeeeee",
}


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rule(name: str) -> tuple[str, str]:
    match = re.search(rf"\.banner--{name}\s*\{{([^}}]*)\}}", CSS)
    assert match, f"missing .banner--{name}"
    body = match.group(1)
    bg = re.search(r"background:\s*(#[0-9a-fA-F]{6})", body)
    fg = re.search(r"(?<!-)color:\s*(#[0-9a-fA-F]{6})", body)
    assert bg and fg
    return bg.group(1).lower(), fg.group(1).lower()


@pytest.mark.parametrize("name", list(EXPECTED))
def test_banner_palette_and_contrast(name: str) -> None:
    background, text = _rule(name)
    assert background == EXPECTED[name]
    assert contrast(background, text) >= 4.5


@pytest.mark.parametrize(
    ("width", "rule"),
    [
        ("1100px", ".pg-layout { grid-template-columns: minmax(0, 1fr); }"),
        ("900px", ".columns--2 { grid-template-columns: minmax(0, 1fr); }"),
        ("700px", ".stepper { flex-direction: column;"),
    ],
)
def test_zoom_breakpoints_stack_the_layout(width: str, rule: str) -> None:
    """D15 / Requirement 10.8: browser zoom reflows instead of scrolling sideways."""
    blocks = re.findall(rf"@media \(max-width: {width}\) \{{(.*?)\n\}}", CSS, re.S)
    blocks += re.findall(rf"@media \(max-width: {width}\) \{{([^\n]*)\}}", CSS)
    assert any(rule in block for block in blocks), f"missing {rule!r} at max-width {width}"


def test_known_contrast_values() -> None:
    assert contrast("#ffffff", "#000000") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)

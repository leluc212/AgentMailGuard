"""Demo_Tour definition and validation (Requirement 17.1, 17.2, 17.8)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.guard_playground.core.cases import CaseCatalog
from services.guard_playground.core.schema import PRESETS

MODES = ("replay", "live")


@dataclass(frozen=True)
class TourStep:
    number: int
    case_id: str | None
    subject: str
    body: str
    presets: tuple[str, ...]
    mode: str
    talking_point: str


@dataclass
class Tour:
    steps: list[TourStep]
    error: str | None = None  # "Step k: <rule>" or the step count when out of range

    @property
    def valid(self) -> bool:
        return self.error is None and bool(self.steps)


def _check_step(n: int, raw: Any, catalog: CaseCatalog) -> TourStep | str:
    if not isinstance(raw, dict):
        return f"Step {n}: not an object"
    point = raw.get("talking_point")
    if not isinstance(point, str) or not point.strip():
        return f"Step {n}: missing talking point"
    if len(point) > 200:
        return f"Step {n}: talking point longer than 200 characters"
    mode = raw.get("mode")
    if mode not in MODES:
        return f"Step {n}: missing or unknown mode"
    presets = raw.get("presets")
    if not isinstance(presets, list) or len(presets) not in (1, 2):
        return f"Step {n}: presets must be one preset or a pair"
    if any(p not in PRESETS for p in presets):
        return f"Step {n}: unknown preset"
    source = raw.get("input")
    if not isinstance(source, dict):
        return f"Step {n}: missing input"
    if "case_id" in source:
        case = catalog.get(str(source["case_id"]))
        if case is None:
            return f"Step {n}: unknown case id"
        return TourStep(n, case.case_id, case.subject, case.body, tuple(presets), mode, point)
    custom = source.get("custom")
    if isinstance(custom, dict):
        subject, body = str(custom.get("subject", "")), str(custom.get("body", ""))
        if not 1 <= len(subject) <= 200 or not 1 <= len(body) <= 5000:
            return f"Step {n}: custom email subject 1-200 and body 1-5,000 characters"
        return TourStep(n, None, subject, body, tuple(presets), mode, point)
    return f"Step {n}: missing input"


def load_tour(data_dir: Path, catalog: CaseCatalog) -> Tour:
    """Validate ``demo_tour.json``; an invalid definition hides the tour (Requirement 17.8)."""
    try:
        raw = json.loads((data_dir / "demo_tour.json").read_text(encoding="utf-8"))
        steps_raw = raw["steps"]
        assert isinstance(steps_raw, list)
    except (OSError, ValueError, KeyError, AssertionError, TypeError):
        return Tour([], "demo_tour.json is missing or invalid")
    if not 4 <= len(steps_raw) <= 6:
        return Tour([], f"the tour has {len(steps_raw)} steps; it needs 4 to 6")
    steps: list[TourStep] = []
    for n, item in enumerate(steps_raw, start=1):
        checked = _check_step(n, item, catalog)
        if isinstance(checked, str):
            return Tour([], checked)
        steps.append(checked)
    return Tour(steps)

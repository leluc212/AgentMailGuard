"""Pipeline_Stepper — Stage_Status per Run (Requirement 13; design.md "Stage_Status").

``stages(r)`` is a pure function from a ``RunResult`` to the 7 stages shown left to right.
It does not import ``mailguard``; the preset table mirrors ``mailguard.pipeline.GuardConfig``.

Decisions beyond the design pseudo-code (kept consistent with Property 3):

* The stop stage is L4 when the outbound decision blocks/quarantines, else gate pass 1 when
  ``gate_passes == 1``, else gate pass 2. If that stage is disabled by the preset (an
  inconsistent Run the real guard cannot produce) no stage is shown as "stopped here".
* Gate pass 2 shows "passed" when ``gate_passes == 2``. When the gate ran a single pass
  (``gate_passes != 2``, e.g. no chunks) and the Run did not stop at pass 1, pass 2 shows
  "skipped" with the note "single pass (no chunks)"; if the Run stops later (at L4) it shows
  "passed" with that note instead, so nothing left of "stopped here" is "skipped".
* With no stop, a layer stage whose row has ``ran=False``, Generation without a draft, and
  Channel isolation that was not applied show "skipped". Left of a stop they fall back to
  "passed" (with a note) so nothing left of "stopped here" is "skipped".
* L2 with zero scanned chunks shows "passed" + "0 chunks scanned" (13.11), taking precedence
  over severity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.guard_playground.core.schema import LayerName, LayerRow, RunResult

StageKey = Literal["L1", "G1", "L2", "G2", "ISO", "GEN", "L4"]
StageStatus = Literal["passed", "flagged", "stopped here", "skipped", "disabled by preset"]

STAGE_ORDER: tuple[StageKey, ...] = ("L1", "G1", "L2", "G2", "ISO", "GEN", "L4")
STAGE_LABELS: dict[StageKey, str] = {
    "L1": "L1 Inbound Guard",
    "G1": "L3 Inbound Gate (pass 1)",
    "L2": "L2 Doc Scanner",
    "G2": "L3 Inbound Gate (pass 2)",
    "ISO": "Channel isolation",
    "GEN": "Generation",
    "L4": "L4 Output Guard",
}
STATUS_ICONS: dict[StageStatus, str] = {
    "passed": "✓",
    "flagged": "!",
    "stopped here": "■",
    "skipped": "–",
    "disabled by preset": "∅",
}
STAGE_LAYER: dict[StageKey, LayerName] = {
    "L1": "inbound_guard",
    "L2": "doc_scanner",
    "L4": "output_guard",
}

_ALL = frozenset(
    {"inbound_guard", "doc_scanner", "inbound_gate", "output_guard", "channel_isolation"}
)
# Components enabled per preset (mirrors mailguard.pipeline.GuardConfig presets).
PRESET_COMPONENTS: dict[str, frozenset[str]] = {
    "C0": frozenset(),
    "C1": frozenset({"inbound_guard", "inbound_gate"}),
    "C2": frozenset({"inbound_guard", "doc_scanner", "inbound_gate", "channel_isolation"}),
    "C3": _ALL,
    "C3-L1": _ALL - {"inbound_guard"},
    "C3-L2": _ALL - {"doc_scanner"},
    "C3-L3": _ALL - {"inbound_gate"},
    "C3-L4": _ALL - {"output_guard"},
    "C3-ISO": _ALL - {"channel_isolation"},
}
_STAGE_COMPONENT: dict[StageKey, str | None] = {
    "L1": "inbound_guard",
    "G1": "inbound_gate",
    "L2": "doc_scanner",
    "G2": "inbound_gate",
    "ISO": "channel_isolation",
    "GEN": None,  # always enabled (C0 still generates a draft)
    "L4": "output_guard",
}
_STOPPING = frozenset({"block", "quarantine"})


@dataclass(frozen=True)
class StageView:
    key: StageKey
    label: str
    status: StageStatus
    icon: str
    note: str | None
    layer: LayerName | None  # links the stage to its layer section, if any


def stage_enabled(preset: str, key: StageKey) -> bool:
    component = _STAGE_COMPONENT[key]
    return component is None or component in PRESET_COMPONENTS.get(preset, frozenset())


def stop_stage(r: RunResult) -> StageKey | None:
    """The stage shown as "stopped here", or None."""
    final = r.final
    if final is None or final.action not in _STOPPING:
        return None
    key: StageKey
    if r.outbound is not None and r.outbound.action in _STOPPING:
        key = "L4"
    elif r.gate_passes == 1:
        key = "G1"
    else:
        key = "G2"
    return key if stage_enabled(r.preset, key) else None


def _row(r: RunResult, layer: LayerName) -> LayerRow:
    return next(row for row in r.layers if row.layer == layer)


def _status(r: RunResult, key: StageKey, stop: StageKey | None) -> tuple[StageStatus, str | None]:
    if not stage_enabled(r.preset, key):
        return "disabled by preset", None
    if stop is not None:
        if STAGE_ORDER.index(key) > STAGE_ORDER.index(stop):
            return "skipped", None
        if key == stop:
            return "stopped here", None
    no_stop = stop is None

    if key in STAGE_LAYER:
        row = _row(r, STAGE_LAYER[key])
        if not row.ran:
            reason = row.not_run_reason or "not run"
            return ("skipped", reason) if no_stop else ("passed", reason)
        if key == "L2" and not r.chunks:
            return "passed", "0 chunks scanned"
        flagged = (
            row.severity != "none"
            or (key == "L1" and bool(r.stripped))
            or (key == "L2" and any(c.quarantined for c in r.chunks))
        )
        return ("flagged" if flagged else "passed"), None
    if key == "G2" and r.gate_passes != 2:
        return ("skipped" if no_stop else "passed"), "single pass (no chunks)"
    if key == "ISO" and not r.isolation_applied:
        return ("skipped", None) if no_stop else ("passed", "not applied")
    if key == "GEN" and not r.draft_generated and no_stop:
        return "skipped", None
    return "passed", None


def stages(r: RunResult) -> list[StageView]:
    """The 7 Pipeline_Stepper stages of ``r``, left to right (Requirement 13)."""
    stop = stop_stage(r)
    views: list[StageView] = []
    for key in STAGE_ORDER:
        status, note = _status(r, key, stop)
        views.append(
            StageView(
                key=key,
                label=STAGE_LABELS[key],
                status=status,
                icon=STATUS_ICONS[status],
                note=note,
                layer=STAGE_LAYER.get(key),
            )
        )
    return views

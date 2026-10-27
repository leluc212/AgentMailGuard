"""Pipeline_Stepper Stage_Status (task 3.2; Requirement 13, Property 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from services.guard_playground.core.schema import ACTIONS, LAYER_ORDER, PRESETS, RunResult
from services.guard_playground.core.stepper import (
    PRESET_COMPONENTS,
    STAGE_LABELS,
    STAGE_ORDER,
    STATUS_ICONS,
    stages,
)


def _layer(name: str, *, ran: bool = True, severity: str = "none", reason: str | None = None):
    return {
        "layer": name,
        "ran": ran,
        "not_run_reason": reason,
        "severity": severity,
        "score": 0.0,
        "decided_by": "rules",
        "latency_ms": 1,
        "findings": [],
        "error": None,
        "fallback_reason": None,
        "judge_configured": False,
    }


def _decision(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "matched_rule_id": "R1",
        "risk_tier": "high",
        "requires_human": action == "human_approval",
    }


def _chunk(quarantined: bool = False) -> dict[str, Any]:
    return {
        "chunk_id": "c1",
        "content": "Refund policy: 30 days.",
        "score": 0.1,
        "quarantined": quarantined,
        "reason_rule_id": None,
        "reason_technique": None,
    }


def _run(**overrides: Any) -> RunResult:
    data: dict[str, Any] = {
        "generated_at": datetime(2026, 10, 26, 14, 5, tzinfo=UTC),
        "mode": "replay",
        "source": "pinned",
        "case_id": "case-001",
        "preset": "C3",
        "draft_model": "gpt-4o-mini",
        "guard_models": {},
        "email": {
            "subject": "s",
            "body": "b",
            "chunks": [{"chunk_id": "c1", "content": "Refund policy: 30 days."}],
            "source": "pinned",
            "case_id": "case-001",
        },
        "layers": [_layer(n) for n in LAYER_ORDER],
        "user_intent": None,
        "stripped": [],
        "removed_ratio": 0.0,
        "chunks": [_chunk()],
        "quarantine_ratio": 0.0,
        "inbound": _decision("draft_only"),
        "outbound": _decision("draft_only"),
        "gate_passes": 2,
        "isolation_applied": True,
        "draft_generated": True,
        "draft_body": "Thanks.",
        "guarded_prompt": "<email>...</email>",
        "thresholds": {"block": 0.85, "flag": 0.5, "strip": 0.5, "quarantine": 0.7},
    }
    data.update(overrides)
    return RunResult.model_validate(data)


def _statuses(r: RunResult) -> list[str]:
    return [s.status for s in stages(r)]


def test_labels_icons_and_layer_links() -> None:
    views = stages(_run())
    assert [v.label for v in views] == [
        "L1 Inbound Guard",
        "L3 Inbound Gate (pass 1)",
        "L2 Doc Scanner",
        "L3 Inbound Gate (pass 2)",
        "Channel isolation",
        "Generation",
        "L4 Output Guard",
    ]
    assert [v.key for v in views] == list(STAGE_ORDER)
    assert [v.layer for v in views] == [
        "inbound_guard",
        None,
        "doc_scanner",
        None,
        None,
        None,
        "output_guard",
    ]
    assert len(set(STATUS_ICONS.values())) == 5
    assert all(v.icon == STATUS_ICONS[v.status] for v in views)


def test_c0_everything_disabled_except_generation() -> None:
    r = _run(
        preset="C0",
        layers=[_layer(n, ran=False, reason="disabled by preset") for n in LAYER_ORDER],
        outbound=None,
        gate_passes=None,
        isolation_applied=False,
    )
    assert _statuses(r) == ["disabled by preset"] * 5 + ["passed", "disabled by preset"]


def test_c3_stop_at_gate_pass_1() -> None:
    r = _run(
        layers=[
            _layer("inbound_guard", severity="high"),
            _layer("doc_scanner", ran=False, reason="stopped by policy"),
            _layer("output_guard", ran=False, reason="stopped by policy"),
        ],
        stripped=[{"text": "ignore previous instructions", "reason": "INJ", "score": 0.9}],
        inbound=_decision("block"),
        outbound=None,
        gate_passes=1,
        isolation_applied=False,
        draft_generated=False,
        draft_body=None,
    )
    assert _statuses(r) == ["flagged", "stopped here"] + ["skipped"] * 5


def test_c3_reaches_l4_benign() -> None:
    r = _run(layers=[_layer(n) for n in LAYER_ORDER])
    assert _statuses(r) == ["passed"] * 7


def test_doc_scanner_low_severity_or_quarantined_chunk_is_flagged() -> None:
    l2_low = [
        _layer("inbound_guard"),
        _layer("doc_scanner", severity="low"),
        _layer("output_guard"),
    ]
    low = _run(layers=l2_low)
    assert _statuses(low)[2] == "flagged"
    quarantined = _run(chunks=[_chunk(quarantined=True)])
    assert _statuses(quarantined)[2] == "flagged"


def test_outbound_block_stops_at_l4() -> None:
    l4_high = [
        _layer("inbound_guard"),
        _layer("doc_scanner"),
        _layer("output_guard", severity="high"),
    ]
    r = _run(layers=l4_high, outbound=_decision("block"))
    assert _statuses(r) == ["passed"] * 6 + ["stopped here"]


def test_c3_l3_gate_disabled() -> None:
    r = _run(preset="C3-L3", gate_passes=None)
    assert _statuses(r) == [
        "passed",
        "disabled by preset",
        "passed",
        "disabled by preset",
        "passed",
        "passed",
        "passed",
    ]


def test_zero_chunks_note_and_single_gate_pass() -> None:
    r = _run(chunks=[], gate_passes=1)
    views = stages(r)
    assert views[2].status == "passed" and views[2].note == "0 chunks scanned"
    assert views[3].status == "skipped" and views[3].note == "single pass (no chunks)"
    # A later stop at L4 must not leave G2 "skipped" on its left.
    blocked = stages(_run(chunks=[], gate_passes=1, outbound=_decision("quarantine")))
    assert blocked[3].status == "passed"
    assert blocked[6].status == "stopped here"


# --------------------------------------------------------------------------- Property 3

_STOP = ("block", "quarantine")
_NON_STOP = tuple(a for a in ACTIONS if a not in _STOP)


def _decision_st(allow_stop: bool) -> st.SearchStrategy[dict[str, Any] | None]:
    actions = ACTIONS if allow_stop else _NON_STOP
    return st.none() | st.sampled_from(actions).map(_decision)


_layer_st = st.builds(
    lambda name, ran, sev, reason: _layer(name, ran=ran, severity=sev, reason=reason),
    st.just("x"),
    st.booleans(),
    st.sampled_from(("none", "low", "medium", "high")),
    st.sampled_from((None, "disabled by preset", "stopped by policy")),
)


@st.composite
def _consistent_run(draw: st.DrawFn) -> RunResult:
    """Random RunResults where a blocking decision only comes from an enabled stage."""
    preset = draw(st.sampled_from(PRESETS))
    components = PRESET_COMPONENTS[preset]
    layers = []
    for name in LAYER_ORDER:
        row = draw(_layer_st)
        row["layer"] = name
        layers.append(row)
    return _run(
        preset=preset,
        layers=layers,
        stripped=draw(st.lists(st.just({"text": "x", "reason": "r", "score": None}), max_size=2)),
        chunks=draw(st.lists(st.booleans().map(_chunk), max_size=3)),
        inbound=draw(_decision_st("inbound_gate" in components)),
        outbound=draw(_decision_st("output_guard" in components)),
        gate_passes=draw(st.none() | st.integers(min_value=0, max_value=3)),
        isolation_applied=draw(st.booleans()),
        draft_generated=draw(st.booleans()),
    )


@settings(max_examples=200, deadline=None)
@given(_consistent_run())
def test_property_3_stepper_consistent(r: RunResult) -> None:
    """Feature: guard-playground, Property 3: Stepper nhất quán.

    **Validates: Requirements 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.10**
    """
    views = stages(r)
    statuses = [v.status for v in views]
    assert [v.label for v in views] == [STAGE_LABELS[k] for k in STAGE_ORDER]
    assert all(s in STATUS_ICONS for s in statuses)
    stops = statuses.count("stopped here")
    blocking = r.final is not None and r.final.action in _STOP
    assert stops == (1 if blocking else 0)
    if stops:
        assert "skipped" not in statuses[: statuses.index("stopped here")]
    assert statuses[STAGE_ORDER.index("GEN")] != "disabled by preset"

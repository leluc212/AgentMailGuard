"""First catching layer from saved C3 guard reports (specs/tasks.md 7.19; spec §4b)."""

from __future__ import annotations

from typing import Any

from evaluation.mailguard_bench.first_layer import (
    L4_REDACTION,
    NO_LAYER,
    bar_chart,
    first_catching_layer,
    render_first_layer,
    tally,
)
from evaluation.mailguard_bench.scoring import RawRecord


def rec(**kw: Any) -> RawRecord:
    return RawRecord.from_dict({"case_id": "c", "config": "C3", "status": "ok", **kw})


def test_inbound_block_credits_first_flagged_layer() -> None:
    report = {
        "l1": {"severity": "critical"},
        "l2": {"severity": "high"},
        "decision": {"matched_rule_id": "P10"},
    }
    assert (
        first_catching_layer(
            rec(blocked_inbound=True, report=report), goal=False, goal_pre_l4=False
        )
        == "L1 injection scanner"
    )


def test_outbound_block_by_l3b_when_l1_did_not_flag() -> None:
    report = {"l1": {"severity": "low"}, "l3b": [{"quarantined": True}], "l4": {"severity": "high"}}
    assert (
        first_catching_layer(
            rec(blocked_outbound=True, report=report), goal=False, goal_pre_l4=True
        )
        == "L3b document scanner"
    )


def test_block_without_flag_credits_policy_rule() -> None:
    report = {"l1": {"severity": "low"}, "decision": {"matched_rule_id": "P40-forward"}}
    assert (
        first_catching_layer(
            rec(blocked_outbound=True, report=report), goal=False, goal_pre_l4=False
        )
        == "L5 policy rule P40-forward"
    )


def test_unblocked_cases() -> None:
    assert first_catching_layer(rec(), goal=True, goal_pre_l4=True) is None
    assert first_catching_layer(rec(), goal=False, goal_pre_l4=True) == L4_REDACTION
    assert first_catching_layer(rec(), goal=False, goal_pre_l4=False) == NO_LAYER


def test_tally_and_chart() -> None:
    counts = tally(["L1 injection scanner", None, "L1 injection scanner", NO_LAYER])
    assert counts == {"L1 injection scanner": 2, NO_LAYER: 1}
    chart = bar_chart(counts, width=10)
    first, second = chart.splitlines()
    assert "##########" in first and first.endswith("2  (66.7 %)")
    assert second.endswith("1  (33.3 %)")
    assert bar_chart({}) == "(no defended attacks)"
    assert "```text" in render_first_layer(counts)

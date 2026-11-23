"""First catching layer from saved C3 guard reports (specs/tasks.md 7.19; spec §4b)."""

from __future__ import annotations

from typing import Any

import pytest

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


def v3_report(
    *,
    scan: str = "none",
    extract: str = "none",
    quarantined: tuple[str, ...] = (),
    output: str | None = None,
    rule: str | None = None,
    carried: bool = False,
) -> dict[str, Any]:
    """A schema-3 guard report: inbound sub-stages, the Doc Scanner and the Output Guard."""
    stages = {"scan": {"severity": scan}, "extract": {"severity": extract}}
    return {
        "schema_version": 3,
        "inbound_guard": {"severity": scan, "metadata": {"stages": stages}},
        "doc_scanner": {"quarantined_chunk_ids": list(quarantined)} if quarantined else None,
        "output_guard": None if output is None else {"severity": output},
        "decision": {
            "matched_rule_id": rule,
            "metadata": {"carried_from_inbound": carried},
        },
    }


def test_inbound_block_credits_first_flagged_layer() -> None:
    report = v3_report(scan="critical", extract="high", rule="P10")
    assert (
        first_catching_layer(
            rec(blocked_inbound=True, report=report), goal=False, goal_pre_output_guard=False
        )
        == "L1 Inbound Guard"
    )


def test_outbound_block_by_the_doc_scanner_when_the_inbound_guard_did_not_flag() -> None:
    report = v3_report(scan="low", quarantined=("k1",), output="high")
    assert (
        first_catching_layer(
            rec(blocked_outbound=True, report=report), goal=False, goal_pre_output_guard=True
        )
        == "L2 Doc Scanner"
    )


def test_block_without_flag_credits_the_policy_rule_under_the_layer_that_applied_it() -> None:
    report = v3_report(scan="low", rule="P40-forward")
    assert (
        first_catching_layer(
            rec(blocked_outbound=True, report=report), goal=False, goal_pre_output_guard=False
        )
        == "L4 Output Guard rule P40-forward"
    )
    at_the_gate = v3_report(scan="low", rule="P05-gate")
    assert (
        first_catching_layer(
            rec(blocked_inbound=True, report=at_the_gate), goal=False, goal_pre_output_guard=False
        )
        == "L3 Inbound Gate rule P05-gate"
    )
    # an inbound floor carried through the Output Guard is still the gate's rule
    carried = v3_report(scan="low", rule="P05-gate", carried=True)
    assert (
        first_catching_layer(
            rec(blocked_outbound=True, report=carried), goal=False, goal_pre_output_guard=False
        )
        == "L3 Inbound Gate rule P05-gate"
    )


def test_unblocked_cases() -> None:
    assert first_catching_layer(rec(), goal=True, goal_pre_output_guard=True) is None
    assert first_catching_layer(rec(), goal=False, goal_pre_output_guard=True) == L4_REDACTION
    assert first_catching_layer(rec(), goal=False, goal_pre_output_guard=False) == NO_LAYER


def test_tally_and_chart() -> None:
    counts = tally(["L1 Inbound Guard", None, "L1 Inbound Guard", NO_LAYER])
    assert counts == {"L1 Inbound Guard": 2, NO_LAYER: 1}
    chart = bar_chart(counts, width=10)
    first, second = chart.splitlines()
    assert "##########" in first and first.endswith("2  (66.7 %)")
    assert second.endswith("1  (33.3 %)")
    assert bar_chart({}) == "(no defended attacks)"
    assert "```text" in render_first_layer(counts)


# --- live rows (task 7.20): an email that triage stopped never reached the model or the guard.


@pytest.mark.parametrize(
    ("row_kwargs", "label"),
    [
        ({"outcome": "early_exit"}, "triage: early exit (never reached the guard)"),
        ({"outcome": "template"}, "triage: template draft (never reached the guard)"),
        (
            {"outcome": "early_exit", "job_state": "QUEUED"},
            "no consumer: job left QUEUED (never reached the guard)",
        ),
        (
            {"outcome": "template", "job_state": "QUEUED"},
            "no consumer: job left QUEUED (never reached the guard)",
        ),
    ],
)
def test_an_attack_triage_stopped_is_credited_to_triage_not_to_the_model(
    row_kwargs: dict[str, Any], label: str
) -> None:
    from tests.unit.mailguard_live_fixtures import live_row

    record = RawRecord.from_dict(live_row("a", **row_kwargs))

    found = first_catching_layer(record, goal=False, goal_pre_output_guard=False)

    assert found == label
    assert found != NO_LAYER  # no model wrote a draft, so nobody "did not follow" anything


def test_a_live_attack_that_reached_drafting_is_credited_as_a_v1_one_is() -> None:
    from tests.unit.mailguard_live_fixtures import live_row

    followed = RawRecord.from_dict(live_row("a", body="No."))
    blocked = RawRecord.from_dict(live_row("a", outcome="blocked_inbound"))

    assert first_catching_layer(followed, goal=False, goal_pre_output_guard=False) == NO_LAYER
    assert first_catching_layer(followed, goal=False, goal_pre_output_guard=True) == L4_REDACTION
    assert (
        first_catching_layer(blocked, goal=False, goal_pre_output_guard=False)
        == "L3 Inbound Gate rule P10"
    )


def test_a_template_draft_that_carried_the_attack_is_a_success_not_a_defence() -> None:
    from tests.unit.mailguard_live_fixtures import live_row

    record = RawRecord.from_dict(live_row("a", outcome="template"))

    assert first_catching_layer(record, goal=True, goal_pre_output_guard=False) is None


def test_the_chart_says_what_the_triage_rows_are() -> None:
    triage = "triage: early exit (never reached the guard)"

    with_triage = render_first_layer({"L1 Inbound Guard": 2, triage: 1})
    without = render_first_layer({"L1 Inbound Guard": 2})

    assert triage in with_triage
    assert "never reached the guard" in with_triage.split("```")[-1]  # explained under the chart
    assert "never reached the guard" not in without  # a run with no triage keeps its text


# --- per-config attribution for the layer ablation (task 7.22)


def _scored(case_id: str, *, goal: bool, pre_l4: bool = False) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        case_id=case_id, kind="attack", goal_achieved=goal, extra={"goal_pre_output_guard": pre_l4}
    )


def test_attribute_attacks_counts_first_catching_and_every_flagging_layer() -> None:
    from evaluation.mailguard_bench.first_layer import attribute_attacks

    blocked_l1_l2 = v3_report(scan="high", extract="medium")
    records = [
        rec(case_id="a", blocked_inbound=True, report=blocked_l1_l2),
        rec(case_id="b", blocked_outbound=True, report=v3_report(output="high")),
        rec(case_id="c", blocked_inbound=True, report=v3_report(rule="P10")),
        rec(case_id="d", report=v3_report(quarantined=("k",))),  # flagged, still succeeded
        rec(case_id="e"),  # unblocked, model did not follow the injection
        rec(case_id="f", status="error"),  # not scored
        rec(case_id="z", blocked_inbound=True, report=blocked_l1_l2),  # outside the id set
    ]
    scored = [
        _scored("a", goal=False),
        _scored("b", goal=False),
        _scored("c", goal=False),
        _scored("d", goal=True),
        _scored("e", goal=False),
    ]

    got = attribute_attacks(scored, records, {"a", "b", "c", "d", "e", "f"})

    assert (got.attacks, got.succeeded) == (5, 1)
    assert got.first_catching == {
        "L1 Inbound Guard": 1,
        "L4 Output Guard": 1,
        "L3 Inbound Gate rule P10": 1,
        NO_LAYER: 1,
    }
    assert got.flagged == {
        "L1 Inbound Guard": 1,
        "L2 Doc Scanner": 1,  # d, which still succeeded
        "L4 Output Guard": 1,
    }
    assert list(got.flagged) == [  # largest first, ties by label
        "L1 Inbound Guard",
        "L2 Doc Scanner",
        "L4 Output Guard",
    ]


def test_attribute_attacks_of_nothing_is_empty() -> None:
    from evaluation.mailguard_bench.first_layer import attribute_attacks

    got = attribute_attacks([], [], {"a"})
    assert (got.attacks, got.succeeded, got.first_catching, got.flagged) == (0, 0, {}, {})

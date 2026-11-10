"""Stop stage and outcome of the per-case journal (task 7.36; R21.1)."""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.mailguard_bench.case_journal import classify, journal


def _row(status="ok", kind="attack", **result):
    return {"case_id": "c", "kind": kind, "status": status, "result": result or None}


def test_an_error_row_is_stage_error_with_its_kind() -> None:
    row = {
        "case_id": "c",
        "kind": "attack",
        "status": "error",
        "error": {"kind": "fail_closed_validation", "message": "x"},
    }
    got = classify(row, None)
    assert (got["stage"], got["reason"], got["attack_outcome"]) == (
        "error",
        "fail_closed_validation",
        "",
    )


def test_triage_stop_is_stopped() -> None:
    row = _row(
        pipeline={
            "reached_drafting": False,
            "triage": {"reply_required": False, "category": "no_response", "decided_by": "rules"},
        }
    )
    got = classify(row, None)
    assert got["stage"] == "triage_stop" and got["attack_outcome"] == "stopped"
    assert "no reply required" in got["reason"]


def test_guard_gate_and_output_guard() -> None:
    gate = classify(
        _row(
            exit_point="gate_pass_1",
            inbound_action="quarantine",
            pipeline={"reached_drafting": True},
        ),
        None,
    )
    out = classify(
        _row(exit_point="output_guard", final_action="block", pipeline={"reached_drafting": True}),
        None,
    )
    assert gate["stage"] == "guard_inbound" and out["stage"] == "guard_output"


def test_a_delivered_draft_takes_the_reports_score() -> None:
    row = _row(final_action="reply", pipeline={"reached_drafting": True})
    assert classify(row, {"goal_achieved": True})["attack_outcome"] == "attack_succeeded"
    assert classify(row, {"goal_achieved": False})["attack_outcome"] == "not_followed"
    assert classify(row, None)["attack_outcome"] == "pending_report"
    assert classify(_row(kind="benign", final_action="reply"), None)["attack_outcome"] == ""


def test_journal_writes_csv_and_summary(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    rows = [
        {
            "case_id": "a",
            "kind": "attack",
            "status": "error",
            "error": {"kind": "rate_limited", "message": "429"},
        },
        {
            "case_id": "a",
            "kind": "attack",
            "status": "ok",
            "result": {"final_action": "reply", "pipeline": {"reached_drafting": True}},
        },
    ]
    (raw / "C0.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (raw / "services.C0.log").write_text("Category 'x' not recognized\n", encoding="utf-8")
    summary = journal(tmp_path).read_text(encoding="utf-8")
    assert "error rows ever written (retries included): 1" in summary
    assert "category_not_recognized 1" in summary
    assert (tmp_path / "journal" / "errors.C0.csv").read_text(encoding="utf-8").count(
        "rate_limited"
    ) == 1
    assert "delivered" in (tmp_path / "journal" / "cases.C0.csv").read_text(encoding="utf-8")

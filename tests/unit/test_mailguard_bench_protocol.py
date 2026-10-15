"""Where each job stopped and why: the protocol facts of a schema-3 report (plan_v3 Task 13).

A fixed synthetic set of per-case records covers all four exit points and two fail-closed cases;
the facts and the per-config tables are asserted exactly. Pure: no guard install, no model.
"""

from __future__ import annotations

from typing import Any

import pytest

from evaluation.mailguard_bench.protocol import (
    CaseProtocol,
    carried_from_inbound,
    case_protocol,
    detected_doc_chunk_ids,
    exit_point,
    fail_closed_quarantined_chunk_ids,
    guard_calls,
    judge_invalid_events,
    protocol_table,
)
from evaluation.mailguard_bench.scoring import RawRecord

CALLS = ("inbound_scan", "inbound_extract", "doc_chunk", "doc_set", "output")


def calls(**counts: int) -> dict[str, int]:
    """``report.metadata["guard_calls"]``: every stage listed, as the guard writes it."""
    return {stage: counts.get(stage, 0) for stage in CALLS}


def decision(action: str, rule: str = "P99-default", **metadata: Any) -> dict[str, Any]:
    return {"action": action, "matched_rule_id": rule, "metadata": metadata}


def report(
    *,
    inbound: dict[str, Any] | None = None,
    final: dict[str, Any] | None = None,
    guard_calls_: dict[str, int] | None = None,
    **parts: Any,
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "inbound_decision": inbound,
        "decision": final if final is not None else inbound,
        "metadata": {"guard_calls": guard_calls_ if guard_calls_ is not None else calls()},
        **parts,
    }


# --- the exit point ------------------------------------------------------------------------


def test_a_block_before_any_model_call_exits_at_the_gate_pass_that_decided() -> None:
    pass_1 = report(inbound=decision("block", gate_passes=1))
    pass_2 = report(inbound=decision("quarantine", gate_passes=2))

    assert exit_point(pass_1, generation_called=False) == "gate_pass_1"
    assert exit_point(pass_2, generation_called=False) == "gate_pass_2"


def test_a_job_that_passed_the_gate_and_kept_a_draft_is_delivered() -> None:
    drafted = report(inbound=decision("draft_only", gate_passes=2), final=decision("draft_only"))
    sent = report(inbound=decision("draft_only", gate_passes=1), final=decision("auto_send"))

    assert exit_point(drafted, generation_called=True) == "delivered"
    assert exit_point(sent, generation_called=True) == "delivered"


@pytest.mark.parametrize("action", ["human_approval", "block", "quarantine"])
def test_a_draft_held_after_generation_exits_at_the_output_guard(action: str) -> None:
    held = report(
        inbound=decision("draft_only", gate_passes=2),
        final=decision(action, decided_by="output_guard"),
    )

    assert exit_point(held, generation_called=True) == "output_guard"


def test_a_carried_inbound_floor_after_generation_is_an_output_guard_exit_and_says_so() -> None:
    # the floor won the merge, but the job still ran to the Output Guard: the exit point stays
    # there and ``carried_from_inbound`` is reported beside it (controller ruling R6)
    carried = report(
        inbound=decision("human_approval", "P04", gate_passes=2),
        final=decision(
            "human_approval", "P04", decided_by="inbound_gate", carried_from_inbound=True
        ),
    )

    assert exit_point(carried, generation_called=True) == "output_guard"
    assert carried_from_inbound(carried) is True


def test_facts_the_guard_did_not_write_read_as_none_and_false_never_as_an_error() -> None:
    # a gate stop and a run with the Output Guard off leave no decided_by or carried flag
    stop = report(inbound=decision("block", gate_passes=1))
    no_output_guard = report(inbound=decision("draft_only", gate_passes=1))
    no_gate: dict[str, Any] = {"schema_version": 3}

    assert carried_from_inbound(stop) is False
    assert carried_from_inbound(no_output_guard) is False
    assert carried_from_inbound(no_gate) is False
    assert exit_point(no_gate, generation_called=True) == "delivered"  # C0T: nothing gated it
    assert exit_point(None, generation_called=True) is None  # C0 and triage-stopped rows
    assert exit_point({}, generation_called=False) is None
    assert guard_calls(None) == calls()  # zeros, not an error


def test_a_gate_stop_without_its_pass_number_reads_as_pass_2() -> None:
    assert exit_point(report(inbound=decision("block")), generation_called=False) == "gate_pass_2"


# --- guard calls, fail-closed events, quarantined chunks -----------------------------------


def test_guard_calls_list_every_stage_in_pipeline_order() -> None:
    got = guard_calls(report(guard_calls_={"doc_chunk": 2, "doc_set": 1}))

    assert list(got) == list(CALLS)
    assert got == calls(doc_chunk=2, doc_set=1)


def test_judge_invalid_events_are_read_from_every_verdict_that_can_fail_closed() -> None:
    sub_stage: dict[str, Any] = {
        "inbound_guard": {
            "metadata": {
                "stages": {
                    "scan": {"error": None, "metadata": {}},
                    "extract": {
                        "error": "judge_invalid:inbound_extract",
                        "metadata": {"judge_invalid": "inbound_extract"},
                    },
                }
            }
        },
        "doc_scanner": {
            "chunks": [
                {"chunk_id": "k1", "error": "judge_invalid:doc_chunk", "metadata": {}},
                {"chunk_id": "k2", "error": "judge_invalid:doc_chunk", "metadata": {}},
                {"chunk_id": "k3", "error": None, "metadata": {}},
            ],
            # the document verdict repeats its chunks' errors; only the set judge's own counts
            "error": "k1: judge_invalid:doc_chunk; k2: judge_invalid:doc_chunk; "
            "set: judge_invalid:doc_set",
            "metadata": {"judge_invalid": "doc_set"},
        },
        "output_guard": {"error": "judge_invalid:output", "metadata": {"judge_invalid": "output"}},
    }

    assert judge_invalid_events(report(**sub_stage)) == (
        "inbound_extract",
        "doc_chunk",
        "doc_chunk",
        "doc_set",
        "output",
    )
    assert judge_invalid_events(report()) == ()
    assert judge_invalid_events(None) == ()


def test_a_chunk_quarantined_only_by_fail_closed_is_not_a_detection() -> None:
    docs: dict[str, Any] = {
        "doc_scanner": {
            "quarantined_chunk_ids": ["k1", "k2"],
            "chunks": [
                {
                    "chunk_id": "k1",
                    "quarantined": True,
                    "decided_by": "error",
                    "error": "RuntimeError: boom",
                    "findings": [{"threat_type": "internal_error"}],
                },
                {"chunk_id": "k2", "quarantined": True, "error": None, "findings": []},
            ],
        }
    }

    assert detected_doc_chunk_ids(report(**docs)) == ("k2",)
    assert fail_closed_quarantined_chunk_ids(report(**docs)) == ("k1",)
    facts = case_protocol(report(**docs), generation_called=False)
    assert facts.doc_quarantined_chunk_ids == ("k1", "k2")  # the fact is kept whole


# --- the per-config tables, on a fixed set of records covering every exit point ----------------


def record(
    case_id: str,
    rep: dict[str, Any] | None,
    *,
    generated: bool,
    poisoned: tuple[str, ...] | None = None,
    status: str = "ok",
) -> RawRecord:
    row: dict[str, Any] = {
        "case_id": case_id,
        "config": "C3",
        "status": status,
        "report": rep,
        "generation": {"calls": 1 if generated else 0},
    }
    if poisoned is not None:
        row["retrieval"] = {"poison_retrieved": bool(poisoned), "poisoned_chunk_ids": poisoned}
    return RawRecord.from_dict(row)


def synthetic_records() -> list[RawRecord]:
    """Eight scored jobs: every exit point, an inbound floor, two fail-closed jobs, an error."""
    stop_1 = report(
        inbound=decision("block", "P02", gate_passes=1),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1),
    )
    stop_2 = report(
        inbound=decision("quarantine", "P01", gate_passes=2),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1, doc_chunk=2, doc_set=1),
        doc_scanner={
            "quarantined_chunk_ids": ["p1", "p2"],
            "chunks": [
                {"chunk_id": "p1", "quarantined": True, "error": None},
                {"chunk_id": "p2", "quarantined": True, "error": None},
            ],
        },
    )
    held = report(
        inbound=decision("draft_only", "P99", gate_passes=2),
        final=decision("block", "P03", decided_by="output_guard", carried_from_inbound=False),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1, output=1),
    )
    floor = report(
        inbound=decision("human_approval", "P04", gate_passes=2),
        final=decision(
            "human_approval", "P04", decided_by="inbound_gate", carried_from_inbound=True
        ),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1, doc_chunk=1, output=1),
        doc_scanner={
            "quarantined_chunk_ids": ["p3"],
            "chunks": [
                {"chunk_id": "p3", "quarantined": True, "error": None},
                {"chunk_id": "c3", "quarantined": False, "error": None},
            ],
        },
    )
    delivered = report(
        inbound=decision("draft_only", "P99", gate_passes=1),
        final=decision("draft_only", "P99", decided_by="output_guard", carried_from_inbound=False),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1, output=1),
    )
    invalid = report(  # the extractor failed closed: P00 asks for a human, nothing was detected
        inbound=decision("human_approval", "P00-internal-error-fail-closed", gate_passes=1),
        final=decision(
            "human_approval",
            "P00-internal-error-fail-closed",
            decided_by="inbound_gate",
            carried_from_inbound=True,
        ),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1, output=1),
        inbound_guard={
            "severity": "none",
            "metadata": {
                "stages": {
                    "scan": {"severity": "none"},
                    "extract": {
                        "severity": "none",
                        "error": "judge_invalid:inbound_extract",
                        "metadata": {"judge_invalid": "inbound_extract"},
                    },
                }
            },
        },
    )
    chunk_invalid = report(  # built like a real guard report, both chunks are poisoned
        inbound=decision("draft_only", "P99", gate_passes=2),
        final=decision("draft_only", "P99", decided_by="output_guard", carried_from_inbound=False),
        guard_calls_=calls(inbound_scan=1, inbound_extract=1, doc_chunk=1, output=1),
        doc_scanner={
            "quarantined_chunk_ids": ["p4", "p5"],
            "metadata": {"set_judge": "llm", "set_judge_added": ["p4"]},
            "chunks": [
                # p4: the chunk judge failed (cheap verdict kept, not quarantined), then the set
                # judge flagged it: model_copy keeps the old error and adds the detector finding
                {
                    "chunk_id": "p4",
                    "quarantined": True,
                    "decided_by": "llm",
                    "error": "judge_invalid:doc_chunk",
                    "metadata": {"judge_invalid": "doc_chunk"},
                    "findings": [{"rule_id": "llm:set_poisoned", "threat_type": "rag_poisoning"}],
                },
                # p5: the chunk scanner raised and quarantined fail-closed: internal-error stub only
                {
                    "chunk_id": "p5",
                    "quarantined": True,
                    "decided_by": "error",
                    "error": "RuntimeError: boom",
                    "metadata": {},
                    "findings": [{"threat_type": "internal_error", "detector": "heuristic"}],
                },
            ],
        },
    )
    return [
        record("a-gate1", stop_1, generated=False),
        record("r-gate2", stop_2, generated=False, poisoned=("p1", "p2")),
        record("a-held", held, generated=True),
        record("r-floor", floor, generated=True, poisoned=("p3",)),
        record("b-delivered", delivered, generated=True),
        record("a-invalid", invalid, generated=True),
        record("r-chunk-invalid", chunk_invalid, generated=True, poisoned=("p4", "p5")),
        record("a-error", None, generated=False, status="error"),  # not scored
        record("c0-native", None, generated=True),  # no guard report: no protocol row
    ]


LLMAIL = {"a-gate1", "a-held", "a-invalid", "a-error"}
RAG = {"r-gate2", "r-floor", "r-chunk-invalid"}


def test_the_per_config_protocol_table_is_exact() -> None:
    table = protocol_table(synthetic_records(), llmail_attack_ids=LLMAIL, rag_attack_ids=RAG)

    assert table is not None
    assert table.jobs == 7  # the error row and the row without a report are not jobs
    assert table.exits == {
        "gate_pass_1": 1,
        "gate_pass_2": 1,
        "output_guard": 3,  # held, floor, invalid (P00: human approval after generation)
        "delivered": 2,
    }
    assert table.floor_won == 2  # the carried inbound floor and the P00 floor
    assert {s: (c.total, round(c.mean, 4), c.max) for s, c in table.calls.items()} == {
        "inbound_scan": (7, 1.0, 1),
        "inbound_extract": (7, 1.0, 1),
        "doc_chunk": (4, 0.5714, 2),
        "doc_set": (1, 0.1429, 1),  # its own column: the set judge ran once, in one job
        "output": (5, 0.7143, 1),
    }
    assert table.max_calls_per_job == 5  # gate_pass_2: 1 + 1 + 2 + 1
    assert table.fail_closed == {
        "inbound_scan": 0,
        "inbound_extract": 1,
        "doc_chunk": 1,
        "doc_set": 0,
        "output": 0,
    }
    assert (
        table.fail_closed_quarantined == 1
    )  # p5: quarantined by the scanner's own fail-closed error, no detection
    assert table.fail_closed_review == 1  # a-invalid: P00 held it for review
    # F8: the inbound catch rate counts LLMail attacks stopped at gate pass 1 or 2
    assert (table.attacks, table.inbound_caught) == (3, 1)
    # every poisoned chunk caught: r-gate2 (p1, p2 both detected) and r-floor (p3) yes;
    # r-chunk-invalid: the set judge caught p4 after its chunk judge failed (a detection), but p5
    # only failed closed, so not every chunk was caught
    assert (table.poison_cases, table.poison_all_caught) == (3, 2)


def test_a_config_with_no_guard_report_has_no_protocol_table() -> None:
    native = [record("a", None, generated=True), record("b", None, generated=True)]

    assert protocol_table(native, llmail_attack_ids={"a"}, rag_attack_ids=set()) is None


def test_a_judge_invalid_job_is_in_no_detection_count() -> None:
    from evaluation.mailguard_bench.scoring import flagged_layers

    by_id = {r.case_id: r for r in synthetic_records()}

    assert flagged_layers(by_id["a-invalid"].report) == []
    assert flagged_layers(by_id["r-chunk-invalid"].report) == ["doc_scanner"]  # p4: set judge
    assert flagged_layers(by_id["r-gate2"].report) == ["doc_scanner"]


def test_the_record_carries_its_protocol_facts_as_the_writer_stores_them() -> None:
    by_id = {r.case_id: r for r in synthetic_records()}

    facts = by_id["r-floor"].protocol

    assert facts == CaseProtocol(
        exit_point="output_guard",
        carried_from_inbound=True,
        guard_calls=calls(inbound_scan=1, inbound_extract=1, doc_chunk=1, output=1),
        judge_invalid=(),
        doc_quarantined_chunk_ids=("p3",),
    )
    assert facts.to_dict()["doc_quarantined_chunk_ids"] == ["p3"]
    assert by_id["c0-native"].protocol is None


def test_a_chunk_with_a_failed_judge_that_the_set_judge_then_quarantined_is_a_detection() -> None:
    # the real guard: the chunk judge fails (no quarantine), the set judge flags the chunk later
    docs: dict[str, Any] = {
        "doc_scanner": {
            "quarantined_chunk_ids": ["c1"],
            "metadata": {"set_judge_added": ["c1"]},
            "chunks": [
                {
                    "chunk_id": "c1",
                    "quarantined": True,
                    "error": "judge_invalid:doc_chunk",
                    "metadata": {"judge_invalid": "doc_chunk"},
                    "findings": [{"rule_id": "llm:set_poisoned", "threat_type": "rag_poisoning"}],
                }
            ],
        }
    }

    assert detected_doc_chunk_ids(report(**docs)) == ("c1",)
    assert fail_closed_quarantined_chunk_ids(report(**docs)) == ()


def test_a_held_action_without_decided_by_is_no_output_guard_exit() -> None:
    # C1, C2, C3-L4: the Output Guard is off, report.decision is still the inbound gate's
    off = report(inbound=decision("human_approval", gate_passes=2))

    assert exit_point(off, generation_called=True) == "delivered"

"""Recount of L2 schema fallbacks in a v1 run (Amendment 1, E.2; R22.12).

The v1 guard (81df5d07) cannot mark a failed L2 answer: when the model answered in prose,
parse_json_or_text returns {"raw_text": ...}, every ExtractorOutput field has a default, and L2
reports a normal verdict that merges nothing from the model. What such a verdict leaves behind
is only the heuristic entity keys (order_ids, emails, amounts, dates), the default confidence
0.5 and no instructions, so the recount is an upper bound and says where it cannot tell.
Fixtures only: the command is never run on the real results here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.l2_recount import (
    HEURISTIC_ENTITY_KEYS,
    L2Recount,
    classify_l2,
    main,
    recount,
    render,
)
from evaluation.mailguard_bench.scoring import RawRecord

HEURISTIC_ONLY = {"order_ids": [], "emails": ["a@b.example"], "amounts": [], "dates": []}


def l2(**over: Any) -> dict[str, Any]:
    """An L2 verdict of the v1 guard whose AI step ran (the shape a real C3 row holds)."""
    verdict: dict[str, Any] = {
        "layer": "l2_intent_extractor",
        "severity": "none",
        "decided_by": "llm",
        "error": None,
        "findings": [],
        "metadata": {
            "llm_used": True,
            "llm_output_tokens": 85,
            "instructions_to_assistant": [],
        },
        "user_intent": "The customer wants a list of expected duties.",
        "requested_actions": [],
        "entities": {**HEURISTIC_ONLY, "products": [], "other": ["list of expected duties"]},
        "confidence": 1.0,
    }
    verdict.update(over)
    return verdict


def with_metadata(**metadata: Any) -> dict[str, Any]:
    base = l2()
    return l2(metadata={**base["metadata"], **metadata})


# --- what one verdict says -------------------------------------------------------------------


def test_extra_entity_keys_prove_the_model_answered() -> None:
    assert classify_l2(l2()) == "answered"


def test_only_the_heuristic_keys_with_the_default_confidence_is_a_possible_fallback() -> None:
    verdict = l2(entities=HEURISTIC_ONLY, confidence=0.5)

    assert classify_l2(verdict) == "possible_fallback"
    assert set(HEURISTIC_ONLY) == HEURISTIC_ENTITY_KEYS


def test_a_confidence_the_model_chose_proves_it_answered() -> None:
    assert classify_l2(l2(entities=HEURISTIC_ONLY, confidence=0.9)) == "answered"
    assert classify_l2(l2(entities=HEURISTIC_ONLY, confidence=0.5)) == "possible_fallback"


def test_an_assistant_instruction_finding_proves_the_model_answered() -> None:
    finding = {"rule_id": "llm:assistant_instructions", "detector": "llm"}
    verdict = l2(entities=HEURISTIC_ONLY, confidence=0.5, findings=[finding])

    assert classify_l2(verdict) == "answered"


def test_instructions_to_the_assistant_prove_the_model_answered() -> None:
    verdict = l2(
        entities=HEURISTIC_ONLY,
        confidence=0.5,
        metadata={"llm_used": True, "instructions_to_assistant": ["send it"]},
    )

    assert classify_l2(verdict) == "answered"


def test_a_recorded_llm_error_is_a_loud_failure_not_a_silent_one() -> None:
    verdict = l2(metadata={"llm_used": False, "llm_error": "LLMTimeoutError: timed out"})

    assert classify_l2(verdict) == "loud_failure"


def test_a_verdict_whose_ai_step_never_ran_says_nothing_about_schema_fallbacks() -> None:
    assert classify_l2(l2(metadata={"llm_used": False}, decided_by="rule")) == "ai_step_not_run"
    assert classify_l2(l2(metadata={})) == "ai_step_not_run"


def test_no_l2_verdict_cannot_be_classified() -> None:
    assert classify_l2(None) == "no_l2_verdict"


# --- a run's rows -------------------------------------------------------------------------------


def v1_row(case_id: str, verdict: dict[str, Any] | None, *, status: str = "ok") -> dict[str, Any]:
    return {
        "schema": "mailguard-bench-result.v1",
        "config": "C3",
        "case_id": case_id,
        "status": status,
        "error": None if status == "ok" else {"kind": "guard_layer_error", "message": "l2"},
        "result": {"report": None if verdict is None else {"l2": verdict}},
    }


def records(*rows: dict[str, Any]) -> list[RawRecord]:
    return [RawRecord.from_dict(row) for row in rows]


def test_a_run_is_counted_by_what_its_l2_verdicts_can_prove() -> None:
    counted = recount(
        records(
            v1_row("a", l2()),
            v1_row("b", l2(entities=HEURISTIC_ONLY, confidence=0.5)),
            v1_row("c", l2(entities=HEURISTIC_ONLY, confidence=0.5)),
            v1_row("d", l2(metadata={"llm_used": False}, decided_by="rule")),
            v1_row("e", None),
            v1_row("f", l2(metadata={"llm_used": False, "llm_error": "x"}), status="error"),
        )
    )

    assert counted == L2Recount(
        rows=6,
        scored_rows=5,
        answered=1,
        possible_fallback=2,
        loud_failure=1,
        ai_step_not_run=1,
        no_l2_verdict=1,
        recorded_schema_fallback=0,
        recorded_rows=0,
    )
    assert counted.recorded_rows == 0


def test_a_row_that_records_its_fallbacks_is_counted_exactly_not_estimated() -> None:
    fallback = {"layer": "l2_intent_extractor", "reason": "non_json", "error": "prose"}
    recorded = v1_row("a", l2())
    recorded["result"]["guard_fallbacks"] = [fallback]
    clean = v1_row("b", l2(entities=HEURISTIC_ONLY, confidence=0.5))
    clean["result"]["guard_fallbacks"] = []

    counted = recount(records(recorded, clean))

    assert counted.recorded_rows == 2 and counted.recorded_schema_fallback == 1
    assert counted.possible_fallback == 0  # a recording replaces the estimate for its row


# --- what the command says --------------------------------------------------------------------


def test_the_text_states_the_bound_and_where_the_data_does_not_allow_a_count() -> None:
    counted = recount(
        records(v1_row("a", l2(entities=HEURISTIC_ONLY, confidence=0.5)), v1_row("b", None))
    )

    text = render("C3", counted)

    assert "C3: 2 rows (2 scored)" in text
    assert "1 of them look like an L2 answer without the schema" in text
    assert "upper bound" in text
    assert "audit__C3.jsonl" in text and "cannot tell" in text
    assert "1 row(s) have no L2 verdict" in text


def test_a_run_with_no_possible_fallback_says_so_without_claiming_none_happened() -> None:
    text = render("C3", recount(records(v1_row("a", l2()))))

    assert "0 of them look like an L2 answer without the schema" in text
    assert "not proof" in text  # an answer with nothing to merge looks the same


def test_the_command_reads_a_run_folder_and_writes_nothing(tmp_path: Path, capsys: Any) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    rows = [v1_row("a", l2(entities=HEURISTIC_ONLY, confidence=0.5)), v1_row("b", l2())]
    (raw / "C3.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    code = main(["--run-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "C3: 2 rows" in out and "1 of them look like" in out
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_the_command_takes_the_config_and_fails_plainly_on_a_missing_file(
    tmp_path: Path, capsys: Any
) -> None:
    (tmp_path / "raw").mkdir()

    code = main(["--run-dir", str(tmp_path), "--config", "C3-L1"])

    assert code == 1
    assert "FAIL" in capsys.readouterr().err

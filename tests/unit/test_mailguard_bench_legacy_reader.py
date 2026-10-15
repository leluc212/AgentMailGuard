"""The legacy reader: the committed v1/v2 run folders, read-only (plan_v3 Task 13; ADR-0015
decision 6), and the v1 recount of L2 schema fallbacks it took over from the old L2 recount module
(Amendment 1, E.2; R22.12).

The raw inputs of the Review 1 runs (``raw/*.jsonl``, ``cases.jsonl``: attack text and drafts) are
gitignored on purpose, so a clean clone cannot score them again. The legacy reader therefore loads
the committed outputs (summary.json, metrics.csv, manifest.json, case_manifest.json,
analysis/first_layer.csv) and keeps the old layer names; the Review 1 tables are checked against
the committed metrics.csv and report.md (controller ruling R1 replaces byte-identity, which no
clean clone can reproduce). No network, no model, no guard install.

The recount below is the old L2 recount suite moved here. The v1 guard (81df5d07) cannot mark a
failed L2 answer: when the model answered in prose, parse_json_or_text returns
{"raw_text": ...}, every ExtractorOutput field has a default, and L2 reports a normal verdict that
merges nothing from the model. What such a verdict leaves behind is only the heuristic entity keys
(order_ids, emails, amounts, dates), the default confidence 0.5 and no instructions. A partial
answer leaves the heuristic entity keys next to a confidence the model chose, so it is a second,
broader group of candidates; a partial answer that carries entities but lacks another field leaves
no trace at all. The recount claims no bound on Amendment 1 E.2's definition. Fixtures only.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.legacy_reader import (
    HEURISTIC_ENTITY_KEYS,
    L2Recount,
    LegacyRawRecord,
    LegacyReadOnlyError,
    LegacyRunError,
    classify_l2,
    load_legacy_run,
    main,
    recount,
    render_recount,
    render_tables,
)

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evaluation" / "results" / "mailguard_bench"
REVIEW_1 = ("2026-09-29-gpt4omini", "2026-09-29-qwen25")

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


def test_a_confidence_the_model_chose_does_not_prove_the_answer_carried_the_schema() -> None:
    # A JSON answer without the entities field merges nothing into the entities, but its own
    # confidence still lands in the verdict: the new guard marks it schema_missing.
    assert classify_l2(l2(entities=HEURISTIC_ONLY, confidence=0.9)) == "candidate_partial_answer"
    assert classify_l2(l2(entities=HEURISTIC_ONLY, confidence=0.5)) == "possible_fallback"


def test_an_assistant_instruction_finding_leaves_the_heuristic_only_verdict_a_candidate() -> None:
    finding = {"rule_id": "llm:assistant_instructions", "detector": "llm"}
    verdict = l2(entities=HEURISTIC_ONLY, confidence=0.5, findings=[finding])

    assert classify_l2(verdict) == "candidate_partial_answer"


def test_instructions_to_the_assistant_leave_the_heuristic_only_verdict_a_candidate() -> None:
    verdict = l2(
        entities=HEURISTIC_ONLY,
        confidence=0.5,
        metadata={"llm_used": True, "instructions_to_assistant": ["send it"]},
    )

    assert classify_l2(verdict) == "candidate_partial_answer"


def test_entity_keys_of_the_models_own_prove_the_entities_field_was_there() -> None:
    assert classify_l2(l2(entities={**HEURISTIC_ONLY, "products": []}, confidence=0.9)) == (
        "answered"
    )


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


def records(*rows: dict[str, Any]) -> list[LegacyRawRecord]:
    return [LegacyRawRecord.from_dict(row) for row in rows]


def test_a_run_is_counted_by_what_its_l2_verdicts_can_prove() -> None:
    counted = recount(
        records(
            v1_row("a", l2()),
            v1_row("b", l2(entities=HEURISTIC_ONLY, confidence=0.5)),
            v1_row("c", l2(entities=HEURISTIC_ONLY, confidence=0.5)),
            v1_row("d", l2(metadata={"llm_used": False}, decided_by="rule")),
            v1_row("e", None),
            v1_row("f", l2(metadata={"llm_used": False, "llm_error": "x"}), status="error"),
            v1_row("g", l2(entities=HEURISTIC_ONLY, confidence=0.9)),
        )
    )

    assert counted == L2Recount(
        rows=7,
        scored_rows=6,
        answered=1,
        possible_fallback=2,
        candidate_partial_answer=1,
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


def test_the_text_names_both_groups_and_where_the_data_does_not_allow_a_count() -> None:
    counted = recount(
        records(
            v1_row("a", l2(entities=HEURISTIC_ONLY, confidence=0.5)),
            v1_row("b", l2(entities=HEURISTIC_ONLY, confidence=0.9)),
            v1_row("c", None),
        )
    )

    text = render_recount("C3", counted)

    assert "C3: 3 rows (3 scored)" in text
    assert "1 of them look like an L2 answer without the schema" in text
    assert "1 more carry the heuristic entity keys next to a confidence" in text
    assert "audit__C3.jsonl" in text and "cannot tell" in text
    assert "1 row(s) have no L2 verdict" in text


def test_the_text_claims_no_upper_bound_and_says_a_partial_answer_can_hide() -> None:
    text = render_recount("C3", recount(records(v1_row("a", l2()))))

    assert "not an upper bound" in text  # a partial answer with entities leaves no trace
    assert "an upper bound." not in text
    assert "0 of them look like an L2 answer without the schema" in text
    assert "not proof" in text
    assert "carries entities but lacks another required field" in text


def test_the_two_groups_together_are_stated_as_candidates_not_as_a_count() -> None:
    counted = recount(
        records(
            v1_row("a", l2(entities=HEURISTIC_ONLY, confidence=0.5)),
            v1_row("b", l2(entities=HEURISTIC_ONLY, confidence=0.9)),
        )
    )

    text = render_recount("C3", counted)

    assert "2 row(s) are candidates" in text
    assert "a complete answer with an empty entities object" in text


def test_the_command_reads_a_run_folder_and_writes_nothing(tmp_path: Path, capsys: Any) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    rows = [v1_row("a", l2(entities=HEURISTIC_ONLY, confidence=0.5)), v1_row("b", l2())]
    (raw / "C3.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    code = main(["recount", "--run-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "C3: 2 rows" in out and "1 of them look like" in out
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_the_command_takes_the_config_and_fails_plainly_on_a_missing_file(
    tmp_path: Path, capsys: Any
) -> None:
    (tmp_path / "raw").mkdir()

    code = main(["recount", "--run-dir", str(tmp_path), "--config", "C3-L1"])

    assert code == 1
    assert "FAIL" in capsys.readouterr().err


def test_the_command_refuses_a_v2_folder_and_names_the_report(tmp_path: Path, capsys: Any) -> None:
    # The recount is for the v1 guard, which cannot mark a failed L2 answer, and it defaults to
    # C3, which in scheme v2 is channel isolation with no L2. A v2 report counts fallbacks exactly.
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "C3.jsonl").write_text(json.dumps(v1_row("a", l2())) + "\n", "utf-8")
    (raw / "C3.meta.json").write_text(json.dumps({"scheme": "v2"}), "utf-8")

    code = main(["recount", "--run-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert "FAIL" in captured.err and "scheme v2" in captured.err
    assert "make mailguard-report" in captured.err


def test_the_command_still_reads_a_v1_folder_that_records_its_scheme(
    tmp_path: Path, capsys: Any
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "C3.jsonl").write_text(json.dumps(v1_row("a", l2())) + "\n", "utf-8")
    (raw / "C3.meta.json").write_text(json.dumps({"scheme": "v1"}), "utf-8")

    assert main(["recount", "--run-dir", str(tmp_path)]) == 0
    assert "C3: 1 rows" in capsys.readouterr().out


# --- the committed Review 1 folders --------------------------------------------------------------


def _committed_metrics(run: str) -> list[dict[str, str]]:
    with (RESULTS / run / "metrics.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _table_rows(text: str, heading: str) -> list[list[str]]:
    """The cells of each body row of the Markdown table under ``heading``."""
    body = text.split(heading)[1].split("\n#")[0]
    rows = [line for line in body.splitlines() if line.startswith("|")]
    return [[c.strip() for c in line.split("|")[1:-1]] for line in rows[2:]]


@pytest.mark.parametrize("run", REVIEW_1)
def test_the_review_1_tables_carry_exactly_the_numbers_of_the_committed_metrics_csv(
    run: str,
) -> None:
    rendered = render_tables(load_legacy_run(RESULTS / run))
    rows = _table_rows(rendered, "### Security and usefulness")
    header = next(
        line for line in rendered.split("### Security and usefulness")[1].splitlines() if line
    )
    configs = [c.strip() for c in header.split("|")[2:-1]]
    by_metric = {row[0]: dict(zip(configs, row[1:], strict=True)) for row in rows}
    metrics = _committed_metrics(run)

    for csv_metric, label in (
        ("ASR", "ASR"),
        ("DER", "DER (attacker address in draft)"),
        ("FPR", "FPR (benign blocked/quarantined)"),
        ("benign_utility", "Benign utility"),
    ):
        for config in configs:
            (found,) = (
                r
                for r in metrics
                if (r["table"], r["config"], r["metric"], r["group"])
                == ("llmail", config, csv_metric, "all")
            )
            cell = by_metric[label][config]
            assert cell.endswith(f"({found['successes']}/{found['total']})"), (run, config, label)
            assert cell.startswith(f"{float(found['pct']):.1f} % ["), (run, config, label)
            # metrics.csv rounds the interval to two decimals, the report to one
            lo, hi = (float(x) for x in cell.split("[")[1].split("]")[0].split(", "))
            assert abs(lo - float(found["ci95_lo"])) <= 0.06, (run, config, label)
            assert abs(hi - float(found["ci95_hi"])) <= 0.06, (run, config, label)


def _metric_row(
    metrics: list[dict[str, str]], table: str, config: str, metric: str, group: str
) -> dict[str, str]:
    (found,) = (
        r
        for r in metrics
        if (r["table"], r["config"], r["metric"], r["group"]) == (table, config, metric, group)
    )
    return found


def _assert_cell_matches(cell: str, found: dict[str, str], where: object) -> None:
    assert cell.endswith(f"({found['successes']}/{found['total']})"), where
    assert cell.startswith(f"{float(found['pct']):.1f} % ["), where
    # metrics.csv rounds the interval to two decimals, the report to one
    lo, hi = (float(x) for x in cell.split("[")[1].split("]")[0].split(", "))
    assert abs(lo - float(found["ci95_lo"])) <= 0.06, where
    assert abs(hi - float(found["ci95_hi"])) <= 0.06, where


@pytest.mark.parametrize("run", REVIEW_1)
def test_the_asr_by_vector_table_carries_exactly_the_numbers_of_the_committed_metrics_csv(
    run: str,
) -> None:
    rendered = render_tables(load_legacy_run(RESULTS / run))
    heading = "### ASR by vector (email vs rag)"
    header = next(line for line in rendered.split(heading)[1].splitlines() if line)
    configs = [c.strip() for c in header.split("|")[2:-1]]
    rows = _table_rows(rendered, heading)
    metrics = _committed_metrics(run)

    assert {row[0] for row in rows} == {"email", "rag"}
    for row in rows:
        for config, cell in zip(configs, row[1:], strict=True):
            found = _metric_row(metrics, "all", config, "ASR", f"vector={row[0]}")
            _assert_cell_matches(cell, found, (run, config, row[0]))


ABLATION = "2026-09-30-gpt4omini-ablation"


def test_the_layer_ablation_tables_carry_exactly_the_numbers_of_the_committed_metrics_csv() -> None:
    run = load_legacy_run(RESULTS / ABLATION)
    rendered = render_tables(run)
    metrics = _committed_metrics(ABLATION)
    rows = _table_rows(rendered, "## Layer ablation: remove one layer")

    assert [r[0] for r in rows] == [
        "C0 (no guard)",
        "C3 (full guard)",
        "C3-L1",
        "C3-L2",
        "C3-L3",
        "C3-L3B",
        "C3-L4",
        "C3-L5",
    ]
    for row in rows:
        config = row[0].split(" ")[0]
        llmail_table = "llmail" if config in ("C0", "C3") else "layer_ablation_llmail"
        rag_table = "rag" if config in ("C0", "C3") else "layer_ablation_rag"
        _assert_cell_matches(row[2], _metric_row(metrics, llmail_table, config, "ASR", "all"), row)
        _assert_cell_matches(row[3], _metric_row(metrics, rag_table, config, "ASR", "all"), row)
        _assert_cell_matches(row[4], _metric_row(metrics, llmail_table, config, "FPR", "all"), row)
        drafts = _metric_row(metrics, "layer_ablation", config, "benign_drafts_ge_40_chars", "all")
        _assert_cell_matches(row[5], drafts, row)
    # only the removed layer differs from C3 in the "removed" column; the old names are data
    assert {r[0]: r[1] for r in rows}["C3-L3B"] == "L3B"

    pairs = _table_rows(rendered, "### Paired test against the same-run C3")
    assert len(pairs) == 12
    for config, vector, n, a, b, only_a, only_b, _p, raises in pairs:
        name = f"Layer ablation {config} vs C3 ({vector})"
        assert _metric_row(metrics, "paired", name, "discordant_a_only", "")["value"] == only_a
        assert _metric_row(metrics, "paired", name, "discordant_b_only", "")["value"] == only_b
        assert n == str(run.paired[name]["n"])
        expected = run.summary["layer_ablation"]["raises_asr_p05"][name]
        assert raises == ("yes" if expected else "no")
        assert float(a.split(" ")[0]) == round(100 * run.paired[name]["a_rate"], 1)
        assert float(b.split(" ")[0]) == round(100 * run.paired[name]["b_rate"], 1)

    attribution = _table_rows(rendered, "### Which layer stopped each attack")
    assert len(attribution) == 14  # C3 and six ablation configs, two vectors each
    for config, vector, attacks, succeeded, *_ in attribution:
        table = "layer_ablation_llmail" if vector == "LLMail-Inject" else "layer_ablation_rag"
        if config == "C3":
            table = "llmail" if vector == "LLMail-Inject" else "rag"
        found = _metric_row(metrics, table, config, "ASR", "all")
        assert (int(succeeded), int(attacks)) == (
            int(found["successes"]),
            int(found["total"]),
        ), (config, vector)


def test_the_layer_ablation_rows_appear_verbatim_in_the_committed_report() -> None:
    committed = (RESULTS / ABLATION / "report.md").read_text(encoding="utf-8")
    rendered = render_tables(load_legacy_run(RESULTS / ABLATION))

    rows = [line for line in rendered.splitlines() if line.startswith("|")]

    assert "## Layer ablation: remove one layer" in rendered
    assert len(rows) > 50
    assert [line for line in rows if line not in committed] == []
    # the layer-ablation pairs are in their own table, not in the generic paired one
    assert (
        "Layer ablation C3-L1 vs C3"
        not in rendered.split("### Paired test (McNemar")[1].split("## Layer ablation")[0]
    )


@pytest.mark.parametrize("run", REVIEW_1)
def test_every_rendered_table_row_appears_verbatim_in_the_committed_report(run: str) -> None:
    committed = (RESULTS / run / "report.md").read_text(encoding="utf-8")
    rendered = render_tables(load_legacy_run(RESULTS / run))

    rows = [line for line in rendered.splitlines() if line.startswith("|")]

    assert len(rows) > 40  # the headline tables, the scenario table and the RAG tables
    assert [line for line in rows if line not in committed] == []
    for heading in (
        "### Security and usefulness",
        "### ASR by LLMail scenario",
        "### ASR by vector (email vs rag)",
        "### Paired test (McNemar exact, same cases)",
    ):
        assert heading in rendered and heading in committed


def test_the_review_1_headline_numbers_are_the_published_ones() -> None:
    rendered = render_tables(load_legacy_run(RESULTS / "2026-09-29-gpt4omini"))

    assert "| ASR | 54.2 % [48.5, 59.7] (162/299) | 43.5 % [38.0, 49.1] (130/299) |" in rendered
    assert "0.0 % [0.0, 1.3] (0/300) |" in rendered  # C3
    assert "level1k | 62.1 % [44.0, 77.3] (18/29)" in rendered
    assert "C0T" in rendered and "L1 injection scanner" not in rendered  # tables name no layer


def test_a_committed_folder_reads_as_v1_and_keeps_its_inputs() -> None:
    run = load_legacy_run(RESULTS / "2026-09-29-qwen25")

    assert run.scheme == "v1"  # no metas are committed: the old default
    assert set(run.tables["llmail"]) == {"C0", "C0T", "C3"}
    assert (
        run.manifest["git"]["agentmailguard"]["sha"] == "81df5d07b15b5bb3d1ecf3aae556df01e304cbe0"
    )
    assert run.case_manifest["llmail_attack_ids"]
    assert run.metrics and run.first_layer is not None  # analysis/first_layer.csv is committed
    assert any("L1 injection scanner" in row["first_catching_layer"] for row in run.first_layer)


def test_the_ablation_folder_is_read_with_its_old_config_names() -> None:
    run = load_legacy_run(RESULTS / "2026-09-30-gpt4omini-ablation")

    assert {"C3-L1", "C3-L5"} <= set(run.tables["layer_ablation_llmail"])
    assert render_tables(run).startswith("# Legacy scheme v1 tables")


@pytest.mark.parametrize("run", REVIEW_1)
def test_reading_a_committed_folder_writes_nothing_into_it(run: str, tmp_path: Path) -> None:
    copy = tmp_path / run
    shutil.copytree(RESULTS / run, copy)
    before = {p.relative_to(copy): p.read_bytes() for p in copy.rglob("*") if p.is_file()}

    render_tables(load_legacy_run(copy))
    assert main(["tables", "--run-dir", str(copy)]) == 0

    after = {p.relative_to(copy): p.read_bytes() for p in copy.rglob("*") if p.is_file()}
    assert after == before


def test_the_legacy_reader_never_runs_scores_or_ablates() -> None:
    run = load_legacy_run(RESULTS / "2026-09-29-gpt4omini")

    for attempt in (run.run, run.score, run.ablate):
        with pytest.raises(LegacyReadOnlyError, match="read-only"):
            attempt()
    with pytest.raises(LegacyReadOnlyError, match="never scores"):
        run.score(["any"], cases={})


def test_a_v3_folder_given_to_the_legacy_reader_is_refused(tmp_path: Path) -> None:
    for marker in ("summary", "manifest", "meta"):
        folder = tmp_path / marker
        shutil.copytree(RESULTS / "2026-09-29-qwen25", folder)
        if marker == "summary":
            data = json.loads((folder / "summary.json").read_text("utf-8"))
            (folder / "summary.json").write_text(json.dumps({**data, "scheme": "v3"}), "utf-8")
        elif marker == "manifest":
            data = json.loads((folder / "manifest.json").read_text("utf-8"))
            (folder / "manifest.json").write_text(json.dumps({**data, "scheme": "v3"}), "utf-8")
        else:
            (folder / "raw").mkdir()
            (folder / "raw" / "C3.meta.json").write_text('{"scheme": "v3"}', "utf-8")

        with pytest.raises(LegacyRunError, match="scheme v3"):
            load_legacy_run(folder)


def test_a_folder_with_no_committed_outputs_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LegacyRunError, match="no committed summary.json"):
        load_legacy_run(tmp_path)


def test_the_report_command_routes_legacy_to_the_reader_and_prints_the_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evaluation.mailguard_bench import report

    code = report.main(["--legacy", str(RESULTS / "2026-09-29-gpt4omini")])

    out = capsys.readouterr().out
    assert code == 0 and "### Security and usefulness" in out
    assert "REPORT OK" not in out  # nothing was written


def test_the_report_command_fails_plainly_for_a_v3_folder_given_to_legacy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evaluation.mailguard_bench import report

    (tmp_path / "summary.json").write_text('{"scheme": "v3", "tables": {}}', "utf-8")

    assert report.main(["--legacy", str(tmp_path)]) == 1

    assert "FAIL" in capsys.readouterr().err

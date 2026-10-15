"""The report of a run under config scheme v3 (plan_v3 Task 13; ADR-0015).

The config table, the five protocol tables (exit points, inbound floor, guard calls per stage,
fail-closed events, the F8 route metrics) and the guard overhead table are asserted exactly on a
fixed set of synthetic per-case records; ``build_report`` is checked end to end on a small v3
folder, and refuses a v1 or v2 folder naming the legacy reader. The build tests need
AgentMailGuard's scorer and skip where mailguard is not installed (CI); the rest is pure.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.artifacts import scheme_v3_rows
from evaluation.mailguard_bench.overhead import overhead
from evaluation.mailguard_bench.protocol import protocol_table
from evaluation.mailguard_bench.scheme_report import (
    SchemeV3Section,
    render_config_section,
    render_no_ai_stage_note,
    render_overhead_table,
    render_protocol_sections,
)
from evaluation.mailguard_bench.scoring import LegacyReportError
from tests.unit.test_mailguard_bench_protocol import (
    LLMAIL,
    RAG,
    synthetic_records,
)
from tests.unit.test_mailguard_bench_report import (
    C0_META,
    C3_META,
    PRICES,
    _amg_or_skip,
    _run_folder,
)


def section() -> SchemeV3Section:
    table = protocol_table(synthetic_records(), llmail_attack_ids=LLMAIL, rag_attack_ids=RAG)
    assert table is not None
    return SchemeV3Section(configs_run=("C0", "C3"), protocol={"C3": table})


def test_the_config_table_names_what_every_v3_config_turns_on() -> None:
    text = "\n".join(render_config_section(section()))

    assert "## Config scheme v3" in text
    assert "| C0 | none (rag-email's own prompt, no guard) | none | yes |" in text
    assert "| C0T | none (the guard's prompt template) | none | no |" in text
    assert (
        "| C1 | L1 Inbound Guard + L3 Inbound Gate | L1 scan judge, L1 intent extraction | no |"
        in text
    )
    assert (
        "| C3 | L1 Inbound Guard + L2 Doc Scanner + L3 Inbound Gate + L4 Output Guard + "
        "channel isolation | L1 scan judge, L1 intent extraction, L2 chunk judge, "
        "L2 set judge, L4 output check | yes |"
    ) in text
    assert (
        "| C3-ISO | L1 Inbound Guard + L2 Doc Scanner + L3 Inbound Gate + L4 Output Guard | "
        "L1 scan judge, L1 intent extraction, L2 chunk judge, L2 set judge, L4 output check | no |"
    ) in text
    assert (
        "| C3-L2 | L1 Inbound Guard + L3 Inbound Gate + L4 Output Guard + channel isolation |"
        in text
    )
    assert text.count("\n| C") == 11  # the header and one row per v3 config


def test_the_five_protocol_tables_are_exact() -> None:
    text = "\n".join(render_protocol_sections(section()))

    def table_after(heading: str) -> list[str]:
        body = text.split(heading)[1].split("\n### ")[0]
        return [line for line in body.splitlines() if line.startswith("|")]

    assert table_after("### Exit points") == [
        "| Config | Jobs | gate pass 1 | gate pass 2 | output guard | delivered |",
        "|---|---|---|---|---|---|",
        "| C3 | 7 | 1 | 1 | 3 | 2 |",
    ]
    assert table_after("### Inbound floor") == [
        "| Config | Jobs | Inbound floor won |",
        "|---|---|---|",
        "| C3 | 7 | 2 |",
    ]
    assert table_after("### Guard calls per stage") == [
        "| Config | Jobs | inbound_scan | inbound_extract | doc_chunk | doc_set | output | "
        "most per job |",
        "|---|---|---|---|---|---|---|---|",
        "| C3 | 7 | 1.00 / 1 | 1.00 / 1 | 0.57 / 2 | 0.14 / 1 | 0.71 / 1 | 5 |",
    ]
    assert table_after("### Fail-closed events") == [
        "| Config | Jobs | inbound_scan | inbound_extract | doc_chunk | doc_set | output | "
        "quarantined by fail-closed | held by P00 |",
        "|---|---|---|---|---|---|---|---|---|",
        "| C3 | 7 | 0 | 1 | 1 | 0 | 0 | 1 | 1 |",
    ]
    assert table_after("### Route metrics (ablation criterion e)") == [
        "| Config | Inbound catch rate | Every poisoned chunk caught |",
        "|---|---|---|",
        "| C3 | 1 of 3 (33.3 %) | 2 of 3 (66.7 %) |",
    ]
    assert "doc_set` column alone" in text  # the set judge's claim is read in its own column
    assert "5 calls per job" in text


def test_a_config_without_attacks_of_a_route_says_n_a_not_zero() -> None:
    only_benign = [r for r in synthetic_records() if r.case_id == "b-delivered"]
    table = protocol_table(only_benign, llmail_attack_ids=set(), rag_attack_ids=set())
    assert table is not None

    text = "\n".join(render_protocol_sections(SchemeV3Section(("C3",), {"C3": table})))

    assert "| C3 | n/a | n/a |" in text


def test_a_run_with_no_guard_report_prints_no_protocol_tables() -> None:
    assert render_protocol_sections(SchemeV3Section(configs_run=("C0",))) == []


def test_the_overhead_table_shows_guard_calls_per_job_and_both_latency_percentiles() -> None:
    records = synthetic_records()
    over = {"C3": overhead("C3", records, {})}

    lines = render_overhead_table(over)

    # 24 judge calls over 8 scored rows (the row without a guard report counts as none); the
    # provider tally of these rows is empty, so it is its own column
    assert lines[2] == (
        "| Config | n | guard calls / job (guard count) | most calls in a job | "
        "guard calls / email (provider tally) | guard p50 / p95 | end-to-end p50 / p95 |"
    )
    assert lines[4] == "| C3 | 8 | 3.00 | 5 | 0.00 | 0.00 s / 0.00 s | 0.00 s / 0.00 s |"
    assert render_overhead_table({}) == []


def test_the_overhead_percentiles_are_the_rows_latencies() -> None:
    from evaluation.mailguard_bench.scoring import RawRecord

    rows = [
        RawRecord.from_dict(
            {
                "case_id": f"c{i}",
                "config": "C3",
                "status": "ok",
                "guard_latency_ms": 100 * i,
                "total_latency_ms": 1000 * i,
            }
        )
        for i in range(1, 6)
    ]

    row = render_overhead_table({"C3": overhead("C3", rows, {})})[4]

    assert row.endswith("| 0.30 s / 0.48 s | 3.00 s / 4.80 s |")  # p50 and p95 of 1..5


def test_the_fallback_note_names_the_configs_that_run_no_guard_ai_step() -> None:
    assert render_no_ai_stage_note(("C0", "C0T", "C3")) == [
        "C0 and C0T run no guard AI step, so they have no fallbacks to report.",
        "",
    ]
    assert render_no_ai_stage_note(("C0", "C3")) == [
        "C0 runs no guard AI step, so it has no fallbacks to report.",
        "",
    ]
    assert render_no_ai_stage_note(("C3",)) == []


def test_the_protocol_reaches_metrics_csv_as_one_value_per_cell() -> None:
    rows = scheme_v3_rows(section())
    by = {(r["metric"], r["group"]): r["value"] for r in rows if r["config"] == "C3"}

    assert by[("jobs", "")] == 7
    exits = ("gate_pass_1", "gate_pass_2", "output_guard", "delivered")
    assert [by[("exit_point", e)] for e in exits] == [1, 1, 3, 2]
    assert by[("inbound_floor_won", "")] == 2
    assert (by[("guard_calls_mean", "doc_set")], by[("guard_calls_max", "doc_chunk")]) == (0.143, 2)
    assert by[("fail_closed_jobs", "inbound_extract")] == 1
    assert by[("fail_closed_quarantined", "")] == 1 and by[("fail_closed_review", "")] == 1
    assert (by[("inbound_catch_caught", "")], by[("inbound_catch_attacks", "")]) == (1, 3)
    assert (by[("poisoned_chunk_all_caught", "")], by[("poisoned_chunk_cases", "")]) == (2, 3)


def test_the_summary_block_is_the_tables_as_numbers() -> None:
    block = section().summary()

    assert block["configs_run"] == ["C0", "C3"]
    c3 = block["protocol"]["C3"]
    assert c3["exit_points"] == {
        "gate_pass_1": 1,
        "gate_pass_2": 1,
        "output_guard": 3,
        "delivered": 2,
    }
    assert c3["guard_calls"]["doc_set"] == {"total": 1, "mean": 0.143, "max": 1}
    assert c3["inbound_catch"] == {"caught": 1, "attacks": 3}
    assert c3["poisoned_chunks"] == {"all_caught": 2, "cases": 3}


# --- build_report: the v3 path end to end, and the refusals ------------------------------------


def _build(run: Path) -> Path:
    harness, metrics, mailguard_dir = _amg_or_skip()
    from evaluation.mailguard_bench.report import build_report

    return build_report(
        run, harness=harness, metrics=metrics, prices=PRICES, mailguard_dir=mailguard_dir
    )


def test_build_report_writes_the_protocol_tables_into_report_summary_and_csv(
    tmp_path: Path,
) -> None:
    run = _run_folder(tmp_path, with_c0t=False)

    text = _build(run).read_text("utf-8")

    assert "## Config scheme v3" in text
    assert "## Where each job stopped, and why" in text
    assert "| C3 | 1 | 1 | 0 | 0 | 0 |" in text  # the one scored job with a report: pass 1
    assert "| C3 | 1 of 1 (100.0 %) | n/a |" in text  # the one scored LLMail attack was caught
    assert "### Guard overhead per job" in text
    summary = json.loads((run / "summary.json").read_text("utf-8"))
    assert summary["scheme"] == "v3"
    assert summary["scheme_v3"]["protocol"]["C3"]["exit_points"]["gate_pass_1"] == 1
    assert "C0" not in summary["scheme_v3"]["protocol"]  # C0 has no guard report
    with (run / "metrics.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    exits = {r["group"]: r["value"] for r in rows if r["metric"] == "exit_point"}
    assert exits == {"gate_pass_1": "1", "gate_pass_2": "0", "output_guard": "0", "delivered": "0"}
    manifest = json.loads((run / "manifest.json").read_text("utf-8"))
    assert manifest["scheme"] == "v3"
    assert manifest["runs"]["C3"]["guarded_prompt_version"] == "guarded.v3"


def _stub_run(tmp_path: Path, scheme: str | None) -> Path:
    """A folder of two raw files whose metas record ``scheme`` (None: no key, so v1)."""
    run = tmp_path / "old"
    (run / "raw").mkdir(parents=True)
    (run / "cases.jsonl").write_text("", "utf-8")
    (run / "case_manifest.json").write_text(
        json.dumps({"llmail_attack_ids": [], "benign_ids": []}), "utf-8"
    )
    for config, meta in (("C0", C0_META), ("C3", C3_META)):
        kept = {k: v for k, v in meta.items() if k != "scheme"}
        if scheme is not None:
            kept["scheme"] = scheme
        (run / "raw" / f"{config}.jsonl").write_text("", "utf-8")
        (run / "raw" / f"{config}.meta.json").write_text(json.dumps(kept), "utf-8")
    return run


@pytest.mark.parametrize("scheme", [None, "v1", "v2"])
def test_a_pre_v3_folder_is_refused_by_the_v3_reader_naming_the_legacy_reader(
    tmp_path: Path, scheme: str | None
) -> None:
    from types import ModuleType

    from evaluation.mailguard_bench.report import build_report

    run = _stub_run(tmp_path, scheme)

    with pytest.raises(LegacyReportError, match="legacy reader") as caught:
        build_report(
            run,
            harness=ModuleType("h"),
            metrics=ModuleType("m"),
            prices=PRICES,
            mailguard_dir=run,
        )

    assert f"scheme {scheme or 'v1'}" in str(caught.value)
    assert not (run / "report.md").exists() and not list(run.glob("ragemail__*.jsonl"))


def test_the_report_command_prints_fail_for_a_v2_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import ModuleType

    from evaluation.mailguard_bench import report

    monkeypatch.setattr(report, "load_amg_harness", lambda _d: ModuleType("h"))
    monkeypatch.setattr(report, "load_amg_metrics", lambda _d: ModuleType("m"))
    run = _stub_run(tmp_path, "v2")

    assert report.main(["--run-dir", str(run), "--mailguard-dir", str(tmp_path)]) == 1

    assert "FAIL" in capsys.readouterr().err


def test_a_run_meta_without_the_v3_scheme_is_refused_before_a_record_is_read() -> None:
    from evaluation.mailguard_bench.runmeta import require_v3_meta

    require_v3_meta(None)  # no meta is the report's own "meta is missing" problem
    require_v3_meta(C3_META)
    for old in ({**C3_META, "scheme": "v2"}, {k: v for k, v in C3_META.items() if k != "scheme"}):
        with pytest.raises(LegacyReportError, match="legacy reader"):
            require_v3_meta(old, name="raw/C3.meta.json")


def test_the_run_meta_records_the_guarded_prompt_version_and_a_resume_keeps_it(
    tmp_path: Path,
) -> None:
    from evaluation.mailguard_bench.runmeta import (
        GUARDED_PROMPT_KEY,
        guarded_prompt_version_from_meta,
        keep_recorded_scoring_meta,
        scoring_meta,
    )

    assert scoring_meta({})[GUARDED_PROMPT_KEY] == "guarded.v3"
    assert guarded_prompt_version_from_meta(scoring_meta({})) == "guarded.v3"
    assert guarded_prompt_version_from_meta({}) is None
    meta_file = tmp_path / "C3.meta.json"
    first = {**scoring_meta({}), GUARDED_PROMPT_KEY: "guarded.v2"}
    meta_file.write_text(json.dumps(first), "utf-8")

    kept: dict[str, Any] = keep_recorded_scoring_meta(scoring_meta({}), meta_file)

    assert kept[GUARDED_PROMPT_KEY] == "guarded.v2"  # the first invocation's, not the resuming one

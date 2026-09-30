"""The meaning reader and the no-API analyses under config scheme v2 (ADR-0012 decision 11).

The reader follows the scheme's configs; the analyses, written for v1's C3 (the full guard), refuse
a v2 folder instead of reading its C3 (channel isolation) as the full guard. No network, no model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.mailguard_bench import analyses
from evaluation.mailguard_bench.meaning import run_meaning
from evaluation.mailguard_bench.results import ResultStore
from tests.unit.mailguard_live_fixtures import live_meta, live_row
from tests.unit.test_mailguard_bench_meaning import BENIGN, LLMAIL, fake_reader, verdict_by_marker


def write_run(tmp_path: Path, configs: tuple[str, ...], *, meta_scheme: str | None) -> Path:
    run = tmp_path / "run"
    (run / "raw").mkdir(parents=True)
    cases = [
        {**LLMAIL, "case_id": "attack-llmail-a"},
        {**BENIGN, "case_id": "benign-llmailfp-0"},
    ]
    (run / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases), "utf-8")
    base = {"generation_model": "gpt-4o-mini"}
    meta = live_meta({**base, **({"scheme": meta_scheme} if meta_scheme else {})})
    for config in configs:
        rows = [
            live_row("attack-llmail-a", config, body="LEAK: sent to contact at contact.com"),
            live_row("benign-llmailfp-0", config, kind="benign", body="Numbers."),
        ]
        (run / "raw" / f"{config}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), "utf-8"
        )
        (run / "raw" / f"{config}.meta.json").write_text(json.dumps(meta), "utf-8")
    return run


async def test_the_meaning_reader_reads_every_config_of_a_v2_run(tmp_path: Path) -> None:
    run = write_run(tmp_path, ("C0", "C0T", "C4", "C5", "C6", "C7"), meta_scheme="v2")
    _provider, reader = fake_reader(responder=verdict_by_marker())

    summaries = await run_meaning(run, reader=reader)

    assert set(summaries) == {"C0", "C0T", "C4", "C5", "C6", "C7"}
    for config in summaries:
        rows = ResultStore(run / "analysis" / f"meaning__{config}.jsonl").latest_records()
        assert {k: v["verdict"] for k, v in rows.items()} == {"attack-llmail-a": "succeeded"}


async def test_the_meaning_reader_of_a_v1_run_reads_the_v1_configs_only(tmp_path: Path) -> None:
    # a stray C4 file in a v1 folder is no config of it; a v1 folder without a scheme key is v1
    run = write_run(tmp_path, ("C0", "C3", "C4"), meta_scheme=None)
    _provider, reader = fake_reader(responder=verdict_by_marker())

    summaries = await run_meaning(run, reader=reader)

    assert set(summaries) == {"C0", "C3"}


async def test_the_meaning_reader_refuses_a_config_of_the_other_scheme(tmp_path: Path) -> None:
    run = write_run(tmp_path, ("C0", "C3"), meta_scheme="v1")
    _provider, reader = fake_reader(responder=verdict_by_marker())

    with pytest.raises(ValueError, match="C7"):
        await run_meaning(run, reader=reader, configs=["C7"])  # no such config in v1


def test_the_analyses_refuse_a_v2_folder_and_say_what_to_run_instead(tmp_path: Path) -> None:
    run = write_run(tmp_path, ("C0", "C3", "C7"), meta_scheme="v2")

    with pytest.raises(ValueError, match="scheme v2") as caught:
        analyses.run_analyses(run, train_half=lambda: [], l1_rows=lambda _source: [])

    assert "make mailguard-report" in str(caught.value)
    assert not (run / "analyses.md").exists() and not (run / "analysis").exists()


def test_the_analyses_command_prints_fail_for_a_v2_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = write_run(tmp_path, ("C7",), meta_scheme="v2")

    assert analyses.main(["--run-dir", str(run), "--mailguard-dir", str(tmp_path)]) == 1

    assert "FAIL" in capsys.readouterr().err

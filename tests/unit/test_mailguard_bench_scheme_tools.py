"""The meaning reader and the no-API analyses under config schemes (ADR-0012 decision 11).

Both read scheme v3 only (plan_v3 Task 13): a v1 or v2 folder is refused, naming the legacy reader,
instead of reading its C3 (a different guard, or channel isolation) as the full guard. The reader
follows the v3 configs. No network, no model.
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
    if meta_scheme is None:
        meta.pop("scheme")  # written before the schemes existed: v1
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


V3_RUN_CONFIGS = ("C0", "C0T", "C1", "C2", "C3", "C3-L1", "C3-L2", "C3-L3", "C3-L4", "C3-ISO")


async def test_the_meaning_reader_reads_every_config_of_a_v3_run(tmp_path: Path) -> None:
    run = write_run(tmp_path, V3_RUN_CONFIGS, meta_scheme="v3")
    _provider, reader = fake_reader(responder=verdict_by_marker())

    summaries = await run_meaning(run, reader=reader)

    assert set(summaries) == set(V3_RUN_CONFIGS)
    for config in summaries:
        rows = ResultStore(run / "analysis" / f"meaning__{config}.jsonl").latest_records()
        assert {k: v["verdict"] for k, v in rows.items()} == {"attack-llmail-a": "succeeded"}


async def test_the_meaning_reader_of_a_v3_run_leaves_out_a_stray_v1_config_file(
    tmp_path: Path,
) -> None:
    run = write_run(tmp_path, ("C0", "C3", "C3-L5"), meta_scheme="v3")  # C3-L5 is v1's
    _provider, reader = fake_reader(responder=verdict_by_marker())

    summaries = await run_meaning(run, reader=reader)

    assert set(summaries) == {"C0", "C3"}


@pytest.mark.parametrize("scheme", [None, "v1", "v2"])
async def test_the_meaning_reader_refuses_a_pre_v3_folder(
    tmp_path: Path, scheme: str | None
) -> None:
    run = write_run(tmp_path, ("C0", "C3"), meta_scheme=scheme)  # no scheme key is v1
    _provider, reader = fake_reader(responder=verdict_by_marker())

    with pytest.raises(ValueError, match="legacy reader"):
        await run_meaning(run, reader=reader)

    assert not (run / "analysis").exists()


async def test_the_meaning_reader_refuses_a_config_of_the_other_scheme(tmp_path: Path) -> None:
    run = write_run(tmp_path, ("C0", "C3"), meta_scheme="v3")
    _provider, reader = fake_reader(responder=verdict_by_marker())

    with pytest.raises(ValueError, match="C7"):
        await run_meaning(run, reader=reader, configs=["C7"])  # no such config in v3


@pytest.mark.parametrize("scheme", ["v1", "v2"])
def test_the_analyses_refuse_a_pre_v3_folder_and_say_what_to_run_instead(
    tmp_path: Path, scheme: str
) -> None:
    run = write_run(tmp_path, ("C0", "C3", "C7"), meta_scheme=scheme)

    with pytest.raises(ValueError, match=f"scheme {scheme}") as caught:
        analyses.run_analyses(run, train_half=lambda: [], l1_rows=lambda _source: [])

    assert "LEGACY=1" in str(caught.value)
    assert not (run / "analyses.md").exists() and not (run / "analysis").exists()


def test_the_analyses_command_prints_fail_for_a_pre_v3_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = write_run(tmp_path, ("C7",), meta_scheme="v2")

    assert analyses.main(["--run-dir", str(run), "--mailguard-dir", str(tmp_path)]) == 1

    assert "FAIL" in capsys.readouterr().err

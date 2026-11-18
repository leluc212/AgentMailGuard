"""Benchmark export for the demo console (task 9.14, R25.10)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "export_bench_for_demo",
    Path(__file__).resolve().parents[2] / "scripts" / "export_bench_for_demo.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
export = _MODULE.export


def _run(root: Path, name: str, files: tuple[str, ...] = ("summary.json", "metrics.csv")) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    for f in files:
        (folder / f).write_text("{}" if f.endswith(".json") else "config,asr\nC0,0.4\n", "utf-8")


def test_only_complete_v3_1800_runs_are_exported(tmp_path: Path) -> None:
    results, out = tmp_path / "results", tmp_path / "out"
    _run(results, "2026-10-19-gpt-4o-mini-v3-1800")
    _run(results, "2026-10-19-qwen2.5-7b-v3-1800", files=("summary.json",))  # incomplete
    _run(results, "2026-09-30-gpt-4o-mini-v2-550")  # another scheme

    runs = export(results, out)

    assert [r["model"] for r in runs] == ["gpt-4o-mini"]
    index = json.loads((out / "index.json").read_text("utf-8"))
    assert index["runs"][0]["metrics"] == "/bench/2026-10-19-gpt-4o-mini-v3-1800/metrics.csv"
    assert (out / "2026-10-19-gpt-4o-mini-v3-1800" / "metrics.csv").is_file()

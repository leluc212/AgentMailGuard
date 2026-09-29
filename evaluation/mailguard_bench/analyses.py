"""No-API analyses of a scored run: leakage, first catching layer, worked examples, threats.

Run after ``evaluation.mailguard_bench.report`` has written ``ragemail__<config>.jsonl``::

    make mailguard-analyses RUN=<run_id>

Writes ``analysis/leakage.json``, ``analysis/first_layer.csv`` and ``analyses.md``; the
next report build adds the near-duplicate-free headline and appends ``analyses.md``.

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4b "No-API analyses";
specs/tasks.md 7.19; R22.12)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.amg import resolve_mailguard_dir
from evaluation.mailguard_bench.examples import pick_examples, render_examples
from evaluation.mailguard_bench.first_layer import first_catching_layer, render_first_layer, tally
from evaluation.mailguard_bench.leakage import (
    LeakageResult,
    case_text,
    check,
    l1_train_rows,
    llmail_train_half,
    render_leakage,
)
from evaluation.mailguard_bench.report import load_cases
from evaluation.mailguard_bench.scoring import RawRecord, read_raw
from evaluation.mailguard_bench.threat_model import render_threat_model

ATTACKS_VS_TRAIN_HALF = "attacks_vs_train_half"
ATTACKS_VS_L1_ROWS = "attacks_vs_l1_train_rows"
BENIGN_VS_L1_ROWS = "benign_vs_l1_train_rows"


def _scored(run_dir: Path, config: str) -> dict[str, dict[str, Any]]:
    path = run_dir / f"ragemail__{config}.jsonl"
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                out[str(row["case_id"])] = row
    return out


def _records(run_dir: Path, config: str) -> dict[str, RawRecord]:
    path = run_dir / "raw" / f"{config}.jsonl"
    return {r.case_id: r for r in read_raw(path)} if path.exists() else {}


def run_leakage(
    cases: dict[str, dict[str, Any]],
    attack_ids: Sequence[str],
    benign_ids: Sequence[str],
    *,
    train_half: Callable[[], list[str]],
    l1_rows: Callable[[str], list[str]],
) -> list[LeakageResult]:
    """The three leakage checks (reference loaders injected so tests need no data)."""
    attacks = {i: case_text(cases[i]) for i in attack_ids if i in cases}
    benign = {i: case_text(cases[i]) for i in benign_ids if i in cases}
    return [
        check(ATTACKS_VS_TRAIN_HALF, attacks, train_half()),
        check(ATTACKS_VS_L1_ROWS, attacks, l1_rows("llmail_attack")),
        check(BENIGN_VS_L1_ROWS, benign, l1_rows("llmail_fp")),
    ]


def run_analyses(
    run_dir: Path,
    *,
    train_half: Callable[[], list[str]],
    l1_rows: Callable[[str], list[str]],
) -> Path:
    """Write the analysis files for ``run_dir`` and return the ``analyses.md`` path.

    Raises:
        FileNotFoundError: If the run has no scored C3 results yet.
    """
    cases = load_cases(run_dir / "cases.jsonl")
    manifest = json.loads((run_dir / "case_manifest.json").read_text(encoding="utf-8"))
    llmail_ids = set(manifest["llmail_attack_ids"]) | set(manifest["benign_ids"])
    c0, c3 = _scored(run_dir, "C0"), _scored(run_dir, "C3")
    if not c3:
        raise FileNotFoundError(f"no ragemail__C3.jsonl in {run_dir}; run the report first")
    c0_records, c3_records = _records(run_dir, "C0"), _records(run_dir, "C3")
    out_dir = run_dir / "analysis"
    out_dir.mkdir(exist_ok=True)

    leakage = run_leakage(
        cases,
        manifest["llmail_attack_ids"],
        manifest["benign_ids"],
        train_half=train_half,
        l1_rows=l1_rows,
    )
    (out_dir / "leakage.json").write_text(
        json.dumps({r.name: r.to_dict() for r in leakage}, indent=2) + "\n", encoding="utf-8"
    )

    layers: dict[str, str | None] = {}
    for case_id, row in c3.items():
        if row.get("kind") == "attack" and case_id in c3_records:
            layers[case_id] = first_catching_layer(
                c3_records[case_id],
                goal=bool(row["goal_achieved"]),
                goal_pre_l4=bool((row.get("extra") or {}).get("goal_pre_l4")),
            )
    llmail_layers = [v for k, v in layers.items() if k in llmail_ids]
    rag_ids = set(manifest.get("rag_attack_ids") or [])
    with (out_dir / "first_layer.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "table", "first_catching_layer"])
        for case_id in sorted(layers):
            table = "llmail" if case_id in llmail_ids else ("rag" if case_id in rag_ids else "")
            writer.writerow([case_id, table, layers[case_id] or "attack succeeded"])

    examples = pick_examples(cases, c0, c3, c0_records, c3_records, layers, llmail_ids=llmail_ids)
    sections = [
        render_leakage(leakage),
        render_first_layer(tally(llmail_layers)),
        render_examples(examples),
        render_threat_model(),
    ]
    path = run_dir / "analyses.md"
    path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; prints ``ANALYSES OK <path>`` or ``FAIL ...``."""
    parser = argparse.ArgumentParser(description="No-API analyses of a benchmark run")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mailguard-dir", type=Path, default=None)
    parser.add_argument("--l1-artifacts", type=Path, default=None)
    args = parser.parse_args(argv)
    mailguard_dir: Path = args.mailguard_dir or resolve_mailguard_dir()
    env_artifacts = os.environ.get("MAILGUARD_ARTIFACTS")
    artifacts: Path | None = args.l1_artifacts or (Path(env_artifacts) if env_artifacts else None)
    print(f"leakage references: worktree {mailguard_dir}, L1 corpus under {artifacts}")
    try:
        path = run_analyses(
            args.run_dir,
            train_half=llmail_train_half,
            l1_rows=lambda source: l1_train_rows(artifacts, source) if artifacts else [],
        )
    except (FileNotFoundError, KeyError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"ANALYSES OK {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

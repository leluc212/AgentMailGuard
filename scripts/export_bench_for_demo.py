"""Copy the committed v3 benchmark results into the demo console's static assets (task 9.14).

ADR-0018 decision 10, R25.10: the console shows committed results read-only and never runs a
case. Each ``evaluation/results/mailguard_bench/<date>-<model>-v3-1800`` folder with a
``summary.json`` and a ``metrics.csv`` is copied to ``services/demo_ui/public/bench/<folder>/``,
and ``index.json`` lists them. Run it before building the console image.

    uv run python scripts/export_bench_for_demo.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "evaluation" / "results" / "mailguard_bench"
OUT = REPO / "services" / "demo_ui" / "public" / "bench"
RUN_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<model>.+)-v3-1800$")
FILES = ("summary.json", "metrics.csv")


def export(results: Path = RESULTS, out: Path = OUT) -> list[dict[str, str]]:
    """Copy every complete v3 1,800-case run; return the index entries written."""
    runs: list[dict[str, str]] = []
    for folder in sorted(p for p in results.glob("*") if p.is_dir()):
        match = RUN_PATTERN.match(folder.name)
        if match is None or not all((folder / f).is_file() for f in FILES):
            continue
        target = out / folder.name
        target.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            shutil.copyfile(folder / name, target / name)
        runs.append(
            {
                "id": folder.name,
                "model": match.group("model"),
                "summary": f"/bench/{folder.name}/summary.json",
                "metrics": f"/bench/{folder.name}/metrics.csv",
            }
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps({"runs": runs}, indent=2), encoding="utf-8")
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    runs = export(args.results, args.out)
    print(f"exported {len(runs)} run(s) to {args.out}")
    return 0 if runs else 1


if __name__ == "__main__":
    sys.exit(main())

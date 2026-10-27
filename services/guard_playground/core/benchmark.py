"""Benchmark_Record table of a pinned case (Requirement 6). Read-only, no model call.

Each ``ragemail__<config>.jsonl`` of the three 1,800-case runs is indexed by case id the first
time it is needed, then kept in memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from services.guard_playground.core.kpi import RUNS

NO_RECORD = "no record"
UNREADABLE = "record unreadable"


@dataclass(frozen=True)
class Cell:
    text: str  # "goal yes · blocked inbound · P01-..." or NO_RECORD / UNREADABLE
    goal: str | None = None
    blocked: str | None = None
    rule: str | None = None


@dataclass(frozen=True)
class Row:
    model: str
    run_dir: str
    cells: list[Cell]


class BenchmarkIndex:
    def __init__(self, results_root: Path) -> None:
        self.results_root = results_root
        self._files: dict[tuple[str, str], dict[str, str] | None] = {}

    def _index(self, run_dir: str, preset: str) -> dict[str, str] | None:
        key = (run_dir, preset)
        if key not in self._files:
            path = self.results_root / run_dir / f"ragemail__{preset}.jsonl"
            lines: dict[str, str] | None = {}
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        start = line.find('"case_id": "')
                        if start < 0:
                            continue
                        start += len('"case_id": "')
                        end = line.find('"', start)
                        lines[line[start:end]] = line  # type: ignore[index]
            except FileNotFoundError:
                lines = {}
            except (OSError, UnicodeDecodeError):
                lines = None
            self._files[key] = lines
        return self._files[key]

    def cell(self, run_dir: str, preset: str, case_id: str) -> Cell:
        index = self._index(run_dir, preset)
        if index is None:
            return Cell(UNREADABLE)
        line = index.get(case_id)
        if line is None:
            return Cell(NO_RECORD)
        try:
            row = json.loads(line)
        except ValueError:
            return Cell(UNREADABLE)
        goal = "yes" if row.get("goal_achieved") else "no"
        if row.get("blocked_inbound"):
            blocked = "yes (inbound)"
        elif row.get("blocked_outbound"):
            blocked = "yes (outbound)"
        else:
            blocked = "no"
        rule = row.get("rule") or "none"
        return Cell(f"goal {goal} · blocked {blocked} · {rule}", goal, blocked, rule)

    def table(self, case_id: str, presets: list[str]) -> list[Row]:
        return [
            Row(model, run_dir, [self.cell(run_dir, p, case_id) for p in presets])
            for run_dir, model in RUNS
        ]

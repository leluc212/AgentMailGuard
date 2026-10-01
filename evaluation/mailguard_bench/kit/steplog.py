"""The kit log: ``<run folder>/kit-log.jsonl``, one JSON line per step (task 7.23).

The teammate reports timings from it, and a rerun of the same command reads it to skip what
already finished. A step is written when it ends, so a machine that dies mid-step leaves no line
for it; a torn last line is skipped when the log is read.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

LOG_NAME = "kit-log.jsonl"


class StepLog:
    """Append-only JSON lines. ``path=None`` (a dry run) writes nothing and reads nothing."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def append(self, record: dict[str, Any]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
        with self._path.open("ab+") as handle:
            torn = False
            if handle.seek(0, os.SEEK_END) > 0:  # a last line the machine died in: start a new one
                handle.seek(-1, os.SEEK_END)
                torn = handle.read(1) != b"\n"
            handle.write((b"\n" if torn else b"") + line)
            handle.flush()

    def records(self) -> list[dict[str, Any]]:
        if self._path is None or not self._path.is_file():
            return []
        found: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn line
            if isinstance(record, dict):
                found.append(record)
        return found


def finished_configs(
    records: Sequence[dict[str, Any]],
    model_profile: str,
    limit: int | None,
    case_ids: Sequence[str] | None = None,
) -> set[str]:
    """Configs whose last step was clean and complete for this model, limit and case list.

    Clean and complete: the runner exited 0, recorded no error row, and every selected case is
    recorded (written by that start or by an earlier one). ``limit``, ``case_ids`` and the model
    are part of the answer: a finished ``--limit 5`` smoke run or a trial over chosen cases is not
    a finished run. A step logged before the case list existed has none.
    """
    wanted = list(case_ids) if case_ids is not None else None
    last: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("step") == "config" and isinstance(record.get("config"), str):
            last[record["config"]] = record
    done: set[str] = set()
    for config, record in last.items():
        counts = record.get("counts")
        if (
            record.get("status") == "ok"
            and record.get("model_profile") == model_profile
            and record.get("limit") == limit
            and record.get("case_ids") == wanted
            and isinstance(counts, dict)
            and counts.get("selected", 0) > 0
            and counts.get("error", 1) == 0
            and counts.get("ok", 0) + counts.get("skipped_already_recorded", 0)
            == counts["selected"]
        ):
            done.add(config)
    return done

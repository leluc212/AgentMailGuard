"""Append-only per-case result rows with resume (task 7.19, spec §4b resilience).

Every case is appended and fsynced when it finishes. A resumed run reads the file, and the
latest row per case_id wins. A line torn by a crash is skipped with a warning (that case has
no row, so it simply runs again), and the next append starts on a fresh line.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RESULT_SCHEMA = "mailguard-bench-result.v1"


class ResultStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.skipped_lines = 0

    def latest_records(self) -> dict[str, dict[str, Any]]:
        """case_id -> latest row. Unparseable (torn) lines are skipped and counted."""
        self.skipped_lines = 0
        latest: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return latest
        with self.path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    self.skipped_lines += 1
                    logger.warning("skipping torn result line %d in %s", line_no, self.path)
                    continue
                if isinstance(row, dict) and row.get("case_id"):
                    latest[str(row["case_id"])] = row
        return latest

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prefix = "\n" if self._ends_mid_line() else ""
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _ends_mid_line(self) -> bool:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return False
        with self.path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"

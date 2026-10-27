"""Replay_Store: precomputed ``RunResult`` files (design.md "core/schema.py"; Requirement 7).

Files live in ``<data_dir>/replay/<case_id>__<preset>__<model_key>.json``. The name is split on
``"__"`` from the right, so a case id may itself contain ``"__"``. A file that fails schema
validation or whose content disagrees with its name is skipped and recorded in ``invalid``
with a short reason that never quotes the file content (Requirement 7.12); its case, preset
and model then report "replay unavailable" (Requirement 7.13).

Loading only reads files; ``write`` is used by ``scripts/playground/build_replay.py``.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.guard_playground.core.redact import redact_data
from services.guard_playground.core.schema import RunResult

# Fixed Draft_Model order (Requirement 7.14, 7.17); unknown model keys sort after these.
MODEL_ORDER: tuple[str, ...] = ("gpt-4o-mini", "qwen", "llama")
SEPARATOR = "__"
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "replay unavailable"

Key = tuple[str, str, str]  # (case_id, preset, model_key)
Status = Literal["ok", "replay unavailable"]


def split_name(stem: str) -> Key | None:
    """``<case_id>__<preset>__<model_key>`` -> key; None when a part is missing or empty."""
    parts = stem.rsplit(SEPARATOR, 2)
    if len(parts) != 3 or not all(parts):
        return None
    case_id, preset, model_key = parts
    return case_id, preset, model_key


def file_name(case_id: str, preset: str, model_key: str) -> str:
    return f"{case_id}{SEPARATOR}{preset}{SEPARATOR}{model_key}.json"


def _model_rank(model_key: str) -> tuple[int, str]:
    if model_key in MODEL_ORDER:
        return MODEL_ORDER.index(model_key), ""
    return len(MODEL_ORDER), model_key


def _check_name_part(label: str, value: str) -> None:
    if not value or SEPARATOR in value or any(c in value for c in '/\\:*?"<>|'):
        raise ValueError(f"{label} {value!r} cannot be used in a Replay_Store file name")


@dataclass
class ReplayStore:
    """Valid entries keyed by (case_id, preset, model_key), plus the skipped files."""

    entries: dict[Key, RunResult] = field(default_factory=dict)
    # (case_id, preset, model_key, reason); reason never contains file content.
    invalid: list[tuple[str, str, str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, directory: Path) -> ReplayStore:
        """Read every ``*.json`` in ``directory``; a missing directory gives an empty store."""
        store = cls()
        if not directory.is_dir():
            return store
        for path in sorted(directory.glob("*.json")):
            key = split_name(path.stem)
            if key is None:
                store.invalid.append((path.stem, "", "", "file name is not case__preset__model"))
                continue
            reason = store._load_one(path, key)
            if reason is not None:
                store.invalid.append((*key, reason))
        return store

    def _load_one(self, path: Path, key: Key) -> str | None:
        """Add the file's entry; return a skip reason instead when it is not usable."""
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return f"unreadable file ({type(exc).__name__})"
        try:
            entry = RunResult.model_validate_json(raw)
        except ValidationError as exc:
            # Only the count and the first error location: error messages may echo input.
            first = exc.errors(include_input=False, include_url=False)[0]
            location = ".".join(str(part) for part in first["loc"]) or "<root>"
            return f"schema validation failed ({exc.error_count()} errors, first at {location})"
        case_id, preset, model_key = key
        if entry.case_id != case_id:
            return "case_id in file does not match file name"
        if entry.preset != preset:
            return "preset in file does not match file name"
        if entry.draft_model != model_key:
            return "draft_model in file does not match file name"
        self.entries[key] = entry
        return None

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, case_id: str, preset: str, model_key: str) -> RunResult | None:
        return self.entries.get((case_id, preset, model_key))

    def has(self, case_id: str, preset: str) -> bool:
        return any(k[0] == case_id and k[1] == preset for k in self.entries)

    def models_for(self, case_id: str, preset: str) -> list[str]:
        """Model keys with a valid entry: gpt-4o-mini, qwen, llama first, then others by name."""
        keys = {k[2] for k in self.entries if k[0] == case_id and k[1] == preset}
        return sorted(keys, key=_model_rank)

    def unavailable(self, case_id: str, preset: str, model_key: str) -> bool:
        """True when the entry is missing or its file was skipped as invalid."""
        return (case_id, preset, model_key) not in self.entries

    def status(self, case_id: str, preset: str, model_key: str) -> Status:
        return STATUS_UNAVAILABLE if self.unavailable(case_id, preset, model_key) else STATUS_OK


def write(entry: RunResult, directory: Path, model_key: str) -> Path:
    """Write ``entry`` atomically (temp file + ``os.replace``) after redacting secrets.

    Raises ``ValueError`` for an entry without ``case_id`` or a name part that cannot be
    stored in a file name; an existing file is left untouched on any failure.
    """
    if not entry.case_id:
        raise ValueError("Replay_Store entries need a case_id")
    _check_name_part("case_id", entry.case_id)
    _check_name_part("preset", entry.preset)
    _check_name_part("model_key", model_key)
    redacted = redact_data(entry)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / file_name(entry.case_id, entry.preset, model_key)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(redacted.model_dump_json(indent=2))
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target

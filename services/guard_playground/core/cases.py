"""Sample_Case catalog (design.md ``core/cases.py``; Requirements 1.1, 1.2, 1.5, 1.6, 1.7).

The catalog is built once at startup from the pinned benchmark dataset ``cases.jsonl`` and the
guard's ``tests/fixtures/emails.json``. Bad lines are skipped and counted; a missing or unreadable
``cases.jsonl`` is recorded as ``CaseCatalog.error`` so the page can say so instead of failing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.guard_playground.settings import PlaygroundSettings

CASE_KINDS = ("attack", "rag_poison", "benign")
ALL = "all"
FIXTURE_SOURCE = "fixture"


@dataclass(frozen=True)
class Case:
    case_id: str
    kind: str  # Case_Kind: attack | rag_poison | benign
    source: str  # Case_Source: the dataset ``source`` value, or "fixture"
    origin: str  # "pinned" (cases.jsonl) | "fixture" (emails.json)
    subject: str
    body: str
    chunks: tuple[tuple[str, str], ...] = ()  # (chunk_id, content)
    technique: str = ""


@dataclass
class CaseCatalog:
    cases: list[Case] = field(default_factory=list)
    excluded_lines: int = 0
    error: str | None = None  # cases.jsonl missing or unreadable
    notes: list[str] = field(default_factory=list)  # e.g. fixtures file missing
    _by_id: dict[str, Case] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for case in self.cases:
            self._by_id.setdefault(case.case_id, case)

    @property
    def sources(self) -> list[str]:
        return sorted({case.source for case in self.cases})

    def get(self, case_id: str) -> Case | None:
        return self._by_id.get(case_id)

    def __len__(self) -> int:
        return len(self.cases)


def case_kind(kind: str, vector: str | None) -> str:
    """Map dataset ``kind``/``vector`` to Case_Kind (kind and vector take precedence)."""
    if kind == "benign":
        return "benign"
    if vector == "rag":
        return "rag_poison"
    return "attack"


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _parse_chunks(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    chunks: list[tuple[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        chunk_id = item.get("chunk_id")
        chunks.append((str(chunk_id) if chunk_id else f"chunk-{index}", content))
    return tuple(chunks)


def _parse_line(line: str) -> Case | None:
    """One ``cases.jsonl`` line to a Case; None when it can't be parsed or lacks a field."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    email = record.get("email")
    if not isinstance(email, dict):
        email = {}
    case_id, kind = record.get("case_id"), record.get("kind")
    subject, body = email.get("subject"), email.get("body_text")
    if not (_non_empty_str(case_id) and _non_empty_str(kind)):
        return None
    if not isinstance(subject, str) or not _non_empty_str(body):
        return None
    vector = record.get("vector")
    source = record.get("source")
    technique = record.get("technique")
    return Case(
        case_id=case_id,
        kind=case_kind(kind, vector if isinstance(vector, str) else None),
        source=source if _non_empty_str(source) else "unknown",
        origin="pinned",
        subject=subject,
        body=body,
        chunks=_parse_chunks(record.get("chunks")),
        technique=technique if isinstance(technique, str) else "",
    )


def _load_pinned(path: Path) -> tuple[list[Case], int, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0, f"Case dataset not found: {path}"
    except (OSError, UnicodeDecodeError) as exc:
        return [], 0, f"Case dataset unreadable: {path} ({type(exc).__name__})"
    cases: list[Case] = []
    excluded = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        case = _parse_line(line)
        if case is None:
            excluded += 1
        else:
            cases.append(case)
    return cases, excluded, None


def _load_fixtures(path: Path) -> tuple[list[Case], str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], f"Fixture emails not found: {path}"
    except (OSError, ValueError) as exc:
        return [], f"Fixture emails unreadable: {path} ({type(exc).__name__})"
    if not isinstance(data, dict):
        return [], f"Fixture emails unreadable: {path} (unexpected format)"
    cases: list[Case] = []
    for group, kind in (("benign", "benign"), ("attacks", "attack")):
        items = data.get(group)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            subject, body = item.get("subject"), item.get("body_text")
            if not isinstance(subject, str) or not _non_empty_str(body):
                continue
            message_id = item.get("message_id")
            suffix = message_id if _non_empty_str(message_id) else f"{kind}-{index}"
            cases.append(
                Case(
                    case_id=f"fixture-{suffix}",
                    kind=kind,
                    source=FIXTURE_SOURCE,
                    origin="fixture",
                    subject=subject,
                    body=body,
                )
            )
    return cases, None


def load_cases(settings: PlaygroundSettings) -> CaseCatalog:
    """Pinned cases then fixtures; never raises for missing or bad data."""
    pinned, excluded, error = _load_pinned(Path(settings.cases_path))
    fixtures, note = _load_fixtures(Path(settings.fixtures_path))
    return CaseCatalog(
        cases=pinned + fixtures,
        excluded_lines=excluded,
        error=error,
        notes=[note] if note else [],
    )


def filter_cases(catalog: CaseCatalog, kind: str = ALL, source: str = ALL) -> list[Case]:
    """Cases whose Case_Kind and Case_Source both match; "all" matches everything."""
    return [
        case
        for case in catalog.cases
        if (kind == ALL or case.kind == kind) and (source == ALL or case.source == source)
    ]


def demo_set(settings: PlaygroundSettings) -> list[str]:
    """Case ids of the Demo_Set from ``data_dir/demo_set.json``; empty when missing or bad."""
    path = Path(settings.data_dir) / "demo_set.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        return []
    return [item for item in ids if isinstance(item, str)]

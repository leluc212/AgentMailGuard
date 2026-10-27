"""Custom_Email input helpers (decision D12; Requirements 2.1-2.8)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from services.guard_playground.core.cases import CaseCatalog
from services.guard_playground.core.limits import MAX_CHUNKS


def split_subject(subject: str, body: str) -> tuple[str, str]:
    """Requirement 2.7: an empty subject takes a leading ``Subject:`` line of the body."""
    if subject.strip():
        return subject, body
    first, sep, rest = body.partition("\n")
    if first.strip().lower().startswith("subject:"):
        return first.strip()[len("subject:") :].strip(), rest.lstrip("\r\n")
    return subject, body


@dataclass(frozen=True)
class Sample:
    key: str
    label: str
    case_id: str
    subject: str
    body: str
    chunks: tuple[str, ...]


def load_samples(data_dir: Path, catalog: CaseCatalog) -> list[Sample]:
    """The one-click samples (Requirement 2.8); entries whose case is missing are skipped."""
    try:
        raw = json.loads((data_dir / "samples.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    samples: list[Sample] = []
    for item in raw.get("samples", []) if isinstance(raw, dict) else []:
        case = catalog.get(str(item.get("case_id", "")))
        if case is None:
            continue
        samples.append(
            Sample(
                key=str(item["key"]),
                label=str(item["label"]),
                case_id=case.case_id,
                subject=case.subject,
                body=case.body,
                chunks=tuple(content for _id, content in case.chunks[:MAX_CHUNKS]),
            )
        )
    return samples[:6]

"""Loaders for the hand-written seed corpus in ``datasets/seed/``.

knowledge base   datasets/seed/support_kb/*.md      -> chunks (one per '##' section)
benign emails    datasets/seed/benign_emails.jsonl  -> 40 realistic support requests
attack templates datasets/seed/attack_templates.yaml -> 24 email-injection templates
poison templates datasets/seed/poison_templates.yaml -> 6 PoisonedRAG-style passages
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mailguard.config.settings import PROJECT_ROOT

SEED_DIR = PROJECT_ROOT / "datasets" / "seed"
KB_DIR = SEED_DIR / "support_kb"

DEFAULT_ATTACKER = {
    "email": "exfil@attacker-mail.example",
    "url": "http://attacker-portal.example/verify",
    "phone": "+1-555-0199",
}


@dataclass(frozen=True)
class KBChunk:
    chunk_id: str
    document_id: str
    title: str
    content: str

    def as_dict(self, poisoned: bool = False) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "content": self.content,
            "metadata": {"title": self.title, "poisoned": poisoned},
            "poisoned": poisoned,
        }


def load_kb(kb_dir: Path = KB_DIR) -> list[KBChunk]:
    chunks: list[KBChunk] = []
    for md in sorted(kb_dir.glob("*.md")):
        doc_id = md.stem
        text = md.read_text(encoding="utf-8")
        title_match = re.match(r"#\s+(.+)", text)
        doc_title = title_match.group(1).strip() if title_match else doc_id
        sections = re.split(r"(?m)^##\s+", text)
        for i, sec in enumerate(sections[1:], start=1):
            heading, _, body = sec.partition("\n")
            content = f"{doc_title} - {heading.strip()}\n{body.strip()}"
            chunks.append(KBChunk(f"{doc_id}-{i:02d}", doc_id, heading.strip(), content))
    return chunks


def load_benign_emails(path: Path = SEED_DIR / "benign_emails.jsonl") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def load_attack_templates(path: Path = SEED_DIR / "attack_templates.yaml") -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("templates", []))


def load_poison_templates(path: Path = SEED_DIR / "poison_templates.yaml") -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("poisons", []))


def fill(text: str, attacker: dict[str, str]) -> str:
    for k, v in attacker.items():
        text = text.replace("{attacker_" + k + "}", v)
    return text


_TOKEN = re.compile(r"[a-z0-9]{3,}")


def simple_retrieve(query: str, chunks: list[KBChunk], k: int = 3) -> list[KBChunk]:
    """Tiny lexical retriever (token overlap) standing in for hybrid RAG in the benchmark."""
    q = set(_TOKEN.findall(query.lower()))
    if not q:
        return chunks[:k]
    scored = []
    for c in chunks:
        toks = set(_TOKEN.findall(c.content.lower()))
        overlap = len(q & toks)
        scored.append((overlap, c))
    scored.sort(key=lambda x: (-x[0], x[1].chunk_id))
    return [c for s, c in scored[:k] if s > 0] or chunks[:k]


__all__ = [
    "DEFAULT_ATTACKER",
    "KB_DIR",
    "SEED_DIR",
    "KBChunk",
    "fill",
    "load_attack_templates",
    "load_benign_emails",
    "load_kb",
    "load_poison_templates",
    "simple_retrieve",
]

"""Leakage check: sampled cases vs AgentMailGuard's classifier-training data (no API).

TF-IDF cosine similarity (word unigrams, sublinear tf, l2-normalised, so the dot product
is the cosine) of every sampled case against a reference set; a case with cosine >= 0.9
to any reference text is a near-duplicate. Three checks are run:

- attacks vs the full LLMail training half (``iter_llmail_attacks(side="train")``), a
  conservative superset of what the L1 classifier saw; the headline ASR is also reported
  without these near-duplicates;
- attacks vs the LLMail rows of the L1 corpus ``train.jsonl`` (what the classifier fitted);
- benign emails vs the ``llmail_fp`` rows of that ``train.jsonl`` (the benchmark's benign
  emails are also L1 negatives, so FPR is optimistic for L1 unless they are excluded).

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4b "Leakage check";
specs/tasks.md 7.19; R22.12)
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

NEAR_DUP_THRESHOLD = 0.9
L1_TRAIN_ROWS = Path("l1_injection") / "train.jsonl"  # under MAILGUARD_ARTIFACTS (Task 1)


def case_text(case: Mapping[str, Any]) -> str:
    """``Subject: <subject>\\n<body>``, the text shape of the L1 corpus rows."""
    email = case.get("email") or {}
    subject = str(email.get("subject") or "")
    body = str(email.get("body_text") or email.get("body") or "")
    return f"Subject: {subject}\n{body}"


def max_cosine(
    queries: Sequence[str], reference: Sequence[str], *, batch_size: int = 2048
) -> tuple[list[float], list[int]]:
    """Highest TF-IDF cosine of each query to any reference text, and that text's index.

    The vocabulary and IDF are fitted on queries plus reference; the reference is scored
    in batches so memory stays bounded. With an empty reference every score is 0.0 and
    every index -1.
    """
    if not queries:
        return [], []
    if not reference:
        return [0.0] * len(queries), [-1] * len(queries)
    vectorizer = TfidfVectorizer(lowercase=True, sublinear_tf=True, dtype=np.float32)
    vectorizer.fit(list(reference) + list(queries))
    q = vectorizer.transform(list(queries))
    best = np.zeros(len(queries), dtype=np.float64)
    where = np.full(len(queries), -1, dtype=np.int64)
    rows = np.arange(len(queries))
    for start in range(0, len(reference), batch_size):
        block = vectorizer.transform(list(reference[start : start + batch_size]))
        sims = linear_kernel(q, block)
        idx = sims.argmax(axis=1)
        top = sims[rows, idx]
        better = top > best
        best[better] = top[better]
        where[better] = idx[better] + start
    return [float(min(1.0, v)) for v in best], [int(i) for i in where]


@dataclass(frozen=True)
class LeakageResult:
    """One check: sampled cases against one reference set."""

    name: str
    n_reference: int
    threshold: float
    max_cosine_by_case: dict[str, float]

    @property
    def ran(self) -> bool:
        """False when the reference data was not available (nothing to compare)."""
        return self.n_reference > 0

    @property
    def near_duplicate_ids(self) -> list[str]:
        """Case ids with cosine >= threshold, sorted."""
        return sorted(k for k, v in self.max_cosine_by_case.items() if v >= self.threshold)

    def to_dict(self) -> dict[str, Any]:
        """JSON form stored in ``analysis/leakage.json``."""
        return {
            "name": self.name,
            "n_reference": self.n_reference,
            "n_cases": len(self.max_cosine_by_case),
            "threshold": self.threshold,
            "near_duplicate_ids": self.near_duplicate_ids,
            "max_cosine_by_case": {k: round(v, 4) for k, v in self.max_cosine_by_case.items()},
        }


def check(
    name: str,
    cases: Mapping[str, str],
    reference: Sequence[str],
    *,
    threshold: float = NEAR_DUP_THRESHOLD,
) -> LeakageResult:
    """Run one check over ``{case_id: text}`` against ``reference``."""
    ids = list(cases)
    scores, _ = max_cosine([cases[i] for i in ids], reference)
    return LeakageResult(name, len(reference), threshold, dict(zip(ids, scores, strict=True)))


def llmail_train_half() -> list[str]:
    """Every attack of the LLMail training half, as AgentMailGuard splits it.

    Imports mailguard lazily; needs the raw LLMail files in the worktree (empty list
    otherwise, which the report states as "not run").
    """
    from mailguard.datasets.build_email_benchmark import iter_llmail_attacks

    return [f"Subject: {it['subject']}\n{it['body']}" for it in iter_llmail_attacks(side="train")]


def l1_train_rows(artifacts_dir: Path, source: str) -> list[str]:
    """Texts of the L1 corpus ``train.jsonl`` rows from ``source`` (empty when absent).

    ``artifacts_dir`` is MAILGUARD_ARTIFACTS: ``make mailguard-prep`` writes the corpus
    there, outside the worktree, so the guard's tracked files stay untouched.
    """
    path = artifacts_dir / L1_TRAIN_ROWS
    if not path.exists():
        return []
    out: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get("source") == source:
                    out.append(str(row.get("text") or ""))
    return out


def render_leakage(results: Sequence[LeakageResult]) -> str:
    """Markdown section for ``analyses.md``."""
    lines = [
        "## Leakage check (TF-IDF cosine, no API)",
        "",
        "| Check | reference texts | cases | near-duplicates (cosine ≥ 0.9) |",
        "|---|---|---|---|",
    ]
    for r in results:
        dup = str(len(r.near_duplicate_ids)) if r.ran else "not run (reference data absent)"
        lines.append(f"| {r.name} | {r.n_reference} | {len(r.max_cosine_by_case)} | {dup} |")
    lines += [
        "",
        "The benchmark and training halves are disjoint by exact subject + body (the split "
        "hashes the sha1 of the labelled subject + body key). "
        "LLMail submissions are often near-copies of each other, so the headline ASR is "
        "also reported without the near-duplicates of the training half. The benign emails "
        "come from `emails_for_fp_tests.json`, which AgentMailGuard also used as L1 "
        "negatives, so FPR is also reported without them.",
    ]
    return "\n".join(lines)

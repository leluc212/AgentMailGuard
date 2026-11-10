"""Overlap checks of a benchmark case set (task 7.36; R22.12).

Fix steps 2 and 3 of docs/benchmark-data-leaks-2026-11-06.md.

Pure functions, no ``mailguard`` import, so CI tests them on synthetic cases. ``build_cases``
calls ``require_clean`` on its selection and fails, naming the ids, when a case

  - shares its content with another case (``duplicates``),
  - has its content in the L1 classifier's corpus (``in_corpus``),
  - is a PoisonedRAG question of the Doc Scanner tuning split (``in_tuning``), or
  - comes from a raw unit the partition did not give to the benchmark (``outside_partition``).

Nothing is dropped here: dropping is the builder's declared pool rule, recorded in the manifest.

The content of a case: an email-vector case is its email (``text_key`` of subject and body, plus
the keys of the untruncated raw text in ``meta["source_keys"]``, since the builder cuts long
emails); a RAG case is its poisoned chunks (its email is a templated question many RAG cases
share) plus their ``meta["source_keys"]``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from evaluation.mailguard_bench.cases import CaseManifestError
from evaluation.mailguard_bench.partition import keys_in, norm_key, text_key

CaseDict = dict[str, Any]
PRAG_PREFIX = "attack-prag-"
ENRON_PREFIX = "benign-enron-"


def content_keys(case: Mapping[str, Any]) -> set[str]:
    """The content keys of one case (see the module docstring)."""
    if case["vector"] == "rag":
        keys = {norm_key(str(c["content"])) for c in case.get("chunks") or [] if c.get("poisoned")}
        keys.update(str(k) for k in (case.get("meta") or {}).get("source_keys") or [])
        return keys
    email = case.get("email") or {}
    keys = {text_key(str(email.get("subject", "")), str(email.get("body_text", "")))}
    keys.update(str(k) for k in (case.get("meta") or {}).get("source_keys") or [])
    return keys


def duplicates(cases: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Content key -> the ids of every case that holds it, for keys held by two or more cases."""
    holders: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        for key in content_keys(case):
            holders[key].add(str(case["case_id"]))
    return {k: sorted(v) for k, v in sorted(holders.items()) if len(v) > 1}


def in_corpus(cases: Iterable[Mapping[str, Any]], corpus_keys: set[str]) -> list[str]:
    """Ids of cases with any content key in ``corpus_keys`` (``norm_key`` of each corpus row)."""
    return sorted(str(c["case_id"]) for c in cases if content_keys(c) & corpus_keys)


def prag_item(case_id: str) -> str | None:
    """``<dataset>-<item id>`` of a PoisonedRAG case id, else None."""
    return case_id.removeprefix(PRAG_PREFIX) if case_id.startswith(PRAG_PREFIX) else None


def in_tuning(cases: Iterable[Mapping[str, Any]], tuning_items: set[str]) -> list[str]:
    """Ids of PoisonedRAG cases whose question is in the Doc Scanner tuning split."""
    return sorted(
        str(c["case_id"])
        for c in cases
        if (item := prag_item(str(c["case_id"]))) and item in tuning_items
    )


def outside_partition(
    cases: Iterable[Mapping[str, Any]], partition: Mapping[str, Any]
) -> list[str]:
    """Ids of Enron or PoisonedRAG cases whose raw unit is not a ``benchmark`` unit.

    An Enron case must be the first message id of a benchmark unit (one case per unit). LLMail
    benign emails are never benchmark units under the partition, so such a case is outside it.
    """
    enron_ids = {
        u["message_ids"][0] for u in partition["units"]["enron_ham"] if u["bucket"] == "benchmark"
    }
    prag = keys_in(partition, "poisonedrag", "benchmark")
    llmail_benign = {
        u["key"] for u in partition["units"]["llmail_benign"] if u["bucket"] == "benchmark"
    }

    def outside(case: Mapping[str, Any]) -> bool:
        cid = str(case["case_id"])
        if cid.startswith(ENRON_PREFIX):
            return cid.removeprefix(ENRON_PREFIX) not in enron_ids
        item = prag_item(cid)
        if item is not None:
            return item not in prag
        if cid.startswith("benign-llmailfp-"):
            return not (content_keys(case) & llmail_benign)
        return False

    return sorted(str(c["case_id"]) for c in cases if outside(c))


def problems(
    cases: Sequence[Mapping[str, Any]],
    *,
    corpus_keys: set[str],
    tuning_items: set[str],
    partition: Mapping[str, Any],
    extra_train_keys: set[str] = frozenset(),  # type: ignore[assignment]
) -> dict[str, Any]:
    """Every finding, by check name; empty lists and dicts when the set is clean."""
    return {
        "duplicates": duplicates(cases),
        "in_l1_corpus": in_corpus(cases, corpus_keys),
        "in_llmail_train_half": in_corpus(cases, set(extra_train_keys)),
        "in_doc_tuning": in_tuning(cases, tuning_items),
        "outside_partition": outside_partition(cases, partition),
    }


def require_clean(cases: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """``problems`` or a CaseManifestError that names every id found (first 20 per check)."""
    found = problems(cases, **kwargs)
    bad = {k: v for k, v in found.items() if v}
    if bad:
        parts = []
        for name, value in bad.items():
            ids = [", ".join(v) for v in value.values()] if isinstance(value, dict) else list(value)
            parts.append(f"{name} ({len(value)}): {'; '.join(ids[:20])}")
        raise CaseManifestError("benchmark cases overlap: " + " | ".join(parts))
    return {k: len(v) for k, v in found.items()}

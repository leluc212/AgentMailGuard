"""Choose the Doc Scanner cheap-stage thresholds on the dev split (never on the pinned cases).

    python -m training.tune_doc_scanner [--out artifacts/doc_scanner_tuning.json]

Reads ``datasets/splits/rag_poison_dev.json`` (``build_rag_poison --dev-split``) and rebuilds its
cases from ``datasets/raw``. Each case's question is wrapped in an email (greeting, question,
signature), as the pipeline sees it: the v2 per-chunk scanner gets subject + body as its echo
query, the v3 signals get ``question_sentences(body)``. For a grid of thresholds it reports, per
style, the share of poisoned cases whose every chunk is caught, and the clean false-positive
count. ADR-0015 design decision 9.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mailguard.config.settings import MailGuardSettings
from mailguard.contracts.email import SourceDocument
from mailguard.datasets.build_rag_poison import load_dev_cases
from mailguard.layers.doc_scanner.chunk_scanner import RetrievedDocumentScanner
from mailguard.layers.doc_scanner.signals import (
    bait_clusters,
    content_tokens,
    echo_vs_question,
    ordered_prefix,
)
from mailguard.prompting.questions import question_sentences

ECHO_GRID = (0.7, 0.8, 0.85, 0.9, 0.95)
MIN_TOKENS_GRID = (4, 6, 8)
CLUSTER_GRID = (0.5, 0.6, 0.7)


def email_body(question: str) -> str:
    return f"Hello,\n\n{question.strip()}\n\nThanks in advance,\nAlex"


def _features(
    cases: list[dict[str, Any]], scanner: RetrievedDocumentScanner
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for case in cases:
        body = email_body(case["question"])
        query = f"Question\n{body}"
        questions = question_sentences(body)
        qlen = max((len(content_tokens(q)) for q in questions), default=0)
        case["_questions"] = questions
        for chunk in case["chunks"]:
            doc = SourceDocument(chunk_id=chunk["chunk_id"], content=chunk["content"])
            out[chunk["chunk_id"]] = {
                "v2": scanner.scan_chunk_sync(doc, query).quarantined,
                "echo": echo_vs_question(chunk["content"], questions),
                "prefix": ordered_prefix(chunk["content"], questions),
                "qlen": qlen,
            }
    return out


def evaluate(cases: list[dict[str, Any]], scanner: RetrievedDocumentScanner) -> dict[str, Any]:
    feats = _features(cases, scanner)
    results: dict[str, Any] = {"v2_only": _score(cases, lambda cid: feats[cid]["v2"])}
    for echo_thr, min_tokens, cluster_thr in itertools.product(
        ECHO_GRID, MIN_TOKENS_GRID, CLUSTER_GRID
    ):
        clusters: dict[str, str] = {}
        for case in cases:
            pairs = [(c["chunk_id"], c["content"]) for c in case["chunks"]]
            echoes = {cid: feats[cid]["echo"] for cid, _ in pairs}
            clusters.update(bait_clusters(pairs, echoes, echo_threshold=cluster_thr))

        def flagged(
            cid: str, e: float = echo_thr, m: int = min_tokens, cl: dict[str, str] = clusters
        ) -> bool:
            f = feats[cid]
            long_q = f["qlen"] >= m
            return bool(
                f["v2"] or (f["prefix"] and long_q) or (f["echo"] >= e and long_q) or cid in cl
            )

        results[f"echo={echo_thr} min_tokens={min_tokens} cluster={cluster_thr}"] = _score(
            cases, flagged
        )
    return results


def _score(cases: list[dict[str, Any]], flagged: Callable[[str], bool]) -> dict[str, Any]:
    caught: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    clean = [0, 0]
    for case in cases:
        hits = [flagged(c["chunk_id"]) for c in case["chunks"]]
        if case["poisoned"]:
            caught[case["style"]][0] += all(hits)
            caught[case["style"]][1] += 1
        else:
            clean[0] += sum(hits)
            clean[1] += len(hits)
    return {
        "caught_all": {s: round(a / b, 3) for s, (a, b) in sorted(caught.items())},
        "clean_fp_count": clean[0],
        "clean_chunks": clean[1],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.disable(logging.WARNING)
    settings = MailGuardSettings(_env_file=None)
    settings.inbound_guard.ml_model_path = "absent-classifier.joblib"  # cheap stages only
    results = evaluate(load_dev_cases(), RetrievedDocumentScanner(settings))
    text = json.dumps(results, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

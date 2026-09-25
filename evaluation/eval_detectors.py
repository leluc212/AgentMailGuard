"""Detection-level evaluation of the cheap guard stages (no LLM, no agent).

    python -m evaluation.eval_detectors l1   [--cases datasets/processed/email_bench/cases.jsonl]
    python -m evaluation.eval_detectors l3b  [--chunks datasets/processed/rag_poison/chunks.jsonl]

``l1``  runs EmailInjectionScanner (rules + ML) on every benchmark email and reports detection
        rate at MEDIUM+ / HIGH+ severity per source and technique, plus FPR on benign emails.
``l3b`` runs RetrievedDocumentScanner on the chunk set and reports precision / recall / F1 of
        quarantine per source (PoisonedRAG factual poisons vs. instruction-style poisons).
Writes ``evaluation/results/detectors/<name>.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from evaluation.harness import BenchCase
from evaluation.metrics import Proportion
from mailguard.config.settings import PROJECT_ROOT, MailGuardSettings
from mailguard.contracts.email import RetrievedChunk
from mailguard.contracts.verdict import Severity
from mailguard.layers.l1_injection_scanner.scanner import EmailInjectionScanner
from mailguard.layers.l3b_document_scanner.scanner import RetrievedDocumentScanner

OUT_DIR = PROJECT_ROOT / "evaluation" / "results" / "detectors"


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def eval_l1(cases_path: Path, settings: MailGuardSettings) -> dict:
    scanner = EmailInjectionScanner(settings)
    cases = BenchCase.load_jsonl(str(cases_path))
    rows = []
    t0 = time.perf_counter()
    for c in cases:
        if c.vector == "rag" and c.kind == "attack":
            continue  # RAG-vector attacks carry benign emails
        v = scanner.inspect_sync(c.guarded_email())
        rows.append((c, v))
    elapsed_ms = (time.perf_counter() - t0) * 1000 / max(1, len(rows))
    out: dict = {
        "n": len(rows),
        "ms_per_email": round(elapsed_ms, 3),
        "ml_available": scanner.classifier.available,
    }
    attacks = [(c, v) for c, v in rows if c.kind == "attack"]
    benign = [(c, v) for c, v in rows if c.kind == "benign"]
    for name, level in (
        ("detected_medium_plus", Severity.MEDIUM),
        ("detected_high_plus", Severity.HIGH),
    ):
        out[name] = Proportion(
            sum(v.severity.rank >= level.rank for _, v in attacks), len(attacks)
        ).fmt()
        out[name + "_fpr"] = Proportion(
            sum(v.severity.rank >= level.rank for _, v in benign), len(benign)
        ).fmt()
    by: dict[str, dict[str, Proportion]] = {
        "source": defaultdict(lambda: Proportion(0, 0)),
        "technique": defaultdict(lambda: Proportion(0, 0)),
    }
    for c, v in attacks:
        for key, val in (("source", c.source), ("technique", c.technique or "?")):
            p = by[key][val]
            p.total += 1
            p.successes += int(v.severity.rank >= Severity.MEDIUM.rank)
    out["detection_by_source"] = {k: p.fmt() for k, p in sorted(by["source"].items())}
    out["detection_by_technique"] = {k: p.fmt() for k, p in sorted(by["technique"].items())}
    fp_by: dict[str, Proportion] = defaultdict(lambda: Proportion(0, 0))
    for c, v in benign:
        p = fp_by[c.source]
        p.total += 1
        p.successes += int(v.severity.rank >= Severity.MEDIUM.rank)
    out["fpr_by_source"] = {k: p.fmt() for k, p in sorted(fp_by.items())}
    out["decided_by"] = dict(sorted(defaultdict(int, {}).items()))
    counts: dict[str, int] = defaultdict(int)
    for _, v in rows:
        counts[v.decided_by] += 1
    out["decided_by"] = dict(counts)
    return out


def eval_l3b(chunks_path: Path, settings: MailGuardSettings) -> dict:
    scanner = RetrievedDocumentScanner(settings)
    rows = [json.loads(line) for line in open(chunks_path, encoding="utf-8") if line.strip()]
    t0 = time.perf_counter()
    results = []
    for r in rows:
        chunk = RetrievedChunk(
            chunk_id=r["chunk_id"], document_id=r.get("source", ""), content=r["content"]
        )
        v = scanner.scan_chunk_sync(chunk, query=r.get("target_query") or None)
        results.append((r, v))
    elapsed_ms = (time.perf_counter() - t0) * 1000 / max(1, len(rows))
    out: dict = {
        "n": len(rows),
        "ms_per_chunk": round(elapsed_ms, 3),
        "ml_available": scanner.classifier.available,
    }
    tp = sum(1 for r, v in results if r["poisoned"] and v.quarantined)
    fp = sum(1 for r, v in results if not r["poisoned"] and v.quarantined)
    fn = sum(1 for r, v in results if r["poisoned"] and not v.quarantined)
    out["overall"] = {**prf(tp, fp, fn), "tp": tp, "fp": fp, "fn": fn}
    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "n": 0}
    )
    for r, v in results:
        s = by_source[r["source"]]
        s["n"] += 1
        if r["poisoned"]:
            s["tp" if v.quarantined else "fn"] += 1
        else:
            s["fp" if v.quarantined else "tn"] += 1
    out["by_source"] = {
        k: {
            **s,
            "recall_or_specificity": round(
                (s["tp"] / (s["tp"] + s["fn"]))
                if s["tp"] + s["fn"]
                else (s["tn"] / max(1, s["tn"] + s["fp"])),
                4,
            ),
        }
        for k, s in sorted(by_source.items())
    }
    # without the query-echo heuristic (PoisonedRAG bait) -> shows what phrasing alone catches
    tp_q = sum(
        1
        for r, v in results
        if r["poisoned"] and any(f.technique == "query_echo" for f in v.findings)
    )
    out["poisoned_flagged_by_query_echo"] = tp_q
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("stage", choices=["l1", "l3b"])
    ap.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "datasets" / "processed" / "email_bench" / "cases.jsonl",
    )
    ap.add_argument(
        "--chunks",
        type=Path,
        default=PROJECT_ROOT / "datasets" / "processed" / "rag_poison" / "chunks.jsonl",
    )
    args = ap.parse_args(argv)
    settings = MailGuardSettings(_env_file=None)  # type: ignore[call-arg]
    result = (
        eval_l1(args.cases, settings) if args.stage == "l1" else eval_l3b(args.chunks, settings)
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{args.stage}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nwritten {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

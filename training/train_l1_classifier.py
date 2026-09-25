"""Train the Layer-1 Stage-2 classifier (TF-IDF word+char n-grams -> calibrated LR).

    python -m training.train_l1_classifier                       # uses datasets/processed/l1_injection
    python -m training.train_l1_classifier --corpus path/to/dir  # custom corpus
    python -m training.train_l1_classifier --seed-only           # tiny smoke model from seed data

Reads ``{train,val,test}.jsonl`` with fields ``text``, ``label`` (0/1), ``source``.
Writes ``artifacts/models/l1_injection_clf_v1.joblib`` and a metrics JSON next to it
(overall + per-source precision/recall/F1/AUROC, calibration bins, latency).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from mailguard.config.settings import PROJECT_ROOT
from mailguard.layers.l1_injection_scanner.classifier import MODEL_VERSION, InjectionClassifier

logger = logging.getLogger("training.l1")

DEFAULT_CORPUS = PROJECT_ROOT / "datasets" / "processed" / "l1_injection"
DEFAULT_OUT = PROJECT_ROOT / "artifacts" / "models" / f"{MODEL_VERSION}.joblib"


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def binary_metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> dict:
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    y_pred = (y_prob >= threshold).astype(int)
    out = {
        "n": int(len(y_true)),
        "positives": int(y_true.sum()),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "fpr": float(((y_pred == 1) & (y_true == 0)).sum() / max(1, (y_true == 0).sum())),
    }
    if len(set(y_true.tolist())) == 2:
        out["auroc"] = float(roc_auc_score(y_true, y_prob))
        out["auprc"] = float(average_precision_score(y_true, y_prob))
        out["brier"] = float(brier_score_loss(y_true, y_prob))
    return out


def calibration_bins(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (y_prob >= lo) & (y_prob < hi if hi < 1 else y_prob <= hi)
        if m.sum() == 0:
            continue
        out.append(
            {
                "bin": f"{lo:.1f}-{hi:.1f}",
                "n": int(m.sum()),
                "mean_prob": float(y_prob[m].mean()),
                "frac_pos": float(y_true[m].mean()),
            }
        )
    return out


def seed_corpus() -> tuple[list[dict], list[dict]]:
    """Tiny corpus from the seed attack templates + benign emails (smoke training only)."""
    from mailguard.datasets.seed import (
        DEFAULT_ATTACKER,
        fill,
        load_attack_templates,
        load_benign_emails,
    )

    pos = [
        {"text": fill(t["text"], DEFAULT_ATTACKER), "label": 1, "source": "seed", "id": t["id"]}
        for t in load_attack_templates()
    ]
    neg = [
        {"text": f"{e['subject']}\n{e['body_text']}", "label": 0, "source": "seed", "id": e["id"]}
        for e in load_benign_emails()
    ]
    rows = pos + neg
    rng = np.random.default_rng(13)
    rng.shuffle(rows)
    cut = int(len(rows) * 0.8)
    return rows[:cut], rows[cut:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--seed-only", action="store_true", help="train a smoke model from seed data only"
    )
    ap.add_argument("--max-train", type=int, default=0, help="cap training rows (0 = all)")
    ap.add_argument("--no-calibrate", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.seed_only:
        train, test = seed_corpus()
        val: list[dict] = []
    else:
        train = read_jsonl(args.corpus / "train.jsonl")
        val = read_jsonl(args.corpus / "val.jsonl")
        test = read_jsonl(args.corpus / "test.jsonl")
        if not train:
            logger.error(
                "no training data at %s; run `python -m mailguard.datasets.build_l1_corpus` first",
                args.corpus,
            )
            return 2
    if args.max_train and len(train) > args.max_train:
        rng = np.random.default_rng(7)
        idx = rng.choice(len(train), size=args.max_train, replace=False)
        train = [train[i] for i in sorted(idx)]
    logger.info("train=%d val=%d test=%d", len(train), len(val), len(test))

    clf = InjectionClassifier()
    t0 = time.perf_counter()
    clf.fit(
        [r["text"] for r in train],
        [int(r["label"]) for r in train],
        calibrate=not args.no_calibrate,
    )
    fit_s = time.perf_counter() - t0
    logger.info("fitted in %.1fs", fit_s)

    metrics: dict = {
        "version": clf.version,
        "train_rows": len(train),
        "fit_seconds": round(fit_s, 1),
    }
    for split_name, rows in (("val", val), ("test", test)):
        if not rows:
            continue
        y = np.asarray([int(r["label"]) for r in rows])
        t1 = time.perf_counter()
        p = np.asarray(clf.predict_proba_batch([r["text"] for r in rows]))
        per_ms = (time.perf_counter() - t1) * 1000 / max(1, len(rows))
        m = binary_metrics(y, p)
        m["latency_ms_per_text"] = round(per_ms, 3)
        m["calibration"] = calibration_bins(y, p)
        by_src: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            by_src[str(r.get("source", "?"))].append(i)
        m["by_source"] = {
            s: binary_metrics(y[idx], p[idx]) for s, idx in sorted(by_src.items()) if len(idx) >= 5
        }
        metrics[split_name] = m
        logger.info(
            "%s: %s", split_name, {k: round(v, 4) for k, v in m.items() if isinstance(v, float)}
        )

    clf.metrics = metrics
    clf.save(args.out)
    with open(args.out.with_suffix(".metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    logger.info("saved %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

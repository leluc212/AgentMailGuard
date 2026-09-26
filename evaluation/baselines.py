"""Layer-1 baselines: public prompt-injection detectors vs. the MailGuard classifier.

    python -m evaluation.baselines --model mailguard-l1 protectai            # CPU, ~5 min
    python -m evaluation.baselines --model promptguard2 --hf-token $HF_TOKEN  # gated model

Models
    mailguard-l1   artifacts/models/l1_injection_clf_v1.joblib (TF-IDF/LR, this work)
    mailguard-l1r  the same plus the rule stage (max of rule score and classifier probability)
    protectai      ProtectAI/deberta-v3-base-prompt-injection-v2 (Apache-2.0, 184M params)
    promptguard2   meta-llama/Llama-Prompt-Guard-2-86M (gated; requires an accepted license + token)

Evaluation sets
    corpus-test    datasets/processed/l1_injection/test.jsonl (held-out split of the training corpus)
    bench-emails   email-vector attack emails + benign emails from the benchmark (out-of-distribution
                   for every detector: full emails with subject, carrier text and injected payload)

Reports precision / recall / F1 / AUROC / FPR at 0.5 and latency; writes
evaluation/results/baselines/<model>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence

import numpy as np

from evaluation.harness import BenchCase
from mailguard.config.settings import PROJECT_ROOT, MailGuardSettings
from mailguard.contracts.email import GuardedEmail
from training.train_l1_classifier import binary_metrics

OUT_DIR = PROJECT_ROOT / "evaluation" / "results" / "baselines"
HF_MODELS = {
    "protectai": "ProtectAI/deberta-v3-base-prompt-injection-v2",
    "promptguard2": "meta-llama/Llama-Prompt-Guard-2-86M",
}


class Detector:
    name: str

    def predict(self, texts: Sequence[str]) -> list[float]:  # probability of injection
        raise NotImplementedError


class MailGuardL1(Detector):
    def __init__(self, with_rules: bool = False) -> None:
        from mailguard.layers.l1_injection_scanner.scanner import EmailInjectionScanner

        self.name = "mailguard-l1r" if with_rules else "mailguard-l1"
        self.with_rules = with_rules
        self.scanner = EmailInjectionScanner(MailGuardSettings(_env_file=None))  # type: ignore[call-arg]
        if not self.scanner.classifier.available:
            raise SystemExit("train the classifier first: python -m training.train_l1_classifier")

    def predict(self, texts: Sequence[str]) -> list[float]:
        probs = self.scanner.classifier.predict_proba_batch(list(texts))
        if not self.with_rules:
            return probs
        out = []
        for text, p in zip(texts, probs, strict=True):
            subject, _, body = (
                text.partition("\n") if text.startswith("Subject:") else ("", "", text)
            )
            email = GuardedEmail(subject=subject.replace("Subject:", "").strip(), body_text=body)
            rule = max((f.score for f in self.scanner.stage_rules(email)), default=0.0)
            out.append(max(p, rule))
        return out


class HFClassifier(Detector):
    def __init__(self, key: str, token: str | None = None, batch_size: int = 8) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.name = key
        model_id = HF_MODELS[key]
        self.tok = AutoTokenizer.from_pretrained(model_id, token=token)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id, token=token)
        self.model.eval()
        self.torch = torch
        self.batch_size = batch_size
        labels = {i: str(lbl).lower() for i, lbl in self.model.config.id2label.items()}
        # positive class = anything that is not "safe"/"benign"
        self.pos_idx = [i for i, lbl in labels.items() if lbl not in ("safe", "benign", "label_0")]
        if not self.pos_idx:
            self.pos_idx = [1]

    def predict(self, texts: Sequence[str]) -> list[float]:
        out: list[float] = []
        with self.torch.no_grad():
            for i in range(0, len(texts), self.batch_size):
                batch = [t[:4000] for t in texts[i : i + self.batch_size]]
                enc = self.tok(
                    batch, truncation=True, max_length=512, padding=True, return_tensors="pt"
                )
                logits = self.model(**enc).logits
                probs = self.torch.softmax(logits, dim=-1)
                out += [float(probs[j, self.pos_idx].sum()) for j in range(probs.shape[0])]
        return out


def load_corpus_test(limit: int) -> tuple[list[str], np.ndarray, list[str]]:
    path = PROJECT_ROOT / "datasets" / "processed" / "l1_injection" / "test.jsonl"
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    if limit:
        rows = rows[:limit]
    return (
        [r["text"] for r in rows],
        np.asarray([int(r["label"]) for r in rows]),
        [r["source"] for r in rows],
    )


def load_bench_emails(limit: int) -> tuple[list[str], np.ndarray, list[str]]:
    path = PROJECT_ROOT / "datasets" / "processed" / "email_bench" / "cases.jsonl"
    cases = [
        c for c in BenchCase.load_jsonl(str(path)) if not (c.kind == "attack" and c.vector == "rag")
    ]
    if limit:
        cases = cases[:limit]
    texts = [
        f"Subject: {c.email.get('subject', '')}\n{c.email.get('body_text', '')}" for c in cases
    ]
    y = np.asarray([1 if c.kind == "attack" else 0 for c in cases])
    return texts, y, [c.source for c in cases]


def evaluate(det: Detector, texts: list[str], y: np.ndarray, sources: list[str]) -> dict:
    t0 = time.perf_counter()
    p = np.asarray(det.predict(texts))
    ms = (time.perf_counter() - t0) * 1000 / max(1, len(texts))
    res = binary_metrics(y, p)
    res["latency_ms_per_text"] = round(ms, 2)
    by: dict[str, list[int]] = {}
    for i, s in enumerate(sources):
        by.setdefault(s, []).append(i)
    res["by_source"] = {
        s: binary_metrics(y[idx], p[idx]) for s, idx in sorted(by.items()) if len(idx) >= 10
    }
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", nargs="+", default=["mailguard-l1", "mailguard-l1r", "protectai"])
    ap.add_argument(
        "--set",
        nargs="+",
        default=["corpus-test", "bench-emails"],
        choices=["corpus-test", "bench-emails"],
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hf-token", default=None)
    args = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sets = {}
    if "corpus-test" in args.set:
        sets["corpus-test"] = load_corpus_test(args.limit)
    if "bench-emails" in args.set:
        sets["bench-emails"] = load_bench_emails(args.limit)
    for name in args.model:
        try:
            det: Detector = (
                MailGuardL1(with_rules=name.endswith("r"))
                if name.startswith("mailguard-l1")
                else HFClassifier(name, token=args.hf_token)
            )
        except Exception as exc:  # gated / offline model: report and continue
            print(f"[{name}] unavailable: {exc}", file=sys.stderr)
            continue
        report = {"model": name, "hf_id": HF_MODELS.get(name)}
        for set_name, (texts, y, sources) in sets.items():
            report[set_name] = evaluate(det, texts, y, sources)
            m = report[set_name]
            print(
                f"{name:14s} {set_name:12s} n={m['n']:5d} P={m['precision']:.3f} R={m['recall']:.3f} "
                f"F1={m['f1']:.3f} FPR={m['fpr']:.3f} AUROC={m.get('auroc', float('nan')):.3f} "
                f"{m['latency_ms_per_text']:.1f} ms"
            )
        with open(OUT_DIR / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())

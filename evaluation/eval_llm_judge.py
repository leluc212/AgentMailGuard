"""Measure an LLM as the Layer-1 judge: precision / recall / F1 / AUROC on labelled emails.

    python -m evaluation.eval_llm_judge --model gpt-4o-mini --set corpus-test --limit 600
    python -m evaluation.eval_llm_judge --model qwen2.5-7b-instruct --set bench-emails --concurrency 2
    python -m evaluation.eval_llm_judge --model llama-3.1-8b-instruct --set corpus-test bench-emails

Models come from configs/models.yaml (Ollama tags or the OpenAI API; a fine-tuned judge
registered in Ollama works the same way). The prompt is mailguard/prompts/l1_judge.v1.txt
and the email is wrapped in nonce markers exactly as in production.

Sets
    corpus-test    held-out split of the L1 training corpus (stratified subsample)
    bench-emails   attack + benign emails of the benchmark (out-of-distribution)

Writes evaluation/results/llm_judge/<model>__<set>.json with the metrics, per-source
breakdown, token usage and latency. Rows with LLM errors are counted as "unavailable" and
scored as benign (fail-open for the *detector metric only*; the pipeline itself fails closed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import defaultdict

import numpy as np

from evaluation.baselines import load_bench_emails, load_corpus_test
from mailguard.config.settings import PROJECT_ROOT, MailGuardSettings
from mailguard.contracts.email import GuardedEmail
from mailguard.layers.l1_injection_scanner.llm_judge import judge_email
from mailguard.llm.protocol import LLMError
from mailguard.llm.registry import ModelRegistry
from training.train_l1_classifier import binary_metrics

OUT_DIR = PROJECT_ROOT / "evaluation" / "results" / "llm_judge"


def to_email(text: str) -> GuardedEmail:
    subject, _, body = text.partition("\n") if text.startswith("Subject:") else ("", "", text)
    return GuardedEmail(subject=subject.replace("Subject:", "").strip(), body_text=body.strip())


def stratified(texts: list[str], y: np.ndarray, sources: list[str], limit: int, seed: int):
    if not limit or limit >= len(texts):
        return texts, y, sources
    rng = random.Random(seed)
    pos = [i for i in range(len(texts)) if y[i] == 1]
    neg = [i for i in range(len(texts)) if y[i] == 0]
    n_pos = min(len(pos), limit // 2)
    idx = sorted(rng.sample(pos, n_pos) + rng.sample(neg, min(len(neg), limit - n_pos)))
    return [texts[i] for i in idx], y[idx], [sources[i] for i in idx]


async def run(model: str, texts: list[str], concurrency: int) -> tuple[list[float], dict]:
    provider = ModelRegistry(MailGuardSettings(_env_file=None)).get(model)  # type: ignore[call-arg]
    sem = asyncio.Semaphore(max(1, concurrency))
    scores: list[float] = [0.0] * len(texts)
    stats = {"errors": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": []}

    async def one(i: int) -> None:
        async with sem:
            try:
                out, res = await judge_email(provider, to_email(texts[i]), max_chars=6000)
                scores[i] = out.injection_score
                stats["input_tokens"] += res.input_tokens
                stats["output_tokens"] += res.output_tokens
                stats["latency_ms"].append(res.latency_ms)
            except LLMError:
                stats["errors"] += 1
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(texts)}", flush=True)

    await asyncio.gather(*(one(i) for i in range(len(texts))))
    return scores, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", required=True)
    ap.add_argument(
        "--set", nargs="+", default=["corpus-test"], choices=["corpus-test", "bench-emails"]
    )
    ap.add_argument("--limit", type=int, default=600, help="stratified subsample per set (0 = all)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for set_name in args.set:
        texts, y, sources = (
            load_corpus_test(0) if set_name == "corpus-test" else load_bench_emails(0)
        )
        texts, y, sources = stratified(texts, y, sources, args.limit, args.seed)
        print(f"{args.model} on {set_name}: n={len(texts)} positives={int(y.sum())}")
        t0 = time.perf_counter()
        scores, stats = asyncio.run(run(args.model, texts, args.concurrency))
        wall = time.perf_counter() - t0
        p = np.asarray(scores)
        report = binary_metrics(y, p)
        by: dict[str, list[int]] = defaultdict(list)
        for i, s in enumerate(sources):
            by[s].append(i)
        report["by_source"] = {
            s: binary_metrics(y[idx], p[idx]) for s, idx in sorted(by.items()) if len(idx) >= 10
        }
        report.update(
            {
                "model": args.model,
                "set": set_name,
                "errors": stats["errors"],
                "input_tokens": stats["input_tokens"],
                "output_tokens": stats["output_tokens"],
                "latency_ms_mean": round(float(np.mean(stats["latency_ms"])), 1)
                if stats["latency_ms"]
                else None,
                "wall_seconds": round(wall, 1),
            }
        )
        path = OUT_DIR / f"{args.model}__{set_name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(
            f"  P={report['precision']:.3f} R={report['recall']:.3f} F1={report['f1']:.3f} "
            f"FPR={report['fpr']:.3f} AUROC={report.get('auroc', float('nan')):.3f} "
            f"errors={stats['errors']} -> {path}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

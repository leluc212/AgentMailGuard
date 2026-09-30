"""Run the email + RAG benchmark for one or more (model, configuration) pairs.

    python -m evaluation.run_benchmark --agent naive --config C0 C1 C2 C3           # offline sanity
    python -m evaluation.run_benchmark --agent qwen2.5-7b-instruct --guard-model qwen2.5-7b-instruct \
        --config C0 C1 C2 C3 C3-L1 C3-L2 C3-L3 C3-L3b C3-L4 C3-L5 --concurrency 2
    python -m evaluation.run_benchmark --agent gpt-4o-mini --guard-model gpt-4o-mini --config C3 --limit 200

Writes ``evaluation/results/<run>/<agent>__<config>.jsonl`` (one CaseResult per line) and
``summary.json``; ``python -m evaluation.report`` turns a results directory into the
paper tables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from evaluation.harness import BenchCase, HarnessRun, build_agent, build_pipeline
from evaluation.metrics import summarize
from mailguard.config.settings import PROJECT_ROOT

logger = logging.getLogger("evaluation.run")
DEFAULT_CASES = PROJECT_ROOT / "datasets" / "processed" / "email_bench" / "cases.jsonl"
RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"

ALL_CONFIGS = ["C0", "C1", "C2", "C3", "C3-L1", "C3-L2", "C3-L3", "C3-L3b", "C3-L4", "C3-L5"]


def select_cases(
    cases: list[BenchCase], *, limit: int, sources: list[str], vectors: list[str], kinds: list[str]
) -> list[BenchCase]:
    out = cases
    if sources:
        out = [c for c in out if c.source in sources]
    if vectors:
        out = [c for c in out if c.kind == "benign" or c.vector in vectors]
    if kinds:
        out = [c for c in out if c.kind in kinds]
    if limit:
        # keep the attack/benign ratio: take proportionally from each kind
        attacks = [c for c in out if c.kind == "attack"]
        benign = [c for c in out if c.kind == "benign"]
        n_att = min(len(attacks), max(1, round(limit * len(attacks) / max(1, len(out)))))
        n_ben = min(len(benign), max(0, limit - n_att))
        out = attacks[:n_att] + benign[:n_ben]
    return out


async def run_pair(
    agent_name: str,
    config: str,
    cases: list[BenchCase],
    *,
    guard_model: str | None,
    concurrency: int,
    out_dir: Path,
    progress: bool,
) -> dict:
    pipeline = build_pipeline(config, guard_model=guard_model)
    agent = build_agent(agent_name)
    run = HarnessRun(pipeline=pipeline, agent=agent)
    t0 = time.perf_counter()
    results = await run.run_all(cases, concurrency=concurrency, progress=progress)
    wall = time.perf_counter() - t0
    path = out_dir / f"{agent_name}__{config}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    summary = summarize(results).to_dict()
    summary.update(
        {
            "guard_model": guard_model,
            "wall_seconds": round(wall, 1),
            "agent_calls": getattr(agent, "calls", None),
            "agent_errors": getattr(agent, "errors", None),
            "agent_input_tokens": getattr(agent, "input_tokens", None),
            "agent_output_tokens": getattr(agent, "output_tokens", None),
            "results_file": str(path.relative_to(PROJECT_ROOT)),
        }
    )
    logger.info(
        "%s %s: ASR=%.1f%% TMR=%.1f%% DER=%.1f%% TSR=%.1f%% FPR=%.1f%% lat=%.0fms (%.0fs)",
        agent_name,
        config,
        summary["ASR"]["pct"],
        summary["TMR"]["pct"],
        summary["DER"]["pct"],
        summary["TSR"]["pct"],
        summary["FPR"]["pct"],
        summary["latency_ms_mean"],
        wall,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument(
        "--agent",
        default="naive",
        help="naive | qwen2.5-7b-instruct | llama-3.1-8b-instruct | gpt-4o-mini",
    )
    ap.add_argument(
        "--guard-model",
        default=None,
        help="model backing the LLM guard stages (default: none -> rules+ML only)",
    )
    ap.add_argument(
        "--config",
        nargs="+",
        default=["C0", "C1", "C2", "C3"],
        help="configs; 'all' expands to every preset + ablation",
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source", nargs="*", default=[])
    ap.add_argument("--vector", nargs="*", default=[], choices=["email", "rag"])
    ap.add_argument("--kind", nargs="*", default=[], choices=["attack", "benign"])
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--run-name", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(levelname)s %(message)s"
    )
    logging.getLogger("mailguard").setLevel(logging.ERROR)

    configs = ALL_CONFIGS if args.config == ["all"] else args.config
    cases = BenchCase.load_jsonl(str(args.cases))
    cases = select_cases(
        cases, limit=args.limit, sources=args.source, vectors=args.vector, kinds=args.kind
    )
    if not cases:
        logger.error("no cases selected")
        return 2
    run_name = args.run_name or datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + f"-{args.agent}"
    out_dir = RESULTS_DIR / run_name
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "run=%s cases=%d (attack=%d benign=%d) configs=%s",
        run_name,
        len(cases),
        sum(c.kind == "attack" for c in cases),
        sum(c.kind == "benign" for c in cases),
        configs,
    )

    summaries = []
    for config in configs:
        summaries.append(
            asyncio.run(
                run_pair(
                    args.agent,
                    config,
                    cases,
                    guard_model=args.guard_model,
                    concurrency=args.concurrency,
                    out_dir=out_dir,
                    progress=not args.quiet,
                )
            )
        )
    meta = {
        "run": run_name,
        "agent": args.agent,
        "guard_model": args.guard_model,
        "cases_file": str(args.cases),
        "n_cases": len(cases),
        "started": datetime.now(UTC).isoformat(),
        "summaries": summaries,
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"\nresults -> {out_dir}")
    print(
        f"{'config':8s} {'ASR':>8s} {'TMR':>8s} {'DER':>8s} {'TSR':>8s} {'FPR':>8s} {'lat ms':>8s}"
    )
    for s in summaries:
        print(
            f"{s['config']:8s} {s['ASR']['pct']:7.1f}% {s['TMR']['pct']:7.1f}% {s['DER']['pct']:7.1f}% "
            f"{s['TSR']['pct']:7.1f}% {s['FPR']['pct']:7.1f}% {s['latency_ms_mean']:8.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

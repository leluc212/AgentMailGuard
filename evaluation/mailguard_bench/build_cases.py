"""Build the pinned case set for the AgentMailGuard benchmark (task 7.19; spec §3; R22.12).

    make mailguard-cases     # after `make mailguard-prep`; reads local files, no API calls

Draws the pools with AgentMailGuard's own builder functions from the pinned worktree
(mailguard/datasets/build_email_benchmark.py), so the case shapes and the LLMail
bench/train split are the guard's, not re-implemented here:

  llmail_attack  llmail_attack_cases(): every attack on the bench half
                 (iter_llmail_attacks(side="bench"); the L1 corpus uses side="train")
  llmail_benign  all 203 LLMail FP emails + 297 seeded Enron ham emails
  rag_attack     18 seed + all 222 valid PoisonedRAG + 60 labeled synthetic cases

then samples with cases.select_cases (seed 20260930) and writes
evaluation/datasets/mailguard/{manifest.json, cases.jsonl}. When manifest.json already
exists, the rebuild must reproduce its ids and hash or the command fails; --force re-pins.
Run with `python -m` from the rag-email root (see guard_env.require_module_origins).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections.abc import Sequence
from pathlib import Path

from evaluation.mailguard_bench.cases import (
    DEFAULT_CASE_DIR,
    MANIFEST_FILE,
    SEED,
    CaseManifestError,
    CasePools,
    build_manifest,
    derive_seed,
    load_case_set,
    reconcile,
    select_cases,
    write_case_set,
)
from evaluation.mailguard_bench.guard_env import (
    GUARD_BRANCH,
    PREP_HINT,
    RAW_MANIFEST,
    REPO_ROOT,
    GuardEnvError,
    WorktreeInfo,
    guard_paths_from_env,
    missing_raw_files,
    require_module_origins,
    require_pinned_worktree,
    sha256_file,
)

PRAG_PER_DATASET = 10**9  # include every valid raw PoisonedRAG question
EXPANDED_COUNTS = {"n_attack": 1000, "n_benign": 500, "n_rag": 300}
ALL_BENCH_ATTACKS = 10**9  # llmail_attack_cases keeps pool[:limit]: take the whole bench half


def load_pools(seed: int) -> CasePools:
    """Call the guard's builder functions (mailguard must be importable)."""
    from mailguard.datasets.build_email_benchmark import (
        llmail_attack_cases,
        llmail_benign_cases,
        poisonedrag_cases,
        seed_rag_attacks,
    )
    from mailguard.datasets.seed import load_kb

    llmail = llmail_attack_cases(ALL_BENCH_ATTACKS, random.Random(derive_seed(seed, "llmail_pool")))
    from mailguard.datasets.build_email_benchmark import enron_benign

    from evaluation.mailguard_bench.synthetic_poison import synthetic_poison_cases

    benign = llmail_benign_cases()
    if len(benign) != 203:
        raise CaseManifestError("expanded dataset requires the pinned 203 LLMail benign emails")
    benign += enron_benign(297, random.Random(derive_seed(seed, "enron_benign")))
    rag = seed_rag_attacks(load_kb(), random.Random(derive_seed(seed, "seed_rag_pool")))
    rag += poisonedrag_cases(PRAG_PER_DATASET, random.Random(derive_seed(seed, "prag_pool")))
    if len(rag) != 240:
        raise CaseManifestError(f"expected 240 real/seed RAG cases, got {len(rag)}")
    rag += synthetic_poison_cases()
    return CasePools(llmail_attack=llmail, llmail_benign=benign, rag_attack=rag)


def provenance(info: WorktreeInfo) -> dict[str, str]:
    """Where the pools came from: guard commit, raw-download manifest hash, split rule."""
    raw_manifest = info.path / RAW_MANIFEST
    return {
        "mailguard_branch": GUARD_BRANCH,
        "mailguard_commit": info.commit,
        "raw_manifest_sha256": sha256_file(raw_manifest) if raw_manifest.is_file() else "missing",
        "llmail_split": "iter_llmail_attacks(side='bench')",
        "prag_per_dataset": str(PRAG_PER_DATASET),
        "benign_sources": "203 llmail_inject + 297 enron_ham",
        "synthetic_poison": "synthetic_poison_v1:60",
    }


def run(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the pinned AgentMailGuard benchmark case set.")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CASE_DIR)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--force", action="store_true", help="re-pin even if the ids change")
    ap.add_argument("--raw-dir", type=Path, help="local raw dataset directory (no downloads)")
    args = ap.parse_args(argv)

    paths = guard_paths_from_env(os.environ)
    info = require_pinned_worktree(paths.root, paths.commit)
    require_module_origins(REPO_ROOT, paths.root)
    raw_root = args.raw_dir.parent.parent if args.raw_dir else paths.root
    missing = missing_raw_files(raw_root)
    if missing:
        raise GuardEnvError(
            "raw datasets missing: " + ", ".join(str(p) for p in missing) + f"; {PREP_HINT}"
        )
    if args.raw_dir:
        from mailguard.datasets import build_email_benchmark

        build_email_benchmark.RAW = args.raw_dir.resolve()
    pools = load_pools(args.seed)
    selection = select_cases(pools, seed=args.seed, **EXPANDED_COUNTS)
    source_info = provenance(info)
    if args.raw_dir:
        source_info["raw_manifest_sha256"] = sha256_file(args.raw_dir / "MANIFEST.json")
    fresh = build_manifest(selection, pools, seed=args.seed, provenance=source_info)
    fresh["benign_source_counts"] = {"llmail_inject": 203, "enron_ham": 297}
    fresh["synthetic_rag_count"] = 60
    manifest_path = args.out_dir / MANIFEST_FILE
    existing = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    )
    reconcile(existing, fresh, force=args.force)
    keep = existing if existing is not None and not args.force else fresh
    write_case_set(selection, keep, args.out_dir)
    loaded = load_case_set(args.out_dir)
    print(f"ok cases {loaded.manifest['counts']} sha256={loaded.manifest['cases_sha256']}")
    print(f"ok pools {loaded.manifest['pool_sizes']} -> {args.out_dir}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except (GuardEnvError, CaseManifestError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

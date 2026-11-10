"""Build the benchmark case set from the partition (task 7.36, ADR-0019; R22.12).

    uv run --project . --with-editable <abs>/agentmailguard --with pandas --with pyarrow \\
        python -m evaluation.mailguard_bench.build_cases \\
        --l1-corpus <filtered L1 corpus dir> --phase1 <dir holding eval_attack.jsonl>

Reads local files only, no API calls. Draws the pools with AgentMailGuard's own builder functions
(``mailguard/datasets/build_email_benchmark.py``) from the pinned guard, so the case shapes and the
LLMail bench/train split are the guard's, then keeps what ``evaluation/datasets/partition_v1.json``
gives the benchmark:

  llmail_attack  the 1,000 phase-2 attacks of the 1,800-case set (``--legacy-dir``), minus
                 those whose content is in the L1 corpus or the LLMail train half or repeats
                 another bench item (lowest case id kept), each replaced by a seeded draw of
                 the same scenario (owner, 2026-11-10; dropped and replacement ids in the
                 manifest)
  llmail_benign  the 500 Enron ham units of the partition's ``benchmark`` bucket, one case per unit
                 (the LLMail benign emails are all ``l1_train``: owner, 2026-11-10)
  rag_attack     the 165 PoisonedRAG questions of the ``benchmark`` bucket, the 18 seed cases, and
                 400 LLMail phase-1 retrieval attacks (scenarios 3 and 4, stratified)
  rag_synthetic  the 60 ``synthetic_poison_v1`` cases, reported apart (not in rag_attack)

The pool rules are declared above and their counts recorded. After selection the set must pass
``case_checks.require_clean`` (no duplicate content, nothing in the filtered L1 corpus or the LLMail
train half, no tuning question, nothing outside the partition), or the builder fails naming the
ids: it never drops a selected case. When ``manifest.json`` exists, the rebuild must reproduce it
or the command fails; ``--force`` re-pins.

Run with ``python -m`` from the rag-email root (see guard_env.require_module_origins).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench import case_checks, llmail_phase1_rag
from evaluation.mailguard_bench.cases import (
    DEFAULT_CASE_DIR,
    LEGACY_CASE_DIR,
    MANIFEST_FILE,
    SEED,
    CaseManifestError,
    CasePools,
    build_manifest,
    cases_sha256,
    derive_seed,
    load_case_set,
    reconcile,
    select_cases,
    stratified_sample,
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
from evaluation.mailguard_bench.partition import (
    DEV_SPLIT,
    PARTITION_PATH,
    keys_in,
    load_partition,
    norm_key,
    text_key,
    verify_raw,
)

ALL = 10**9
COUNTS = {"n_attack": 1000, "n_benign": 500, "n_rag": 583, "n_ablation": 0}
N_PHASE1_RAG = 400
EXPECTED_RAG_SOURCES = {"poisonedrag": 165, "seed_rag": 18, llmail_phase1_rag.SOURCE: N_PHASE1_RAG}
SYNTHETIC_SET = "rag_synthetic"
PHASE1_FILE = "eval_attack.jsonl"


def read_corpus_keys(corpus: Path) -> set[str]:
    """``norm_key`` of every row of the filtered L1 corpus (train, val and test)."""
    keys: set[str] = set()
    for split in ("train", "val", "test"):
        for line in (corpus / f"{split}.jsonl").read_text(encoding="utf-8").split("\n"):
            if line.strip():
                keys.add(norm_key(str(json.loads(line)["text"])))
    return keys


def _keep(cases: Sequence[dict[str, Any]], drop: set[str]) -> list[dict[str, Any]]:
    return [c for c in cases if not (case_checks.content_keys(c) & drop)]


def attack_pool(
    items: Sequence[dict[str, Any]], *, excluded: set[str]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Bench-half attacks, one per content (lowest case id kept), none with ``excluded`` content."""
    first: dict[str, dict[str, Any]] = {}
    for c in sorted(items, key=lambda c: c["case_id"]):
        keys = case_checks.content_keys(c)
        if not any(k in first for k in keys):
            for k in keys:
                first[k] = c
    unique = sorted({c["case_id"]: c for c in first.values()}.values(), key=lambda c: c["case_id"])
    kept = _keep(unique, excluded)
    tally = {
        "bench_half": len(items),
        "duplicate_content_dropped": len(items) - len(unique),
        "in_l1_or_train_half_dropped": len(unique) - len(kept),
        "pool": len(kept),
    }
    return kept, tally


def legacy_attacks(
    bench: Sequence[dict[str, Any]],
    pool: Sequence[dict[str, Any]],
    legacy_ids: Sequence[str],
    *,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The legacy attacks still in ``pool``, each dropped one replaced from its own scenario.

    Owner (2026-11-10): keep the 1,000 phase-2 cases of the 1,800-case set, drop the ones that
    overlap, and replace each with a case of the same scenario. ``bench`` is the bench half before
    the pool rules (it gives a dropped case its scenario). A scenario short of replacements fails.
    """
    from evaluation.mailguard_bench.cases import llmail_scenario

    by_id = {c["case_id"]: c for c in pool}
    scenario = {c["case_id"]: llmail_scenario(c) for c in bench}
    missing = [i for i in legacy_ids if i not in scenario]
    if missing:
        raise CaseManifestError(f"legacy attack ids not in the bench half: {missing[:20]}")
    kept = [by_id[i] for i in legacy_ids if i in by_id]
    dropped = sorted(i for i in legacy_ids if i not in by_id)
    need = Counter(scenario[i] for i in dropped)
    extra = top_up(pool, kept, need, seed=seed)
    return kept + extra, {
        "legacy": len(legacy_ids),
        "legacy_kept": len(kept),
        "legacy_dropped": dropped,
        "replacements": sorted(c["case_id"] for c in extra),
    }


def top_up(
    pool: Sequence[dict[str, Any]],
    kept: Sequence[dict[str, Any]],
    need: Mapping[str, int],
    *,
    seed: int,
) -> list[dict[str, Any]]:
    """``need[scenario]`` more cases per scenario from ``pool`` minus ``kept`` (seeded)."""
    from evaluation.mailguard_bench.cases import llmail_scenario

    taken = {c["case_id"] for c in kept}
    rng = random.Random(seed)
    extra: list[dict[str, Any]] = []
    for scenario in sorted(need):
        if need[scenario] <= 0:
            continue
        free = sorted(
            (c for c in pool if c["case_id"] not in taken and llmail_scenario(c) == scenario),
            key=lambda c: c["case_id"],
        )
        if len(free) < need[scenario]:
            raise CaseManifestError(
                f"scenario {scenario}: {len(free)} free cases, need {need[scenario]}"
            )
        extra += rng.sample(free, need[scenario])
    return extra


def load_pools(
    seed: int,
    *,
    partition: Mapping[str, Any],
    corpus_keys: set[str],
    phase1_rows: Sequence[Mapping[str, Any]],
    legacy_attack_ids: Sequence[str],
) -> tuple[CasePools, list[dict[str, Any]], dict[str, Any], set[str]]:
    """The pools (guard builders + partition), synthetic cases, tallies and train-half keys."""
    from mailguard.datasets.build_email_benchmark import (
        enron_benign,
        iter_llmail_attacks,
        llmail_attack_cases,
        poisonedrag_cases,
        seed_rag_attacks,
    )
    from mailguard.datasets.seed import load_kb

    from evaluation.mailguard_bench.synthetic_poison import synthetic_poison_cases

    train_half = {text_key(i["subject"], i["body"]) for i in iter_llmail_attacks(side="train")}
    tallies: dict[str, Any] = {}

    bench = llmail_attack_cases(ALL, random.Random(derive_seed(seed, "llmail_pool")))
    for c in bench:  # the untruncated text's key, so a cut body still meets the corpus check
        c["meta"]["source_keys"] = []
    raw_by_id = {f"attack-llmail-{i['key'][:12]}": i for i in iter_llmail_attacks(side="bench")}
    for c in bench:
        raw = raw_by_id[c["case_id"]]
        full = text_key(raw["subject"], raw["body"])
        if full != text_key(c["email"]["subject"], c["email"]["body_text"]):
            c["meta"]["source_keys"] = [full]
    attack_candidates, tallies["llmail_attack"] = attack_pool(
        bench, excluded=corpus_keys | train_half
    )
    attacks, tallies["llmail_attack_selection"] = legacy_attacks(
        bench, attack_candidates, legacy_attack_ids, seed=derive_seed(seed, "llmail_replacements")
    )

    enron_units = {
        u["message_ids"][0] for u in partition["units"]["enron_ham"] if u["bucket"] == "benchmark"
    }
    enron = [
        c
        for c in enron_benign(ALL, random.Random(0))
        if c["case_id"].removeprefix("benign-enron-") in enron_units
    ]
    tallies["llmail_benign"] = {"enron_benchmark_units": len(enron_units), "pool": len(enron)}

    prag_ok = keys_in(partition, "poisonedrag", "benchmark")
    prag_all = poisonedrag_cases(ALL, random.Random(derive_seed(seed, "prag_pool")))
    prag = [c for c in prag_all if c["case_id"].removeprefix("attack-prag-") in prag_ok]
    seed_rag = seed_rag_attacks(load_kb(), random.Random(derive_seed(seed, "seed_rag_pool")))
    p1_pool, tallies["llmail_phase1"] = llmail_phase1_rag.pool(
        phase1_rows, excluded_keys=corpus_keys | train_half
    )
    p1 = stratified_sample(
        p1_pool,
        N_PHASE1_RAG,
        key=llmail_phase1_rag.scenario_of,
        seed=derive_seed(seed, "llmail_phase1"),
    )
    tallies["rag_attack"] = {
        "poisonedrag_valid": len(prag_all),
        "poisonedrag_benchmark": len(prag),
        "seed_rag": len(seed_rag),
        "llmail_phase1_pool": len(p1_pool),
        "llmail_phase1_drawn": len(p1),
        "llmail_phase1_by_scenario": dict(
            sorted(Counter(map(llmail_phase1_rag.scenario_of, p1)).items())
        ),
    }
    rag = prag + seed_rag + p1
    found = dict(Counter(c["source"] for c in rag))
    if found != EXPECTED_RAG_SOURCES:
        raise CaseManifestError(f"RAG pool {found} != the planned {EXPECTED_RAG_SOURCES}")
    pools = CasePools(llmail_attack=attacks, llmail_benign=enron, rag_attack=rag)
    return pools, synthetic_poison_cases(), tallies, train_half


def provenance(info: WorktreeInfo) -> dict[str, str]:
    """Where the pools came from: guard commit, raw-download manifest hash, split rule."""
    raw_manifest = info.path / RAW_MANIFEST
    return {
        "mailguard_branch": GUARD_BRANCH,
        "mailguard_commit": info.commit,
        "raw_manifest_sha256": sha256_file(raw_manifest) if raw_manifest.is_file() else "missing",
        "llmail_split": "iter_llmail_attacks(side='bench')",
        "benign_sources": "500 enron_ham (partition_v1 benchmark units)",
        "rag_sources": "165 poisonedrag + 18 seed_rag + 400 llmail_inject_phase1 (scenarios 3, 4)",
        "synthetic_poison": "synthetic_poison_v1:60, set rag_synthetic, reported apart",
    }


def run(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the benchmark case set from the partition.")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CASE_DIR)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--force", action="store_true", help="re-pin even if the ids change")
    ap.add_argument("--partition", type=Path, default=PARTITION_PATH)
    ap.add_argument(
        "--l1-corpus", type=Path, required=True, help="the filtered L1 corpus directory"
    )
    ap.add_argument("--phase1", type=Path, required=True, help=f"directory holding {PHASE1_FILE}")
    ap.add_argument(
        "--legacy-dir",
        type=Path,
        default=LEGACY_CASE_DIR,
        help="the 1,800-case set whose attacks are kept",
    )
    args = ap.parse_args(argv)

    paths = guard_paths_from_env(os.environ)
    info = require_pinned_worktree(paths.root, paths.commit)
    require_module_origins(REPO_ROOT, paths.root)
    missing = missing_raw_files(paths.root)
    if missing:
        raise GuardEnvError(
            "raw datasets missing: " + ", ".join(map(str, missing)) + f"; {PREP_HINT}"
        )
    partition = load_partition(args.partition)
    verify_raw(partition, paths.root / "datasets" / "raw", paths.root)
    corpus_keys = read_corpus_keys(args.l1_corpus)
    phase1_path = args.phase1 / PHASE1_FILE
    phase1_rows = llmail_phase1_rag.read_rows(phase1_path)

    legacy = json.loads((args.legacy_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    pools, synthetic, tallies, train_half = load_pools(
        args.seed,
        partition=partition,
        corpus_keys=corpus_keys,
        phase1_rows=phase1_rows,
        legacy_attack_ids=legacy["sets"]["llmail_attack"],
    )
    selection = select_cases(pools, seed=args.seed, **COUNTS)
    split = json.loads((paths.root / DEV_SPLIT).read_text(encoding="utf-8"))
    tuning = {f"{r['dataset']}-{r['item_id']}" for r in split["poisoned"]}
    checks = case_checks.require_clean(
        [*selection.all_cases(), *synthetic],
        corpus_keys=corpus_keys,
        tuning_items=tuning,
        partition=partition,
        extra_train_keys=train_half,
    )

    source_info = provenance(info)
    source_info["partition_sha256"] = sha256_file(args.partition)
    source_info["l1_corpus_sha256"] = json.dumps(
        {s: sha256_file(args.l1_corpus / f"{s}.jsonl") for s in ("train", "val", "test")},
        sort_keys=True,
    )
    source_info["llmail_phase1_sha256"] = sha256_file(phase1_path)
    fresh = build_manifest(selection, pools, seed=args.seed, provenance=source_info)
    # The synthetic cases are their own set: every config runs them, the report keeps them apart.
    every = sorted([*selection.all_cases(), *synthetic], key=lambda c: c["case_id"])
    fresh["sets"][SYNTHETIC_SET] = sorted(c["case_id"] for c in synthetic)
    fresh["counts"][SYNTHETIC_SET] = len(synthetic)
    fresh["cases_sha256"] = cases_sha256(every)
    fresh["task"] = "7.36"
    fresh["pool_rules"] = tallies
    fresh["overlap_checks"] = checks
    fresh["source_counts"] = {
        name: dict(sorted(Counter(c["source"] for c in every if c["case_id"] in set(ids)).items()))
        for name, ids in fresh["sets"].items()
    }
    manifest_path = args.out_dir / MANIFEST_FILE
    existing = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    )
    reconcile(existing, fresh, force=args.force)
    keep = existing if existing is not None and not args.force else fresh

    class _All:  # write_case_set takes a selection; this one also holds the synthetic set
        def all_cases(self) -> list[dict[str, Any]]:
            return every

    write_case_set(_All(), keep, args.out_dir)  # type: ignore[arg-type]
    loaded = load_case_set(args.out_dir)
    print(f"ok cases {loaded.manifest['counts']} sha256={loaded.manifest['cases_sha256']}")
    print(f"ok checks {checks} -> {args.out_dir}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except (GuardEnvError, CaseManifestError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

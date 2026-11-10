"""One partition of the raw benign and RAG items, fixed before training or tuning (task 7.36; R22.12).

    uv run --project . --with-editable <abs>/agentmailguard python -m evaluation.mailguard_bench.partition

Every raw item a benchmark case can be built from is assigned to exactly one use:

  l1_train    the L1 classifier corpus may hold it (train, val or test split of that corpus)
  doc_tuning  the Doc Scanner threshold-tuning split holds it (rag_poison_dev.json)
  benchmark   a benchmark case may be built from it
  unused      neither (fails a builder's filter)

Units (docs/benchmark-data-leaks-2026-11-06.md, fix step 1):

  enron_ham          one group of Enron ham messages with the same normalised text, by message_id
                     (two ids with one text in different buckets would still be a leak)
  llmail_benign      one distinct normalised text of emails_for_fp_tests.json, by its indices
  poisonedrag        one target question, ``<dataset>-<item id>``

Decisions of the owner (option C, 2026-11-10): 500 Enron groups for the benchmark, drawn with
seed 20260930 among the groups the benchmark builder accepts (80-2,500 characters); every
other Enron group the L1 builder accepts (60-4,000) stays in l1_train. All 160 distinct LLMail
benign texts stay in l1_train. A PoisonedRAG question in the tuning split is doc_tuning; a
machine-checkable one outside it is benchmark; the rest are unused.

The normalisation is the L1 builder's ``norm`` (lower case, whitespace runs to one space), applied
to ``"Subject: <subject>\\n<body>"``; ``NORM_RULE`` records it in the file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.guard_env import REPO_ROOT

PARTITION_SCHEMA = "mailguard-partition/v1"
PARTITION_PATH = REPO_ROOT / "evaluation" / "datasets" / "partition_v1.json"
SEED = 20260930
BUCKETS = ("l1_train", "doc_tuning", "benchmark", "unused")
N_ENRON_BENCHMARK = 500
ENRON_L1_CHARS = (60, 4000)  # build_l1_corpus.load_enron, on len(message)
ENRON_BENCH_CHARS = (80, 2500)  # build_email_benchmark.enron_benign, on len(message)
NORM_RULE = "re.sub(r'\\s+', ' ', text.strip().lower()) of 'Subject: <subject>\\n<body>'"
PRAG_DATASETS = ("nq", "hotpotqa", "msmarco")

RAW_FILES = {
    "enron_ham": "enron_ham/test.jsonl",
    "llmail_benign": "llmail_inject/data/emails_for_fp_tests.json",
    **{f"poisonedrag_{n}": f"poisonedrag/results/adv_targeted_results/{n}.json" for n in PRAG_DATASETS},
}
DEV_SPLIT = Path("datasets/splits/rag_poison_dev.json")  # inside the guard directory


class PartitionError(ValueError):
    """The partition is inconsistent, or a raw file does not match the one it was built from."""


def norm(text: str) -> str:
    """The L1 builder's normalisation (``build_l1_corpus.norm``; a test keeps them equal)."""
    return re.sub(r"\s+", " ", text.strip().lower())


def text_key(subject: str, body: str) -> str:
    """sha256 of the normalised ``Subject: <subject>\\n<body>``: the content key of an email."""
    return norm_key(f"Subject: {subject}\n{body}")


def norm_key(text: str) -> str:
    """sha256 of an already-joined text after ``norm``."""
    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- pure assignment
def assign_enron(
    hams: Sequence[Mapping[str, Any]], *, n_benchmark: int, seed: int
) -> list[dict[str, Any]]:
    """Group ham by normalised text, then give each group one bucket.

    A group is benchmark-eligible only when every member passes the benchmark filter, so no id
    of a benchmark group can enter the L1 corpus. ``n_benchmark`` groups are drawn from the
    eligible ones (sorted by their first id, seeded); a short supply fails.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for d in hams:
        groups.setdefault(text_key(str(d.get("subject", "")), str(d.get("message", ""))), []).append(d)

    def chars(d: Mapping[str, Any]) -> int:
        return len(str(d.get("message", "")))

    def within(d: Mapping[str, Any], bounds: tuple[int, int]) -> bool:
        return bounds[0] <= chars(d) <= bounds[1]

    units = []
    for key, members in groups.items():
        ids = sorted(str(d["message_id"]) for d in members)
        units.append(
            {
                "key": key,
                "message_ids": ids,
                "bench_ok": all(within(d, ENRON_BENCH_CHARS) for d in members),
                "l1_ok": any(within(d, ENRON_L1_CHARS) for d in members),
            }
        )
    units.sort(key=lambda u: u["message_ids"][0])
    eligible = [u for u in units if u["bench_ok"]]
    if len(eligible) < n_benchmark:
        raise PartitionError(f"only {len(eligible)} Enron groups fit the benchmark, need {n_benchmark}")
    picked = {u["key"] for u in random.Random(seed).sample(eligible, n_benchmark)}
    out = []
    for u in units:
        if u["key"] in picked:
            bucket = "benchmark"
        elif u["l1_ok"]:
            bucket = "l1_train"
        else:
            bucket = "unused"
        out.append({"key": u["key"], "message_ids": u["message_ids"], "bucket": bucket})
    return out


def assign_llmail_benign(texts: Sequence[tuple[str, str]], *, bucket: str) -> list[dict[str, Any]]:
    """One unit per distinct normalised text, with the indices of every copy."""
    if bucket not in BUCKETS:
        raise PartitionError(f"unknown bucket {bucket!r}")
    groups: dict[str, list[int]] = {}
    for i, (subject, body) in enumerate(texts):
        groups.setdefault(text_key(subject, body), []).append(i)
    units = [{"key": k, "indices": v, "bucket": bucket} for k, v in groups.items()]
    return sorted(units, key=lambda u: u["indices"][0])


def assign_poisonedrag(
    all_items: Iterable[str], valid: Iterable[str], tuning: Iterable[str]
) -> list[dict[str, Any]]:
    """Tuning wins; a valid question outside tuning is benchmark; the rest are unused."""
    every, ok, dev = set(all_items), set(valid), set(tuning)
    stray = (ok | dev) - every
    if stray:
        raise PartitionError(f"PoisonedRAG ids not in the raw files: {sorted(stray)[:10]}")
    out = []
    for item in sorted(every):
        bucket = "doc_tuning" if item in dev else ("benchmark" if item in ok else "unused")
        out.append({"item": item, "bucket": bucket})
    return out


def counts(units: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    found = Counter(str(u["bucket"]) for u in units)
    return {b: found.get(b, 0) for b in BUCKETS}


def check_partition(partition: Mapping[str, Any]) -> None:
    """Every id in one unit and one bucket, per source; fails naming the ids."""
    if partition.get("schema") != PARTITION_SCHEMA:
        raise PartitionError(f"schema {partition.get('schema')!r} is not {PARTITION_SCHEMA}")
    for source, field in (("enron_ham", "message_ids"), ("llmail_benign", "indices"), ("poisonedrag", "item")):
        seen: dict[str, str] = {}
        keys: set[str] = set()
        for unit in partition["units"][source]:
            if unit["bucket"] not in BUCKETS:
                raise PartitionError(f"{source}: unknown bucket {unit['bucket']!r}")
            if "key" in unit:
                if unit["key"] in keys:
                    raise PartitionError(f"{source}: content key {unit['key']} is in two units")
                keys.add(unit["key"])
            ids = unit[field] if isinstance(unit[field], list) else [unit[field]]
            for raw_id in map(str, ids):
                if raw_id in seen:
                    raise PartitionError(f"{source}: id {raw_id} is in two units")
                seen[raw_id] = unit["bucket"]


def benchmark_keys(partition: Mapping[str, Any]) -> dict[str, set[str]]:
    """Content keys of benchmark units per email source (what the L1 filter removes)."""
    return {
        source: {u["key"] for u in partition["units"][source] if u["bucket"] == "benchmark"}
        for source in ("enron_ham", "llmail_benign")
    }


def keys_in(partition: Mapping[str, Any], source: str, bucket: str) -> set[str]:
    field = "item" if source == "poisonedrag" else "key"
    return {str(u[field]) for u in partition["units"][source] if u["bucket"] == bucket}


def load_partition(path: Path = PARTITION_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    check_partition(data)
    return data


def verify_raw(partition: Mapping[str, Any], raw_dir: Path, guard_dir: Path) -> None:
    """The raw files must be the ones the partition was built from."""
    bad = []
    for name, rel in RAW_FILES.items():
        if sha256_path(raw_dir / rel) != partition["sources"][name]["sha256"]:
            bad.append(rel)
    if sha256_path(guard_dir / DEV_SPLIT) != partition["sources"]["doc_tuning_split"]["sha256"]:
        bad.append(str(DEV_SPLIT))
    if bad:
        raise PartitionError(f"raw files differ from the partition's: {bad}")


# --------------------------------------------------------------------------- build from raw
def build(guard_dir: Path, *, seed: int = SEED) -> dict[str, Any]:
    """Read the raw files and the tuning split; needs the guard importable (parse rules)."""
    from mailguard.datasets.build_email_benchmark import parse_llmail_email, poisonedrag_cases

    raw = guard_dir / "datasets" / "raw"
    hams = [
        d
        for d in (json.loads(line) for line in (raw / RAW_FILES["enron_ham"]).read_text(encoding="utf-8").split("\n") if line.strip())
        if d.get("label_text") == "ham"
    ]
    fp = json.loads((raw / RAW_FILES["llmail_benign"]).read_text(encoding="utf-8"))
    llmail = [parse_llmail_email(str(t)) for t in fp]
    all_items = []
    for name in PRAG_DATASETS:
        data = json.loads((raw / RAW_FILES[f"poisonedrag_{name}"]).read_text(encoding="utf-8"))
        all_items += [f"{name}-{item.get('id', key)}" for key, item in data.items()]
    valid = [str(c["case_id"]).removeprefix("attack-prag-") for c in poisonedrag_cases(10**9, random.Random(0))]
    split = json.loads((guard_dir / DEV_SPLIT).read_text(encoding="utf-8"))
    tuning = {f"{r['dataset']}-{r['item_id']}" for r in split["poisoned"]}

    units = {
        "enron_ham": assign_enron(hams, n_benchmark=N_ENRON_BENCHMARK, seed=seed),
        "llmail_benign": assign_llmail_benign(llmail, bucket="l1_train"),
        "poisonedrag": assign_poisonedrag(all_items, valid, tuning),
    }
    sources = {name: {"path": f"datasets/raw/{rel}", "sha256": sha256_path(raw / rel)} for name, rel in RAW_FILES.items()}
    sources["doc_tuning_split"] = {"path": str(DEV_SPLIT.as_posix()), "sha256": sha256_path(guard_dir / DEV_SPLIT)}
    partition = {
        "schema": PARTITION_SCHEMA,
        "task": "7.36",
        "seed": seed,
        "buckets": list(BUCKETS),
        "normalisation": NORM_RULE,
        "rules": {
            "enron_ham": (
                f"ham only, grouped by normalised text; {N_ENRON_BENCHMARK} groups whose every message is "
                f"{ENRON_BENCH_CHARS[0]}-{ENRON_BENCH_CHARS[1]} chars -> benchmark (seeded draw, groups sorted by "
                f"first message_id); else {ENRON_L1_CHARS[0]}-{ENRON_L1_CHARS[1]} chars -> l1_train; else unused"
            ),
            "llmail_benign": "every distinct normalised text -> l1_train (owner, 2026-11-10)",
            "poisonedrag": "in rag_poison_dev.json -> doc_tuning; machine-checkable (poisonedrag_cases) -> benchmark; else unused",
        },
        "sources": sources,
        "counts": {source: counts(u) for source, u in units.items()},
        "units": units,
    }
    check_partition(partition)
    return partition


def write_json(path: Path, data: Any) -> None:
    """Pretty JSON with LF line ends (a Windows CRLF write would change the file's hash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=1, sort_keys=False, ensure_ascii=False) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guard-dir", type=Path, default=REPO_ROOT / "agentmailguard")
    ap.add_argument("--out", type=Path, default=PARTITION_PATH)
    ap.add_argument("--force", action="store_true", help="overwrite a partition that differs")
    args = ap.parse_args(argv)
    fresh = build(args.guard_dir.resolve())
    if args.out.is_file() and not args.force:
        if json.loads(args.out.read_text(encoding="utf-8")) != fresh:
            print(f"FAIL {args.out} exists and differs; the partition is fixed (--force to replace)", file=sys.stderr)
            return 1
    write_json(args.out, fresh)
    print(f"ok partition {fresh['counts']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

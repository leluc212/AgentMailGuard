"""Remove the benchmark's emails from the L1 corpus built by the guard (task 7.36; R22.12).

    # 1. the guard's own builder, unchanged (as `make mailguard-prep` runs it)
    uv run ... --directory <guard> python -m mailguard.datasets.build_l1_corpus --out-dir <full>
    # 2. this filter
    uv run --project . python -m evaluation.mailguard_bench.l1_corpus_filter --full <full> --out <filtered>

A row is removed when its normalised text is the content key of a ``benchmark`` unit of
``evaluation/datasets/partition_v1.json``, whatever source the builder's dedupe kept it under.
Rows are copied otherwise, with their ``split``: the builder splits by a content hash
(``hsplit``), so removing a row moves no other row between train, val and test.

The filter fails unless it removes exactly one row per benchmark key: a missing key means the
corpus is not the one the partition was built for, a second row would mean the dedupe broke.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.partition import (
    PARTITION_PATH,
    benchmark_keys,
    load_partition,
    norm_key,
    write_json,
)

SPLITS = ("train", "val", "test")


class CorpusFilterError(ValueError):
    """The corpus does not hold the benchmark keys exactly once each."""


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def filter_rows(
    rows_by_split: Mapping[str, Sequence[Mapping[str, Any]]], removed_keys: Mapping[str, set[str]]
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, Any]]:
    """Drop rows whose content key is a benchmark key; return kept rows and the tally."""
    wanted = {k: source for source, keys in removed_keys.items() for k in keys}
    kept: dict[str, list[Mapping[str, Any]]] = {}
    hits: Counter[str] = Counter()
    by: Counter[tuple[str, str, str]] = Counter()
    for split, rows in rows_by_split.items():
        kept[split] = []
        for r in rows:
            key = norm_key(str(r["text"]))
            if key in wanted:
                hits[key] += 1
                by[(wanted[key], str(r["source"]), split)] += 1
            else:
                kept[split].append(r)
    missing = sorted(set(wanted) - set(hits))
    twice = sorted(k for k, n in hits.items() if n > 1)
    if missing or twice:
        raise CorpusFilterError(
            f"expected each of {len(wanted)} benchmark keys once: {len(missing)} absent "
            f"(first {missing[:5]}), {len(twice)} more than once (first {twice[:5]})"
        )
    tally = {
        "expected": {source: len(keys) for source, keys in removed_keys.items()},
        "removed": sum(hits.values()),
        "removed_by": [
            {"partition_source": p, "corpus_source": s, "split": sp, "rows": n}
            for (p, s, sp), n in sorted(by.items())
        ],
    }
    return kept, tally


def stats(rows_by_split: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Same shape as the builder's stats.json."""
    rows = [r for split in SPLITS for r in rows_by_split.get(split, [])]
    return {
        "total": len(rows),
        "by_split": {s: len(rows_by_split.get(s, [])) for s in SPLITS},
        "by_label": dict(sorted(Counter(int(r["label"]) for r in rows).items())),
        "by_source": {
            s: dict(sorted(Counter(int(r["label"]) for r in rows if r["source"] == s).items()))
            for s in sorted({str(r["source"]) for r in rows})
        },
    }


def write_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    """The builder's line format (json.dumps, ensure_ascii=False), LF; returns the sha256."""
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", type=Path, required=True, help="output dir of build_l1_corpus")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--partition", type=Path, default=PARTITION_PATH)
    args = ap.parse_args(argv)
    partition = load_partition(args.partition)
    full = {s: read_rows(args.full / f"{s}.jsonl") for s in SPLITS}
    try:
        kept, tally = filter_rows(full, benchmark_keys(partition))
    except CorpusFilterError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    digests = {f"{s}.jsonl": write_rows(args.out / f"{s}.jsonl", kept[s]) for s in SPLITS}
    report = {
        "partition_sha256": hashlib.sha256(args.partition.read_bytes()).hexdigest(),
        "full": stats(full),
        "filtered": stats(kept),
        **tally,
        "sha256": digests,
    }
    write_json(args.out / "stats.json", stats(kept))
    write_json(args.out / "filter_report.json", report)
    print(f"ok removed {tally['removed']} rows {tally['removed_by']}")
    print(f"ok filtered {report['filtered']['by_split']} (full {report['full']['by_split']}) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

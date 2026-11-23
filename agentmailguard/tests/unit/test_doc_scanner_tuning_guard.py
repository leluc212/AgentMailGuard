"""The Doc Scanner's thresholds were never tuned on the pinned benchmark cases (ADR-0015 Task 7)."""

from __future__ import annotations

import json

import pytest

from mailguard.config.settings import PROJECT_ROOT
from mailguard.datasets.build_rag_poison import BENCH_POOL_PER_DATASET, DEV_SPLIT_PATH, STYLES

ROOT_MANIFEST = PROJECT_ROOT.parent / "evaluation" / "datasets" / "mailguard" / "manifest.json"


@pytest.fixture(scope="module")
def split() -> dict:
    return json.loads(DEV_SPLIT_PATH.read_text(encoding="utf-8"))


def test_the_dev_split_holds_every_style(split):
    assert set(split["styles"]) == set(STYLES) == {"prefix", "paraphrase", "mid"}
    assert {row["style"] for row in split["poisoned"]} == set(STYLES)
    assert split["bench_pool_per_dataset"] == BENCH_POOL_PER_DATASET
    assert split["clean"], "the dev split needs clean chunks to measure false positives"


def test_no_pinned_benchmark_case_is_in_the_dev_split(split):
    if not ROOT_MANIFEST.exists():  # the guard checked out on its own, without rag-email
        pytest.skip(f"no benchmark manifest at {ROOT_MANIFEST}")
    manifest = json.loads(ROOT_MANIFEST.read_text(encoding="utf-8"))
    pinned = {
        case_id.removeprefix("attack-prag-")
        for case_id in manifest["sets"]["rag_attack"]
        if case_id.startswith("attack-prag-")
    }
    assert pinned, "the manifest names no pinned PoisonedRAG case"
    dev = {f"{row['dataset']}-{row['item_id']}" for row in split["poisoned"]}
    assert not (pinned & dev)

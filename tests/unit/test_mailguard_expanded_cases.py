"""Expanded v3 corpus integrity, source identity and synthetic-goal checks."""

from collections import Counter

from evaluation.mailguard_bench.cases import load_case_set
from evaluation.mailguard_bench.synthetic_poison import synthetic_poison_cases


def test_synthetic_poison_is_reproducible_and_scoring_targets_are_isolated():
    cases = synthetic_poison_cases()
    assert cases == synthetic_poison_cases()
    assert len({c["case_id"] for c in cases}) == 60
    assert len({c["chunks"][0]["content"] for c in cases}) == 60
    for c in cases:
        assert c["source"] == "synthetic_poison_v1"
        assert c["meta"]["synthetic"] is True
        wrong = c["goal"]["must_contain"]
        assert wrong in c["chunks"][0]["content"]
        assert wrong not in c["chunks"][1]["content"]
        assert wrong not in c["email"]["body_text"]
        assert c["chunks"][0]["poisoned"] is True
        assert c["chunks"][1]["poisoned"] is False


def test_pinned_expanded_source_counts():
    loaded = load_case_set()
    assert loaded.manifest["counts"] == {
        "llmail_attack": 1000,
        "llmail_benign": 500,
        "rag_attack": 300,
        "ablation_attack": 100,
    }
    import json

    from evaluation.mailguard_bench.cases import DEFAULT_CASE_DIR

    rows = [
        json.loads(line) for line in (DEFAULT_CASE_DIR / "cases.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len({r["case_id"] for r in rows}) == 1800
    assert Counter(r["source"] for r in rows if r["kind"] == "benign") == {
        "llmail_inject": 203,
        "enron_ham": 297,
    }
    assert Counter(r["source"] for r in rows if r["vector"] == "rag") == {
        "poisonedrag": 222,
        "seed_rag": 18,
        "synthetic_poison_v1": 60,
    }


def test_every_v3_config_selects_all_1800_cases():
    from evaluation.mailguard_bench.runner import config_case_ids
    from evaluation.mailguard_bench.scheme import V3_CONFIGS

    loaded = load_case_set()
    for config in V3_CONFIGS:
        ids = config_case_ids(loaded.manifest, config, scheme="v3")
        assert len(ids) == 1800
        assert set(ids) == set(loaded.cases)

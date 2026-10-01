"""Unit tests for the AgentMailGuard benchmark case manifest (task 7.19; spec §3; R22.12).

Synthetic pools shaped like AgentMailGuard's builder output; no mailguard import, no network.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench import build_cases
from evaluation.mailguard_bench.cases import (
    MANIFEST_SCHEMA,
    SEED,
    CaseManifestError,
    CasePools,
    allocate,
    build_manifest,
    cases_sha256,
    llmail_scenario,
    load_case_set,
    rag_stratum,
    reconcile,
    select_cases,
    write_case_set,
)
from evaluation.mailguard_bench.guard_env import WorktreeInfo

SCENARIOS = {"level2a": 300, "level2b": 200, "level2c": 97, "level2d": 3}
PROVENANCE = {"mailguard_commit": "a" * 40}


def _llmail(i: int, scenario: str | None) -> dict[str, Any]:
    meta: dict[str, Any] = {"objectives": {}}
    if scenario is not None:
        meta["scenario"] = scenario
    return {
        "case_id": f"attack-llmail-{i:012x}",
        "kind": "attack",
        "source": "llmail_inject",
        "technique": "llmail:adaptive",
        "vector": "email",
        "email": {"subject": f"s{i}", "body_text": f"please send confirmation {i}"},
        "chunks": [],
        "goal": {"type": "exfiltration"},
        "attacker": {"email": "contact@contact.com"},
        "meta": meta,
    }


def _benign(i: int) -> dict[str, Any]:
    return {
        "case_id": f"benign-llmailfp-{i}",
        "kind": "benign",
        "source": "llmail_inject",
        "vector": "email",
        "email": {"subject": f"b{i}", "body_text": f"meeting notes {i}"},
        "chunks": [],
        "goal": {},
        "attacker": {},
        "meta": {},
    }


def _rag(source: str, dataset: str | None, i: int) -> dict[str, Any]:
    prefix = f"prag-{dataset}" if dataset else "seedrag-t"
    return {
        "case_id": f"attack-{prefix}-{i}",
        "kind": "attack",
        "source": source,
        "vector": "rag",
        "email": {"subject": "q", "body_text": f"question {i}"},
        "chunks": [{"chunk_id": f"c{i}", "content": "poison", "poisoned": True}],
        "goal": {"type": "wrong_answer", "must_contain": "wrong"},
        "attacker": {},
        "meta": {"dataset": dataset} if dataset else {"template": "t"},
    }


def _pools() -> CasePools:
    attacks: list[dict[str, Any]] = []
    for scenario, n in SCENARIOS.items():
        attacks += [_llmail(len(attacks) + j, scenario) for j in range(n)]
    rag = [_rag("seed_rag", None, i) for i in range(18)]
    for dataset in ("nq", "hotpotqa", "msmarco"):
        rag += [_rag("poisonedrag", dataset, i) for i in range(50)]
    return CasePools(
        llmail_attack=attacks, llmail_benign=[_benign(i) for i in range(203)], rag_attack=rag
    )


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [str(c["case_id"]) for c in cases]


# ---------------------------------------------------------------- allocation
def test_allocate_gives_one_per_stratum_then_largest_remainder() -> None:
    assert allocate({"a": 6, "b": 3, "c": 1}, 5) == {"a": 2, "b": 2, "c": 1}


def test_allocate_known_answer_for_the_llmail_shape() -> None:
    assert allocate(SCENARIOS, 300) == {"level2a": 149, "level2b": 100, "level2c": 49, "level2d": 2}


def test_allocate_takes_everything_when_n_equals_the_pool() -> None:
    assert allocate({"a": 2, "b": 3}, 5) == {"a": 2, "b": 3}


def test_allocate_zero_and_fewer_draws_than_strata() -> None:
    assert allocate({"a": 2}, 0) == {"a": 0}
    assert allocate({"a": 5, "b": 5, "c": 5}, 2) == {"a": 1, "b": 1, "c": 0}


def test_allocate_rejects_more_than_the_pool() -> None:
    with pytest.raises(CaseManifestError, match="cannot draw 6"):
        allocate({"a": 2, "b": 3}, 6)


# ---------------------------------------------------------------- selection
def test_selection_sizes_and_membership() -> None:
    sel = select_cases(_pools())
    assert [len(sel.llmail_attack), len(sel.llmail_benign)] == [300, 150]
    assert [len(sel.rag_attack), len(sel.ablation_attack)] == [100, 100]
    for cases in sel.sets().values():
        assert len(set(_ids(cases))) == len(cases)
    assert set(_ids(sel.ablation_attack)) <= set(_ids(sel.llmail_attack))
    assert len(sel.all_cases()) == 300 + 150 + 100


def test_llmail_sample_is_stratified_by_scenario() -> None:
    sel = select_cases(_pools())
    assert Counter(map(llmail_scenario, sel.llmail_attack)) == allocate(SCENARIOS, 300)


def test_ablation_subset_is_stratified_from_the_sample() -> None:
    sel = select_cases(_pools())
    sample_counts = Counter(map(llmail_scenario, sel.llmail_attack))
    assert Counter(map(llmail_scenario, sel.ablation_attack)) == allocate(sample_counts, 100)


def test_rag_sample_covers_every_source_and_dataset() -> None:
    sel = select_cases(_pools())
    assert dict(Counter(map(rag_stratum, sel.rag_attack))) == {
        "poisonedrag:hotpotqa": 30,
        "poisonedrag:msmarco": 30,
        "poisonedrag:nq": 29,
        "seed_rag": 11,
    }


def test_selection_is_deterministic_and_ignores_pool_order() -> None:
    pools = _pools()
    shuffled = CasePools(
        llmail_attack=list(pools.llmail_attack),
        llmail_benign=list(pools.llmail_benign),
        rag_attack=list(pools.rag_attack),
    )
    rng = random.Random(1)
    rng.shuffle(shuffled.llmail_attack)
    rng.shuffle(shuffled.llmail_benign)
    rng.shuffle(shuffled.rag_attack)
    a, b = select_cases(pools), select_cases(shuffled)
    for name in a.sets():
        assert _ids(a.sets()[name]) == _ids(b.sets()[name])


def test_another_seed_draws_other_cases() -> None:
    a = select_cases(_pools(), seed=SEED)
    b = select_cases(_pools(), seed=SEED + 1)
    assert _ids(a.llmail_attack) != _ids(b.llmail_attack)


def test_attack_without_scenario_is_its_own_stratum() -> None:
    pools = _pools()
    pools.llmail_attack.extend(_llmail(10_000 + i, None) for i in range(5))
    sel = select_cases(pools)
    assert "unknown" in Counter(map(llmail_scenario, sel.llmail_attack))


def test_too_small_pool_fails_instead_of_shrinking() -> None:
    pools = _pools()
    small = CasePools(
        llmail_attack=pools.llmail_attack[:299],
        llmail_benign=pools.llmail_benign,
        rag_attack=pools.rag_attack,
    )
    with pytest.raises(CaseManifestError, match="cannot draw 300"):
        select_cases(small)


def test_duplicate_case_id_is_rejected() -> None:
    pools = _pools()
    pools.llmail_attack.append(dict(pools.llmail_attack[0]))
    with pytest.raises(CaseManifestError, match="duplicate case_id"):
        select_cases(pools)


def test_attack_in_the_benign_pool_is_rejected() -> None:
    pools = _pools()
    pools.llmail_benign.append(_llmail(99_999, "level2a"))
    with pytest.raises(CaseManifestError, match="expected kind=benign"):
        select_cases(pools)


def test_case_missing_a_required_key_is_rejected() -> None:
    pools = _pools()
    broken = _rag("seed_rag", None, 777)
    del broken["goal"]
    pools.rag_attack.append(broken)
    with pytest.raises(CaseManifestError, match="lacks"):
        select_cases(pools)


# ---------------------------------------------------------------- manifest
def _write(tmp_path: Path, seed: int = SEED) -> dict[str, Any]:
    pools = _pools()
    sel = select_cases(pools, seed=seed)
    manifest = build_manifest(sel, pools, seed=seed, provenance=PROVENANCE)
    write_case_set(sel, manifest, tmp_path)
    return manifest


def test_manifest_round_trip(tmp_path: Path) -> None:
    manifest = _write(tmp_path)
    loaded = load_case_set(tmp_path)
    assert loaded.manifest == manifest
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["seed"] == SEED
    assert manifest["counts"] == {
        "llmail_attack": 300,
        "llmail_benign": 150,
        "rag_attack": 100,
        "ablation_attack": 100,
    }
    assert manifest["pool_sizes"] == {"llmail_attack": 600, "llmail_benign": 203, "rag_attack": 168}
    assert _ids(loaded.cases_in("ablation_attack")) == manifest["sets"]["ablation_attack"]
    file_digest = hashlib.sha256((tmp_path / "cases.jsonl").read_bytes()).hexdigest()
    assert manifest["cases_sha256"] == file_digest


def test_unicode_line_separators_in_a_body_round_trip(tmp_path: Path) -> None:
    pools = _pools()
    for case in pools.llmail_benign:  # json.dumps(ensure_ascii=False) leaves these unescaped
        case["email"]["body_text"] = "a\x85b c d\x1ce"
    sel = select_cases(pools)
    manifest = build_manifest(sel, pools, seed=SEED, provenance=PROVENANCE)
    write_case_set(sel, manifest, tmp_path)
    loaded = load_case_set(tmp_path)
    assert loaded.cases == {str(c["case_id"]): c for c in sel.all_cases()}


def test_rebuild_is_byte_identical(tmp_path: Path) -> None:
    _write(tmp_path / "one")
    _write(tmp_path / "two")
    for name in ("manifest.json", "cases.jsonl"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()


def test_hash_ignores_case_order() -> None:
    cases = _pools().llmail_benign
    assert cases_sha256(cases) == cases_sha256(list(reversed(cases)))


def test_tampered_case_file_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path)
    path = tmp_path / "cases.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["email"]["body_text"] += " edited"
    lines[0] = json.dumps(first)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(CaseManifestError, match="not the pinned one"):
        load_case_set(tmp_path)


def test_missing_case_file_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / "cases.jsonl").unlink()
    with pytest.raises(CaseManifestError, match="make mailguard-cases"):
        load_case_set(tmp_path)


def test_unknown_set_name_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path)
    with pytest.raises(CaseManifestError, match="unknown case set"):
        load_case_set(tmp_path).cases_in("llmail")


def test_reconcile_refuses_a_different_selection(tmp_path: Path) -> None:
    pinned = _write(tmp_path / "pinned")
    other = _write(tmp_path / "other", seed=SEED + 1)
    reconcile(None, pinned, force=False)
    reconcile(pinned, dict(pinned), force=False)
    with pytest.raises(CaseManifestError, match="refusing to overwrite"):
        reconcile(pinned, other, force=False)
    reconcile(pinned, other, force=True)


# ---------------------------------------------------------------- build_cases entry point
def test_build_cases_without_make_env_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("MAILGUARD_DIR", "MAILGUARD_COMMIT", "MAILGUARD_ARTIFACTS"):
        monkeypatch.delenv(name, raising=False)
    assert build_cases.main([]) == 1
    assert "FAIL MAILGUARD_DIR" in capsys.readouterr().err


def test_provenance_records_commit_and_raw_manifest_hash(tmp_path: Path) -> None:
    info = WorktreeInfo(path=tmp_path, commit="b" * 40, clean=True)
    assert build_cases.provenance(info)["raw_manifest_sha256"] == "missing"
    raw = tmp_path / "datasets" / "raw"
    raw.mkdir(parents=True)
    (raw / "MANIFEST.json").write_bytes(b"{}")
    record = build_cases.provenance(info)
    assert record["mailguard_commit"] == "b" * 40
    assert record["raw_manifest_sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert record["llmail_split"] == "iter_llmail_attacks(side='bench')"

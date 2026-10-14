"""v2 parity: the guard's outcome on every matrix case equals the committed golden.

A difference is either a regression or an intended change; intended changes are listed in
``tests.parity.INTENDED_DELTAS`` with the ADR-0015 bullet and the test that proves them.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.parity import (
    DOC_VARIANTS,
    GOLDEN_PATH,
    INTENDED_DELTAS,
    OUTCOME_KEYS,
    PROMPT_NAMES,
    ParityCase,
    apply_deltas,
    capture,
    cases,
    diff_records,
    load_golden,
    offline_settings,
    render_golden,
    run_case,
)

SUBTREE = Path(__file__).resolve().parents[2]
CASES = cases()


@pytest.fixture(scope="module")
def expected() -> dict[str, dict]:
    return apply_deltas(load_golden(), INTENDED_DELTAS)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case_matches_golden(case: ParityCase, expected, tmp_path):
    actual = run_case(case, offline_settings(tmp_path))
    assert tuple(sorted(actual)) == tuple(sorted(OUTCOME_KEYS))
    want = expected.get(case.id)
    assert want is not None, f"{case.id} has no golden entry"
    assert actual == want, f"{case.id} differs from the v2 golden:\n{diff_records(want, actual)}"


def test_every_case_has_a_golden_entry():
    golden = load_golden()
    missing = [c.id for c in CASES if c.id not in golden]
    assert not missing, f"cases without a golden entry: {missing}"


def test_no_golden_entry_is_orphaned():
    ids = {c.id for c in CASES}
    orphans = sorted(k for k in load_golden() if k not in ids)
    assert not orphans, f"golden entries without a case: {orphans}"


def test_matrix_size():
    per_variant = Counter(c.docs for c in CASES)
    print(
        f"parity matrix: {len(CASES)} cases; per doc variant: {dict(sorted(per_variant.items()))}"
    )
    assert len(CASES) >= 80
    assert len({c.id for c in CASES}) == len(CASES)
    assert all(per_variant[v] >= 8 for v in DOC_VARIANTS)


def test_parity_matrix_exercises_every_judge_prompt():
    golden = load_golden()
    uses = {
        name: sum(1 for r in golden.values() if name in r["llm_calls"]) for name in PROMPT_NAMES
    }
    print(f"golden entries calling each guard prompt: {uses}")
    thin = {name: n for name, n in uses.items() if n < 8}
    assert not thin, f"guard prompts exercised by fewer than 8 golden entries: {thin}"


def test_golden_is_byte_stable(tmp_path):
    regenerated = render_golden(capture(offline_settings(tmp_path)))
    assert regenerated.encode("utf-8") == GOLDEN_PATH.read_bytes()


def test_intended_deltas_name_an_existing_test():
    for case_id, delta in INTENDED_DELTAS.items():
        assert set(delta) == {"fields", "why", "test"}, f"{case_id}: malformed delta"
        assert delta["why"].strip(), f"{case_id}: delta does not say why"
    wanted = {delta["test"] for delta in INTENDED_DELTAS.values()}
    if not wanted:
        return
    # addopts already holds -q; clear it so a single -q lists one node id per line
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="]
    collected = subprocess.run(
        [*command, "-p", "no:cacheprovider"],
        cwd=SUBTREE,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    missing = sorted(wanted - set(collected))
    assert not missing, f"intended deltas name tests that do not exist: {missing}"

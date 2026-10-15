"""Ratchet, root twin: the benchmark code and its tests carry no v2 guard identifiers (Task 15a).

Same forbidden patterns as ``agentmailguard/tests/unit/test_no_legacy_identifiers.py``, over
``evaluation/mailguard_bench`` and ``tests/unit/test_mailguard_*`` (never ``evaluation/results/``).
Only the legacy reader and its test, which read v1/v2 run folders, may name the old layers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

FORBIDDEN = re.compile(
    r"l1_injection_scanner|l2_intent_extractor|l3_channel_isolation|l3b_document_scanner"
    r"|l4_output_scanner|l5_policy_engine"
    r"|\bL[1-5]B?__"
    r"|GUARD_MODELS__(?:JUDGE|EXTRACTOR|DOC_SCANNER)\b"
    r"|report\.l3b"
    r"|\.l(?:[1-5]|3b)\b"
)

ALLOWLIST: dict[str, str] = {
    "evaluation/mailguard_bench/legacy_reader.py": "reads v1/v2 run folders (old names)",
    "tests/unit/test_mailguard_bench_legacy_reader.py": "legacy test data for that reader",
    "tests/unit/test_mailguard_v3_identifiers.py": "this ratchet names the forbidden patterns",
}


def _files() -> list[Path]:
    files = sorted((REPO / "evaluation" / "mailguard_bench").rglob("*.py"))
    files += sorted((REPO / "tests" / "unit").glob("test_mailguard_*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def test_the_benchmark_code_has_no_v2_identifiers() -> None:
    bad: list[str] = []
    for path in _files():
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWLIST:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            bad.extend(f"{rel}:{no}: {m.group(0)!r}" for m in FORBIDDEN.finditer(line))
    assert bad == []


def test_the_ratchet_scans_something() -> None:
    assert len(_files()) > 30


def test_the_allowlist_is_only_legacy_files_that_exist() -> None:
    for rel in ALLOWLIST:
        assert (REPO / rel).is_file(), rel


@pytest.mark.parametrize("text", ["l4_output_scanner", "L2__X", "scheme.l3", "GUARD_MODELS__JUDGE"])
def test_the_pattern_catches_old_names(text: str) -> None:
    assert FORBIDDEN.search(text)

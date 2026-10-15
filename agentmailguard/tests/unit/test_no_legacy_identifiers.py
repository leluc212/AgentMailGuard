"""Ratchet: the v2 guard identifiers stay out of the v3 guard (plan_v3 F5, Task 15a).

Scans the guard's own tree for the v2 layer names, the ``L1__``..``L5__`` env prefixes, the old
``GUARD_MODELS__`` keys and attribute access on a report (``report.l3b``, ``.l1`` ... ``.l5``).
Every allowlist entry is narrow (a file, or a file plus a regex) and carries its reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

GUARD_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("mailguard", "services", "evaluation", "training", "configs", "tests")
SUFFIXES = {".py", ".yaml", ".yml", ".json", ".toml", ".txt", ".cfg", ".ini", ".sh"}

FORBIDDEN = re.compile(
    r"l1_injection_scanner|l2_intent_extractor|l3_channel_isolation|l3b_document_scanner"
    r"|l4_output_scanner|l5_policy_engine"
    r"|\bL[1-5]B?__"
    r"|GUARD_MODELS__(?:JUDGE|EXTRACTOR|DOC_SCANNER)\b"
    r"|report\.l3b"
    r"|\.l(?:[1-5]|3b)\b"
)

# (path relative to the guard root, regex limiting the exemption or None for the whole file, reason)
ALLOWLIST: tuple[tuple[str, str | None, str], ...] = (
    (
        "tests/unit/test_no_legacy_identifiers.py",
        None,
        "this ratchet names the forbidden patterns",
    ),
    (
        "tests/golden/",
        None,
        "golden files were recorded under the v2 guard and are never regenerated",
    ),
    (
        "tests/deltas/task6_guard_config.json",
        None,
        "recorded v2-keyed parity deltas (frozen data)",
    ),
    (
        "tests/deltas/task7_doc_scanner_before_gate.json",
        None,
        "recorded v2-keyed parity deltas (frozen data)",
    ),
    (
        "tests/parity.py",
        r"l[1-5]b?_[a-z_]+",
        "parity maps a v2 golden key to its v3 layer (docstring and GOLDEN_PROMPT_KEY map)",
    ),
    (
        "mailguard/config/settings.py",
        r"\bL[1-5]B?__|GUARD_MODELS__(?:JUDGE|EXTRACTOR|DOC_SCANNER)\b",
        "LEGACY_ENV_KEYS: the migration table from v2 env keys to v3 keys",
    ),
    (
        "tests/unit/test_settings_v3.py",
        r"\bL[1-5]B?__|GUARD_MODELS__(?:JUDGE|EXTRACTOR|DOC_SCANNER)\b",
        "tests the LEGACY_ENV_KEYS migration table, so it must name the old keys",
    ),
    (
        "training/train_l1_classifier.py",
        r"\.l1\b",
        "logger name training.l1 of the L1 classifier trainer (script name is kept)",
    ),
    (
        "mailguard/datasets/build_l1_corpus.py",
        r"\.l1\b",
        "logger name mailguard.datasets.l1 of the L1 corpus builder (script name is kept)",
    ),
)


def _exempt(rel: str, text: str) -> bool:
    for path, pattern, _reason in ALLOWLIST:
        if rel != path and not (path.endswith("/") and rel.startswith(path)):
            continue
        if pattern is None or re.fullmatch(pattern, text):
            return True
    return False


def _files() -> list[Path]:
    found: list[Path] = []
    for name in SCAN_DIRS:
        base = GUARD_ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                path.is_file()
                and path.suffix in SUFFIXES
                and "__pycache__" not in path.parts
                and "results" not in path.relative_to(GUARD_ROOT).parts
            ):
                found.append(path)
    return found


def _violations() -> list[str]:
    bad: list[str] = []
    for path in _files():
        rel = path.relative_to(GUARD_ROOT).as_posix()
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in FORBIDDEN.finditer(line):
                if not _exempt(rel, match.group(0)):
                    bad.append(f"{rel}:{no}: {match.group(0)!r}")
    return bad


def test_the_guard_tree_has_no_v2_identifiers() -> None:
    assert _violations() == []


def test_the_ratchet_scans_something() -> None:
    assert len(_files()) > 50


@pytest.mark.parametrize(
    "text",
    [
        "l2_intent_extractor",
        "L3B__LLM_ENABLED",
        "GUARD_MODELS__JUDGE",
        "report.l3b",
        "verdicts.l4",
        "guard.l1.score",
    ],
)
def test_the_pattern_catches_each_family(text: str) -> None:
    assert FORBIDDEN.search(text)


@pytest.mark.parametrize(
    "text",
    ["GUARD_MODELS__INBOUND_JUDGE", "INBOUND_GUARD__SCAN", "l1_injection_clf_v1", "x.layers"],
)
def test_the_pattern_leaves_v3_names_alone(text: str) -> None:
    assert not FORBIDDEN.search(text)


def test_every_allowlist_entry_still_matches_something() -> None:
    """A stale exemption is a hole: each entry must still cover at least one real hit."""
    for path, pattern, reason in ALLOWLIST:
        assert reason
        target = GUARD_ROOT / path
        assert target.exists(), f"allowlisted path is gone: {path}"
        if path == "tests/unit/test_no_legacy_identifiers.py":
            continue
        files = [f for f in target.rglob("*") if f.is_file()] if target.is_dir() else [target]
        hits = [
            m.group(0)
            for f in files
            for m in FORBIDDEN.finditer(f.read_text(encoding="utf-8", errors="ignore"))
            if pattern is None or re.search(pattern, m.group(0))
        ]
        assert hits, f"allowlist entry has no hit left: {path}"

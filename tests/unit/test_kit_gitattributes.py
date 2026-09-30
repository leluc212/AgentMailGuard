"""The repository's line-ending policy, read back with ``git check-attr`` (task 7.23).

A teammate on Windows clones the repository; a checkout that turns LF into CRLF breaks shell
scripts and Makefiles under WSL and changes the bytes the pinned inputs are hashed from. The
policy: text files are LF in the working tree on every OS, binaries are never touched, and the
MIME fixtures that are CRLF on purpose are byte-exact.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _attrs(path: str) -> dict[str, str]:
    """``git check-attr text eol diff -- path`` as ``{attribute: value}``."""
    out = subprocess.run(
        ["git", "check-attr", "text", "eol", "diff", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, str] = {}
    for line in out.splitlines():
        _, name, value = line.rsplit(": ", 2)
        result[name] = value
    return result


@pytest.mark.parametrize(
    "path",
    [
        "Makefile",
        "packages/core/settings.py",
        "evaluation/datasets/mailguard/cases.jsonl",
        "docs/BENCHMARK.md",
        "migrations/versions/0001_initial.sql",
        "tests/fixtures/triage/some_new_mail.eml",
        "docker-compose.yml",
    ],
)
def test_text_files_are_lf_in_every_working_tree(path: str) -> None:
    attrs = _attrs(path)
    assert attrs["text"] == "auto"
    assert attrs["eol"] == "lf"


@pytest.mark.parametrize(
    "path",
    [
        "evaluation/mailguard_bench/pinned/l1_injection_clf_v1.joblib",
        "artifacts/models/triage_ml_v1.joblib",
        "docs/architecture/AgentMailGuard_Security_Architecture.2048x1320.light.png",
        "docs/architecture.zip",
        "docs/manual.pdf",
    ],
)
def test_binary_files_are_never_converted(path: str) -> None:
    attrs = _attrs(path)
    assert attrs["text"] == "unset"
    assert attrs["diff"] == "unset"


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/mime/04_iso_8859_1_latin1.eml",
        "tests/fixtures/mime/05_windows_1252.eml",
        "tests/fixtures/mime/06_shift_jis.eml",
    ],
)
def test_crlf_mime_fixtures_are_byte_exact(path: str) -> None:
    assert _attrs(path)["text"] == "unset"


def test_result_tables_keep_the_bytes_the_csv_writer_produced() -> None:
    path = "evaluation/results/mailguard_bench/2026-09-29-qwen25/metrics.csv"
    assert _attrs(path)["text"] == "unset"


def test_every_tracked_text_file_is_lf_in_the_index() -> None:
    """Nothing tracked is CRLF unless its attributes say it is byte-exact."""
    out = subprocess.run(
        ["git", "ls-files", "--eol"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    offenders = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if meta.split()[0] in {"i/crlf", "i/mixed"} and _attrs(path)["text"] != "unset":
            offenders.append(path)
    assert offenders == []

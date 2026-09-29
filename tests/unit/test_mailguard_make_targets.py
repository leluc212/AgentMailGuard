"""The benchmark's Make targets are owner-run and never part of ``make ci`` (task 7.19; R24.5)."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = (REPO / "Makefile").read_text(encoding="utf-8")


def targets() -> set[str]:
    return set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", MAKEFILE, re.MULTILINE))


def recipe(target: str) -> str:
    match = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n?)+)", MAKEFILE, re.MULTILINE)
    assert match, f"no recipe for {target}"
    return match.group(1)


def ci_prerequisites() -> list[str]:
    match = re.search(r"^ci:(.*)$", MAKEFILE, re.MULTILINE)
    assert match
    return match.group(1).split()


def phony() -> list[str]:
    match = re.search(r"^\.PHONY:(.*)$", MAKEFILE, re.MULTILINE)
    assert match
    return match.group(1).split()


def test_no_mailguard_target_runs_in_ci() -> None:
    assert not [t for t in ci_prerequisites() if t.startswith("mailguard")]


def test_report_target_runs_the_module_from_the_repo_root_with_the_worktree() -> None:
    assert "mailguard-report" in targets() and "mailguard-report" in phony()
    body = recipe("mailguard-report")
    # `python -m` from the repo root keeps rag-email's `evaluation` package ahead of
    # AgentMailGuard's (both are top-level); --with-editable leaves uv.lock untouched.
    assert "--with-editable $(MAILGUARD_DIR)" in MAKEFILE
    assert "-m evaluation.mailguard_bench.report" in body
    assert 'test -n "$(RUN)"' in body

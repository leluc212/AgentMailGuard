"""The benchmark's Make targets are owner-run and never part of ``make ci`` (task 7.19; R24.5)."""

from __future__ import annotations

import re
import subprocess
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


def test_analyses_target_scores_then_analyses_then_reports() -> None:
    assert "mailguard-analyses" in targets() and "mailguard-analyses" in phony()
    steps = re.findall(r"-m (evaluation\.mailguard_bench\.\w+)", recipe("mailguard-analyses"))
    assert steps == [
        "evaluation.mailguard_bench.report",
        "evaluation.mailguard_bench.analyses",
        "evaluation.mailguard_bench.report",
    ]


RUNBOOK = (REPO / "docs" / "demo-runbook.md").read_text(encoding="utf-8")


def test_every_benchmark_command_in_the_runbook_is_a_make_target() -> None:
    named = set(re.findall(r"make (mailguard-[a-z0-9-]+)", RUNBOOK))
    assert {"mailguard-bench", "mailguard-report", "mailguard-analyses"} <= named
    assert named <= targets(), f"runbook names unknown targets: {sorted(named - targets())}"


def _make(*args: str) -> str:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_the_worker_count_reaches_the_runner_as_an_argument_only() -> None:
    # demo-runbook §9.8 passes CONCURRENCY=2 on the make command line. Make exports such a
    # variable to every recipe, and AppSettings reads CONCURRENCY as its `concurrency` group,
    # so every run stopped at "1 validation error for AppSettings" before its first case.
    leaked = _make(
        "-s",
        "--eval",
        'print-env: ; @env | grep "^CONCURRENCY=" || true',
        "print-env",
        "CONCURRENCY=2",
    )
    assert leaked == ""
    dry_run = _make("-n", "mailguard-bench", "RUN=r", "CONFIG=C0", "CONCURRENCY=2")
    assert "--concurrency 2" in dry_run

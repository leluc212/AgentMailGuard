"""The AgentMailGuard pin: v2 pins 1a3ef62, v1 stays reproducible at 81df5d07 (task 7.20).

ADR-0012 decision 3: guard fixes (b) and (c) were committed on feature/mailguard-defense-stack and
that commit is pinned for v2. v1 keeps its own pin, so its published numbers can be reproduced and
are never mixed with v2's. No network, no docker, no mailguard import: the Make targets are run
against throw-away local git repositories.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from evaluation.mailguard_bench.guard_env import (
    DEFAULT_MAILGUARD_COMMIT,
    REPO_ROOT,
    V1_MAILGUARD_COMMIT,
    V2_MAILGUARD_COMMIT,
    GuardEnvError,
    guard_paths_from_env,
    require_pinned_worktree,
)

MAKEFILE = REPO_ROOT / "Makefile"
V1 = "81df5d07b15b5bb3d1ecf3aae556df01e304cbe0"
V2 = "1a3ef62b7368703c22c3f90111abdde0678d5617"
needs_git_and_make = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("make") is None, reason="needs git and make"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _makefile_default() -> str:
    match = re.search(r"^MAILGUARD_COMMIT \?= (\S+)$", MAKEFILE.read_text("utf-8"), re.M)
    assert match, "the Makefile has no `MAILGUARD_COMMIT ?=` line"
    return match.group(1)


def test_v2_pins_the_guard_commit_with_the_visible_fallback_fixes() -> None:
    assert V2_MAILGUARD_COMMIT == V2
    assert DEFAULT_MAILGUARD_COMMIT == V2_MAILGUARD_COMMIT


def test_v1_keeps_the_commit_its_published_results_were_produced_with() -> None:
    assert V1_MAILGUARD_COMMIT == V1
    root = REPO_ROOT / "evaluation/results/mailguard_bench"
    manifests = sorted(root.glob("2026-09-29-*/manifest.json"))
    assert manifests, "the v1 result folders are gone"
    for manifest in manifests:
        assert (
            json.loads(manifest.read_text("utf-8"))["git"]["agentmailguard"]["sha"]
            == V1_MAILGUARD_COMMIT
        )


def test_the_makefile_default_is_the_v2_pin() -> None:
    assert _makefile_default() == DEFAULT_MAILGUARD_COMMIT


def test_the_env_names_the_pin_and_nothing_else_supplies_it() -> None:
    paths = guard_paths_from_env(
        {"MAILGUARD_DIR": "/g", "MAILGUARD_COMMIT": V1, "MAILGUARD_ARTIFACTS": "/a"}
    )
    assert paths.commit == V1  # a v1 run sets MAILGUARD_COMMIT to the old pin and gets it
    with pytest.raises(GuardEnvError, match="MAILGUARD_COMMIT not set"):
        guard_paths_from_env({"MAILGUARD_DIR": "/g", "MAILGUARD_ARTIFACTS": "/a"})


@pytest.fixture
def guard_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A repo with two commits standing for the v1 and the v2 guard commit."""
    root = tmp_path / "guard"
    root.mkdir()
    _git(root, "init", "-q")
    shas = []
    for n in (1, 2):
        (root / "f.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", f"c{n}")
        shas.append(_git(root, "rev-parse", "HEAD"))
    return root, shas


@needs_git_and_make
def test_a_worktree_at_the_old_commit_is_a_valid_v1_pin_and_is_refused_for_v2(
    guard_repo: tuple[Path, list[str]],
) -> None:
    root, (first, second) = guard_repo
    _git(root, "checkout", "-q", "--detach", first)

    assert require_pinned_worktree(root, first).commit == first  # v1: pinned to the old commit
    with pytest.raises(GuardEnvError, match="pinned commit is " + second):
        require_pinned_worktree(root, second)


def test_the_refusal_says_which_guard_generation_the_worktree_is_at(
    guard_repo: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluation.mailguard_bench import guard_env

    root, (first, second) = guard_repo
    _git(root, "checkout", "-q", "--detach", first)
    monkeypatch.setattr(guard_env, "V1_MAILGUARD_COMMIT", first)
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_COMMIT", second)

    with pytest.raises(GuardEnvError) as excinfo:
        require_pinned_worktree(root, second)

    message = str(excinfo.value)
    assert "v1 guard" in message and "MAILGUARD_DIR" in message and "MAILGUARD_COMMIT" in message


# --- `make mailguard-worktree`: create or check the worktree at the pinned commit


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> tuple[Path, list[str]]:
    """A clone of a local "origin" with the guard branch (what the target fetches) and a
    copy of the real Makefile, so the real recipe runs against local repositories only."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "feature/mailguard-defense-stack")
    shas = []
    for n in (1, 2):
        (origin / "f.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-q", "-m", f"c{n}")
        shas.append(_git(origin, "rev-parse", "HEAD"))
    work = tmp_path / "work" / "rag-email"
    work.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "checkout", "-q", "--detach", shas[0])
    shutil.copy(MAKEFILE, work / "Makefile")
    return work, shas


def _make_worktree(work: Path, commit: str) -> subprocess.CompletedProcess[str]:
    target = work.parent / "guard-wt"
    return subprocess.run(
        ["make", "mailguard-worktree", f"MAILGUARD_DIR={target}", f"MAILGUARD_COMMIT={commit}"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )


@needs_git_and_make
def test_make_mailguard_worktree_creates_the_worktree_at_the_pinned_commit(
    repo_with_origin: tuple[Path, list[str]],
) -> None:
    work, (first, second) = repo_with_origin

    done = _make_worktree(work, second)  # the commit is only on origin's branch: it is fetched

    assert done.returncode == 0, done.stderr
    assert _git(work.parent / "guard-wt", "rev-parse", "HEAD") == second
    assert f"@ {second}" in done.stdout


@needs_git_and_make
def test_make_mailguard_worktree_checks_an_existing_worktree_and_refuses_another_commit(
    repo_with_origin: tuple[Path, list[str]],
) -> None:
    work, (first, second) = repo_with_origin
    assert _make_worktree(work, first).returncode == 0

    again = _make_worktree(work, first)
    assert again.returncode == 0 and "not re-adding" in again.stdout

    wrong = _make_worktree(work, second)  # the directory holds the v1-style commit
    assert wrong.returncode != 0
    assert f"is not at MAILGUARD_COMMIT={second}" in wrong.stderr
    assert "MAILGUARD_DIR" in wrong.stderr  # it says how to get a worktree of the other pin


@needs_git_and_make
@pytest.mark.parametrize(("override", "pin"), [(None, V2), (V1, V1)])
def test_the_v1_runner_runs_under_whichever_pin_the_environment_names(
    override: str | None, pin: str
) -> None:
    args = ["make", "-n", "mailguard-bench", "CONFIG=C3", "RUN=r"]
    if override:
        args.append(f"MAILGUARD_COMMIT={override}")

    # Hermetic: a MAILGUARD_COMMIT exported by the caller (the overlay runs of the unit suite set
    # it to the worktree's HEAD) would win over the Makefile's default, which is what `None` tests.
    env = {k: v for k, v in os.environ.items() if k != "MAILGUARD_COMMIT"}

    done = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, check=True, env=env)

    assert f"MAILGUARD_COMMIT={pin} " in done.stdout
    assert "-m evaluation.mailguard_bench.runner" in done.stdout


def test_the_runbook_states_both_pins_where_the_operator_reads_them() -> None:
    text = (REPO_ROOT / "docs/demo-runbook.md").read_text("utf-8")
    section9 = text[text.index("## 9. AgentMailGuard benchmark") : text.index("## Appendix A")]
    section99 = section9[section9.index("### 9.9 ") :]

    assert V1 in section9 and V2 in section9
    assert V2 in section99  # the v2 procedure names the pin it runs on
    assert "guarded.v2" in section99

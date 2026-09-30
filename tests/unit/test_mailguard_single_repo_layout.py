"""The single-repository layout of the AgentMailGuard benchmark (task 7.24; R22.12, R24.5).

After the final merge (ADR-0012 decision 6) the guard lives inside this repository under
``agentmailguard/`` (a git subtree with full history). The same function names that check a
separate worktree then check a subdirectory: it is pinned iff its tree is the pinned commit's
tree and nothing tracked or untracked under it differs. No network, no docker, no mailguard
import: every repository here is a throw-away local one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from evaluation.mailguard_bench import guard_env
from evaluation.mailguard_bench.guard_env import (
    REPO_ROOT,
    V1_MAILGUARD_COMMIT,
    V1_MAILGUARD_TREE,
    V2_MAILGUARD_COMMIT,
    V2_MAILGUARD_TREE,
    GuardEnvError,
    guard_layout,
    require_pinned_worktree,
    worktree_info,
)

MAKEFILE = REPO_ROOT / "Makefile"
PREFIX = "agentmailguard"
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


def _guard_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A guard repository with two commits standing for the v1 and the v2 guard commit."""
    root = tmp_path / "guard-src"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("datasets/raw/*\n", encoding="utf-8")
    (root / "mailguard").mkdir()
    shas = []
    for n in (1, 2):
        (root / "mailguard" / "__init__.py").write_text(f"VERSION = {n}\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", f"guard c{n}")
        shas.append(_git(root, "rev-parse", "HEAD"))
    return root, shas


def _outer_repo(tmp_path: Path, guard: Path, sha: str, name: str = "outer") -> Path:
    """A repository holding the guard's tree at ``sha`` under ``agentmailguard/`` (what
    `git subtree add --prefix=agentmailguard` leaves: the tree at that prefix is the commit's)."""
    outer = tmp_path / name
    outer.mkdir()
    _git(outer, "init", "-q")
    (outer / "README.md").write_text("rag-email\n", encoding="utf-8")
    (outer / ".gitignore").write_text("agentmailguard/datasets/raw/*\n", encoding="utf-8")
    _git(outer, "add", ".")
    _git(outer, "commit", "-q", "-m", "rag-email")
    _git(outer, "fetch", "-q", str(guard), sha)
    _git(outer, "read-tree", f"--prefix={PREFIX}/", "-u", "FETCH_HEAD")
    _git(outer, "commit", "-q", "-m", "add the guard")
    return outer


@pytest.fixture
def subtree(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """(outer repo, its agentmailguard/ directory, [v1-like sha, v2-like sha])."""
    guard, shas = _guard_repo(tmp_path)
    outer = _outer_repo(tmp_path, guard, shas[1])
    return outer, outer / PREFIX, shas


# --- the pinned tree ids are recorded next to the pinned commits


def _commit_tree_anywhere(commit: str) -> str | None:
    candidates = [REPO_ROOT]
    for var in ("MAILGUARD_DIR",):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))
    candidates += [
        REPO_ROOT.parent / "AgentMailGuard-bench",
        REPO_ROOT.parent / "AgentMailGuard-dev",
    ]
    for repo in candidates:
        if not repo.is_dir():
            continue
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{commit}^{{tree}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode == 0:
            return done.stdout.strip()
    return None


@pytest.mark.parametrize(
    ("commit", "tree"),
    [(V1_MAILGUARD_COMMIT, V1_MAILGUARD_TREE), (V2_MAILGUARD_COMMIT, V2_MAILGUARD_TREE)],
)
def test_each_recorded_tree_is_the_tree_of_its_commit(commit: str, tree: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", tree)
    actual = _commit_tree_anywhere(commit)
    if actual is None:
        pytest.skip(f"commit {commit} is not in any local repository")
    assert tree == actual


def test_the_two_generations_have_different_trees() -> None:
    assert V1_MAILGUARD_TREE != V2_MAILGUARD_TREE


# --- layout detection


def test_a_separate_worktree_is_the_worktree_layout(tmp_path: Path) -> None:
    guard, shas = _guard_repo(tmp_path)
    assert guard_layout(guard) == "worktree"
    assert worktree_info(guard).layout == "worktree"


def test_a_subdirectory_of_another_repository_is_the_subtree_layout(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    outer, sub, _ = subtree
    assert guard_layout(sub) == "subtree"
    assert guard_layout(outer) == "worktree"


# --- pin verification in the subtree layout


def test_a_subtree_whose_tree_is_the_pinned_commits_tree_is_pinned(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    _, sub, (_, pinned) = subtree  # the outer repo does not contain commit `pinned` at all

    info = require_pinned_worktree(sub, pinned)

    assert info.layout == "subtree"
    assert info.commit == pinned
    assert info.clean
    assert info.path == sub.resolve()


def test_the_pinned_commit_is_reported_only_when_the_tree_matches(
    tmp_path: Path, subtree: tuple[Path, Path, list[str]]
) -> None:
    _, sub, (_, pinned) = subtree
    assert worktree_info(sub, pinned).commit == pinned
    # asked about a commit whose tree it does not hold, the subtree claims no commit
    other = "f" * 40
    assert worktree_info(sub, other).commit == ""


def test_a_subtree_of_the_other_commit_is_refused_naming_both(
    tmp_path: Path,
    subtree: tuple[Path, Path, list[str]],
) -> None:
    _, sub, (first, second) = subtree
    guard = tmp_path / "guard-src"
    older = _outer_repo(tmp_path, guard, first, name="outer-old")

    with pytest.raises(GuardEnvError) as excinfo:
        require_pinned_worktree(older / PREFIX, second)

    message = str(excinfo.value)
    assert str(older / PREFIX) in message
    assert second in message
    assert "pinned commit" in message


def test_the_refusal_says_which_guard_generation_the_subtree_holds(
    tmp_path: Path, subtree: tuple[Path, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, (first, second) = subtree
    guard = tmp_path / "guard-src"
    older = _outer_repo(tmp_path, guard, first, name="outer-old")
    tree_first = _git(guard, "rev-parse", f"{first}^{{tree}}")
    tree_second = _git(guard, "rev-parse", f"{second}^{{tree}}")
    monkeypatch.setattr(guard_env, "V1_MAILGUARD_COMMIT", first)
    monkeypatch.setattr(guard_env, "V1_MAILGUARD_TREE", tree_first)
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_COMMIT", second)
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_TREE", tree_second)

    assert worktree_info(older / PREFIX).commit == first
    with pytest.raises(GuardEnvError) as excinfo:
        require_pinned_worktree(older / PREFIX, second)

    message = str(excinfo.value)
    assert "v1 guard" in message and "MAILGUARD_DIR" in message and "MAILGUARD_COMMIT" in message


def test_a_modified_tracked_file_under_the_prefix_fails(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    _, sub, (_, pinned) = subtree
    (sub / "mailguard" / "__init__.py").write_text("VERSION = 99\n", encoding="utf-8")
    with pytest.raises(GuardEnvError, match="uncommitted"):
        require_pinned_worktree(sub, pinned)


def test_an_untracked_file_under_the_prefix_fails(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    _, sub, (_, pinned) = subtree
    (sub / "mailguard" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(GuardEnvError, match="uncommitted"):
        require_pinned_worktree(sub, pinned)


def test_ignored_downloads_under_the_prefix_keep_the_subtree_clean(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    _, sub, (_, pinned) = subtree
    raw = sub / "datasets" / "raw"
    raw.mkdir(parents=True)
    (raw / "MANIFEST.json").write_text("{}", encoding="utf-8")
    assert require_pinned_worktree(sub, pinned).clean


def test_changes_outside_the_prefix_do_not_make_the_subtree_dirty(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    outer, sub, (_, pinned) = subtree
    (outer / "README.md").write_text("edited rag-email\n", encoding="utf-8")
    (outer / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    assert require_pinned_worktree(sub, pinned).clean


def test_a_directory_that_is_not_committed_in_head_is_refused(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    (outer / PREFIX).mkdir(parents=True)
    _git(outer, "init", "-q")
    (outer / "README.md").write_text("x\n", encoding="utf-8")
    _git(outer, "add", ".")
    _git(outer, "commit", "-q", "-m", "x")
    (outer / PREFIX / "f.txt").write_text("untracked guard copy\n", encoding="utf-8")

    with pytest.raises(GuardEnvError, match=PREFIX):
        require_pinned_worktree(outer / PREFIX, "a" * 40)


def test_a_shallow_clone_without_the_pinned_commit_object_still_verifies(
    tmp_path: Path, subtree: tuple[Path, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    outer, _, (_, pinned) = subtree
    tree = _git(tmp_path / "guard-src", "rev-parse", f"{pinned}^{{tree}}")
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{outer}", str(clone)], check=True
    )
    absent = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", pinned], capture_output=True, check=False
    )
    assert absent.returncode != 0, "the clone unexpectedly has the pinned commit object"
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_COMMIT", pinned)
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_TREE", tree)

    info = require_pinned_worktree(clone / PREFIX, pinned)

    assert info.commit == pinned and info.layout == "subtree" and info.clean


def test_the_worktree_layout_keeps_reporting_its_head_commit(tmp_path: Path) -> None:
    guard, (first, second) = _guard_repo(tmp_path)
    info = require_pinned_worktree(guard, second)
    assert (info.layout, info.commit, info.clean) == ("worktree", second, True)


# --- what reaches run manifests and fingerprints names the guard's commit, not the outer HEAD


def test_the_run_fingerprint_names_the_pinned_commit_not_the_outer_head(
    subtree: tuple[Path, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluation.mailguard_bench.artifacts import git_head as manifest_git_head
    from evaluation.mailguard_bench.guard_env import checkout_commit

    outer, sub, (_, pinned) = subtree
    tree = _git(outer, "rev-parse", f"HEAD:{PREFIX}")
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_COMMIT", pinned)
    monkeypatch.setattr(guard_env, "V2_MAILGUARD_TREE", tree)
    outer_head = _git(outer, "rev-parse", "HEAD")

    assert checkout_commit(sub) == pinned
    assert manifest_git_head(sub) == {"sha": pinned, "dirty": False}
    assert manifest_git_head(outer)["sha"] == outer_head  # rag-email itself is unchanged

    (sub / "mailguard" / "__init__.py").write_text("VERSION = 5\n", encoding="utf-8")
    assert checkout_commit(sub) == pinned  # dirtiness is the doctor's job; the tree is what matters
    assert manifest_git_head(sub) == {"sha": pinned, "dirty": True}


def test_a_subtree_holding_an_unknown_tree_reports_no_commit(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    from evaluation.mailguard_bench.artifacts import git_head as manifest_git_head
    from evaluation.mailguard_bench.guard_env import checkout_commit

    _, sub, _ = subtree  # its tree is neither the real v1 nor the real v2 tree
    assert checkout_commit(sub) is None
    assert manifest_git_head(sub)["sha"] is None


# --- `python -m` from the rag-email root keeps rag-email's packages ahead of the subtree's


_PROBE = """
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("guard_env_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["guard_env_probe"] = mod
spec.loader.exec_module(mod)
repo = Path(sys.argv[2])
try:
    print(json.dumps({"ok": mod.require_module_origins(repo, repo / "agentmailguard")}))
except mod.GuardEnvError as exc:
    print(json.dumps({"error": str(exc)}))
"""


def _fake_repo_with_subtree(tmp_path: Path) -> Path:
    repo = tmp_path / "rag"
    for pkg in (
        "services",
        "evaluation",
        "agentmailguard/services",
        "agentmailguard/evaluation",
        "agentmailguard/mailguard",
    ):
        (repo / pkg).mkdir(parents=True)
        (repo / pkg / "__init__.py").write_text("", encoding="utf-8")
    return repo


def _probe(cwd: Path, repo: Path, pythonpath: list[Path]) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in pythonpath)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, str(Path(guard_env.__file__)), str(repo)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_the_editable_overlay_leaves_services_and_evaluation_to_rag_email(tmp_path: Path) -> None:
    repo = _fake_repo_with_subtree(tmp_path)
    # `python -m` from the root puts the cwd first; the editable install appends the guard root.
    out = _probe(repo, repo, [repo / "agentmailguard"])
    assert '"ok"' in out, out
    assert str(repo / "agentmailguard" / "mailguard") in out
    assert str(repo / "agentmailguard" / "services") not in out


def test_a_guard_package_that_shadows_rag_email_inside_the_repo_is_refused(
    tmp_path: Path,
) -> None:
    repo = _fake_repo_with_subtree(tmp_path)
    # run from elsewhere with the guard root first: `services` is the guard's copy, which lives
    # INSIDE the repository, so "is under the repo root" is not enough to accept an origin.
    out = _probe(tmp_path, repo, [repo / "agentmailguard", repo])
    assert '"error"' in out, out
    assert "python -m" in out


# --- the Make targets


def _make_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("MAILGUARD_")}


def _repo_copy(tmp_path: Path, name: str = "work") -> Path:
    work = tmp_path / name
    work.mkdir()
    shutil.copy(MAKEFILE, work / "Makefile")
    return work


def _make_n(work: Path, *args: str) -> str:
    return subprocess.run(
        ["make", "-n", "--no-print-directory", *args],
        cwd=work,
        env=_make_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@needs_git_and_make
def test_mailguard_dir_defaults_to_the_subtree_when_it_is_there(tmp_path: Path) -> None:
    work = _repo_copy(tmp_path)
    (work / "agentmailguard" / "mailguard").mkdir(parents=True)
    (work / "agentmailguard" / "mailguard" / "__init__.py").write_text("", encoding="utf-8")

    out = _make_n(work, "mailguard-smoke")

    assert f"MAILGUARD_DIR={work / 'agentmailguard'} " in out
    assert f"--with-editable {work / 'agentmailguard'} " in out


@needs_git_and_make
def test_mailguard_dir_defaults_to_the_sibling_worktree_without_the_subtree(
    tmp_path: Path,
) -> None:
    work = _repo_copy(tmp_path)
    out = _make_n(work, "mailguard-smoke")
    assert f"MAILGUARD_DIR={tmp_path / 'AgentMailGuard-bench'} " in out
    assert f"MAILGUARD_ARTIFACTS={tmp_path / 'AgentMailGuard-bench-artifacts'} " in out


@needs_git_and_make
def test_artifacts_default_to_the_pinned_copy_when_the_joblib_is_there(tmp_path: Path) -> None:
    work = _repo_copy(tmp_path)
    pinned = work / "evaluation" / "mailguard_bench" / "pinned"
    pinned.mkdir(parents=True)
    (pinned / "l1_injection_clf_v1.joblib").write_bytes(b"x")

    assert f"MAILGUARD_ARTIFACTS={pinned} " in _make_n(work, "mailguard-smoke")


@needs_git_and_make
def test_a_directory_named_pinned_without_the_joblib_is_not_the_artifacts_dir(
    tmp_path: Path,
) -> None:
    work = _repo_copy(tmp_path)
    (work / "evaluation" / "mailguard_bench" / "pinned").mkdir(parents=True)
    assert f"MAILGUARD_ARTIFACTS={tmp_path / 'AgentMailGuard-bench-artifacts'} " in _make_n(
        work, "mailguard-smoke"
    )


@needs_git_and_make
def test_the_environment_still_overrides_both_defaults(tmp_path: Path) -> None:
    work = _repo_copy(tmp_path)
    out = _make_n(work, "mailguard-smoke", "MAILGUARD_DIR=/elsewhere", "MAILGUARD_ARTIFACTS=/art")
    assert "MAILGUARD_DIR=/elsewhere " in out and "MAILGUARD_ARTIFACTS=/art " in out


def _makefile_constant(name: str) -> str:
    match = re.search(rf"^{name} \??= (\S+)$", MAKEFILE.read_text("utf-8"), re.M)
    assert match, f"the Makefile has no `{name} =` line"
    return match.group(1)


def test_the_makefile_knows_the_same_trees_as_guard_env() -> None:
    assert _makefile_constant("MAILGUARD_V1_COMMIT") == V1_MAILGUARD_COMMIT
    assert _makefile_constant("MAILGUARD_V1_TREE") == V1_MAILGUARD_TREE
    assert _makefile_constant("MAILGUARD_V2_TREE") == V2_MAILGUARD_TREE


def _subtree_work(tmp_path: Path, sha_index: int = 1) -> tuple[Path, list[str], Path]:
    guard, shas = _guard_repo(tmp_path)
    outer = _outer_repo(tmp_path, guard, shas[sha_index], name="work")
    shutil.copy(MAKEFILE, outer / "Makefile")
    _git(outer, "add", "Makefile")
    _git(outer, "commit", "-q", "-m", "makefile")
    return outer, shas, outer / PREFIX


def _make_worktree(work: Path, commit: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "make",
            "--no-print-directory",
            "mailguard-worktree",
            f"MAILGUARD_COMMIT={commit}",
            *extra,
        ],
        cwd=work,
        env=_make_env(),
        capture_output=True,
        text=True,
        check=False,
    )


@needs_git_and_make
def test_mailguard_worktree_in_the_subtree_layout_adds_no_worktree_and_verifies_the_pin(
    tmp_path: Path,
) -> None:
    work, (_, pinned), sub = _subtree_work(tmp_path)
    before = _git(work, "worktree", "list")

    done = _make_worktree(work, pinned)

    assert done.returncode == 0, done.stderr
    assert f"ok AgentMailGuard subtree {sub} @ {pinned}" in done.stdout
    assert _git(work, "worktree", "list") == before
    assert "not re-adding" not in done.stdout


@needs_git_and_make
def test_mailguard_worktree_in_the_subtree_layout_refuses_another_commit(tmp_path: Path) -> None:
    work, (first, second), sub = _subtree_work(tmp_path)

    wrong = _make_worktree(work, first)  # the subtree holds the tree of the second commit

    assert wrong.returncode != 0
    assert f"is not at MAILGUARD_COMMIT={first}" in wrong.stderr
    assert "MAILGUARD_DIR" in wrong.stderr


@needs_git_and_make
def test_mailguard_worktree_in_the_subtree_layout_refuses_uncommitted_changes(
    tmp_path: Path,
) -> None:
    work, (_, pinned), sub = _subtree_work(tmp_path)
    (sub / "mailguard" / "__init__.py").write_text("VERSION = 7\n", encoding="utf-8")

    dirty = _make_worktree(work, pinned)

    assert dirty.returncode != 0
    assert "uncommitted changes" in dirty.stderr


@needs_git_and_make
def test_mailguard_worktree_in_the_subtree_layout_verifies_a_shallow_clone_by_constant(
    tmp_path: Path,
) -> None:
    # A shallow clone has no commit object to ask for its tree: the Makefile's constants serve.
    work, (_, pinned), _ = _subtree_work(tmp_path)
    tree = _git(work, "rev-parse", f"HEAD:{PREFIX}")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{work}", str(shallow)], check=True
    )
    assert _make_worktree(shallow, pinned).returncode != 0  # unknown commit and no constant

    done = _make_worktree(shallow, V2_MAILGUARD_COMMIT, f"MAILGUARD_V2_TREE={tree}")

    assert done.returncode == 0, done.stderr
    assert f"@ {V2_MAILGUARD_COMMIT}" in done.stdout


@needs_git_and_make
def test_the_worktree_layout_recipe_is_unchanged_when_mailguard_dir_is_not_a_subdirectory(
    tmp_path: Path,
) -> None:
    work = _repo_copy(tmp_path, "rag-email")
    out = _make_n(work, "mailguard-worktree", f"MAILGUARD_DIR={tmp_path / 'guard-wt'}")
    assert "git fetch origin feature/mailguard-defense-stack" in out
    assert "git worktree add --detach" in out


# --- rag-email's own checks and images never see the subtree


def _pyproject() -> dict[str, object]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))


def test_ruff_skips_the_subtree_and_keeps_its_default_excludes() -> None:
    ruff = _pyproject()["tool"]["ruff"]  # type: ignore[index]
    assert "agentmailguard" in ruff["extend-exclude"]
    assert ".venv" in ruff["exclude"]  # extend-exclude adds; the existing list stays


def test_mypy_skips_the_subtree() -> None:
    mypy = _pyproject()["tool"]["mypy"]  # type: ignore[index]
    assert any(
        re.search(pattern, "agentmailguard/mailguard/__init__.py") for pattern in mypy["exclude"]
    )
    assert not any(re.search(pattern, "services/api/app.py") for pattern in mypy["exclude"])


def test_pytest_only_collects_tests_and_never_recurses_into_the_subtree() -> None:
    pytest_cfg = _pyproject()["tool"]["pytest"]["ini_options"]  # type: ignore[index]
    assert pytest_cfg["testpaths"] == ["tests"]
    assert "agentmailguard" in pytest_cfg["norecursedirs"]
    assert {".*", "build", "dist", "node_modules", "venv"} <= set(pytest_cfg["norecursedirs"])


def test_uv_does_not_treat_the_subtree_as_a_workspace_member() -> None:
    uv = _pyproject()["tool"]["uv"]  # type: ignore[index]
    assert "workspace" not in uv


def test_docker_context_leaves_out_the_subtree_and_the_pinned_joblib() -> None:
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "agentmailguard" in lines
    assert "evaluation" in lines or "evaluation/mailguard_bench/pinned" in lines


def test_the_runtime_image_copies_no_benchmark_or_guard_directory() -> None:
    copied = re.findall(
        r"^COPY\s+(?:--from=\S+\s+)?(\S+)", (REPO_ROOT / "Dockerfile").read_text("utf-8"), re.M
    )
    assert copied, "the Dockerfile has no COPY lines"
    for source in copied:
        assert not source.startswith(("agentmailguard", "evaluation")), source


def test_the_runbook_explains_both_layouts_and_the_tree_pin() -> None:
    text = (REPO_ROOT / "docs/demo-runbook.md").read_text("utf-8")
    section9 = text[text.index("## 9. AgentMailGuard benchmark") : text.index("## Appendix A")]
    assert "agentmailguard/" in section9 and "git rev-parse HEAD:agentmailguard" in section9
    assert "git status --porcelain -- agentmailguard" in section9
    assert "../AgentMailGuard-bench" in section9  # the worktree layout is still documented
    assert "evaluation/mailguard_bench/pinned" in section9


# --- review fixes (work package R7 review): prep never writes into the pinned directory


PINNED_REL = Path("evaluation") / "mailguard_bench" / "pinned"


def _work_with_pinned_joblib(tmp_path: Path) -> tuple[Path, Path]:
    work = _repo_copy(tmp_path, "rag-email")
    pinned = work / PINNED_REL
    pinned.mkdir(parents=True)
    (pinned / "l1_injection_clf_v1.joblib").write_bytes(b"pinned")
    return work, pinned


def _writes(make_n_output: str) -> list[str]:
    """The path arguments of every recipe line that creates or writes something."""
    found: list[str] = []
    for line in make_n_output.splitlines():
        found += re.findall(r"mkdir -p (\S+)", line)
        found += re.findall(r"--(?:out|out-dir)[ =](\S+)", line)
    return found


@needs_git_and_make
def test_prep_writes_beside_the_repository_not_into_the_pinned_directory(tmp_path: Path) -> None:
    work, pinned = _work_with_pinned_joblib(tmp_path)

    out = _make_n(work, "mailguard-prep")

    targets = _writes(out)
    assert targets, "mailguard-prep has no writing step the test recognises"
    assert all(not Path(t).is_relative_to(pinned) for t in targets), targets
    expected = tmp_path / "AgentMailGuard-bench-artifacts"
    assert any(Path(t).is_relative_to(expected) for t in targets), targets


@needs_git_and_make
def test_prep_keeps_writing_to_an_explicit_artifacts_directory(tmp_path: Path) -> None:
    work, _ = _work_with_pinned_joblib(tmp_path)

    out = _make_n(work, "mailguard-prep", f"MAILGUARD_ARTIFACTS={tmp_path / 'mine'}")

    assert all(Path(t).is_relative_to(tmp_path / "mine") for t in _writes(out))


@needs_git_and_make
def test_prep_refuses_an_output_directory_inside_the_pinned_one(tmp_path: Path) -> None:
    work, pinned = _work_with_pinned_joblib(tmp_path)
    before = (pinned / "l1_injection_clf_v1.joblib").read_bytes()

    done = subprocess.run(
        ["make", "--no-print-directory", "mailguard-prep", f"MAILGUARD_PREP_OUT={pinned}"],
        cwd=work,
        env=_make_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode != 0
    assert "pinned" in done.stderr and "MAILGUARD_PREP_OUT" in done.stderr
    assert (pinned / "l1_injection_clf_v1.joblib").read_bytes() == before
    assert sorted(p.name for p in pinned.iterdir()) == ["l1_injection_clf_v1.joblib"]


# --- review fixes: one rule for the default guard directory, shared with the Makefile


def test_default_guard_dir_is_the_subtree_when_it_is_there(tmp_path: Path) -> None:
    from evaluation.mailguard_bench.guard_env import default_guard_dir

    repo = tmp_path / "rag"
    (repo / PREFIX / "mailguard").mkdir(parents=True)
    (repo / PREFIX / "mailguard" / "__init__.py").write_text("", encoding="utf-8")
    assert default_guard_dir(repo) == repo / PREFIX


def test_default_guard_dir_is_the_sibling_worktree_otherwise(tmp_path: Path) -> None:
    from evaluation.mailguard_bench.guard_env import default_guard_dir

    repo = tmp_path / "rag"
    (repo / PREFIX).mkdir(parents=True)  # a directory without the package does not count
    assert default_guard_dir(repo) == tmp_path / "AgentMailGuard-bench"


def test_the_scorer_and_the_report_find_the_subtree_without_mailguard_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluation.mailguard_bench import amg

    repo = tmp_path / "rag"
    (repo / PREFIX / "mailguard").mkdir(parents=True)
    (repo / PREFIX / "mailguard" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(amg, "REPO_ROOT", repo)
    monkeypatch.delenv("MAILGUARD_DIR", raising=False)

    assert amg.resolve_mailguard_dir() == (repo / PREFIX).resolve()
    monkeypatch.setenv("MAILGUARD_DIR", str(tmp_path / "x"))
    assert amg.resolve_mailguard_dir() == (tmp_path / "x").resolve()


@needs_git_and_make
def test_the_makefile_and_the_python_default_name_the_same_directory(tmp_path: Path) -> None:
    from evaluation.mailguard_bench.guard_env import default_guard_dir

    for with_subtree in (False, True):
        work = _repo_copy(tmp_path, f"w{with_subtree}")
        if with_subtree:
            (work / PREFIX / "mailguard").mkdir(parents=True)
            (work / PREFIX / "mailguard" / "__init__.py").write_text("", encoding="utf-8")
        assert f"MAILGUARD_DIR={default_guard_dir(work)} " in _make_n(work, "mailguard-smoke")


# --- review fixes: any pinned commit in the subtree layout reaches manifests and fingerprints


def test_a_third_pinned_commit_reaches_the_manifest_and_the_fingerprint(
    subtree: tuple[Path, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluation.mailguard_bench import guard_build
    from evaluation.mailguard_bench.artifacts import git_head as manifest_git_head
    from evaluation.mailguard_bench.guard_env import checkout_commit

    _, sub, (first, pinned) = subtree  # neither is the v1 or the v2 pin of guard_env
    info = require_pinned_worktree(sub, pinned)  # the pin check passes for any commit git knows
    assert info.commit == pinned

    monkeypatch.setenv("MAILGUARD_COMMIT", pinned)  # what every `make mailguard-*` exports
    assert checkout_commit(sub) == pinned
    assert guard_build.git_head(sub) == pinned
    assert manifest_git_head(sub) == {"sha": pinned, "dirty": False}
    # the runner and the live run both name the commit require_pinned_worktree accepted
    assert guard_build.git_head(sub) == info.commit

    monkeypatch.setenv("MAILGUARD_COMMIT", first)  # the subtree does not hold that commit's tree
    assert checkout_commit(sub) is None
    assert manifest_git_head(sub)["sha"] is None


def test_an_explicit_expected_commit_beats_the_environment(
    subtree: tuple[Path, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluation.mailguard_bench.guard_env import checkout_commit

    _, sub, (first, pinned) = subtree
    monkeypatch.setenv("MAILGUARD_COMMIT", first)
    assert checkout_commit(sub, pinned) == pinned


# --- review fixes: the manifest's `dirty` means the same thing in both layouts (tracked changes;
# the pin check itself, worktree_info().clean, is stricter and also refuses untracked files)


def test_manifest_dirty_is_tracked_changes_only_in_both_layouts(
    subtree: tuple[Path, Path, list[str]], tmp_path: Path
) -> None:
    from evaluation.mailguard_bench.artifacts import git_head as manifest_git_head

    (tmp_path / "wt").mkdir()
    guard, _ = _guard_repo(tmp_path / "wt")
    _, sub, _ = subtree
    for checkout in (guard, sub):
        assert manifest_git_head(checkout)["dirty"] is False
        (checkout / "stray.txt").write_text("x", encoding="utf-8")
        assert manifest_git_head(checkout)["dirty"] is False, checkout
        (checkout / "mailguard" / "__init__.py").write_text("VERSION = 9\n", encoding="utf-8")
        assert manifest_git_head(checkout)["dirty"] is True, checkout


# --- review fixes: the report manifest names the layout, guard_smoke names the subtree


def test_guard_smoke_line_names_the_subtree_and_keeps_the_worktree_line(
    subtree: tuple[Path, Path, list[str]], tmp_path: Path
) -> None:
    from evaluation.mailguard_bench.guard_env import checkout_line

    (tmp_path / "wt").mkdir()
    guard, (_, second) = _guard_repo(tmp_path / "wt")
    assert checkout_line(require_pinned_worktree(guard, second)) == (
        f"ok worktree {guard.resolve()} @ {second} (clean)"
    )
    _, sub, (_, pinned) = subtree
    info = require_pinned_worktree(sub, pinned)
    assert (
        checkout_line(info) == f"ok subtree (tree {info.tree}) {sub.resolve()} @ {pinned} (clean)"
    )


# --- review fixes: a failing git in layout detection says why, and the WSL hint is shown


def test_a_refused_subtree_hints_at_the_windows_mount_causes(
    subtree: tuple[Path, Path, list[str]],
) -> None:
    _, sub, (_, pinned) = subtree
    (sub / "mailguard" / "__init__.py").write_text("VERSION = 9\n", encoding="utf-8")
    with pytest.raises(GuardEnvError) as err:
        require_pinned_worktree(sub, pinned)
    assert "uncommitted changes" in str(err.value)
    assert "/mnt/c" in str(err.value) and "core.filemode" in str(err.value)


@needs_git_and_make
def test_the_makefile_refusal_carries_the_same_hint(tmp_path: Path) -> None:
    work, (_, pinned), sub = _subtree_work(tmp_path)
    (sub / "mailguard" / "__init__.py").write_text("VERSION = 7\n", encoding="utf-8")
    done = _make_worktree(work, pinned)
    assert done.returncode != 0 and "/mnt/c" in done.stderr and "core.filemode" in done.stderr


def test_a_git_failure_reaches_the_user_with_gits_own_words(tmp_path: Path) -> None:
    # `detected dubious ownership` and any other reason git cannot answer: guard_layout falls
    # back to the worktree checks, whose GuardEnvError carries git's stderr.
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(GuardEnvError, match="not a git repository"):
        worktree_info(not_a_repo)


def test_no_git_binary_is_a_guard_env_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, _ = _guard_repo(tmp_path)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(GuardEnvError, match="git"):
        worktree_info(guard)

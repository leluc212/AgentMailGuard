"""AgentMailGuard environment checks for the C0/C3 benchmark.

Task 7.19; ADR-0010 item 2 (AgentMailGuard is a git worktree installed editable, pinned by
its commit); task 7.24 and ADR-0012 decision 6 (after the final merge the guard lives inside this
repository under ``agentmailguard/``, a git subtree, pinned by its tree); R22.12 (reproducible run
artifacts); R24.5 (no live credentials in CI).

This module never imports ``mailguard``. CI imports it, and CI does not install the guard.
Everything that needs the guard lives in ``guard_factory``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = Path(__file__).resolve().parent
GUARD_MODELS_YAML = BENCH_DIR / "guard_models.yaml"
DEFAULT_GUARD_MODEL = "gemma-4-26b-a4b-it"
GUARD_BRANCH = "feature/mailguard-defense-stack"
V1_MAILGUARD_COMMIT = "81df5d07b15b5bb3d1ecf3aae556df01e304cbe0"
"""The guard commit of the v1 benchmark (task 7.19). Its published results, and any v1 rerun,
are pinned to it: run with ``MAILGUARD_COMMIT`` set to it and ``MAILGUARD_DIR`` at a worktree of
it. A v1 result and a v2 result are never mixed."""
V1_MAILGUARD_TREE = "1a955c156a23cb6ad6dbde041c93080dfb69ceca"
"""``git rev-parse V1_MAILGUARD_COMMIT^{tree}``. In the single-repository layout the guard is a
subdirectory and the pin is this tree: a subtree is at a guard commit iff its tree is the
commit's tree. Recorded here so a shallow clone, which lacks the commit object, still verifies;
a test keeps it equal to git's answer wherever the commit is present."""
V2_MAILGUARD_COMMIT = "915cb1e2b86395e4389673deb9914308cc39627b"
"""The guard commit the v2 benchmark pins (ADR-0012 decision 3, moved by ADR-0014): v1's guard
plus the visible fallback of the AI stages and the strictest-rule-wins fix in L5 (``1a3ef62``),
plus OpenRouter provider routing and per-call provenance in its OpenAI provider (``6c7b9a9``,
``915cb1e``), transport only: no layer, prompt or rule changed."""
V2_MAILGUARD_TREE = "f659748a611340e093d41189dc1f445a2f842134"
"""``git rev-parse V2_MAILGUARD_COMMIT^{tree}``; see ``V1_MAILGUARD_TREE``."""
DEFAULT_MAILGUARD_COMMIT = V2_MAILGUARD_COMMIT
"""What the Makefile's ``MAILGUARD_COMMIT ?=`` is (a test keeps the two equal)."""
L1_MODEL_NAME = "l1_injection_clf_v1.joblib"
RAW_MANIFEST = Path("datasets/raw/MANIFEST.json")
REQUIRED_RAW_FILES: tuple[Path, ...] = (
    Path("datasets/raw/llmail_inject/data/raw_submissions_phase2.jsonl"),
    Path("datasets/raw/llmail_inject/data/labelled_unique_submissions_phase2.json"),
    Path("datasets/raw/llmail_inject/data/emails_for_fp_tests.json"),
    Path("datasets/raw/poisonedrag/results/adv_targeted_results/nq.json"),
    Path("datasets/raw/poisonedrag/results/adv_targeted_results/hotpotqa.json"),
    Path("datasets/raw/poisonedrag/results/adv_targeted_results/msmarco.json"),
)
PREP_HINT = "run `make mailguard-prep` first"
SUBTREE_DIRTY_HINT = (
    " (on Windows, clone inside the Linux file system, not under /mnt/c, and check "
    "`git config core.filemode` and `core.autocrlf`: file-mode and line-ending noise also "
    "shows as changes)"
)
"""Why a subtree that nobody edited can still be dirty; the Makefile's refusal says the same."""

ModuleResolver = Callable[[str], Path | None]


Layout = Literal["worktree", "subtree"]


class GuardEnvError(RuntimeError):
    """The AgentMailGuard worktree or environment is not the pinned, complete one."""


@dataclass(frozen=True)
class WorktreeInfo:
    """Where the AgentMailGuard checkout is, which commit it is at, and whether it is clean.

    ``layout`` is ``"worktree"`` (its own git worktree or clone: ``commit`` is its HEAD) or
    ``"subtree"`` (a subdirectory of the repository it sits in: ``commit`` is the pinned commit
    whose tree the subdirectory holds, or ``""`` when it holds the tree of none). ``tree`` is the
    subdirectory's tree id in HEAD, for a subtree.
    """

    path: Path
    commit: str
    clean: bool
    layout: Layout = "worktree"
    tree: str | None = None


@dataclass(frozen=True)
class GuardPaths:
    """The three locations the Make targets pass in through the environment."""

    root: Path
    commit: str
    artifacts: Path

    @property
    def l1_model(self) -> Path:
        """The L1 classifier trained by `make mailguard-prep` (outside the worktree)."""
        return self.artifacts / L1_MODEL_NAME


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def guard_paths_from_env(environ: Mapping[str, str]) -> GuardPaths:
    """Read MAILGUARD_DIR, MAILGUARD_COMMIT and MAILGUARD_ARTIFACTS (set by the Make targets)."""
    names = ("MAILGUARD_DIR", "MAILGUARD_COMMIT", "MAILGUARD_ARTIFACTS")
    missing = [name for name in names if not environ.get(name)]
    if missing:
        raise GuardEnvError(
            f"{', '.join(missing)} not set; run through the `make mailguard-*` targets"
        )
    return GuardPaths(
        root=Path(environ["MAILGUARD_DIR"]).resolve(),
        commit=environ["MAILGUARD_COMMIT"],
        artifacts=Path(environ["MAILGUARD_ARTIFACTS"]).resolve(),
    )


def default_guard_dir(repo_root: Path = REPO_ROOT) -> Path:
    """Where the guard is when ``MAILGUARD_DIR`` is not set: the one rule the Makefile also has.

    ``<repo_root>/agentmailguard`` (the single-repository layout) when it holds the package,
    else the sibling worktree ``<repo_root>/../AgentMailGuard-bench``.
    """
    subtree = repo_root / "agentmailguard"
    if (subtree / "mailguard" / "__init__.py").is_file():
        return subtree
    return repo_root.parent / "AgentMailGuard-bench"


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise GuardEnvError(
            f"git {' '.join(args)} failed in {path}: cannot run git ({exc})"
        ) from exc
    if result.returncode != 0:
        raise GuardEnvError(f"git {' '.join(args)} failed in {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_ok(path: Path, *args: str) -> str | None:
    """Stdout of a git command in ``path``, or None when git fails (or is absent)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def guard_layout(path: Path) -> Layout:
    """``"subtree"`` when ``path`` is a subdirectory of the git repository it sits in.

    A path that is its own repository root (a worktree, a clone), one that does not exist and
    one outside git are ``"worktree"``: the worktree checks then report what is wrong.
    """
    top = _git_ok(path, "rev-parse", "--show-toplevel")
    if top is None:
        return "worktree"
    return "worktree" if Path(top).resolve() == path.resolve() else "subtree"


def _commit_tree(path: Path, commit: str) -> str | None:
    """Tree id of a pinned commit: the recorded constant, else git's answer, else None."""
    recorded = {
        V1_MAILGUARD_COMMIT: V1_MAILGUARD_TREE,
        V2_MAILGUARD_COMMIT: V2_MAILGUARD_TREE,
    }
    if commit in recorded:
        return recorded[commit]
    return _git_ok(path, "rev-parse", "--verify", "-q", f"{commit}^{{tree}}")


def _subtree_info(path: Path, expected_commit: str | None) -> WorktreeInfo:
    prefix = _git(path, "rev-parse", "--show-prefix").rstrip("/")
    tree = _git_ok(path, "rev-parse", f"HEAD:{prefix}")
    if tree is None:
        raise GuardEnvError(
            f"{path} is inside a git repository but `{prefix}` is not committed in its HEAD; "
            "the subtree layout needs the guard committed under that directory "
            "(`git subtree add`)"
        )
    status = _git(path, "status", "--porcelain", "--untracked-files=normal", "--", ".")
    candidates = [c for c in (expected_commit, V2_MAILGUARD_COMMIT, V1_MAILGUARD_COMMIT) if c]
    commit = next((c for c in candidates if _commit_tree(path, c) == tree), "")
    return WorktreeInfo(
        path=path.resolve(), commit=commit, clean=status == "", layout="subtree", tree=tree
    )


def worktree_info(path: Path, expected_commit: str | None = None) -> WorktreeInfo:
    """Read where the guard is, its commit and its cleanliness. Git-ignored files do not count.

    Worktree layout: HEAD of the worktree. Subtree layout: the pinned commit (``expected_commit``,
    else the v2 or v1 pin) whose tree the subdirectory holds, ``""`` if none. Only tracked and
    untracked files under the subdirectory make a subtree dirty.
    """
    if not path.is_dir():
        raise GuardEnvError(
            f"AgentMailGuard worktree not found at {path}; run `make mailguard-worktree`"
        )
    if guard_layout(path) == "subtree":
        return _subtree_info(path, expected_commit)
    commit = _git(path, "rev-parse", "HEAD")
    status = _git(path, "status", "--porcelain", "--untracked-files=normal")
    return WorktreeInfo(path=path.resolve(), commit=commit, clean=status == "")


def checkout_line(info: WorktreeInfo) -> str:
    """The first line of a passing check: ``ok worktree ...`` or ``ok subtree (tree ...) ...``."""
    where = "worktree" if info.layout == "worktree" else f"{info.layout} (tree {info.tree})"
    return f"ok {where} {info.path} @ {info.commit} (clean)"


def checkout_commit(path: Path, expected_commit: str | None = None) -> str | None:
    """The guard commit a checkout is at, for run manifests and fingerprints; None if unknown.

    Worktree layout: HEAD. Subtree layout: the pinned commit whose tree the subdirectory holds
    (never the enclosing repository's HEAD), None when it holds the tree of none. None outside git.
    The commit asked about is ``expected_commit``, else ``MAILGUARD_COMMIT`` (every
    ``make mailguard-*`` target exports it, so a run's metas and the pin check that admitted the
    run name the same commit whichever commit that is), else the v2 and the v1 pin.
    """
    if guard_layout(path) == "subtree":
        try:
            expected = expected_commit or os.environ.get("MAILGUARD_COMMIT") or None
            return _subtree_info(path, expected).commit or None
        except GuardEnvError:
            return None
    return _git_ok(path, "rev-parse", "HEAD") or None


def _generation_hint(found: str, expected: str) -> str:
    """When a worktree and the pin are the v1 and the v2 guard, say so and how to fix it."""
    names = {V1_MAILGUARD_COMMIT: "v1", V2_MAILGUARD_COMMIT: "v2"}
    if found not in names or expected not in names or found == expected:
        return ""
    return (
        f" (the worktree is the {names[found]} guard, the run is pinned to the {names[expected]} "
        f"guard: point MAILGUARD_DIR at a worktree of {expected}, or set MAILGUARD_COMMIT to "
        f"{found} for a {names[found]} run; v1 and v2 results are never mixed)"
    )


def require_pinned_worktree(path: Path, expected_commit: str) -> WorktreeInfo:
    """Fail unless the guard is clean and exactly at the pinned commit (its tree, for a subtree)."""
    info = worktree_info(path, expected_commit)
    if info.commit != expected_commit:
        if info.layout == "subtree" and not info.commit:
            raise GuardEnvError(
                f"subtree {path} holds tree {info.tree}, which is the tree of no known guard "
                f"commit; the pinned commit is {expected_commit} "
                f"(tree {_commit_tree(path, expected_commit) or 'unknown'})"
            )
        raise GuardEnvError(
            f"{info.layout} {path} is at {info.commit}, the pinned commit is {expected_commit}"
            f"{_generation_hint(info.commit, expected_commit)}"
        )
    if not info.clean:
        raise GuardEnvError(
            f"{info.layout} {path} has uncommitted changes; the benchmark pins a clean commit"
            f"{SUBTREE_DIRTY_HINT if info.layout == 'subtree' else ''}"
        )
    return info


def module_origin(name: str) -> Path | None:
    """Return the file a top-level import of ``name`` would load, or None if absent."""
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).resolve()


def require_module_origins(
    repo_root: Path, guard_root: Path, resolve: ModuleResolver = module_origin
) -> dict[str, str]:
    """Fail unless `services`/`evaluation` are rag-email's and `mailguard` is the worktree's.

    AgentMailGuard also ships top-level `services` and `evaluation` packages, and its editable
    install puts its root on sys.path ahead of rag-email's. Only `python -m` from the rag-email
    root (cwd first on sys.path) keeps rag-email's packages in front.
    """
    origins: dict[str, str] = {}
    for name in ("services", "evaluation"):
        origin = resolve(name)
        # Under rag-email's own `<name>/`, not merely under its root: in the single-repository
        # layout the guard's copy of `<name>` lives inside the repository too.
        if origin is None or not origin.is_relative_to(repo_root.resolve() / name):
            raise GuardEnvError(
                f"`import {name}` resolves to {origin}, not rag-email's {repo_root / name}; "
                "run with `python -m` from the rag-email root (AgentMailGuard ships a "
                f"top-level `{name}` too)"
            )
        origins[name] = str(origin)
    origin = resolve("mailguard")
    if origin is None:
        raise GuardEnvError(
            "mailguard is not importable; run through `make mailguard-*` "
            "(uv run --with-editable <worktree>)"
        )
    if not origin.is_relative_to(guard_root.resolve()):
        raise GuardEnvError(f"mailguard resolves to {origin}, not the pinned worktree {guard_root}")
    origins["mailguard"] = str(origin)
    return origins


def guard_provider_env(base_url: str, api_key: str | None) -> dict[str, str]:
    """Map rag-email's LLM__OPENAI_* settings onto the env vars the guard's provider reads.

    mailguard.llm.openai_provider.OpenAIProvider falls back to OPENAI_BASE_URL and
    OPENAI_API_KEY via os.getenv when the model spec has no base_url/api_key, and
    guard_models.yaml deliberately has neither, so the key never lands in a file.
    """
    if not api_key:
        raise GuardEnvError(
            "LLM__OPENAI_API_KEY is empty; the guard's LLM stages need the Gemini key"
        )
    return {"OPENAI_BASE_URL": base_url.rstrip("/"), "OPENAI_API_KEY": api_key}


def l1_artifact(paths: GuardPaths) -> tuple[Path, str]:
    """Return the trained L1 classifier path and its sha256, or fail with the prep hint."""
    path = paths.l1_model
    if not path.is_file():
        raise GuardEnvError(f"L1 classifier artifact missing at {path}; {PREP_HINT}")
    return path, sha256_file(path)


def missing_raw_files(guard_root: Path) -> list[Path]:
    """Raw dataset files the case builder needs that are not in the worktree yet."""
    return [guard_root / rel for rel in REQUIRED_RAW_FILES if not (guard_root / rel).is_file()]

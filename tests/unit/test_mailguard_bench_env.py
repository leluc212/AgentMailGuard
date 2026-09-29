"""Unit tests for the AgentMailGuard benchmark environment checks (task 7.19; R22.12, R24.5).

No mailguard import and no network: these run in CI.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from evaluation.mailguard_bench.guard_env import (
    DEFAULT_GUARD_MODEL,
    GUARD_MODELS_YAML,
    REPO_ROOT,
    REQUIRED_RAW_FILES,
    GuardEnvError,
    GuardPaths,
    guard_paths_from_env,
    guard_provider_env,
    l1_artifact,
    missing_raw_files,
    module_origin,
    require_module_origins,
    require_pinned_worktree,
    sha256_file,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def guard_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "AgentMailGuard-bench"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("datasets/raw/*\n", encoding="utf-8")
    (root / "README.md").write_text("guard\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD")


def test_pinned_clean_worktree_passes(guard_repo: tuple[Path, str]) -> None:
    root, commit = guard_repo
    info = require_pinned_worktree(root, commit)
    assert info.commit == commit
    assert info.clean


def test_git_ignored_downloads_keep_the_worktree_clean(guard_repo: tuple[Path, str]) -> None:
    root, commit = guard_repo
    raw = root / "datasets" / "raw"
    raw.mkdir(parents=True)
    (raw / "MANIFEST.json").write_text("{}", encoding="utf-8")
    assert require_pinned_worktree(root, commit).clean


def test_wrong_commit_fails(guard_repo: tuple[Path, str]) -> None:
    root, _ = guard_repo
    with pytest.raises(GuardEnvError, match="pinned commit"):
        require_pinned_worktree(root, "0" * 40)


def test_modified_tracked_file_fails(guard_repo: tuple[Path, str]) -> None:
    root, commit = guard_repo
    (root / "README.md").write_text("edited\n", encoding="utf-8")
    with pytest.raises(GuardEnvError, match="uncommitted"):
        require_pinned_worktree(root, commit)


def test_missing_worktree_names_the_make_target(tmp_path: Path) -> None:
    with pytest.raises(GuardEnvError, match="make mailguard-worktree"):
        require_pinned_worktree(tmp_path / "absent", "0" * 40)


def test_rag_email_packages_resolve_to_this_repo() -> None:
    for name in ("services", "evaluation"):
        origin = module_origin(name)
        assert origin is not None
        assert origin.is_relative_to(REPO_ROOT)


def _resolver(mapping: dict[str, Path | None]) -> Callable[[str], Path | None]:
    return lambda name: mapping.get(name)


def test_module_origins_accept_the_expected_layout(tmp_path: Path) -> None:
    guard = tmp_path / "guard"
    origins = require_module_origins(
        REPO_ROOT,
        guard,
        _resolver(
            {
                "services": REPO_ROOT / "services" / "__init__.py",
                "evaluation": REPO_ROOT / "evaluation" / "__init__.py",
                "mailguard": guard / "mailguard" / "__init__.py",
            }
        ),
    )
    assert set(origins) == {"services", "evaluation", "mailguard"}


def test_guard_services_shadowing_rag_email_fails(tmp_path: Path) -> None:
    guard = tmp_path / "guard"
    resolve = _resolver(
        {
            "services": guard / "services" / "__init__.py",
            "evaluation": REPO_ROOT / "evaluation" / "__init__.py",
            "mailguard": guard / "mailguard" / "__init__.py",
        }
    )
    with pytest.raises(GuardEnvError, match="python -m"):
        require_module_origins(REPO_ROOT, guard, resolve)


def test_missing_mailguard_fails(tmp_path: Path) -> None:
    resolve = _resolver(
        {
            "services": REPO_ROOT / "services" / "__init__.py",
            "evaluation": REPO_ROOT / "evaluation" / "__init__.py",
        }
    )
    with pytest.raises(GuardEnvError, match="mailguard is not importable"):
        require_module_origins(REPO_ROOT, tmp_path / "guard", resolve)


def test_mailguard_from_another_checkout_fails(tmp_path: Path) -> None:
    resolve = _resolver(
        {
            "services": REPO_ROOT / "services" / "__init__.py",
            "evaluation": REPO_ROOT / "evaluation" / "__init__.py",
            "mailguard": tmp_path / "other" / "mailguard" / "__init__.py",
        }
    )
    with pytest.raises(GuardEnvError, match="pinned worktree"):
        require_module_origins(REPO_ROOT, tmp_path / "guard", resolve)


def test_guard_provider_env_maps_rag_email_llm_settings() -> None:
    env = guard_provider_env("https://generativelanguage.googleapis.com/v1beta/openai/", "k-123")
    assert env == {
        "OPENAI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
        "OPENAI_API_KEY": "k-123",
    }


@pytest.mark.parametrize("key", [None, ""])
def test_guard_provider_env_requires_a_key(key: str | None) -> None:
    with pytest.raises(GuardEnvError, match="LLM__OPENAI_API_KEY"):
        guard_provider_env("https://example.invalid/v1", key)


def test_guard_paths_from_env_names_missing_vars() -> None:
    with pytest.raises(GuardEnvError, match="MAILGUARD_COMMIT, MAILGUARD_ARTIFACTS"):
        guard_paths_from_env({"MAILGUARD_DIR": "/x"})


def test_guard_paths_from_env_reads_all_three(tmp_path: Path) -> None:
    paths = guard_paths_from_env(
        {
            "MAILGUARD_DIR": str(tmp_path / "wt"),
            "MAILGUARD_COMMIT": "a" * 40,
            "MAILGUARD_ARTIFACTS": str(tmp_path / "art"),
        }
    )
    assert paths.l1_model == (tmp_path / "art" / "l1_injection_clf_v1.joblib").resolve()


def test_l1_artifact_missing_points_at_prep(tmp_path: Path) -> None:
    paths = GuardPaths(root=tmp_path, commit="a" * 40, artifacts=tmp_path / "art")
    with pytest.raises(GuardEnvError, match="make mailguard-prep"):
        l1_artifact(paths)


def test_l1_artifact_present_returns_its_sha256(tmp_path: Path) -> None:
    paths = GuardPaths(root=tmp_path, commit="a" * 40, artifacts=tmp_path)
    paths.l1_model.write_bytes(b"model-bytes")
    path, digest = l1_artifact(paths)
    assert path == paths.l1_model
    assert digest == hashlib.sha256(b"model-bytes").hexdigest() == sha256_file(path)


def test_missing_raw_files_lists_only_absent_ones(tmp_path: Path) -> None:
    for rel in REQUIRED_RAW_FILES[:2]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}", encoding="utf-8")
    assert missing_raw_files(tmp_path) == [tmp_path / rel for rel in REQUIRED_RAW_FILES[2:]]


def test_guard_models_yaml_registers_gemma_without_secrets() -> None:
    loaded = yaml.safe_load(GUARD_MODELS_YAML.read_text(encoding="utf-8"))
    spec = loaded["models"][DEFAULT_GUARD_MODEL]
    assert spec == {
        "backend": "openai",
        "model": DEFAULT_GUARD_MODEL,
        "json_mode": "json_object",
        "timeout_s": 60,
    }

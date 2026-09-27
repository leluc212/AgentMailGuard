"""Runtime image contract (RA.11, R20.1, R24.1).

The Docker image must ship every file production code loads at runtime, install from
uv.lock without dev dependencies, and never bake the host .env into /app. CI has no
docker build step, so this test parses the Dockerfile and .dockerignore instead.
"""

from __future__ import annotations

import fnmatch
import shlex
from pathlib import Path

import pytest
import yaml

from packages.core.settings import AgentProfileSettings, CategoryRoutingSettings, TriageSettings
from packages.db.migrator import DEFAULT_MIGRATIONS_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
FILE_BODY_SUFFIXES = (".txt", ".j2", ".md")


def _copied_sources() -> set[str]:
    sources: set[str] = set()
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = shlex.split(line)[1:]
        if any(p.startswith("--from") for p in parts):
            continue
        args = [p for p in parts if not p.startswith("--")]
        sources.update(src.rstrip("/").removeprefix("./") for src in args[:-1])
    return sources


def _ignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [ln.strip().strip("/") for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _is_dockerignored(rel_path: str) -> bool:
    parts = rel_path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in _ignore_patterns():
        bare = pattern.removeprefix("**/")
        for prefix in prefixes:
            if fnmatch.fnmatch(prefix, pattern) or fnmatch.fnmatch(prefix.split("/")[-1], bare):
                return True
    return False


def _runtime_assets() -> list[str]:
    triage, routing, profiles = TriageSettings(), CategoryRoutingSettings(), AgentProfileSettings()
    assets = [
        triage.rules_path,
        triage.ml_model_path,
        triage.templates_path,
        routing.categories_config_path,
        profiles.config_path,
        profiles.prompts_dir,
        profiles.schemas_dir,
        DEFAULT_MIGRATIONS_DIR.relative_to(REPO_ROOT).as_posix(),
    ]
    profile_cfg = yaml.safe_load((REPO_ROOT / profiles.config_path).read_text(encoding="utf-8"))
    for profile in profile_cfg["profiles"].values():
        assets += [profile["prompt_template"], profile["output_schema"]]
    tmpl_cfg = yaml.safe_load((REPO_ROOT / triage.templates_path).read_text(encoding="utf-8"))
    assets += [
        t["body"]
        for t in tmpl_cfg["templates"]
        if str(t.get("body", "")).endswith(FILE_BODY_SUFFIXES)
    ]
    return sorted(set(assets))


@pytest.mark.parametrize("asset", _runtime_assets())
def test_runtime_asset_exists_in_repo(asset: str) -> None:
    assert (REPO_ROOT / asset).exists(), f"{asset} is loaded at runtime but missing from the repo"


@pytest.mark.parametrize("asset", _runtime_assets())
def test_runtime_asset_is_copied_into_image(asset: str) -> None:
    sources = _copied_sources()
    assert any(asset == s or asset.startswith(f"{s}/") for s in sources), (
        f"{asset} is loaded at runtime but no Dockerfile COPY covers it; sources: {sorted(sources)}"
    )


@pytest.mark.parametrize("asset", _runtime_assets())
def test_runtime_asset_is_not_dockerignored(asset: str) -> None:
    assert not _is_dockerignored(asset), f"{asset} is excluded from the build context"


def test_env_file_is_never_sent_to_the_image() -> None:
    assert _is_dockerignored(".env")
    assert "." not in _copied_sources(), "COPY . would bake the host .env into /app"


def test_image_installs_locked_runtime_dependencies_only() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv sync --locked --no-dev" in text
    assert "uv pip install" not in text, "must install from uv.lock, not re-resolve"
    assert "--no-editable" not in text, "migrator resolves migrations/ via __file__ under /app"
    assert "astral-sh/uv:latest" not in text, "pin the uv image tag"
    assert "--mount=" not in text, "compose uses the classic builder on this host (no buildx)"
    assert 'PATH="/app/.venv/bin:$PATH"' in text


def test_pyyaml_is_a_declared_runtime_dependency() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert '"pyyaml' in text, "production imports yaml directly; declare it, not rely on uvicorn"

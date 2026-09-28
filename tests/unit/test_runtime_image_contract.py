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
        "services/frontend/templates",
        "services/frontend/static",
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


def test_image_bakes_the_bpe_encoding() -> None:
    """No container may download the tokenizer at start (4.13b live gate: ~56 s startup).

    tiktoken fetches its encoding over the network on first use, with no timeout; baking it
    into the image keeps readiness fast and makes offline deployments work.
    """
    from packages.knowledge.token_counter import DEFAULT_ENCODING

    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "TIKTOKEN_CACHE_DIR=" in text
    fetch = f"tiktoken.get_encoding('{DEFAULT_ENCODING}')"
    assert fetch in text
    assert text.index(fetch) > text.index("RUN uv sync --locked --no-dev\n")


@pytest.mark.parametrize(
    "variable",
    [
        "ROUTER_FORCE_SINGLE_TIER",
        "ROUTER_CONFIDENCE_THRESHOLD",
        "LLM__OPENAI_API_KEY",
        "LLM__ANTHROPIC_API_KEY",
        "LLM__OPENAI_BASE_URL",
        "LLM__FAST_MODEL",
        "LLM__STRONG_MODEL",
        "LLM__FALLBACK_MODEL",
        "LLM__PRICE_TABLE",
        "EMBEDDING__MODEL_NAME",
        "EMBEDDING__BASE_URL",
        "EMBEDDING__API_KEY",
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
        "BUSINESS_DATA__TIMEOUT_MS",
        "BUSINESS_DATA__SNAPSHOT_ORDERS",
        "BUSINESS_DATA__SNAPSHOT_TICKETS",
    ],
)
def test_compose_forwards_the_switch_into_app_containers(variable: str) -> None:
    """R15.6: the single-tier switch (and the real-provider keys) must reach the workers."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    ai_worker_env = compose["services"]["ai-worker"]["environment"]
    assert variable in ai_worker_env
    assert str(ai_worker_env[variable]).startswith("${" + variable), "must follow the host .env"


@pytest.mark.parametrize(
    "variable",
    [
        "LLM__OPENAI_BASE_URL",
        "LLM__FAST_MODEL",
        "LLM__STRONG_MODEL",
        "LLM__FALLBACK_MODEL",
        "LLM__PRICE_TABLE",
    ],
)
def test_compose_llm_endpoint_defaults_defer_to_settings(variable: str) -> None:
    """R20.6: an unset host value reaches the container blank, and settings restores its default.

    A non-blank compose default would duplicate settings.py (and drift); the settings-side
    blank-means-default validator is covered in tests/unit/test_settings.py.
    """
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for service in ("api", "triage-worker", "ai-worker"):
        env = compose["services"][service]["environment"]
        assert env[variable] == "${" + variable + ":-}", service


def test_dispatch_worker_runs_its_entrypoint_with_readiness() -> None:
    """6.5: the dispatch-worker is a real service, not the Phase-0 stub."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["dispatch-worker"]
    assert service["command"] == ["python", "-m", "services.dispatch_worker.main"]
    assert "http://localhost:8006/readyz" in service["healthcheck"]["test"]
    assert service["environment"]["SERVICE_NAME"] == "dispatch_worker"
    assert service["environment"]["DATABASE__HOST"] == "postgres"  # merged *app-env
    assert service["environment"]["GMAIL_ACCESS_TOKEN"] == "${GMAIL_ACCESS_TOKEN:-}"


def test_review_ui_and_api_publish_on_loopback_only() -> None:
    """ADR-0009: the review UI has no login, so its port and the API's bind to 127.0.0.1."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["frontend"]["ports"] == ["127.0.0.1:3001:3001"]
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:8000:8000"]


def test_frontend_runs_the_review_ui_with_its_settings() -> None:
    """R23.6 / 6.8: the frontend container runs services.frontend with FRONTEND__* set."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    frontend = compose["services"]["frontend"]
    assert frontend["command"] == [
        "uvicorn",
        "services.frontend.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "3001",
    ]
    env = frontend["environment"]
    assert env["FRONTEND__API_BASE_URL"] == "http://api:8000"
    assert str(env["FRONTEND__ORGANIZATION_ID"]).startswith("${FRONTEND__ORGANIZATION_ID")
    assert frontend["healthcheck"]["test"][-1] == "http://localhost:3001/readyz"


def test_compose_forwards_the_gmail_token_only_to_the_services_that_call_gmail() -> None:
    """6.10: mail-connector syncs and dispatch-worker drafts/sends; nothing else gets the token."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    holders = sorted(
        name
        for name, service in compose["services"].items()
        if "GMAIL_ACCESS_TOKEN" in (service.get("environment") or {})
    )
    assert holders == ["dispatch-worker", "mail-connector"]
    for name in holders:
        env = compose["services"][name]["environment"]
        assert env["GMAIL_ACCESS_TOKEN"] == "${GMAIL_ACCESS_TOKEN:-}", name

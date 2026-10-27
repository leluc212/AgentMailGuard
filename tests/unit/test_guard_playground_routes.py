"""Guard Playground skeleton routes (task 1.1).

Requirements: 9.8 (guard unavailable -> /guard says so, /healthz keeps serving), 10.1 (shared
review.css and htmx.min.js), 11.1 (in-process ASGI transport), 11.2 (no network, no keys).
"""

from __future__ import annotations

import hashlib
import importlib.abc
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.guard_playground.settings import DEFAULT_REPO_ROOT, PlaygroundSettings
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    CREDENTIAL_ENV_VARS,
    OutboundNetworkBlockedError,
    asgi_client,
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
    fixture_playground_app_factory,
)

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")

# Same pinned htmx build as the Review UI (tests/unit/test_frontend_review_ui.py).
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"

AppFactory = Callable[..., Any]


class _BlockMailguard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname.partition(".")[0] == "mailguard":
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def _force_guard_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``mailguard`` unimportable even where the overlay put it on PYTHONPATH."""
    for name in [n for n in sys.modules if n.partition(".")[0] == "mailguard"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockMailguard(), *sys.meta_path])


def test_settings_derive_paths_from_repo_root(tmp_path: Path) -> None:
    s = PlaygroundSettings(repo_root=tmp_path)
    assert s.guard_root == tmp_path / "agentmailguard"
    assert s.cases_path == tmp_path / "evaluation/datasets/mailguard/cases.jsonl"
    assert s.fixtures_path == tmp_path / "agentmailguard/tests/fixtures/emails.json"
    assert s.results_root == tmp_path / "evaluation/results/mailguard_bench"
    assert s.figures_dir == tmp_path / "paper/figures/final/3models"
    assert s.data_dir == tmp_path / "services/guard_playground/data"
    assert (s.host, s.port, s.run_timeout_s, s.batch_concurrency) == ("127.0.0.1", 3010, 120.0, 2)
    assert (s.max_upload_bytes, s.max_batch_emails) == (5 * 1024 * 1024, 50)


def test_settings_default_repo_root_and_env_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (DEFAULT_REPO_ROOT / "services" / "guard_playground" / "settings.py").is_file()
    monkeypatch.setenv("PLAYGROUND__REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("PLAYGROUND__L1_MODEL_PATH", str(tmp_path / "clf.joblib"))
    monkeypatch.setenv("PLAYGROUND__LIVE_ENABLED", "false")
    s = PlaygroundSettings(_env_file=None)
    assert s.repo_root == tmp_path
    assert s.guard_root == tmp_path / "agentmailguard"
    assert s.l1_model_path == tmp_path / "clf.joblib"
    assert s.live_enabled is False


async def test_healthz_is_ok(playground_app_factory: AppFactory) -> None:
    async with asgi_client(playground_app_factory()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readyz_reports_guard_cases_and_replay(playground_app_factory: AppFactory) -> None:
    async with asgi_client(playground_app_factory()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["guard_importable"] is True
    # No cases.jsonl under tmp_path; the real guard fixtures (5 benign + 5 attacks) load.
    assert body["case_count"] == 10
    assert body["replay_entry_count"] == 0


async def test_guard_page_renders_english_shell(playground_app_factory: AppFactory) -> None:
    async with asgi_client(playground_app_factory()) as client:
        response = await client.get("/guard")
    assert response.status_code == 200
    html = response.text
    assert '<html lang="en">' in html
    assert 'href="#main">Skip to main content</a>' in html
    assert 'href="/guard" aria-current="page">Playground</a>' in html
    assert 'href="/guard/results">Results</a>' in html
    for asset in ("/static/review.css", "/pg-static/playground.css", "/static/htmx.min.js"):
        assert asset in html
    assert "Guard library unavailable" not in html


async def test_shared_and_playground_static_files_are_served(
    playground_app_factory: AppFactory,
) -> None:
    async with asgi_client(playground_app_factory()) as client:
        css = await client.get("/static/review.css")
        htmx = await client.get("/static/htmx.min.js")
        pg_css = await client.get("/pg-static/playground.css")
        pg_js = await client.get("/pg-static/playground.js")
    assert css.status_code == 200 and ".skip-link" in css.text
    assert htmx.status_code == 200
    assert hashlib.sha256(htmx.content).hexdigest() == HTMX_SHA256
    assert pg_css.status_code == 200 and pg_js.status_code == 200


async def test_docs_and_openapi_are_disabled(playground_app_factory: AppFactory) -> None:
    async with asgi_client(playground_app_factory()) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404


async def test_guard_unavailable_message_while_health_keeps_serving(
    playground_app_factory: AppFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "no-guard"
    empty.mkdir()
    _force_guard_import_failure(monkeypatch)
    app = playground_app_factory(guard_root=empty)
    async with asgi_client(app) as client:
        page = await client.get("/guard")
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
    assert page.status_code == 200
    assert "Guard library unavailable" in page.text
    assert health.status_code == 200
    assert ready.json()["guard_importable"] is False
    assert ready.json()["guard_error"]


async def test_outbound_network_is_blocked() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(OutboundNetworkBlockedError):
            await client.get("https://api.openai.com/v1/models")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OutboundNetworkBlockedError):
            sock.connect(("203.0.113.1", 443))
    finally:
        sock.close()


def test_credentials_are_unset() -> None:
    import os

    assert not any(os.environ.get(name) for name in CREDENTIAL_ENV_VARS)

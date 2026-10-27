"""Safety and isolation of the Guard Playground (task 6.3; Requirements 8.1, 8.3, 8.7, 8.8,
9.1-9.7, 11.5, 11.6).
"""

from __future__ import annotations

import ast
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from services.guard_playground.settings import PlaygroundSettings
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    asgi_client,
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
    fixture_playground_app_factory,
)
from tests.unit.test_guard_playground_live_routes import StubRunner, _form, _setup

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")
REPO = PlaygroundSettings().repo_root
PACKAGE = REPO / "services" / "guard_playground"
SENTINELS = {
    "BENCH_OPENAI_API_KEY": "sk-SENTINEL-openai-0123456789",
    "BENCH_OPENROUTER_API_KEY": "sk-or-SENTINEL-0123456789",
}


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    if not root.is_dir():
        return {}
    return {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()
    }


async def test_sentinel_keys_never_reach_pages_or_logs(
    playground_app_factory: Callable[..., Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    _setup(tmp_path)

    class LeakyRunner(StubRunner):
        async def __call__(self, email: Any, preset: str, model_key: str, **kw: Any) -> Any:
            logging.getLogger("services.guard_playground").error(
                "provider said %s", SENTINELS["BENCH_OPENAI_API_KEY"]
            )
            from services.guard_playground.core.runner import LiveRunError

            raise LiveRunError("authentication error", "bad key [redacted]")

    from tests.unit.test_guard_playground_live_routes import _finish

    pages = []
    with caplog.at_level(logging.INFO):
        async with asgi_client(playground_app_factory(runner=LeakyRunner())) as client:
            for path in ("/guard", "/guard/results", "/readyz", "/guard/run-status"):
                pages.append((await client.get(path)).text)
            response = await client.post("/guard/run", data=_form())
            pages.append(response.text)
            pages.extend(await _finish(client, response.text))
    text = "\n".join(pages) + caplog.text
    for value in SENTINELS.values():
        assert value not in text
    assert "authentication error" in text


def test_package_never_builds_an_audited_pipeline_or_sends_mail() -> None:
    """Every MailGuardPipeline construction passes audit=False; no mail-sending import."""
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "MailGuardPipeline":
                audit = [k for k in node.keywords if k.arg == "audit"]
                assert audit and isinstance(audit[0].value, ast.Constant), path
                assert audit[0].value.value is False, path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("packages.adapters", "services.dispatch")), path
                assert "smtp" not in node.module.lower(), path


async def test_runs_write_nothing_under_results_or_guard(
    playground_app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    results, guard = REPO / "evaluation" / "results", REPO / "agentmailguard"
    before = (_snapshot(results), _snapshot(guard / "logs"))
    _setup(tmp_path)
    from tests.unit.test_guard_playground_live_routes import _finish

    async with asgi_client(playground_app_factory(runner=StubRunner())) as client:
        response = await client.post("/guard/run", data=_form())
        await _finish(client, response.text)
        await client.get("/guard/results")
    assert (_snapshot(results), _snapshot(guard / "logs")) == before


def test_guard_package_is_unchanged_and_review_ui_adds_one_link() -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "agentmailguard", "Dockerfile", ".dockerignore"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if status.returncode != 0:
        pytest.skip("git not available")
    assert status.stdout.strip() == ""
    diff = subprocess.run(
        ["git", "diff", "--unified=0", "--", "services/frontend/templates/base.html"],
        cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    changed = [
        line for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]  # fmt: skip
    assert changed in ([], ['+        <li><a href="http://127.0.0.1:3010/guard">Guard</a></li>'])


def test_compose_publishes_on_loopback_only() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split("  guard-playground:", 1)[1].split("\nvolumes:", 1)[0]
    assert '"127.0.0.1:3010:3010"' in block
    assert "depends_on" not in block
    mounts = [line.strip() for line in block.splitlines() if line.strip().startswith("- ./")]
    assert mounts and all(m.endswith(":ro") for m in mounts)

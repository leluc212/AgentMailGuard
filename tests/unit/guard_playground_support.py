"""Shared fixtures and helpers for the Guard Playground tests (Requirement 11.1, 11.2).

Not a conftest: Playground test modules opt in by importing these fixtures, so no other test
in ``tests/unit`` is affected. Typical use::

    from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
        fixture_block_network, fixture_guard_import_isolation,
        fixture_no_credentials, fixture_playground_app_factory,
    )

Fixtures are registered under their short names (``block_network``, ``no_credentials``,
``guard_import_isolation``, ``playground_app_factory``); the ``fixture_`` function names keep
test parameters from shadowing the imports (ruff F811).
    pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")

``guard_import_isolation`` undoes the Playground's ``sys.path`` addition and drops any
``mailguard`` modules it imported, so later test modules that ``importorskip("mailguard")``
still skip in a plain ``pytest`` run exactly as they did before.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.guard_playground.settings import PlaygroundSettings

CREDENTIAL_ENV_VARS = (
    "OPENAI_API_KEY",
    "BENCH_OPENAI_API_KEY",
    "BENCH_OPENROUTER_API_KEY",
    "LLM__OPENAI_API_KEY",
)


class OutboundNetworkBlockedError(RuntimeError):
    """Raised when a Playground test tries to reach the network."""


def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(name="block_network")
def fixture_block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any outbound connection: httpx sends off-ASGI and non-loopback sockets."""
    original_send = httpx.AsyncClient.send

    async def guarded_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> Any:
        if not isinstance(self._transport, httpx.ASGITransport):
            raise OutboundNetworkBlockedError(f"outbound HTTP blocked in tests: {request.url}")
        return await original_send(self, request, **kwargs)

    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        # AF_UNIX paths and loopback (asyncio's self-pipe on Windows) stay allowed.
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise OutboundNetworkBlockedError(f"outbound socket blocked in tests: {address!r}")
        original_connect(self, address)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(name="no_credentials")
def fixture_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every model credential the Playground could read."""
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(name="guard_import_isolation")
def fixture_guard_import_isolation() -> Iterator[None]:
    """Restore ``sys.path`` and drop newly imported ``mailguard`` modules after the test."""
    saved_path = list(sys.path)
    before = {name for name in sys.modules if name.partition(".")[0] == "mailguard"}
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for name in [n for n in sys.modules if n.partition(".")[0] == "mailguard"]:
            if name not in before:
                del sys.modules[name]


def playground_settings(tmp_path: Path, **overrides: Any) -> PlaygroundSettings:
    """Settings whose data paths live under ``tmp_path``; the guard root is the real one."""
    repo = PlaygroundSettings().repo_root
    values: dict[str, Any] = {
        "repo_root": tmp_path,
        "guard_root": repo / "agentmailguard",
        "data_dir": tmp_path / "data",
        **overrides,
    }
    return PlaygroundSettings(**values)


@pytest.fixture(name="playground_app_factory")
def fixture_playground_app_factory(
    tmp_path: Path, guard_import_isolation: None
) -> Callable[..., Any]:
    """Build a Playground app on tmp_path settings; imports ``main`` lazily (isolated)."""

    def build(*, runner: Any = None, **overrides: Any) -> Any:
        from services.guard_playground.main import create_app

        return create_app(playground_settings(tmp_path, **overrides), runner=runner)

    return build


def asgi_client(app: Any) -> httpx.AsyncClient:
    """In-process client, the transport style of tests/unit/test_frontend_review_ui.py."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://playground")

"""scripts/connect_gmail.py registers the live test mailbox safely (6.10; R17.1, R24.5)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TOKEN = "ya29.test-token-never-printed"


def _load() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("connect_gmail", SCRIPTS / "connect_gmail.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cg = _load()


def test_credentials_ref_points_at_the_env_var() -> None:
    assert cg.CREDENTIALS_REF == "env:GMAIL_ACCESS_TOKEN"


def test_token_comes_from_the_environment_first() -> None:
    assert (
        cg.resolve_gmail_token({"GMAIL_ACCESS_TOKEN": TOKEN}, {"GMAIL_ACCESS_TOKEN": "x"}) == TOKEN
    )


def test_token_falls_back_to_the_dotenv_file() -> None:
    assert cg.resolve_gmail_token({}, {"GMAIL_ACCESS_TOKEN": f"  {TOKEN} "}) == TOKEN


@pytest.mark.parametrize("dotenv", [{}, {"GMAIL_ACCESS_TOKEN": ""}, {"GMAIL_ACCESS_TOKEN": None}])
def test_a_blank_token_is_refused(dotenv: dict[str, str | None]) -> None:
    with pytest.raises(cg.ConnectError, match="GMAIL_ACCESS_TOKEN"):
        cg.resolve_gmail_token({"GMAIL_ACCESS_TOKEN": "  "}, dotenv)


def test_profile_of_the_named_account_returns_its_history_id() -> None:
    profile = {"emailAddress": "Demo.Box@gmail.com", "historyId": "123456", "messagesTotal": 3}
    assert cg.check_profile(profile, "demo.box@gmail.com") == "123456"


def test_profile_of_another_account_is_refused() -> None:
    with pytest.raises(cg.ConnectError, match="other@gmail.com"):
        cg.check_profile({"emailAddress": "other@gmail.com", "historyId": "1"}, "demo@gmail.com")


def test_profile_without_history_id_is_refused() -> None:
    with pytest.raises(cg.ConnectError, match="historyId"):
        cg.check_profile({"emailAddress": "demo@gmail.com"}, "demo@gmail.com")


async def test_fetch_profile_sends_the_bearer_token_to_the_profile_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"emailAddress": "demo@gmail.com", "historyId": "9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        profile = await cg.fetch_profile(http, TOKEN)
    assert profile["historyId"] == "9"
    assert str(seen[0].url) == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_an_expired_token_names_the_fix_and_hides_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 401}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(cg.ConnectError) as exc_info:
            await cg.fetch_profile(http, TOKEN)
    assert "expired" in str(exc_info.value)
    assert "§3.2" in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)

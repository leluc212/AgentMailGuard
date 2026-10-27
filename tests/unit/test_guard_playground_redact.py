"""Tests for core/redact.py (Requirements 8.3, 8.4, 11.5)."""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest
from pydantic import BaseModel

from services.guard_playground.core.redact import (
    REDACTED,
    RedactingFilter,
    install_log_redaction,
    redact,
    redact_data,
    secrets,
)
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
    fixture_playground_app_factory,
)

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")

SENTINEL = "sk-test-SENTINEL-0123456789abcdef"


@pytest.fixture(name="sentinel_key")
def fixture_sentinel_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    return SENTINEL


def test_secrets_reads_suffixed_vars_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "tok-12345678" not in secrets()
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-12345678")
    monkeypatch.setenv("DB_PASSWORD", "pw-abcdefgh")
    monkeypatch.setenv("APP_SECRET", "sec-abcdefgh")
    monkeypatch.setenv("SHORT_KEY", "1234567")  # 7 chars: ignored
    monkeypatch.setenv("NOT_A_SECRET_NAME", "visible-value-123")
    found = secrets()
    assert {"tok-12345678", "pw-abcdefgh", "sec-abcdefgh"} <= set(found)
    assert "1234567" not in found
    assert "visible-value-123" not in found


def test_redact_replaces_secret(sentinel_key: str) -> None:
    text = f"Authorization: Bearer {sentinel_key} failed; key={sentinel_key}"
    out = redact(text)
    assert sentinel_key not in out
    assert out == f"Authorization: Bearer {REDACTED} failed; key={REDACTED}"
    assert redact("nothing here") == "nothing here"


def test_redact_longest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A_KEY", "abcdefgh")
    monkeypatch.setenv("B_KEY", "abcdefgh-ijklmnop")
    assert redact("x abcdefgh-ijklmnop y abcdefgh") == f"x {REDACTED} y {REDACTED}"


class _Inner(BaseModel):
    note: str


class _Result(BaseModel):
    error: str | None
    items: list[_Inner]


def test_redact_data_nested(sentinel_key: str) -> None:
    data: dict[str, Any] = {
        "msg": f"bad key {sentinel_key}",
        "list": [sentinel_key, 3, None, ("t", sentinel_key)],
        "model": _Result(error=sentinel_key, items=[_Inner(note=f"n {sentinel_key}")]),
    }
    out = redact_data(data)
    assert sentinel_key not in repr(out)
    assert out["list"][1:3] == [3, None]
    assert isinstance(out["model"], _Result)
    assert out["model"].items[0].note == f"n {REDACTED}"


def test_filter_redacts_message_args_and_exception(sentinel_key: str) -> None:
    try:
        raise RuntimeError(f"boom {sentinel_key}")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "call with %s", (sentinel_key,), exc_info
    )
    assert RedactingFilter().filter(record) is True
    formatted = logging.Formatter().format(record)
    assert sentinel_key not in formatted
    assert REDACTED in record.getMessage()


def test_log_output_has_no_sentinel(sentinel_key: str, caplog: pytest.LogCaptureFixture) -> None:
    install_log_redaction()
    logger = logging.getLogger("services.guard_playground.test")
    with caplog.at_level(logging.INFO):
        logger.info("key is %s", sentinel_key)
        logger.warning(f"direct {sentinel_key}")
        try:
            raise ValueError(sentinel_key)
        except ValueError:
            logger.exception("failure")
    assert caplog.records
    assert sentinel_key not in caplog.text
    assert all(sentinel_key not in r.getMessage() for r in caplog.records)


def test_create_app_attaches_filter_once(playground_app_factory: Any) -> None:
    playground_app_factory()
    playground_app_factory()
    for handler in logging.getLogger().handlers:
        count = sum(isinstance(f, RedactingFilter) for f in handler.filters)
        assert count == 1

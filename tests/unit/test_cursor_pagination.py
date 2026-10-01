"""Opaque keyset cursors for GET /v1/drafts (task 6.1; R23.6)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.pagination import InvalidCursorError, decode_cursor, encode_cursor


def test_cursor_round_trips_timestamp_and_id() -> None:
    created = datetime(2026, 9, 28, 9, 30, 15, 123456, tzinfo=UTC)
    item = uuid4()
    cursor = encode_cursor(created, item)
    assert "=" not in cursor
    assert decode_cursor(cursor) == (created, item)


@pytest.mark.parametrize("cursor", ["", "not-a-cursor", "e30", "W10"])
def test_garbage_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_naive_timestamp_cursor_is_rejected() -> None:
    raw = json.dumps({"t": "2026-09-28T09:30:00", "id": str(uuid4())}).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_encoding_a_naive_timestamp_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        encode_cursor(datetime(2026, 9, 28), uuid4())

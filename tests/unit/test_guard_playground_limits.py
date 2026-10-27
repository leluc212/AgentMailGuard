"""Tests for services/guard_playground/core/limits.py (Req 8.5, 8.6, 2.2, 2.3, 11.4; Property 9)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from services.guard_playground.core.limits import (
    MAX_BODY,
    MAX_CHUNK,
    MAX_CHUNKS,
    MAX_SUBJECT,
    check_email,
    clean_chunks,
    limits_message,
)

BODY_REQUIRED = "Body is required"


def test_constants() -> None:
    assert (MAX_SUBJECT, MAX_BODY, MAX_CHUNKS, MAX_CHUNK) == (500, 20_000, 5, 4_000)


def test_exactly_at_every_limit_is_accepted() -> None:
    chunks = ["c" * MAX_CHUNK] * MAX_CHUNKS
    assert check_email("s" * MAX_SUBJECT, "b" * MAX_BODY, chunks) == []
    assert limits_message([]) is None


def test_empty_subject_and_no_chunks_accepted() -> None:
    assert check_email("", "hello", []) == []


@pytest.mark.parametrize("body", ["", " ", "\n\t  \r\n"])
def test_empty_or_whitespace_body_rejected(body: str) -> None:
    assert check_email("subject", body, []) == [BODY_REQUIRED]


@pytest.mark.parametrize(
    ("subject", "body", "chunks", "expected"),
    [
        ("s" * 501, "b", [], "Subject is longer than 500 characters (501)"),
        ("", "b" * 20_001, [], "Body is longer than 20,000 characters (20,001)"),
        ("", "b", ["c"] * 6, "More than 5 KB chunks (6)"),
        ("", "b", ["c", "c" * 4_001], "KB chunk 2 is longer than 4,000 characters (4,001)"),
    ],
    ids=["subject", "body", "chunk-count", "chunk-length"],
)
def test_each_limit_over_by_one_rejected(
    subject: str, body: str, chunks: list[str], expected: str
) -> None:
    assert check_email(subject, body, chunks) == [expected]


def test_multiple_violations_listed_together_in_fixed_order() -> None:
    chunks = ["c" * 4_001, "ok", "ok", "c" * 5_000, "ok", "ok"]
    errors = check_email("s" * 600, " ", chunks)
    assert errors == [
        BODY_REQUIRED,
        "Subject is longer than 500 characters (600)",
        "More than 5 KB chunks (6)",
        "KB chunk 1 is longer than 4,000 characters (4,001)",
        "KB chunk 4 is longer than 4,000 characters (5,000)",
    ]
    message = limits_message(errors)
    assert message == "Fix these before running: " + "; ".join(errors)


def test_body_over_limit_and_subject_over_limit_together() -> None:
    errors = check_email("s" * 501, "b" * 20_001, [])
    assert errors == [
        "Subject is longer than 500 characters (501)",
        "Body is longer than 20,000 characters (20,001)",
    ]


def test_clean_chunks_drops_blank_and_keeps_order_and_text() -> None:
    chunks = ["", "  first ", " \n\t", "second\n", "   ", "third"]
    assert clean_chunks(chunks) == ["  first ", "second\n", "third"]
    assert clean_chunks([]) == []
    assert clean_chunks(["", " "]) == []


def test_length_counts_python_str_characters() -> None:
    # 500 non-ASCII characters are 500 characters, not their UTF-8 byte length.
    assert check_email("é" * 500, "b", []) == []
    assert check_email("😀" * 501, "b", []) == ["Subject is longer than 500 characters (501)"]


def _near(limit: int) -> st.SearchStrategy[int]:
    return st.integers(min_value=max(0, limit - 3), max_value=limit + 3)


@settings(max_examples=200, deadline=None)
@given(
    subject_len=st.one_of(st.just(0), _near(MAX_SUBJECT)),
    body_len=st.one_of(st.just(0), _near(MAX_BODY)),
    body_blank=st.booleans(),
    chunk_lens=st.lists(st.one_of(st.integers(1, 10), _near(MAX_CHUNK)), max_size=MAX_CHUNKS + 3),
)
def test_property_9_limits(
    subject_len: int, body_len: int, body_blank: bool, chunk_lens: list[int]
) -> None:
    """Property 9: the error set is exactly the set of violated limits.

    **Validates: Requirements 8.5, 11.4**
    """
    subject = "s" * subject_len
    body = (" " if body_blank else "b") * body_len
    chunks = ["c" * n for n in chunk_lens]

    expected: set[str] = set()
    if body_len == 0 or body_blank:
        expected.add(BODY_REQUIRED)
    if subject_len > MAX_SUBJECT:
        expected.add(f"Subject is longer than 500 characters ({subject_len:,})")
    if body_len > MAX_BODY:
        expected.add(f"Body is longer than 20,000 characters ({body_len:,})")
    if len(chunk_lens) > MAX_CHUNKS:
        expected.add(f"More than 5 KB chunks ({len(chunk_lens)})")
    for k, n in enumerate(chunk_lens, start=1):
        if n > MAX_CHUNK:
            expected.add(f"KB chunk {k} is longer than 4,000 characters ({n:,})")

    errors = check_email(subject, body, chunks)
    assert len(errors) == len(set(errors))
    assert set(errors) == expected
    assert (limits_message(errors) is None) == (not expected)

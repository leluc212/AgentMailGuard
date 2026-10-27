"""External_Data upload and Batch_Run with a stub runner (tasks 8.2, 8.3; Requirement 21)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from services.guard_playground.core.external import parse_upload
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    asgi_client,
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
    fixture_playground_app_factory,
)
from tests.unit.test_guard_playground_live_routes import StubRunner

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")

EML = (
    b"From: a@example.com\r\nTo: b@example.com\r\nSubject: Hello there\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>Please <b>refund</b> me.</p>\r\n"
)


def test_eml_html_body_is_turned_into_text() -> None:
    result = parse_upload("mail.EML", EML)
    assert result.error is None
    (item,) = result.emails
    assert (item.subject, item.body, item.chunks) == ("Hello there", "Please refund me.", ())


def test_json_minimal_and_case_schema_and_excluded() -> None:
    data = [
        {"id": "m1", "subject": "s", "body": "b", "chunks": ["c1", "  "]},
        {
            "case_id": "c-9",
            "email": {"subject": "x", "body_text": "y"},
            "chunks": [{"content": "k"}],
        },
        {"id": "bad", "subject": "s"},
        {"id": "long", "body": "x" * 20_001},
    ]
    result = parse_upload("batch.json", json.dumps(data).encode())
    assert [(e.position, e.ext_id, e.chunks) for e in result.emails] == [
        (1, "m1", ("c1",)),
        (2, "c-9", ("k",)),
    ]
    assert result.excluded[0].startswith("#3") and "body" in result.excluded[0]
    assert result.excluded[1].startswith("#4 (id long)") and "20,000" in result.excluded[1]


@pytest.mark.parametrize(
    ("name", "data", "error"),
    [
        ("x.txt", b"hi", "Unsupported file type"),
        ("x.json", b"{not json", "could not be parsed"),
        ("x.jsonl", b"\n".join([b'{"body": "b"}'] * 51), "limit is 50"),
        ("x.json", b"x" * (5 * 1024 * 1024 + 1), "larger than 5 MB"),
    ],
    ids=["extension", "unparsable", "too-many", "too-large"],
)
def test_whole_file_rejected(name: str, data: bytes, error: str) -> None:
    result = parse_upload(name, data)
    assert result.error is not None and error in result.error and result.emails == []


async def test_upload_one_email_fills_the_form_and_writes_nothing(
    playground_app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    before = sorted(p for p in tmp_path.rglob("*"))
    async with asgi_client(playground_app_factory()) as client:
        response = await client.post("/guard/upload", files={"file": ("one.eml", EML)})
    assert "1 valid email" in response.text and 'hx-swap-oob="true"' in response.text
    assert 'value="Hello there"' in response.text
    assert sorted(p for p in tmp_path.rglob("*")) == before  # Requirement 21.13


async def test_upload_refused_in_replay_only(playground_app_factory: Callable[..., Any]) -> None:
    async with asgi_client(playground_app_factory(live_enabled=False)) as client:
        response = await client.post("/guard/upload", files={"file": ("one.eml", EML)})
    assert "External data requires Live mode" in response.text


async def test_batch_runs_every_email_and_shows_totals(
    playground_app_factory: Callable[..., Any],
) -> None:
    runner = StubRunner()
    lines = "\n".join(
        json.dumps({"id": f"e{i}", "subject": f"s{i}", "body": "b"}) for i in range(3)
    )
    async with asgi_client(playground_app_factory(runner=runner)) as client:
        up = await client.post("/guard/upload", files={"file": ("b.jsonl", lines.encode())})
        upload_id = re.search(r'"upload_id": "([0-9a-f]+)"', up.text)
        assert upload_id and "Run all 3 emails" in up.text
        form = {"upload_id": upload_id.group(1), "model_key": "gpt-4o-mini", "compare": "1",
                "left_preset": "C0", "right_preset": "C3"}  # fmt: skip
        page = (await client.post("/guard/batch", data=form)).text
        batch_id = re.search(r"/guard/batch/([0-9a-f]+)", page)
        assert batch_id
        for _ in range(300):
            page = (await client.get(f"/guard/batch/{batch_id.group(1)}")).text
            if "6 of 6" in page:
                break
            await asyncio.sleep(0.01)
        row = await client.get(f"/guard/batch/{batch_id.group(1)}/row/2")
    assert "6 of 6" in page and "Totals" in page and "DRAFT ONLY: 3" in page
    assert "External data" in page and 'hx-trigger="every 2s"' not in page
    assert len(runner.calls) == 6 and {c[0].source for c in runner.calls} == {"external"}
    assert "External data" in row.text and "C3 · LIVE · gpt-4o-mini" in row.text

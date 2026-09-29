"""The evaluation mailbox the live benchmark's feeder creates per case (CLAUDE.md §4, R1.3).

Provider names live only in ``packages/adapters/``; the feeder asks this module for a mailbox and
never names a provider itself. The insert runs against a recording pool here: the real
PostgreSQL insert is covered by the integrator's integration run.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from packages.adapters import evaluation
from packages.adapters.evaluation import create_eval_mailbox


class RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "INSERT 0 1"


class RecordingPool:
    """Just enough of asyncpg.Pool: ``async with pool.acquire() as conn``."""

    def __init__(self) -> None:
        self.connection = RecordingConnection()

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self.connection


def _insert_columns(sql: str) -> list[str]:
    match = re.search(r"INSERT INTO mailbox\s*\((.*?)\)\s*VALUES", sql, re.S)
    assert match is not None
    return [column.strip() for column in match.group(1).split(",")]


async def test_inserts_an_imap_mailbox_for_the_organization() -> None:
    pool = RecordingPool()
    org_id = uuid4()

    mailbox_id = await create_eval_mailbox(
        pool,
        organization_id=org_id,
        address="case-0001@eval.example.com",
    )

    assert isinstance(mailbox_id, UUID)
    ((sql, args),) = pool.connection.executed
    row = dict(zip(_insert_columns(sql), args, strict=True))
    assert row["id"] == mailbox_id
    assert row["organization_id"] == org_id
    assert row["address"] == "case-0001@eval.example.com"
    assert row["provider"] == "imap"
    assert row["credentials_ref"] == "eval/none"


async def test_every_call_creates_a_distinct_mailbox() -> None:
    pool = RecordingPool()

    first = await create_eval_mailbox(pool, organization_id=uuid4(), address="a@eval.example.com")
    second = await create_eval_mailbox(pool, organization_id=uuid4(), address="b@eval.example.com")

    assert first != second
    assert len(pool.connection.executed) == 2


def test_the_insert_names_every_not_null_column_of_the_mailbox_table() -> None:
    """migrations/0001: id, organization_id, provider and address have no default."""
    assert {"id", "organization_id", "provider", "address"} <= set(
        _insert_columns(evaluation.INSERT_EVAL_MAILBOX_SQL)
    )


def test_the_credentials_reference_resolves_to_no_secret() -> None:
    """The mailbox is never fetched from or sent through, so its reference names no credential."""
    from packages.adapters.registry import resolve_provider_credentials

    assert resolve_provider_credentials(evaluation.EVAL_CREDENTIALS_REF, "imap") == {}

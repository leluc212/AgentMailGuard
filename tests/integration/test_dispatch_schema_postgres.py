"""Migration 0005: dispatch handle and review feedback (R5.5, R16.7, R17.3, R19.2; ADR-0009).

Runs in the isolated rag_email_test database (tests/integration/conftest.py).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.migrator import apply_migrations, discover_migrations, rollback_migrations
from packages.domain.entities import GeneratedDraft

DISPATCH_COLUMNS = ("provider_draft_id", "provider_draft_message_id", "dispatch_idempotency_key")


@pytest.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool[Any]]:
    settings = AppSettings().database
    await apply_migrations(dsn=settings.asyncpg_dsn)
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_message(conn: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert org, mailbox, thread and one inbound email; return (org, message, thread)."""
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    await conn.execute(
        "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
    )
    await conn.execute(
        "INSERT INTO mailbox (id, organization_id, provider, address) VALUES ($1, $2, 'gmail', $3)",
        mbx_id,
        org_id,
        f"box-{mbx_id.hex[:6]}@example.com",
    )
    await conn.execute(
        "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
        " VALUES ($1, $2, $3, $4)",
        thread_id,
        org_id,
        mbx_id,
        f"th-{thread_id.hex[:6]}",
    )
    await conn.execute(
        "INSERT INTO email_message (id, organization_id, mailbox_id, thread_id,"
        " provider_message_id, direction, received_at)"
        " VALUES ($1, $2, $3, $4, $5, 'inbound', now())",
        msg_id,
        org_id,
        mbx_id,
        thread_id,
        f"prov-{msg_id.hex[:6]}",
    )
    return org_id, msg_id, thread_id


async def _insert_draft(
    conn: Any, org_id: uuid.UUID, msg_id: uuid.UUID, thread_id: uuid.UUID, key: str | None
) -> uuid.UUID:
    draft_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO generated_draft (id, organization_id, message_id, thread_id, action, body,"
        " dispatch_idempotency_key) VALUES ($1, $2, $3, $4, 'reply', 'Hello', $5)",
        draft_id,
        org_id,
        msg_id,
        thread_id,
        key,
    )
    return draft_id


async def _columns(conn: Any, table: str, names: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1 AND column_name = ANY($2::text[])
        """,
        table,
        list(names),
    )
    return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}


async def _indexes(conn: Any, table: str) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = $1",
        table,
    )
    return {r["indexname"]: r["indexdef"] for r in rows}


async def test_0005_adds_the_dispatch_and_review_columns(db_pool: asyncpg.Pool[Any]) -> None:
    """design.md §6: three nullable TEXT dispatch columns and feedback.review_ms INT."""
    async with db_pool.acquire() as conn:
        draft_cols = await _columns(conn, "generated_draft", DISPATCH_COLUMNS)
        feedback_cols = await _columns(conn, "feedback", ("review_ms",))
        draft_idx = await _indexes(conn, "generated_draft")
        feedback_idx = await _indexes(conn, "feedback")

    assert draft_cols == dict.fromkeys(DISPATCH_COLUMNS, ("text", "YES"))
    assert feedback_cols == {"review_ms": ("integer", "YES")}
    assert "UNIQUE" in draft_idx["uq_generated_draft_dispatch_key"]
    assert "UNIQUE" in feedback_idx["uq_feedback_draft"]
    assert "idx_feedback_draft" not in feedback_idx  # superseded by uq_feedback_draft


async def test_dispatch_key_is_unique_but_unclaimed_drafts_coexist(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """R17.3, R19.2: one draft can hold a dispatch key; NULL (unclaimed) is not a key."""
    async with db_pool.acquire() as conn:
        org_id, msg_id, thread_id = await _seed_message(conn)
        try:
            await _insert_draft(conn, org_id, msg_id, thread_id, None)
            await _insert_draft(conn, org_id, msg_id, thread_id, None)
            key = f"dispatch-{uuid.uuid4().hex}"
            await _insert_draft(conn, org_id, msg_id, thread_id, key)
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert_draft(conn, org_id, msg_id, thread_id, key)
        finally:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_feedback_allows_one_row_per_draft(db_pool: asyncpg.Pool[Any]) -> None:
    """R16.7 / 6.1: a repeated approve cannot write a second feedback row."""
    async with db_pool.acquire() as conn:
        org_id, msg_id, thread_id = await _seed_message(conn)
        try:
            draft_id = await _insert_draft(conn, org_id, msg_id, thread_id, None)
            await conn.execute(
                "INSERT INTO feedback (id, organization_id, draft_id, decision, review_ms)"
                " VALUES ($1, $2, $3, 'accepted', 4200)",
                uuid.uuid4(),
                org_id,
                draft_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO feedback (id, organization_id, draft_id, decision)"
                    " VALUES ($1, $2, $3, 'rejected')",
                    uuid.uuid4(),
                    org_id,
                    draft_id,
                )
            review_ms = await conn.fetchval(
                "SELECT review_ms FROM feedback WHERE draft_id = $1 AND organization_id = $2",
                draft_id,
                org_id,
            )
            assert review_ms == 4200
        finally:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_draft_store_round_trips_the_dispatch_handle(db_pool: asyncpg.Pool[Any]) -> None:
    """insert_draft writes and _row_to_draft reads the three ADR-0009 columns."""
    async with db_pool.acquire() as conn:
        org_id, msg_id, thread_id = await _seed_message(conn)
    store = PostgresDraftStore(db_pool)
    try:
        plain = await store.create_draft(
            GeneratedDraft(
                organization_id=org_id, message_id=msg_id, thread_id=thread_id, body="Hi"
            )
        )
        assert plain.provider_draft_id is None
        assert plain.provider_draft_message_id is None
        assert plain.dispatch_idempotency_key is None

        key = f"dispatch-{uuid.uuid4().hex}"
        handled = await store.create_draft(
            GeneratedDraft(
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                body="Hi again",
                provider_draft_id="r-8123",
                provider_draft_message_id="18c2f0a9d1e4b7ab",
                dispatch_idempotency_key=key,
            )
        )
        stored = await store.get_draft(handled.id, org_id)
        assert stored is not None
        assert stored.provider_draft_id == "r-8123"
        assert stored.provider_draft_message_id == "18c2f0a9d1e4b7ab"
        assert stored.dispatch_idempotency_key == key
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_0005_rolls_back_cleanly_and_reapplies() -> None:
    """R5.5: 0005.down removes exactly what 0005.up added and restores idx_feedback_draft."""
    dsn = AppSettings().database.asyncpg_dsn
    await apply_migrations(dsn=dsn)
    steps = sum(1 for m in discover_migrations() if m.version >= "0005")
    rolled_back = await rollback_migrations(dsn=dsn, steps=steps)
    assert "0005" in rolled_back

    conn = await asyncpg.connect(dsn)
    try:
        assert await _columns(conn, "generated_draft", DISPATCH_COLUMNS) == {}
        assert await _columns(conn, "feedback", ("review_ms",)) == {}
        feedback_idx = await _indexes(conn, "feedback")
        assert "idx_feedback_draft" in feedback_idx
        assert "uq_feedback_draft" not in feedback_idx
        assert "uq_generated_draft_dispatch_key" not in await _indexes(conn, "generated_draft")
    finally:
        await conn.close()

    applied = await apply_migrations(dsn=dsn)
    assert "0005" in applied
    conn = await asyncpg.connect(dsn)
    try:
        assert set(await _columns(conn, "generated_draft", DISPATCH_COLUMNS)) == set(
            DISPATCH_COLUMNS
        )
    finally:
        await conn.close()

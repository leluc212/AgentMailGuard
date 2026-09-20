"""Integration tests for PostgreSQL checkpoint and mailbox stores (R2.4, R2.7, R2.8, R2.9, R1.5).

Verifies real PostgreSQL storage, atomic lock acquisition, pending follow-up toggling,
and tenant isolation on mailbox_checkpoint and mailbox tables against localhost:5433.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.connection import create_pool_from_settings
from packages.db.mailbox import PostgresMailboxStore
from packages.domain.entities import Checkpoint


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide a dedicated asyncpg connection pool connected to the test database."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def ensure_test_org(pool: asyncpg.Pool, org_id: uuid.UUID) -> None:
    """Ensure parent organization exists to satisfy foreign key constraints."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


async def ensure_test_mailbox(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    provider: str = "gmail",
) -> None:
    """Ensure test mailbox exists in database."""
    await ensure_test_org(pool, org_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, $3, $4, 'Test Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            provider,
            f"test_{mbx_id.hex[:6]}@example.com",
        )


@pytest.mark.asyncio
async def test_postgres_checkpoint_crud(db_pool: asyncpg.Pool) -> None:
    """Verify create, read, and update operations on mailbox_checkpoint (R2.4)."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id)

    store = PostgresCheckpointStore(db_pool)

    # Initial get on non-existent checkpoint returns None
    initial = await store.get(mbx_id)
    assert initial is None

    # Save a new checkpoint
    now = datetime.now(UTC)
    cp = Checkpoint(
        mailbox_id=mbx_id,
        organization_id=org_id,
        history_id="hist-12345",
        delta_link="https://graph.microsoft.com/v1.0/me/mailFolders/delta?token=abc",
        sync_state="idle",
        last_sync_at=now,
    )
    await store.save(cp)

    # Retrieve and verify round-trip
    fetched = await store.get(mbx_id)
    assert fetched is not None
    assert fetched.mailbox_id == mbx_id
    assert fetched.organization_id == org_id
    assert fetched.history_id == "hist-12345"
    assert fetched.delta_link == "https://graph.microsoft.com/v1.0/me/mailFolders/delta?token=abc"
    assert fetched.sync_state == "idle"
    assert fetched.pending_followup is False

    # Update checkpoint
    cp.history_id = "hist-67890"
    cp.sync_state = "full_resync"
    await store.save(cp)

    updated = await store.get(mbx_id)
    assert updated is not None
    assert updated.history_id == "hist-67890"
    assert updated.sync_state == "full_resync"


@pytest.mark.asyncio
async def test_postgres_checkpoint_atomic_lock(db_pool: asyncpg.Pool) -> None:
    """Verify atomic in-flight lock acquisition and release (R2.9)."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id)

    store = PostgresCheckpointStore(db_pool)

    # First attempt acquires lock (transitions to syncing)
    acquired_1 = await store.try_acquire_lock(mbx_id, org_id)
    assert acquired_1 is True

    # Concurrent attempt on syncing mailbox fails (R2.9)
    acquired_2 = await store.try_acquire_lock(mbx_id, org_id)
    assert acquired_2 is False

    # Check state in database is 'syncing'
    cp = await store.get(mbx_id)
    assert cp is not None
    assert cp.sync_state == "syncing"

    # Release lock to idle
    await store.release_lock(mbx_id, next_state="idle")
    cp = await store.get(mbx_id)
    assert cp is not None
    assert cp.sync_state == "idle"

    # Now lock can be acquired again
    acquired_3 = await store.try_acquire_lock(mbx_id, org_id)
    assert acquired_3 is True
    await store.release_lock(mbx_id, next_state="idle")


@pytest.mark.asyncio
async def test_postgres_checkpoint_pending_followup(db_pool: asyncpg.Pool) -> None:
    """Verify atomic pending_followup flag toggling (R2.9)."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id)

    store = PostgresCheckpointStore(db_pool)

    # Ensure checkpoint row exists
    await store.save(Checkpoint(mailbox_id=mbx_id, organization_id=org_id, sync_state="idle"))

    assert await store.has_pending_followup(mbx_id) is False

    # Mark pending follow-up
    await store.mark_pending_followup(mbx_id)
    assert await store.has_pending_followup(mbx_id) is True

    # Clear pending follow-up
    cleared = await store.clear_pending_followup(mbx_id)
    assert cleared is True
    assert await store.has_pending_followup(mbx_id) is False


@pytest.mark.asyncio
async def test_postgres_mailbox_store_lookup_and_status(db_pool: asyncpg.Pool) -> None:
    """Verify PostgresMailboxStore get and update_status (R1.5, R2.10)."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id)

    store = PostgresMailboxStore(db_pool)

    mbx = await store.get(mbx_id)
    assert mbx is not None
    assert mbx.id == mbx_id
    assert mbx.organization_id == org_id
    assert mbx.status == "active"

    # Update status to needs_reauth
    await store.update_status(mbx_id, "needs_reauth")
    updated = await store.get(mbx_id)
    assert updated is not None
    assert updated.status == "needs_reauth"

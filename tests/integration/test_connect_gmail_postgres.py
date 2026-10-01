"""connect_gmail.register_mailbox upserts the mailbox and arms its checkpoint (6.10)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("connect_gmail", SCRIPTS / "connect_gmail.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cg = _load()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


async def test_register_twice_keeps_one_mailbox_and_moves_the_cursor(pool: asyncpg.Pool) -> None:
    org_id = uuid.uuid4()
    address = f"demo-{org_id.hex[:6]}@gmail.com"
    await pool.execute("INSERT INTO organization (id, name) VALUES ($1, 'cg')", org_id)
    try:
        # A mailbox marked needs_reauth after a 401 must come back active (runbook §7).
        first = await cg.register_mailbox(
            pool, organization_id=org_id, address=address, history_id="100"
        )
        await pool.execute(
            "UPDATE mailbox SET status = 'needs_reauth' WHERE id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        await pool.execute(
            "UPDATE mailbox_checkpoint SET sync_state = 'error', pending_followup = true"
            " WHERE mailbox_id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        second = await cg.register_mailbox(
            pool, organization_id=org_id, address=address.upper(), history_id="250"
        )
        assert second == first

        mbx = await pool.fetchrow(
            "SELECT provider, address, credentials_ref, status FROM mailbox"
            " WHERE id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        assert mbx is not None
        assert dict(mbx) == {
            "provider": "gmail",
            "address": address,
            "credentials_ref": "env:GMAIL_ACCESS_TOKEN",
            "status": "active",
        }
        cp = await pool.fetchrow(
            "SELECT history_id, sync_state, pending_followup FROM mailbox_checkpoint"
            " WHERE mailbox_id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        assert cp is not None
        assert dict(cp) == {"history_id": "250", "sync_state": "idle", "pending_followup": False}
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_register_into_a_missing_organization_is_refused(pool: asyncpg.Pool) -> None:
    with pytest.raises(cg.ConnectError, match="make seed"):
        await cg.register_mailbox(
            pool, organization_id=uuid.uuid4(), address="x@gmail.com", history_id="1"
        )

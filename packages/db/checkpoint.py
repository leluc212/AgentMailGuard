"""Mailbox checkpoint store implementation (R2.4, R2.7, R2.8, R2.9).

Provides durable PostgreSQL and in-memory backends for tracking per-mailbox
synchronization state, delta links, history IDs, and atomic in-flight sync locks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import Checkpoint

logger = logging.getLogger(__name__)


@runtime_checkable
class CheckpointStore(Protocol):
    """Protocol defining mailbox checkpoint persistence and atomic concurrency locking."""

    async def get(self, mailbox_id: UUID | str) -> Checkpoint | None:
        """Retrieve checkpoint for a mailbox."""
        ...

    async def save(self, checkpoint: Checkpoint) -> None:
        """Persist or update checkpoint state."""
        ...

    async def try_acquire_lock(
        self,
        mailbox_id: UUID | str,
        organization_id: UUID | str | None = None,
    ) -> bool:
        """Atomically attempt to acquire the in-flight sync lock (R2.9).

        Returns True if acquired (state transitioned to 'syncing'), False if already syncing.
        """
        ...

    async def release_lock(
        self,
        mailbox_id: UUID | str,
        next_state: str = "idle",
    ) -> None:
        """Release the in-flight lock, setting sync_state to next_state (e.g. 'idle' or 'error')."""
        ...

    async def mark_pending_followup(self, mailbox_id: UUID | str) -> None:
        """Mark that a subsequent sync request was received while syncing (R2.9)."""
        ...

    async def clear_pending_followup(self, mailbox_id: UUID | str) -> bool:
        """Atomically clear the pending follow-up flag. Return True if it was set."""
        ...

    async def has_pending_followup(self, mailbox_id: UUID | str) -> bool:
        """Check if a follow-up sync is pending (R2.9)."""
        ...


class PostgresCheckpointStore(CheckpointStore):
    """PostgreSQL implementation of CheckpointStore querying mailbox_checkpoint."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get(self, mailbox_id: UUID | str) -> Checkpoint | None:
        """Retrieve checkpoint for a mailbox by mailbox_id."""
        mbx_uuid = UUID(str(mailbox_id))
        query = """
            SELECT mailbox_id, organization_id, history_id, delta_link,
                   sync_state, last_sync_at, last_full_sync_at, pending_followup
            FROM mailbox_checkpoint
            WHERE mailbox_id = $1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, mbx_uuid)
            if row is None:
                return None

            return Checkpoint(
                mailbox_id=row["mailbox_id"],
                organization_id=row["organization_id"],
                history_id=row["history_id"],
                delta_link=row["delta_link"],
                sync_state=row["sync_state"],
                last_sync_at=row["last_sync_at"],
                last_full_sync_at=row["last_full_sync_at"],
                pending_followup=row["pending_followup"],
            )

    async def save(self, checkpoint: Checkpoint) -> None:
        """Persist or update checkpoint state in PostgreSQL."""
        mbx_uuid = UUID(str(checkpoint.mailbox_id))
        org_uuid = UUID(str(checkpoint.organization_id)) if checkpoint.organization_id else None

        query = """
            INSERT INTO mailbox_checkpoint (
                mailbox_id, organization_id, history_id, delta_link,
                sync_state, last_sync_at, last_full_sync_at, pending_followup
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (mailbox_id) DO UPDATE SET
                organization_id = COALESCE(
                    EXCLUDED.organization_id, mailbox_checkpoint.organization_id
                ),
                history_id = EXCLUDED.history_id,
                delta_link = EXCLUDED.delta_link,
                sync_state = EXCLUDED.sync_state,
                last_sync_at = EXCLUDED.last_sync_at,
                last_full_sync_at = COALESCE(
                    EXCLUDED.last_full_sync_at, mailbox_checkpoint.last_full_sync_at
                ),
                pending_followup = EXCLUDED.pending_followup;
        """
        async with self.pool.acquire() as conn:
            # If org_uuid was not supplied on checkpoint, fetch from mailbox table
            if org_uuid is None:
                org_row = await conn.fetchrow(
                    "SELECT organization_id FROM mailbox WHERE id = $1", mbx_uuid
                )
                if org_row:
                    org_uuid = org_row["organization_id"]
                else:
                    raise ValueError(
                        f"Cannot save checkpoint: missing org_id and mailbox {mbx_uuid} not found"
                    )

            await conn.execute(
                query,
                mbx_uuid,
                org_uuid,
                checkpoint.history_id,
                checkpoint.delta_link,
                checkpoint.sync_state,
                checkpoint.last_sync_at,
                checkpoint.last_full_sync_at,
                checkpoint.pending_followup,
            )

    async def try_acquire_lock(
        self,
        mailbox_id: UUID | str,
        organization_id: UUID | str | None = None,
    ) -> bool:
        """Atomically acquire in-flight lock on mailbox_checkpoint (R2.9)."""
        mbx_uuid = UUID(str(mailbox_id))
        org_uuid = UUID(str(organization_id)) if organization_id else None

        async with self.pool.acquire() as conn:
            # Try updating existing checkpoint first
            update_query = """
                UPDATE mailbox_checkpoint
                SET sync_state = 'syncing'
                WHERE mailbox_id = $1 AND sync_state != 'syncing'
                RETURNING mailbox_id;
            """
            row = await conn.fetchrow(update_query, mbx_uuid)
            if row is not None:
                return True

            # If no row returned, check if row exists
            exists = await conn.fetchval(
                "SELECT 1 FROM mailbox_checkpoint WHERE mailbox_id = $1", mbx_uuid
            )
            if exists:
                # Row exists and is already 'syncing'
                return False

            # Row doesn't exist yet, insert new row with sync_state='syncing'
            if org_uuid is None:
                org_row = await conn.fetchrow(
                    "SELECT organization_id FROM mailbox WHERE id = $1", mbx_uuid
                )
                if org_row:
                    org_uuid = org_row["organization_id"]
                else:
                    return False

            insert_query = """
                INSERT INTO mailbox_checkpoint (
                    mailbox_id, organization_id, sync_state, last_sync_at
                ) VALUES ($1, $2, 'syncing', now())
                ON CONFLICT (mailbox_id) DO UPDATE
                SET sync_state = 'syncing'
                WHERE mailbox_checkpoint.sync_state != 'syncing'
                RETURNING mailbox_id;
            """
            inserted = await conn.fetchrow(insert_query, mbx_uuid, org_uuid)
            return inserted is not None

    async def release_lock(
        self,
        mailbox_id: UUID | str,
        next_state: str = "idle",
    ) -> None:
        """Release in-flight lock, setting sync_state."""
        mbx_uuid = UUID(str(mailbox_id))
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE mailbox_checkpoint
                SET sync_state = $2
                WHERE mailbox_id = $1;
                """,
                mbx_uuid,
                next_state,
            )

    async def mark_pending_followup(self, mailbox_id: UUID | str) -> None:
        """Atomically set pending_followup to true."""
        mbx_uuid = UUID(str(mailbox_id))
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE mailbox_checkpoint
                SET pending_followup = true
                WHERE mailbox_id = $1;
                """,
                mbx_uuid,
            )

    async def clear_pending_followup(self, mailbox_id: UUID | str) -> bool:
        """Atomically clear pending_followup and return previous state."""
        mbx_uuid = UUID(str(mailbox_id))
        async with self.pool.acquire() as conn:
            prior = await conn.fetchval(
                """
                UPDATE mailbox_checkpoint
                SET pending_followup = false
                WHERE mailbox_id = $1
                RETURNING (
                    SELECT pending_followup FROM mailbox_checkpoint WHERE mailbox_id = $1
                );
                """,
                mbx_uuid,
            )
            # Fetch directly if returning didn't catch prior
            return bool(prior) if prior is not None else False

    async def has_pending_followup(self, mailbox_id: UUID | str) -> bool:
        """Check if pending_followup is true."""
        mbx_uuid = UUID(str(mailbox_id))
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT pending_followup FROM mailbox_checkpoint WHERE mailbox_id = $1;",
                mbx_uuid,
            )
            return bool(val)


class InMemoryCheckpointStore(CheckpointStore):
    """In-memory async-safe implementation of CheckpointStore for fast unit testing."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, Checkpoint] = {}
        self._lock = asyncio.Lock()

    async def get(self, mailbox_id: UUID | str) -> Checkpoint | None:
        async with self._lock:
            cp = self._checkpoints.get(str(mailbox_id))
            if cp is None:
                return None
            # Return a copy to prevent in-place mutation without save
            return Checkpoint(
                mailbox_id=cp.mailbox_id,
                organization_id=cp.organization_id,
                history_id=cp.history_id,
                delta_link=cp.delta_link,
                sync_state=cp.sync_state,
                last_sync_at=cp.last_sync_at,
                last_full_sync_at=cp.last_full_sync_at,
                pending_followup=cp.pending_followup,
            )

    async def save(self, checkpoint: Checkpoint) -> None:
        async with self._lock:
            self._checkpoints[str(checkpoint.mailbox_id)] = Checkpoint(
                mailbox_id=checkpoint.mailbox_id,
                organization_id=checkpoint.organization_id,
                history_id=checkpoint.history_id,
                delta_link=checkpoint.delta_link,
                sync_state=checkpoint.sync_state,
                last_sync_at=checkpoint.last_sync_at,
                last_full_sync_at=checkpoint.last_full_sync_at,
                pending_followup=checkpoint.pending_followup,
            )

    async def try_acquire_lock(
        self,
        mailbox_id: UUID | str,
        organization_id: UUID | str | None = None,
    ) -> bool:
        async with self._lock:
            key = str(mailbox_id)
            cp = self._checkpoints.get(key)
            if cp is None:
                self._checkpoints[key] = Checkpoint(
                    mailbox_id=mailbox_id,
                    organization_id=organization_id,
                    sync_state="syncing",
                    last_sync_at=datetime.now(UTC),
                )
                return True

            if cp.sync_state == "syncing":
                return False

            cp.sync_state = "syncing"
            return True

    async def release_lock(
        self,
        mailbox_id: UUID | str,
        next_state: str = "idle",
    ) -> None:
        async with self._lock:
            cp = self._checkpoints.get(str(mailbox_id))
            if cp:
                cp.sync_state = next_state

    async def mark_pending_followup(self, mailbox_id: UUID | str) -> None:
        async with self._lock:
            cp = self._checkpoints.get(str(mailbox_id))
            if cp:
                cp.pending_followup = True
            else:
                self._checkpoints[str(mailbox_id)] = Checkpoint(
                    mailbox_id=mailbox_id,
                    pending_followup=True,
                )

    async def clear_pending_followup(self, mailbox_id: UUID | str) -> bool:
        async with self._lock:
            cp = self._checkpoints.get(str(mailbox_id))
            if cp and cp.pending_followup:
                cp.pending_followup = False
                return True
            return False

    async def has_pending_followup(self, mailbox_id: UUID | str) -> bool:
        async with self._lock:
            cp = self._checkpoints.get(str(mailbox_id))
            return bool(cp and cp.pending_followup)

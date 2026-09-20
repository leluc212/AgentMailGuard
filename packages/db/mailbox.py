"""Mailbox store interface and backends (R1.1, R1.5, R2.10).

Provides mailbox lookup and status lifecycle management (e.g. marking needs_reauth).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import Mailbox

logger = logging.getLogger(__name__)


@runtime_checkable
class MailboxStore(Protocol):
    """Protocol for mailbox persistence operations."""

    async def get(self, mailbox_id: UUID | str) -> Mailbox | None:
        """Fetch mailbox entity by mailbox_id."""
        ...

    async def update_status(self, mailbox_id: UUID | str, status: str) -> None:
        """Update mailbox operational status (e.g. 'active', 'paused', 'needs_reauth')."""
        ...

    async def list_mailboxes(
        self,
        organization_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[Mailbox], int]:
        """List mailboxes for an organization with pagination and optional filters."""
        ...


class PostgresMailboxStore(MailboxStore):
    """PostgreSQL implementation of MailboxStore."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get(self, mailbox_id: UUID | str) -> Mailbox | None:
        """Fetch mailbox entity by mailbox_id."""
        mbx_uuid = UUID(str(mailbox_id))
        query = """
            SELECT id, organization_id, provider, address, display_name, status, credentials_ref
            FROM mailbox
            WHERE id = $1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, mbx_uuid)
            if row is None:
                return None

            return Mailbox(
                id=row["id"],
                organization_id=row["organization_id"],
                provider=row["provider"],
                address=row["address"],
                display_name=row["display_name"],
                status=row["status"],
                credentials_ref=row["credentials_ref"],
            )

    async def update_status(self, mailbox_id: UUID | str, status: str) -> None:
        """Update mailbox status."""
        mbx_uuid = UUID(str(mailbox_id))
        query = """
            UPDATE mailbox
            SET status = $2
            WHERE id = $1;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, mbx_uuid, status)
            logger.info("Updated mailbox %s status to '%s'", mbx_uuid, status)

    async def list_mailboxes(
        self,
        organization_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[Mailbox], int]:
        """List mailboxes for an organization with pagination and optional filters."""
        org_u = UUID(str(organization_id))
        count_query = """
            SELECT COUNT(*)
            FROM mailbox
            WHERE organization_id = $1
              AND ($2::text IS NULL OR status = $2)
              AND ($3::text IS NULL OR provider = $3);
        """
        data_query = """
            SELECT id, organization_id, provider, address, display_name, status, credentials_ref
            FROM mailbox
            WHERE organization_id = $1
              AND ($2::text IS NULL OR status = $2)
              AND ($3::text IS NULL OR provider = $3)
            ORDER BY created_at ASC, id ASC
            LIMIT $4 OFFSET $5;
        """
        async with self.pool.acquire() as conn:
            total_count = await conn.fetchval(count_query, org_u, status, provider) or 0
            rows = await conn.fetch(data_query, org_u, status, provider, limit, offset)
            mailboxes = [
                Mailbox(
                    id=row["id"],
                    organization_id=row["organization_id"],
                    provider=row["provider"],
                    address=row["address"],
                    display_name=row["display_name"],
                    status=row["status"],
                    credentials_ref=row["credentials_ref"],
                )
                for row in rows
            ]
            return mailboxes, int(total_count)


class InMemoryMailboxStore(MailboxStore):
    """In-memory implementation of MailboxStore for testing."""

    def __init__(self, initial_mailboxes: list[Mailbox] | None = None) -> None:
        self._mailboxes: dict[str, Mailbox] = {}
        self._lock = asyncio.Lock()
        if initial_mailboxes:
            for mbx in initial_mailboxes:
                self._mailboxes[str(mbx.id)] = mbx

    def add(self, mailbox: Mailbox) -> None:
        """Synchronously add a mailbox for test fixture setup."""
        self._mailboxes[str(mailbox.id)] = mailbox

    async def get(self, mailbox_id: UUID | str) -> Mailbox | None:
        async with self._lock:
            mbx = self._mailboxes.get(str(mailbox_id))
            if mbx is None:
                return None
            return Mailbox(
                id=mbx.id,
                organization_id=mbx.organization_id,
                provider=mbx.provider,
                address=mbx.address,
                display_name=mbx.display_name,
                status=mbx.status,
                credentials_ref=mbx.credentials_ref,
            )

    async def update_status(self, mailbox_id: UUID | str, status: str) -> None:
        async with self._lock:
            mbx = self._mailboxes.get(str(mailbox_id))
            if mbx:
                mbx.status = status

    async def list_mailboxes(
        self,
        organization_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[Mailbox], int]:
        org_u = UUID(str(organization_id))
        async with self._lock:
            matched: list[Mailbox] = []
            for mbx in self._mailboxes.values():
                if UUID(str(mbx.organization_id)) != org_u:
                    continue
                if status is not None and mbx.status != status:
                    continue
                if provider is not None and mbx.provider != provider:
                    continue
                matched.append(
                    Mailbox(
                        id=mbx.id,
                        organization_id=mbx.organization_id,
                        provider=mbx.provider,
                        address=mbx.address,
                        display_name=mbx.display_name,
                        status=mbx.status,
                        credentials_ref=mbx.credentials_ref,
                    )
                )
            total = len(matched)
            paginated = matched[offset : offset + limit]
            return paginated, total

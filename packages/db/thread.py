"""Email thread persistence and association store (R4.5, R4.6, design.md §5.2).

Provides durable PostgreSQL and in-memory backends for thread association,
subject matching, array-based participant overlap querying, and atomic
counter/timestamp maintenance with multi-tenant isolation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import EmailThread

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@runtime_checkable
class ThreadStore(Protocol):
    """Protocol defining email thread query, association, and state updates."""

    async def get_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> EmailThread | None:
        """Retrieve a thread by its primary identifier."""
        ...

    async def find_by_provider_thread_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_thread_id: str,
    ) -> EmailThread | None:
        """Find an existing thread matching the mail provider's native thread ID."""
        ...

    async def find_by_rfc822_message_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        rfc822_message_id: str,
    ) -> UUID | None:
        """Find the thread ID of an existing message matching the In-Reply-To Message-ID."""
        ...

    async def find_by_references(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        references_ids: list[str],
    ) -> UUID | None:
        """Find the thread ID of an existing message matching any Message-ID in References."""
        ...

    async def find_by_subject_and_participants(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        subject_normalized: str,
        participants: list[str],
        window: timedelta,
        reference_time: datetime,
    ) -> EmailThread | None:
        """Find an existing thread by normalized subject and overlapping participants."""
        ...

    async def create_thread(
        self,
        thread: EmailThread,
    ) -> EmailThread:
        """Persist a new email thread."""
        ...

    async def update_thread_on_message(
        self,
        thread_id: UUID | str,
        organization_id: UUID | str,
        message_time: datetime,
        participants: list[str],
        provider_thread_id: str | None = None,
    ) -> EmailThread:
        """Atomically increment counters and update timestamps for an active thread."""
        ...

    async def list_threads(
        self,
        organization_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
        mailbox_id: UUID | str | None = None,
        status: str | None = None,
    ) -> tuple[list[EmailThread], int]:
        """List email threads for an organization with pagination and optional filters."""
        ...


class InMemoryThreadStore:
    """In-memory thread store for isolated pure unit testing."""

    def __init__(self) -> None:
        # Key: (org_id, thread_id) -> EmailThread
        self.threads: dict[tuple[UUID, UUID], EmailThread] = {}
        # Key: (org_id, mailbox_id, rfc822_message_id) -> thread_id
        self.message_thread_map: dict[tuple[UUID, UUID, str], UUID] = {}

    def register_message(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        rfc822_message_id: str,
        thread_id: UUID | str,
    ) -> None:
        """Register an existing message rfc822_message_id to thread_id for test fixtures."""
        key = (_to_uuid(organization_id), _to_uuid(mailbox_id), rfc822_message_id.strip())
        self.message_thread_map[key] = _to_uuid(thread_id)

    async def get_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> EmailThread | None:
        return self.threads.get((_to_uuid(organization_id), _to_uuid(thread_id)))

    async def find_by_provider_thread_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_thread_id: str,
    ) -> EmailThread | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)
        for thread in self.threads.values():
            if (
                thread.organization_id == org_u
                and thread.mailbox_id == mbx_u
                and thread.provider_thread_id == provider_thread_id
            ):
                return thread
        return None

    async def find_by_rfc822_message_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        rfc822_message_id: str,
    ) -> UUID | None:
        key = (_to_uuid(organization_id), _to_uuid(mailbox_id), rfc822_message_id.strip())
        return self.message_thread_map.get(key)

    async def find_by_references(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        references_ids: list[str],
    ) -> UUID | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)
        # Search from latest (rightmost) reference backwards
        for ref_id in reversed(references_ids):
            clean_ref = ref_id.strip()
            key = (org_u, mbx_u, clean_ref)
            if key in self.message_thread_map:
                return self.message_thread_map[key]
        return None

    async def find_by_subject_and_participants(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        subject_normalized: str,
        participants: list[str],
        window: timedelta,
        reference_time: datetime,
    ) -> EmailThread | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)
        subj_clean = subject_normalized.strip().lower()
        if not subj_clean:
            return None

        part_set = {p.strip().lower() for p in participants if p.strip()}
        cutoff = reference_time - window

        candidates: list[EmailThread] = []
        for thread in self.threads.values():
            if thread.organization_id != org_u or thread.mailbox_id != mbx_u:
                continue
            if thread.subject_normalized.strip().lower() != subj_clean:
                continue
            if thread.last_message_at < cutoff:
                continue
            thread_part_set = {p.strip().lower() for p in thread.participants}
            if part_set & thread_part_set:
                candidates.append(thread)

        if not candidates:
            return None
        # Return candidate with the most recent last_message_at
        candidates.sort(key=lambda t: t.last_message_at, reverse=True)
        return candidates[0]

    async def create_thread(
        self,
        thread: EmailThread,
    ) -> EmailThread:
        key = (thread.organization_id, thread.id)
        self.threads[key] = thread
        return thread

    async def update_thread_on_message(
        self,
        thread_id: UUID | str,
        organization_id: UUID | str,
        message_time: datetime,
        participants: list[str],
        provider_thread_id: str | None = None,
    ) -> EmailThread:
        key = (_to_uuid(organization_id), _to_uuid(thread_id))
        thread = self.threads.get(key)
        if not thread:
            raise KeyError(f"Thread {thread_id} not found in organization {organization_id}")

        new_count = thread.message_count + 1
        new_first = min(thread.first_message_at, message_time)
        new_last = max(thread.last_message_at, message_time)
        merged_participants = sorted(
            set(thread.participants) | {p.strip().lower() for p in participants if p.strip()}
        )
        updated_provider_thread_id = thread.provider_thread_id or provider_thread_id

        updated = EmailThread(
            id=thread.id,
            organization_id=thread.organization_id,
            mailbox_id=thread.mailbox_id,
            subject_normalized=thread.subject_normalized,
            provider_thread_id=updated_provider_thread_id,
            participants=merged_participants,
            first_message_at=new_first,
            last_message_at=new_last,
            message_count=new_count,
            status=thread.status,
        )
        self.threads[key] = updated
        return updated

    async def list_threads(
        self,
        organization_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
        mailbox_id: UUID | str | None = None,
        status: str | None = None,
    ) -> tuple[list[EmailThread], int]:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id) if mailbox_id is not None else None
        matched: list[EmailThread] = []
        for thread in self.threads.values():
            if thread.organization_id != org_u:
                continue
            if mbx_u is not None and thread.mailbox_id != mbx_u:
                continue
            if status is not None and thread.status != status:
                continue
            matched.append(thread)
        matched.sort(key=lambda t: t.last_message_at, reverse=True)
        total = len(matched)
        paginated = matched[offset : offset + limit]
        return paginated, total


class PostgresThreadStore:
    """PostgreSQL implementation of ThreadStore using asyncpg."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    def _row_to_thread(self, row: asyncpg.Record) -> EmailThread:
        return EmailThread(
            id=row["id"],
            organization_id=row["organization_id"],
            mailbox_id=row["mailbox_id"],
            provider_thread_id=row["provider_thread_id"],
            subject_normalized=row["subject_normalized"] or "",
            participants=list(row["participants"] or []),
            first_message_at=row["first_message_at"],
            last_message_at=row["last_message_at"],
            message_count=row["message_count"],
            status=row["status"],
        )

    async def get_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> EmailThread | None:
        org_u = _to_uuid(organization_id)
        thread_u = _to_uuid(thread_id)

        query = """
            SELECT id, organization_id, mailbox_id, provider_thread_id,
                   subject_normalized, participants, first_message_at, last_message_at,
                   message_count, status
            FROM email_thread
            WHERE id = $1 AND organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, thread_u, org_u)
            return self._row_to_thread(row) if row else None

    async def find_by_provider_thread_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_thread_id: str,
    ) -> EmailThread | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)

        query = """
            SELECT id, organization_id, mailbox_id, provider_thread_id,
                   subject_normalized, participants, first_message_at, last_message_at,
                   message_count, status
            FROM email_thread
            WHERE organization_id = $1 AND mailbox_id = $2 AND provider_thread_id = $3
            LIMIT 1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, org_u, mbx_u, provider_thread_id)
            return self._row_to_thread(row) if row else None

    async def find_by_rfc822_message_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        rfc822_message_id: str,
    ) -> UUID | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)

        query = """
            SELECT thread_id
            FROM email_message
            WHERE organization_id = $1 AND mailbox_id = $2 AND rfc822_message_id = $3
            LIMIT 1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, org_u, mbx_u, rfc822_message_id.strip())
            return _to_uuid(row["thread_id"]) if row else None

    async def find_by_references(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        references_ids: list[str],
    ) -> UUID | None:
        if not references_ids:
            return None

        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)
        clean_refs = [r.strip() for r in references_ids if r.strip()]
        if not clean_refs:
            return None

        query = """
            SELECT thread_id, rfc822_message_id
            FROM email_message
            WHERE organization_id = $1 AND mailbox_id = $2 AND rfc822_message_id = ANY($3);
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, org_u, mbx_u, clean_refs)
            if not rows:
                return None

            match_map = {row["rfc822_message_id"]: _to_uuid(row["thread_id"]) for row in rows}
            for ref in reversed(clean_refs):
                if ref in match_map:
                    return match_map[ref]
            return None

    async def find_by_subject_and_participants(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        subject_normalized: str,
        participants: list[str],
        window: timedelta,
        reference_time: datetime,
    ) -> EmailThread | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)
        subj_clean = subject_normalized.strip()
        clean_parts = [p.strip().lower() for p in participants if p.strip()]

        if not subj_clean or not clean_parts:
            return None

        cutoff = reference_time - window

        query = """
            SELECT id, organization_id, mailbox_id, provider_thread_id,
                   subject_normalized, participants, first_message_at, last_message_at,
                   message_count, status
            FROM email_thread
            WHERE organization_id = $1
              AND mailbox_id = $2
              AND LOWER(subject_normalized) = LOWER($3)
              AND participants && $4::text[]
              AND last_message_at >= $5
            ORDER BY last_message_at DESC
            LIMIT 1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, org_u, mbx_u, subj_clean, clean_parts, cutoff)
            return self._row_to_thread(row) if row else None

    async def create_thread(
        self,
        thread: EmailThread,
    ) -> EmailThread:
        query = """
            INSERT INTO email_thread (
                id, organization_id, mailbox_id, provider_thread_id,
                subject_normalized, participants, first_message_at, last_message_at,
                message_count, status
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
            )
            ON CONFLICT (id) DO UPDATE SET
                message_count = email_thread.message_count
            RETURNING id, organization_id, mailbox_id, provider_thread_id,
                      subject_normalized, participants, first_message_at, last_message_at,
                      message_count, status;
        """
        clean_parts = sorted({p.strip().lower() for p in thread.participants if p.strip()})
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                thread.id,
                thread.organization_id,
                thread.mailbox_id,
                thread.provider_thread_id,
                thread.subject_normalized,
                clean_parts,
                thread.first_message_at,
                thread.last_message_at,
                thread.message_count,
                thread.status,
            )
            return self._row_to_thread(row)

    async def update_thread_on_message(
        self,
        thread_id: UUID | str,
        organization_id: UUID | str,
        message_time: datetime,
        participants: list[str],
        provider_thread_id: str | None = None,
    ) -> EmailThread:
        thread_u = _to_uuid(thread_id)
        org_u = _to_uuid(organization_id)
        clean_parts = [p.strip().lower() for p in participants if p.strip()]

        query = """
            UPDATE email_thread
            SET
                message_count = message_count + 1,
                first_message_at = LEAST(first_message_at, $3),
                last_message_at = GREATEST(last_message_at, $3),
                participants = (
                    SELECT array_agg(DISTINCT p)
                    FROM unnest(participants || $4::text[]) AS p
                ),
                provider_thread_id = COALESCE(email_thread.provider_thread_id, $5)
            WHERE id = $1 AND organization_id = $2
            RETURNING id, organization_id, mailbox_id, provider_thread_id,
                      subject_normalized, participants, first_message_at, last_message_at,
                      message_count, status;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                thread_u,
                org_u,
                message_time,
                clean_parts,
                provider_thread_id,
            )
            if not row:
                raise KeyError(f"Thread {thread_id} not found in organization {organization_id}")
            return self._row_to_thread(row)

    async def list_threads(
        self,
        organization_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
        mailbox_id: UUID | str | None = None,
        status: str | None = None,
    ) -> tuple[list[EmailThread], int]:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id) if mailbox_id is not None else None

        count_query = """
            SELECT COUNT(*)
            FROM email_thread
            WHERE organization_id = $1
              AND ($2::uuid IS NULL OR mailbox_id = $2)
              AND ($3::text IS NULL OR status = $3);
        """
        data_query = """
            SELECT id, organization_id, mailbox_id, provider_thread_id,
                   subject_normalized, participants, first_message_at, last_message_at,
                   message_count, status
            FROM email_thread
            WHERE organization_id = $1
              AND ($2::uuid IS NULL OR mailbox_id = $2)
              AND ($3::text IS NULL OR status = $3)
            ORDER BY last_message_at DESC NULLS LAST, id ASC
            LIMIT $4 OFFSET $5;
        """
        async with self.pool.acquire() as conn:
            total_count = await conn.fetchval(count_query, org_u, mbx_u, status) or 0
            rows = await conn.fetch(data_query, org_u, mbx_u, status, limit, offset)
            threads = [self._row_to_thread(row) for row in rows]
            return threads, int(total_count)

"""Dispatch unit of work: claim, provider-draft handle and finish (design.md §5.8, ADR-0009).

Requirements:
- R17.3 / R19.2: the dispatch key ``key(org, mailbox, original provider_message_id,
  "dispatch")`` is claimed into ``generated_draft.dispatch_idempotency_key`` (UNIQUE) in the
  same transaction as ``DRAFTED | RETRY_PENDING -> DISPATCHED``.
- R17.4 / R17.7 / R18.5: finish writes the provider ref, draft ``dispatched``, the outbound
  ``email_message`` (send_reply mode only), the thread update and ``DISPATCHED -> COMPLETED``
  in one transaction.
- R18.7: the claim records ``queue_name = email.dispatch`` so an operator replay of a
  dead-lettered dispatch is routed back to the dispatch-worker, not to generation;
  ``set_dispatch_queue`` does the same for a failure before or during the claim.
- R19.3: ``job_lock`` serializes the deliveries of one job. Postgres holds a session-level
  advisory lock on a dedicated pool connection, so a crashed worker's session end
  releases it (and asyncpg's pool reset runs ``pg_advisory_unlock_all()`` on release).

Every statement is scoped by ``organization_id``. Rows are locked job first, then draft
(the same order as the review store), so approve and dispatch never deadlock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.db.draft import _row_to_draft, fetch_draft_for_job
from packages.db.job import JOB_SELECT_COLUMNS, InMemoryJobStore, PostgresJobStore
from packages.db.message import PostgresMessageStore, insert_message_on
from packages.db.thread import PostgresThreadStore, touch_thread_on
from packages.domain import DispatchMode
from packages.domain.entities import (
    EmailThread,
    GeneratedDraft,
    Job,
    Mailbox,
    NormalizedMessage,
)
from packages.domain.review import DraftStatus
from packages.domain.state_machine import JobState

_CLAIMABLE = frozenset({JobState.DRAFTED.value, JobState.RETRY_PENDING.value})
# Generation is done in these states, so a failed dispatch may take over the replay route.
_ROUTABLE_TO_DISPATCH = (
    JobState.DRAFTED.value,
    JobState.DISPATCHED.value,
    JobState.RETRY_PENDING.value,
    JobState.FAILED.value,
)


def _lock_key(organization_id: UUID, job_id: UUID) -> str:
    return f"dispatch:{organization_id}:{job_id}"


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class DispatchContext:
    """Everything one dispatch needs, loaded tenant-scoped."""

    job: Job
    draft: GeneratedDraft
    original: NormalizedMessage
    thread: EmailThread
    mailbox: Mailbox
    category: str | None


class ClaimStatus(StrEnum):
    """What step 1 found."""

    CLAIMED = "claimed"
    RESUMED = "resumed"
    ALREADY_COMPLETED = "already_completed"
    NOT_DISPATCHABLE = "not_dispatchable"


@dataclass(frozen=True)
class ClaimOutcome:
    """Step 1 result: the job and draft as they are after the claim transaction."""

    status: ClaimStatus
    job: Job
    draft: GeneratedDraft


class DispatchKeyConflictError(Exception):
    """The dispatch key is held by another draft, or this draft holds a different key."""


def _finish_payloads(
    draft_id: UUID, mode: DispatchMode, provider_ref: str, outbound_id: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    event = {
        "step": "finish",
        "dispatch_mode": mode.value,
        "provider_ref": provider_ref,
        "outbound_message_id": outbound_id,
    }
    result = {"draft_id": str(draft_id), "dispatch_mode": mode.value, "provider_ref": provider_ref}
    return event, result


def _claim_payload(draft_id: UUID, key: str, from_state: str) -> dict[str, Any]:
    return {
        "step": "claim",
        "draft_id": str(draft_id),
        "dispatch_idempotency_key": key,
        "operator_replay": from_state == JobState.RETRY_PENDING.value,
    }


@runtime_checkable
class DispatchStore(Protocol):
    """Persistence side of DispatchService; every method is tenant-scoped."""

    def job_lock(self, organization_id: UUID, job_id: UUID) -> AbstractAsyncContextManager[bool]:
        """Hold the per-job dispatch lock; yields False (without waiting) when it is taken."""
        ...

    async def set_dispatch_queue(
        self, *, organization_id: UUID, job_id: UUID, queue_name: str
    ) -> None:
        """Route an operator replay of this job to the dispatch-worker (R18.7)."""
        ...

    async def load(self, organization_id: UUID, job_id: UUID) -> DispatchContext | None:
        """Job, its draft, the original email, thread, mailbox and latest category."""
        ...

    async def claim(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        idempotency_key: str,
        queue_name: str,
    ) -> ClaimOutcome:
        """Step 1 in one transaction; raises DispatchKeyConflictError on a key clash."""
        ...

    async def record_provider_draft(
        self,
        *,
        organization_id: UUID,
        draft_id: UUID,
        provider_draft_id: str,
        provider_draft_message_id: str | None,
    ) -> GeneratedDraft:
        """Step 2: store the provider draft handle; the first stored handle wins."""
        ...

    async def finish(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        """Step 5 in one transaction; a COMPLETED job is returned unchanged."""
        ...


class PostgresDispatchStore:
    """PostgreSQL dispatch unit of work (asyncpg)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._jobs = PostgresJobStore(pool)
        self._messages = PostgresMessageStore(pool)
        self._threads = PostgresThreadStore(pool)

    @asynccontextmanager
    async def job_lock(self, organization_id: UUID, job_id: UUID) -> AsyncIterator[bool]:
        """Session-level ``pg_try_advisory_lock`` held on its own connection for the whole
        dispatch. Never waits: a busy lock yields False. A worker that dies ends its
        session, and PostgreSQL releases the lock with it."""
        key = _lock_key(organization_id, job_id)
        async with self._pool.acquire() as conn:
            held = bool(
                await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key)
            )
            try:
                yield held
            finally:
                if held:
                    await conn.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)

    async def set_dispatch_queue(
        self, *, organization_id: UUID, job_id: UUID, queue_name: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE processing_job SET queue_name = $3"
                " WHERE id = $1 AND organization_id = $2 AND state = ANY($4::text[])",
                job_id,
                organization_id,
                queue_name,
                list(_ROUTABLE_TO_DISPATCH),
            )

    async def load(self, organization_id: UUID, job_id: UUID) -> DispatchContext | None:
        async with self._pool.acquire() as conn:
            job_row = await conn.fetchrow(
                f"SELECT {JOB_SELECT_COLUMNS} FROM processing_job"
                " WHERE id = $1 AND organization_id = $2",
                job_id,
                organization_id,
            )
            if job_row is None:
                return None
            draft = await fetch_draft_for_job(conn, job_id, organization_id)
            if draft is None:
                return None
            mailbox_row = await conn.fetchrow(
                """
                SELECT b.id, b.organization_id, b.provider, b.address, b.display_name,
                       b.status, b.credentials_ref
                FROM mailbox b
                JOIN email_message m
                  ON m.mailbox_id = b.id AND m.organization_id = b.organization_id
                WHERE m.id = $1 AND m.organization_id = $2
                """,
                _to_uuid(draft.message_id),
                organization_id,
            )
            category = await conn.fetchval(
                """
                SELECT category FROM classification_result
                WHERE message_id = $1 AND organization_id = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                _to_uuid(draft.message_id),
                organization_id,
            )
        original = await self._messages.get_message(organization_id, draft.message_id)
        thread = await self._threads.get_thread(organization_id, draft.thread_id)
        if mailbox_row is None or original is None or thread is None:
            return None
        return DispatchContext(
            job=PostgresJobStore._row_to_job(job_row),  # noqa: SLF001
            draft=draft,
            original=original,
            thread=thread,
            mailbox=Mailbox(
                id=mailbox_row["id"],
                organization_id=mailbox_row["organization_id"],
                provider=mailbox_row["provider"],
                address=mailbox_row["address"],
                display_name=mailbox_row["display_name"],
                status=mailbox_row["status"],
                credentials_ref=mailbox_row["credentials_ref"],
            ),
            category=category,
        )

    async def claim(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        idempotency_key: str,
        queue_name: str,
    ) -> ClaimOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            job = await self._lock_job(conn, organization_id, job_id)
            draft = await fetch_draft_for_job(conn, job_id, organization_id)
            if draft is None or draft.id != draft_id:
                raise KeyError(f"Draft {draft_id} is not the draft of job {job_id}")
            if job.state == JobState.COMPLETED.value:
                return ClaimOutcome(ClaimStatus.ALREADY_COMPLETED, job, draft)
            if job.state == JobState.DISPATCHED.value:
                return ClaimOutcome(ClaimStatus.RESUMED, job, draft)
            if job.state not in _CLAIMABLE:
                return ClaimOutcome(ClaimStatus.NOT_DISPATCHABLE, job, draft)
            try:
                row = await conn.fetchrow(
                    """
                    UPDATE generated_draft SET dispatch_idempotency_key = $3
                    WHERE id = $1 AND organization_id = $2
                      AND (dispatch_idempotency_key IS NULL OR dispatch_idempotency_key = $3)
                    RETURNING *
                    """,
                    draft_id,
                    organization_id,
                    idempotency_key,
                )
            except asyncpg.UniqueViolationError as err:
                raise DispatchKeyConflictError(
                    f"Dispatch key of draft {draft_id} is already held by another draft"
                ) from err
            if row is None:
                raise DispatchKeyConflictError(f"Draft {draft_id} holds a different dispatch key")
            await conn.execute(
                "UPDATE processing_job SET queue_name = $3, lease_expires_at = NULL"
                " WHERE id = $1 AND organization_id = $2",
                job_id,
                organization_id,
                queue_name,
            )
            claimed, _ = await self._jobs.transition_job_state_on(
                conn,
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.DISPATCHED,
                payload=_claim_payload(draft_id, idempotency_key, job.state),
            )
            return ClaimOutcome(ClaimStatus.CLAIMED, claimed, _row_to_draft(row))

    async def record_provider_draft(
        self,
        *,
        organization_id: UUID,
        draft_id: UUID,
        provider_draft_id: str,
        provider_draft_message_id: str | None,
    ) -> GeneratedDraft:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE generated_draft
                SET provider_draft_id = $3, provider_draft_message_id = $4
                WHERE id = $1 AND organization_id = $2 AND provider_draft_id IS NULL
                RETURNING *
                """,
                draft_id,
                organization_id,
                provider_draft_id,
                provider_draft_message_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM generated_draft WHERE id = $1 AND organization_id = $2",
                    draft_id,
                    organization_id,
                )
            if row is None:
                raise KeyError(f"Draft {draft_id} not found for organization {organization_id}")
            return _row_to_draft(row)

    async def finish(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        async with self._pool.acquire() as conn, conn.transaction():
            job = await self._lock_job(conn, organization_id, job_id)
            if job.state == JobState.COMPLETED.value:
                return job
            await conn.execute(
                "UPDATE generated_draft SET status = $3, provider_ref = $4"
                " WHERE id = $1 AND organization_id = $2",
                draft_id,
                organization_id,
                DraftStatus.DISPATCHED.value,
                provider_ref,
            )
            outbound_id: str | None = None
            if outbound is not None and await insert_message_on(conn, outbound):
                outbound_id = str(outbound.message_id)
                await touch_thread_on(
                    conn,
                    thread_id=_to_uuid(outbound.thread_id),
                    organization_id=organization_id,
                    message_time=outbound.received_at,
                    participants=[outbound.sender.email, *(r.email for r in outbound.recipients)],
                )
            event, result = _finish_payloads(draft_id, mode, provider_ref, outbound_id)
            done, _ = await self._jobs.transition_job_state_on(
                conn,
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.COMPLETED,
                payload=event,
                result_ref=result,
            )
            return done

    async def _lock_job(self, conn: Any, organization_id: UUID, job_id: UUID) -> Job:
        row = await conn.fetchrow(
            f"SELECT {JOB_SELECT_COLUMNS} FROM processing_job"
            " WHERE id = $1 AND organization_id = $2 FOR UPDATE",
            job_id,
            organization_id,
        )
        if row is None:
            raise KeyError(f"Job {job_id} not found for organization {organization_id}")
        return PostgresJobStore._row_to_job(row)  # noqa: SLF001


class InMemoryDispatchStore:
    """In-memory DispatchStore for unit tests; validates each transition before storing."""

    def __init__(self, job_store: InMemoryJobStore | None = None) -> None:
        self.jobs = job_store or InMemoryJobStore()
        self.drafts: dict[UUID, GeneratedDraft] = {}
        self.messages: dict[UUID, NormalizedMessage] = {}
        self.threads: dict[UUID, EmailThread] = {}
        self.mailboxes: dict[UUID, Mailbox] = {}
        self.categories: dict[UUID, str] = {}
        self.outbound: list[NormalizedMessage] = []
        self._lock = asyncio.Lock()
        self._job_locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}

    @asynccontextmanager
    async def job_lock(self, organization_id: UUID, job_id: UUID) -> AsyncIterator[bool]:
        lock = self._job_locks.setdefault((organization_id, job_id), asyncio.Lock())
        if lock.locked():
            yield False
            return
        async with lock:
            yield True

    async def set_dispatch_queue(
        self, *, organization_id: UUID, job_id: UUID, queue_name: str
    ) -> None:
        job = await self.jobs.get_job(organization_id, job_id)
        if job is not None and job.state in _ROUTABLE_TO_DISPATCH:
            job.queue_name = queue_name

    def add_mailbox(self, mailbox: Mailbox) -> None:
        self.mailboxes[_to_uuid(mailbox.id)] = mailbox

    def add_thread(self, thread: EmailThread) -> None:
        self.threads[thread.id] = thread

    def add_message(self, message: NormalizedMessage) -> None:
        self.messages[_to_uuid(message.message_id)] = message

    def add_draft(self, draft: GeneratedDraft) -> None:
        self.drafts[draft.id] = draft

    def set_category(self, message_id: UUID | str, category: str) -> None:
        self.categories[_to_uuid(message_id)] = category

    def _draft_for_job(self, organization_id: UUID, job_id: UUID) -> GeneratedDraft | None:
        mine = [
            d
            for d in self.drafts.values()
            if d.job_id is not None
            and str(d.job_id) == str(job_id)
            and _to_uuid(d.organization_id) == organization_id
        ]
        return max(mine, key=lambda d: d.created_at) if mine else None

    async def load(self, organization_id: UUID, job_id: UUID) -> DispatchContext | None:
        job = await self.jobs.get_job(organization_id, job_id)
        draft = self._draft_for_job(organization_id, job_id)
        if job is None or draft is None:
            return None
        original = self.messages.get(_to_uuid(draft.message_id))
        thread = self.threads.get(_to_uuid(draft.thread_id))
        if original is None or thread is None or thread.organization_id != organization_id:
            return None
        mailbox = self.mailboxes.get(_to_uuid(original.mailbox_id))
        if mailbox is None or _to_uuid(mailbox.organization_id) != organization_id:
            return None
        return DispatchContext(
            # A snapshot, as Postgres returns: InMemoryJobStore transitions the stored Job in
            # place, and the service reads the state it loaded (DRAFTED vs a redelivery).
            job=replace(job),
            draft=draft,
            original=original,
            thread=thread,
            mailbox=mailbox,
            category=self.categories.get(_to_uuid(original.message_id)),
        )

    async def claim(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        idempotency_key: str,
        queue_name: str,
    ) -> ClaimOutcome:
        async with self._lock:
            job = await self.jobs.get_job(organization_id, job_id)
            draft = self._draft_for_job(organization_id, job_id)
            if job is None or draft is None or draft.id != draft_id:
                raise KeyError(f"Job {job_id} with draft {draft_id} not found")
            if job.state == JobState.COMPLETED.value:
                return ClaimOutcome(ClaimStatus.ALREADY_COMPLETED, job, draft)
            if job.state == JobState.DISPATCHED.value:
                return ClaimOutcome(ClaimStatus.RESUMED, job, draft)
            if job.state not in _CLAIMABLE:
                return ClaimOutcome(ClaimStatus.NOT_DISPATCHABLE, job, draft)
            if draft.dispatch_idempotency_key not in (None, idempotency_key) or any(
                other.id != draft.id and other.dispatch_idempotency_key == idempotency_key
                for other in self.drafts.values()
            ):
                raise DispatchKeyConflictError(f"Dispatch key of draft {draft_id} is taken")
            from_state = job.state
            claimed, _ = await self.jobs.transition_job_state(
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.DISPATCHED,
                payload=_claim_payload(draft_id, idempotency_key, from_state),
            )
            claimed.queue_name = queue_name
            claimed.lease_expires_at = None
            stored = replace(draft, dispatch_idempotency_key=idempotency_key)
            self.drafts[stored.id] = stored
            return ClaimOutcome(ClaimStatus.CLAIMED, claimed, stored)

    async def record_provider_draft(
        self,
        *,
        organization_id: UUID,
        draft_id: UUID,
        provider_draft_id: str,
        provider_draft_message_id: str | None,
    ) -> GeneratedDraft:
        async with self._lock:
            draft = self.drafts.get(draft_id)
            if draft is None or _to_uuid(draft.organization_id) != organization_id:
                raise KeyError(f"Draft {draft_id} not found for organization {organization_id}")
            if draft.provider_draft_id is None:
                draft = replace(
                    draft,
                    provider_draft_id=provider_draft_id,
                    provider_draft_message_id=provider_draft_message_id,
                )
                self.drafts[draft_id] = draft
            return draft

    async def finish(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        async with self._lock:
            job = await self.jobs.get_job(organization_id, job_id)
            if job is None:
                raise KeyError(f"Job {job_id} not found for organization {organization_id}")
            if job.state == JobState.COMPLETED.value:
                return job
            duplicate = outbound is not None and any(
                m.provider_message_id == outbound.provider_message_id
                and str(m.mailbox_id) == str(outbound.mailbox_id)
                for m in self.messages.values()
            )
            outbound_id = (
                str(outbound.message_id) if outbound is not None and not duplicate else None
            )
            event, result = _finish_payloads(draft_id, mode, provider_ref, outbound_id)
            # Transition first: it raises on an illegal state before anything is stored.
            done, _ = await self.jobs.transition_job_state(
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.COMPLETED,
                payload=event,
                result_ref=result,
            )
            self.drafts[draft_id] = replace(
                self.drafts[draft_id],
                status=DraftStatus.DISPATCHED.value,
                provider_ref=provider_ref,
            )
            if outbound is not None and not duplicate:
                self.messages[_to_uuid(outbound.message_id)] = outbound
                self.outbound.append(outbound)
                thread = self.threads[_to_uuid(outbound.thread_id)]
                self.threads[thread.id] = replace(
                    thread,
                    message_count=thread.message_count + 1,
                    last_message_at=max(thread.last_message_at, outbound.received_at),
                    participants=sorted(
                        set(thread.participants)
                        | {outbound.sender.email.lower()}
                        | {r.email.lower() for r in outbound.recipients}
                    ),
                )
            return done

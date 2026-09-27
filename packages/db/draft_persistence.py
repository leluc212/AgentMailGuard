"""Persist a generated draft together with the job's GENERATING -> DRAFTED transition.

Requirements: R16.4 (persist every draft), R18.1 (DRAFTED state), R18.4 (event row),
R18.5 (state change in the same transaction as its side effect), R19.4 / R19.7
(one draft per job across redeliveries; design.md §9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.db.draft import (
    InMemoryDraftStore,
    fetch_draft_for_job,
    insert_draft,
)
from packages.db.job import InMemoryJobStore, PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job, ProcessingEvent
from packages.domain.state_machine import JobState


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class DraftPersistOutcome:
    """Result of persisting a draft.

    ``created`` is False (and ``event`` None) when an earlier delivery of the same job
    already persisted its draft; ``draft`` is then that earlier draft.
    """

    draft: GeneratedDraft
    job: Job
    event: ProcessingEvent | None
    created: bool


def drafted_event_payload(draft: GeneratedDraft) -> dict[str, Any]:
    """Audit payload for the GENERATING -> DRAFTED ``processing_event`` (R18.4)."""
    return {
        "draft_id": str(draft.id),
        "model": draft.model_name,
        "model_tier": draft.model_tier,
        "escalation_reason": draft.escalation_reason,
        "prompt_version": draft.prompt_version,
        "input_tokens": draft.input_tokens,
        "output_tokens": draft.output_tokens,
        "cost_estimate": draft.cost_estimate,
        "citation_mismatch": draft.citation_mismatch,
    }


def _require_job_id(draft: GeneratedDraft) -> UUID:
    if draft.job_id is None or str(draft.job_id) == "":
        raise ValueError(f"Draft {draft.id} has no job_id; generated drafts belong to a job")
    return _to_uuid(draft.job_id)


@runtime_checkable
class DraftPersistence(Protocol):
    """Unit of work persisting a draft and transitioning its job to DRAFTED."""

    async def persist_drafted(self, draft: GeneratedDraft) -> DraftPersistOutcome:
        """Insert ``draft`` and move its job GENERATING -> DRAFTED atomically.

        Raises:
            ValueError: If the draft has no ``job_id``.
            KeyError: If the job does not exist in the draft's organization.
            IllegalStateTransitionError: If the job is not in GENERATING (and not
                already DRAFTED with a draft). Nothing is persisted.
        """
        ...

    async def find_draft_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GeneratedDraft | None:
        """Return the job's persisted draft, or ``None``."""
        ...


class PostgresDraftPersistence:
    """PostgreSQL unit of work: row lock, draft insert and transition on one connection."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._jobs = PostgresJobStore(pool)

    async def persist_drafted(self, draft: GeneratedDraft) -> DraftPersistOutcome:
        job_id = _require_job_id(draft)
        org_id = _to_uuid(draft.organization_id)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, organization_id, message_id, thread_id, job_type, state,
                       attempt, max_attempts, idempotency_key, result_ref, queue_name,
                       priority, lease_expires_at, last_error, next_retry_at, trace_id,
                       created_at, updated_at
                FROM processing_job
                WHERE id = $1 AND organization_id = $2
                FOR UPDATE;
                """,
                job_id,
                org_id,
            )
            if row is None:
                raise KeyError(f"Job {job_id} not found for organization {org_id}")
            if row["state"] == JobState.DRAFTED.value:
                existing = await fetch_draft_for_job(conn, job_id, org_id)
                if existing is not None:
                    return DraftPersistOutcome(
                        draft=existing,
                        job=PostgresJobStore._row_to_job(row),  # noqa: SLF001
                        event=None,
                        created=False,
                    )
            stored = await insert_draft(conn, draft)
            job, event = await self._jobs.transition_job_state_on(
                conn,
                organization_id=org_id,
                job_id=job_id,
                target_state=JobState.DRAFTED,
                payload=drafted_event_payload(stored),
                result_ref={"draft_id": str(stored.id)},
                message_id=stored.message_id,
                thread_id=stored.thread_id,
            )
            return DraftPersistOutcome(draft=stored, job=job, event=event, created=True)

    async def find_draft_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GeneratedDraft | None:
        async with self._pool.acquire() as conn:
            return await fetch_draft_for_job(conn, _to_uuid(job_id), _to_uuid(organization_id))


class InMemoryDraftPersistence:
    """In-memory unit of work for unit tests; validates the transition before storing."""

    def __init__(self, job_store: InMemoryJobStore, draft_store: InMemoryDraftStore) -> None:
        self._jobs = job_store
        self._drafts = draft_store

    async def persist_drafted(self, draft: GeneratedDraft) -> DraftPersistOutcome:
        job_id = _require_job_id(draft)
        org_id = _to_uuid(draft.organization_id)
        job = await self._jobs.get_job(org_id, job_id)
        if job is None:
            raise KeyError(f"Job {job_id} not found for organization {org_id}")
        if job.state == JobState.DRAFTED.value:
            existing = await self.find_draft_for_job(org_id, job_id)
            if existing is not None:
                return DraftPersistOutcome(draft=existing, job=job, event=None, created=False)
        # Transition first: it raises on an illegal state before anything is stored.
        job, event = await self._jobs.transition_job_state(
            organization_id=org_id,
            job_id=job_id,
            target_state=JobState.DRAFTED,
            payload=drafted_event_payload(draft),
            result_ref={"draft_id": str(draft.id)},
            message_id=draft.message_id,
            thread_id=draft.thread_id,
        )
        stored = await self._drafts.create_draft(draft)
        return DraftPersistOutcome(draft=stored, job=job, event=event, created=True)

    async def find_draft_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GeneratedDraft | None:
        drafts = await self._drafts.list_drafts_for_job(job_id, organization_id)
        return drafts[0] if drafts else None

"""Processing job and event persistence store (R5.2, R7.3, R18.1–R18.5, R19.2, R19.4).

Provides authoritative lifecycle management for asynchronous processing jobs,
atomic state machine transitions, deterministic idempotency deduplication,
and chronological processing event telemetry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import Job, ProcessingEvent
from packages.domain.state_machine import JobState, transition_job

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _opt_uuid(val: UUID | str | None) -> UUID | None:
    if val is None or val == "":
        return None
    return val if isinstance(val, UUID) else UUID(str(val))


def _to_json_val(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    return str(val)


def _from_json_val(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


@runtime_checkable
class JobStore(Protocol):
    """Protocol for asynchronous job and telemetry event operations."""

    async def create_job(
        self,
        job: Job,
        initial_event_payload: dict[str, Any] | None = None,
    ) -> tuple[Job, bool]:
        """Create a new job row and matching initial ProcessingEvent (R18.1, R18.4, R18.5).

        Returns:
            (job, is_new): If a job with the same idempotency_key already exists,
            returns the existing job with is_new=False. Otherwise returns the newly
            created job with is_new=True.
        """
        ...

    async def get_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
    ) -> Job | None:
        """Fetch job by ID with strict organization scoping."""
        ...

    async def get_job_by_idempotency_key(
        self,
        organization_id: UUID | str,
        idempotency_key: str,
    ) -> Job | None:
        """Fetch job by deterministic idempotency key with organization scoping."""
        ...

    async def transition_job_state(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
        target_state: JobState | str,
        payload: dict[str, Any] | None = None,
        result_ref: dict[str, Any] | None = None,
        error_message: str | None = None,
        message_id: UUID | str | None = None,
        thread_id: UUID | str | None = None,
    ) -> tuple[Job, ProcessingEvent]:
        """Atomically transition job state and record event (R18.3–R18.5)."""
        ...

    async def list_events_for_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[ProcessingEvent]:
        """List chronological events for a message (R18.6)."""
        ...

    async def list_events_for_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
    ) -> list[ProcessingEvent]:
        """List chronological events for a job."""
        ...


class PostgresJobStore(JobStore):
    """PostgreSQL implementation of JobStore backed by asyncpg connection pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create_job(
        self,
        job: Job,
        initial_event_payload: dict[str, Any] | None = None,
    ) -> tuple[Job, bool]:
        """Insert a new processing_job and initial processing_event (R18.4, R18.5).

        Enforces database uniqueness on idempotency_key (R19.4). If already present,
        returns the existing row with is_new=False.
        """
        org_id = _to_uuid(job.organization_id)
        job_id = _to_uuid(job.id)
        msg_id = _opt_uuid(job.message_id)
        thd_id = _opt_uuid(job.thread_id)
        now = datetime.now(UTC)

        insert_job_query = """
            INSERT INTO processing_job (
                id, organization_id, message_id, thread_id, job_type, state,
                attempt, max_attempts, idempotency_key, result_ref, queue_name,
                priority, lease_expires_at, last_error, next_retry_at, trace_id,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10::jsonb, $11,
                $12, $13, $14, $15, $16,
                $17, $18
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, organization_id, message_id, thread_id, job_type, state,
                      attempt, max_attempts, idempotency_key, result_ref, queue_name,
                      priority, lease_expires_at, last_error, next_retry_at, trace_id,
                      created_at, updated_at;
        """

        insert_event_query = """
            INSERT INTO processing_event (
                job_id, message_id, organization_id, event_type,
                state_from, state_to, payload, trace_id, created_at
            ) VALUES (
                $1, $2, $3, 'state_transition',
                NULL, $4, $5::jsonb, $6, $7
            );
        """

        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                insert_job_query,
                job_id,
                org_id,
                msg_id,
                thd_id,
                job.job_type,
                job.state,
                job.attempt,
                job.max_attempts,
                job.idempotency_key,
                _to_json_val(job.result_ref),
                job.queue_name,
                job.priority,
                job.lease_expires_at,
                job.last_error,
                job.next_retry_at,
                job.trace_id,
                now,
                now,
            )

            if row is not None:
                # Successfully inserted new job; record initial processing_event
                await conn.execute(
                    insert_event_query,
                    job_id,
                    msg_id,
                    org_id,
                    job.state,
                    _to_json_val(initial_event_payload or {}),
                    job.trace_id,
                    now,
                )
                created = self._row_to_job(row)
                logger.info(
                    "Created processing_job %s (state=%s, idempotency_key=%s)",
                    created.id,
                    created.state,
                    created.idempotency_key,
                )
                return created, True

            # Duplicate idempotency_key detected; load existing job
            existing = await self.get_job_by_idempotency_key(org_id, job.idempotency_key)
            if existing is None:
                # Fallback lookup by key without org if cross-org conflict
                query_fallback = """
                        SELECT id, organization_id, message_id, thread_id, job_type, state,
                               attempt, max_attempts, idempotency_key, result_ref, queue_name,
                               priority, lease_expires_at, last_error, next_retry_at, trace_id,
                               created_at, updated_at
                        FROM processing_job
                        WHERE idempotency_key = $1;
                    """
                fb_row = await conn.fetchrow(query_fallback, job.idempotency_key)
                assert fb_row is not None, "Conflict occurred but row not found"
                existing = self._row_to_job(fb_row)

            logger.info(
                "Job creation skipped: existing job found for idempotency_key=%s (id=%s, state=%s)",
                job.idempotency_key,
                existing.id,
                existing.state,
            )
            return existing, False

    async def get_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
    ) -> Job | None:
        """Fetch job by ID with strict organization scoping."""
        org_u = _to_uuid(organization_id)
        job_u = _to_uuid(job_id)

        query = """
            SELECT id, organization_id, message_id, thread_id, job_type, state,
                   attempt, max_attempts, idempotency_key, result_ref, queue_name,
                   priority, lease_expires_at, last_error, next_retry_at, trace_id,
                   created_at, updated_at
            FROM processing_job
            WHERE id = $1 AND organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, job_u, org_u)
            if row is None:
                return None
            return self._row_to_job(row)

    async def get_job_by_idempotency_key(
        self,
        organization_id: UUID | str,
        idempotency_key: str,
    ) -> Job | None:
        """Fetch job by deterministic idempotency key with organization scoping."""
        org_u = _to_uuid(organization_id)

        query = """
            SELECT id, organization_id, message_id, thread_id, job_type, state,
                   attempt, max_attempts, idempotency_key, result_ref, queue_name,
                   priority, lease_expires_at, last_error, next_retry_at, trace_id,
                   created_at, updated_at
            FROM processing_job
            WHERE idempotency_key = $1 AND organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, idempotency_key, org_u)
            if row is None:
                return None
            return self._row_to_job(row)

    async def transition_job_state(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
        target_state: JobState | str,
        payload: dict[str, Any] | None = None,
        result_ref: dict[str, Any] | None = None,
        error_message: str | None = None,
        message_id: UUID | str | None = None,
        thread_id: UUID | str | None = None,
    ) -> tuple[Job, ProcessingEvent]:
        """Atomically transition job state and record event (R18.3–R18.5)."""
        org_u = _to_uuid(organization_id)
        job_u = _to_uuid(job_id)
        msg_u = _opt_uuid(message_id)
        thd_u = _opt_uuid(thread_id)

        select_for_update = """
            SELECT id, organization_id, message_id, thread_id, job_type, state,
                   attempt, max_attempts, idempotency_key, result_ref, queue_name,
                   priority, lease_expires_at, last_error, next_retry_at, trace_id,
                   created_at, updated_at
            FROM processing_job
            WHERE id = $1 AND organization_id = $2
            FOR UPDATE;
        """

        update_query = """
            UPDATE processing_job
            SET state = $1,
                updated_at = $2,
                result_ref = COALESCE($3::jsonb, result_ref),
                last_error = COALESCE($4, last_error),
                message_id = COALESCE($5, message_id),
                thread_id = COALESCE($6, thread_id)
            WHERE id = $7 AND organization_id = $8
            RETURNING id, organization_id, message_id, thread_id, job_type, state,
                      attempt, max_attempts, idempotency_key, result_ref, queue_name,
                      priority, lease_expires_at, last_error, next_retry_at, trace_id,
                      created_at, updated_at;
        """

        insert_event_query = """
            INSERT INTO processing_event (
                job_id, message_id, organization_id, event_type,
                state_from, state_to, payload, trace_id, created_at
            ) VALUES (
                $1, $2, $3, 'state_transition',
                $4, $5, $6::jsonb, $7, $8
            )
            RETURNING id, job_id, message_id, organization_id, event_type,
                      state_from, state_to, payload, trace_id, created_at;
        """

        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(select_for_update, job_u, org_u)
            if row is None:
                raise KeyError(f"Job {job_id} not found for organization {organization_id}")

            current_job = self._row_to_job(row)
            if msg_u is not None:
                current_job.message_id = msg_u
            if thd_u is not None:
                current_job.thread_id = thd_u

            # Execute state machine validation & event generation (R18.3, R18.4)
            updated_job, event = transition_job(
                job=current_job,
                target_state=target_state,
                payload=payload,
                trace_id=current_job.trace_id,
            )

            now = datetime.now(UTC)
            upd_row = await conn.fetchrow(
                update_query,
                updated_job.state,
                now,
                _to_json_val(result_ref),
                error_message,
                msg_u,
                thd_u,
                job_u,
                org_u,
            )
            assert upd_row is not None

            final_job = self._row_to_job(upd_row)
            target_msg_id = final_job.message_id or event.message_id

            ev_row = await conn.fetchrow(
                insert_event_query,
                job_u,
                _opt_uuid(target_msg_id),
                org_u,
                event.state_from,
                event.state_to,
                _to_json_val(event.payload),
                event.trace_id,
                event.created_at,
            )
            assert ev_row is not None
            persisted_event = self._row_to_event(ev_row)

            logger.info(
                "Transitioned job %s: %s -> %s (event_id=%s)",
                job_u,
                event.state_from,
                event.state_to,
                persisted_event.id,
            )
            return final_job, persisted_event

    async def list_events_for_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[ProcessingEvent]:
        """List chronological events for a message (R18.6)."""
        org_u = _to_uuid(organization_id)
        msg_u = _to_uuid(message_id)

        query = """
            SELECT id, job_id, message_id, organization_id, event_type,
                   state_from, state_to, payload, trace_id, created_at
            FROM processing_event
            WHERE message_id = $1 AND organization_id = $2
            ORDER BY created_at ASC, id ASC;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, msg_u, org_u)
            return [self._row_to_event(r) for r in rows]

    async def list_events_for_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
    ) -> list[ProcessingEvent]:
        """List chronological events for a job."""
        org_u = _to_uuid(organization_id)
        job_u = _to_uuid(job_id)

        query = """
            SELECT id, job_id, message_id, organization_id, event_type,
                   state_from, state_to, payload, trace_id, created_at
            FROM processing_event
            WHERE job_id = $1 AND organization_id = $2
            ORDER BY created_at ASC, id ASC;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, job_u, org_u)
            return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_job(row: asyncpg.Record) -> Job:
        return Job(
            id=row["id"],
            organization_id=row["organization_id"],
            message_id=row["message_id"],
            thread_id=row["thread_id"],
            job_type=row["job_type"],
            state=row["state"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            idempotency_key=row["idempotency_key"],
            result_ref=_from_json_val(row["result_ref"]),
            queue_name=row["queue_name"],
            priority=row["priority"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            next_retry_at=row["next_retry_at"],
            trace_id=row["trace_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_event(row: asyncpg.Record) -> ProcessingEvent:
        return ProcessingEvent(
            id=row["id"],
            job_id=row["job_id"],
            message_id=row["message_id"],
            organization_id=row["organization_id"],
            event_type=row["event_type"],
            state_from=row["state_from"],
            state_to=row["state_to"],
            payload=_from_json_val(row["payload"]) or {},
            trace_id=row["trace_id"],
            created_at=row["created_at"],
        )


class InMemoryJobStore(JobStore):
    """In-memory implementation of JobStore for deterministic unit testing."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._idempotency_map: dict[str, str] = {}  # idempotency_key -> job_id
        self._events: list[ProcessingEvent] = []
        self._event_id_seq = 1
        self._lock = asyncio.Lock()

    async def create_job(
        self,
        job: Job,
        initial_event_payload: dict[str, Any] | None = None,
    ) -> tuple[Job, bool]:
        async with self._lock:
            key = job.idempotency_key
            if key in self._idempotency_map:
                existing_id = self._idempotency_map[key]
                existing_job = self._jobs[existing_id]
                return existing_job, False

            job_id_str = str(job.id)
            self._jobs[job_id_str] = job
            self._idempotency_map[key] = job_id_str

            event = ProcessingEvent(
                id=self._event_id_seq,
                job_id=job.id,
                message_id=job.message_id,
                organization_id=job.organization_id,
                event_type="state_transition",
                state_from=None,
                state_to=job.state,
                payload=initial_event_payload or {},
                trace_id=job.trace_id,
                created_at=datetime.now(UTC),
            )
            self._event_id_seq += 1
            self._events.append(event)
            return job, True

    async def get_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
    ) -> Job | None:
        async with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return None
            if str(job.organization_id) != str(organization_id):
                return None
            return job

    async def get_job_by_idempotency_key(
        self,
        organization_id: UUID | str,
        idempotency_key: str,
    ) -> Job | None:
        async with self._lock:
            job_id_str = self._idempotency_map.get(idempotency_key)
            if not job_id_str:
                return None
            job = self._jobs.get(job_id_str)
            if job is None:
                return None
            if str(job.organization_id) != str(organization_id):
                return None
            return job

    async def transition_job_state(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
        target_state: JobState | str,
        payload: dict[str, Any] | None = None,
        result_ref: dict[str, Any] | None = None,
        error_message: str | None = None,
        message_id: UUID | str | None = None,
        thread_id: UUID | str | None = None,
    ) -> tuple[Job, ProcessingEvent]:
        async with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None or str(job.organization_id) != str(organization_id):
                raise KeyError(f"Job {job_id} not found for organization {organization_id}")

            if message_id is not None:
                job.message_id = message_id
            if thread_id is not None:
                job.thread_id = thread_id

            updated_job, raw_event = transition_job(
                job=job,
                target_state=target_state,
                payload=payload,
                trace_id=job.trace_id,
            )

            if result_ref is not None:
                updated_job.result_ref = result_ref
            if error_message is not None:
                updated_job.last_error = error_message

            event = ProcessingEvent(
                id=self._event_id_seq,
                job_id=raw_event.job_id,
                message_id=raw_event.message_id or updated_job.message_id,
                organization_id=raw_event.organization_id,
                event_type=raw_event.event_type,
                state_from=raw_event.state_from,
                state_to=raw_event.state_to,
                payload=raw_event.payload,
                trace_id=raw_event.trace_id,
                created_at=raw_event.created_at,
            )
            self._event_id_seq += 1
            self._events.append(event)
            return updated_job, event

    async def list_events_for_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[ProcessingEvent]:
        async with self._lock:
            org_str = str(organization_id)
            msg_str = str(message_id)
            return [
                e
                for e in self._events
                if str(e.organization_id) == org_str and str(e.message_id) == msg_str
            ]

    async def list_events_for_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
    ) -> list[ProcessingEvent]:
        async with self._lock:
            org_str = str(organization_id)
            job_str = str(job_id)
            return [
                e
                for e in self._events
                if str(e.organization_id) == org_str and str(e.job_id) == job_str
            ]

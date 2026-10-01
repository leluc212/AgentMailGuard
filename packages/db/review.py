"""Draft review persistence: list, read, edit, approve and reject with feedback rows.

Requirements:
- R16.6: list (keyset-paginated), read, edit, approve and reject drafts, org-scoped (R23.6).
- R16.7: one ``feedback`` row per draft (``UNIQUE (draft_id)``, migration 0005) with the
  decision, edited body, character edit distance, optional rating, a free-text reviewer
  label and ``review_ms``.
- R18.4 / R18.5: reject moves the job DRAFTED -> COMPLETED in the same transaction as the
  draft status and its feedback row.
- design.md §5.8 (review API, feedback), ADR-0009.

Locks are taken job first, then draft: the same order as the dispatch claim and finish
(packages/db/dispatch.py), so a repeated approve racing a dispatch cannot deadlock.

The generated body is kept for the edit distance: the first PATCH of a draft writes a
``draft_edited`` processing_event whose payload holds ``original_body``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import asyncpg

from packages.core.pagination import encode_cursor
from packages.db.draft import _row_to_draft
from packages.db.job import JOB_SELECT_COLUMNS, InMemoryJobStore, PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.domain.entities import GeneratedDraft, Job, NormalizedMessage
from packages.domain.review import (
    DraftStatus,
    ReviewVerdict,
    approval_verdict,
    rejection_verdict,
)
from packages.domain.state_machine import JobState

DRAFT_EDITED_EVENT = "draft_edited"
"""``processing_event.event_type`` of a reviewer edit; the first one carries original_body."""

_DECIDED_APPROVED = frozenset({DraftStatus.APPROVED.value, DraftStatus.DISPATCHED.value})


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class DraftView:
    """A draft with what the review queue shows next to it."""

    draft: GeneratedDraft
    mailbox_id: UUID
    original_subject: str
    sender_email: str
    original_provider_message_id: str
    category: str | None
    job_state: str | None


@dataclass(frozen=True)
class CitedChunk:
    """A knowledge chunk the draft cites, with its text (R16.5, R23.4)."""

    citation_id: str
    chunk_id: str
    document_id: str | None
    external_id: str | None
    content: str
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessFactView:
    """One ``[BUSINESS DATA]`` fact status as stored on the CONTEXT_READY event (R13.5)."""

    entity: str
    reference: str | None
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class BusinessDataView:
    """Customer status and fact statuses the draft was generated with (R13.5, R13.7)."""

    customer_status: str | None
    degraded: bool
    facts: tuple[BusinessFactView, ...] = ()

    @classmethod
    def from_context_payload(cls, payload: Mapping[str, Any] | None) -> BusinessDataView | None:
        """Read the payload written by ``packages.business.fetch.business_payload``."""
        if not payload or "business_fact_statuses" not in payload:
            return None
        facts = tuple(
            BusinessFactView(
                entity=str(fact.get("entity", "")),
                reference=fact.get("reference"),
                status=str(fact.get("status", "")),
                reason=fact.get("reason"),
            )
            for fact in payload.get("business_fact_statuses") or []
            if isinstance(fact, Mapping)
        )
        customer_status = payload.get("customer_status")
        return cls(
            customer_status=str(customer_status) if customer_status is not None else None,
            degraded=bool(payload.get("business_data_degraded", False)),
            facts=facts,
        )


@dataclass(frozen=True)
class FeedbackRecord:
    """One ``feedback`` row (R16.7)."""

    id: UUID
    organization_id: UUID
    draft_id: UUID
    decision: str
    edited_body: str | None
    edit_distance: int | None
    rating: int | None
    reviewer: str | None
    review_ms: int | None
    comment: str | None
    created_at: datetime


@dataclass(frozen=True)
class DraftDetail:
    """Everything the review screen needs for one draft (tasks 6.1, 6.8)."""

    view: DraftView
    original: NormalizedMessage
    thread_summary: str | None
    cited_chunks: tuple[CitedChunk, ...]
    business_data: BusinessDataView | None
    feedback: FeedbackRecord | None


@dataclass(frozen=True)
class DraftPage:
    """One keyset page, newest first; ``next_cursor`` is None on the last page."""

    items: tuple[DraftView, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ReviewInput:
    """Reviewer-supplied fields of a decision; ``review_ms`` is measured by the client."""

    reviewer: str | None = None
    rating: int | None = None
    review_ms: int | None = None
    comment: str | None = None


@dataclass(frozen=True)
class DecisionOutcome:
    """Result of approve/reject. ``created`` is False for a repeated call."""

    view: DraftView
    job: Job | None
    feedback: FeedbackRecord
    created: bool


class DraftConflictError(Exception):
    """The draft's or job's state forbids the requested review action (HTTP 409)."""

    def __init__(self, code: str, message: str, *, status: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@runtime_checkable
class ReviewStore(Protocol):
    """Tenant-scoped review operations behind /v1/drafts."""

    async def list_drafts(
        self,
        organization_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        mailbox_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> DraftPage:
        """Drafts newest first, strictly before ``after`` (created_at, id) when given."""
        ...

    async def get_draft_detail(self, organization_id: UUID, draft_id: UUID) -> DraftDetail | None:
        """The draft with its original email, thread summary, citations and facts."""
        ...

    async def edit_draft(
        self, organization_id: UUID, draft_id: UUID, *, body: str, subject: str | None = None
    ) -> DraftView | None:
        """Replace the body (and subject) while ``status=draft``; else DraftConflictError."""
        ...

    async def approve_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        """Mark approved and write the feedback row, once; repeats return the first result."""
        ...

    async def reject_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        """Mark rejected, write feedback and move the job DRAFTED -> COMPLETED, once."""
        ...


_VIEW_SELECT = """
    SELECT d.*,
           m.mailbox_id AS mailbox_id,
           m.subject AS original_subject,
           m.sender_email AS sender_email,
           m.provider_message_id AS original_provider_message_id,
           j.state AS job_state,
           c.category AS category
    FROM generated_draft d
    JOIN email_message m
      ON m.id = d.message_id AND m.organization_id = d.organization_id
    LEFT JOIN processing_job j
      ON j.id = d.job_id AND j.organization_id = d.organization_id
    LEFT JOIN LATERAL (
        SELECT cr.category
        FROM classification_result cr
        WHERE cr.message_id = d.message_id AND cr.organization_id = d.organization_id
        ORDER BY cr.created_at DESC
        LIMIT 1
    ) c ON TRUE
"""


def _row_to_view(row: asyncpg.Record) -> DraftView:
    return DraftView(
        draft=_row_to_draft(row),
        mailbox_id=row["mailbox_id"],
        original_subject=row["original_subject"] or "",
        sender_email=row["sender_email"] or "",
        original_provider_message_id=row["original_provider_message_id"],
        category=row["category"],
        job_state=row["job_state"],
    )


def _row_to_feedback(row: asyncpg.Record) -> FeedbackRecord:
    return FeedbackRecord(
        id=row["id"],
        organization_id=row["organization_id"],
        draft_id=row["draft_id"],
        decision=row["decision"],
        edited_body=row["edited_body"],
        edit_distance=row["edit_distance"],
        rating=row["rating"],
        reviewer=row["reviewer"],
        review_ms=row["review_ms"],
        comment=row["comment"],
        created_at=row["created_at"],
    )


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


class PostgresReviewStore:
    """PostgreSQL review unit of work (asyncpg)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._jobs = PostgresJobStore(pool)
        self._messages = PostgresMessageStore(pool)

    async def list_drafts(
        self,
        organization_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        mailbox_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> DraftPage:
        after_ts, after_id = after if after is not None else (None, None)
        query = (
            _VIEW_SELECT
            + """
            WHERE d.organization_id = $1
              AND ($2::text IS NULL OR d.status = $2)
              AND ($3::text IS NULL OR c.category = $3)
              AND ($4::uuid IS NULL OR m.mailbox_id = $4)
              AND ($5::timestamptz IS NULL OR (d.created_at, d.id) < ($5::timestamptz, $6::uuid))
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT $7
            """
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                query, organization_id, status, category, mailbox_id, after_ts, after_id, limit + 1
            )
        views = tuple(_row_to_view(r) for r in rows[:limit])
        next_cursor = (
            encode_cursor(views[-1].draft.created_at, views[-1].draft.id)
            if len(rows) > limit and views
            else None
        )
        return DraftPage(items=views, next_cursor=next_cursor)

    async def get_draft_detail(self, organization_id: UUID, draft_id: UUID) -> DraftDetail | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                _VIEW_SELECT + " WHERE d.id = $1 AND d.organization_id = $2",
                draft_id,
                organization_id,
            )
            if row is None:
                return None
            view = _row_to_view(row)
            summary = await conn.fetchval(
                "SELECT summary FROM thread_state WHERE thread_id = $1 AND organization_id = $2",
                _to_uuid(view.draft.thread_id),
                organization_id,
            )
            chunks = await self._cited_chunks(conn, organization_id, view.draft.citations)
            business = await self._business_data(conn, organization_id, view.draft.job_id)
            feedback = await self._feedback(conn, organization_id, draft_id)
        original = await self._messages.get_message(organization_id, view.draft.message_id)
        if original is None:
            return None
        return DraftDetail(
            view=view,
            original=original,
            thread_summary=summary,
            cited_chunks=chunks,
            business_data=business,
            feedback=feedback,
        )

    async def edit_draft(
        self, organization_id: UUID, draft_id: UUID, *, body: str, subject: str | None = None
    ) -> DraftView | None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                _VIEW_SELECT + " WHERE d.id = $1 AND d.organization_id = $2 FOR UPDATE OF d",
                draft_id,
                organization_id,
            )
            if row is None:
                return None
            view = _row_to_view(row)
            draft = view.draft
            if draft.status != DraftStatus.DRAFT.value:
                raise DraftConflictError(
                    "DRAFT_NOT_EDITABLE",
                    f"Draft '{draft_id}' is '{draft.status}'; only a draft can be edited.",
                    status=draft.status,
                )
            payload: dict[str, Any] = {"draft_id": str(draft.id), "body_chars": len(body)}
            if await self._generated_body(conn, organization_id, draft) is None:
                payload["original_body"] = draft.body
            await conn.execute(
                """
                INSERT INTO processing_event (job_id, message_id, organization_id, event_type,
                                              state_from, state_to, payload)
                VALUES ($1, $2, $3, $4, $5, $5, $6::jsonb)
                """,
                _to_uuid(draft.job_id) if draft.job_id else None,
                _to_uuid(draft.message_id),
                organization_id,
                DRAFT_EDITED_EVENT,
                view.job_state or JobState.DRAFTED.value,
                json.dumps(payload),
            )
            updated = await conn.fetchrow(
                """
                UPDATE generated_draft SET body = $3, subject = COALESCE($4, subject)
                WHERE id = $1 AND organization_id = $2
                RETURNING *
                """,
                draft_id,
                organization_id,
                body,
                subject,
            )
            assert updated is not None
        return replace(view, draft=_row_to_draft(updated))

    async def approve_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await self._lock(conn, organization_id, draft_id)
            if locked is None:
                return None
            view, job = locked
            draft = view.draft
            if job is None:
                raise DraftConflictError(
                    "DRAFT_HAS_NO_JOB",
                    f"Draft '{draft_id}' has no processing job to dispatch.",
                    status=draft.status,
                )
            if draft.status == DraftStatus.REJECTED.value:
                raise DraftConflictError(
                    "DRAFT_ALREADY_REJECTED",
                    f"Draft '{draft_id}' was rejected.",
                    status=draft.status,
                )
            if draft.status in _DECIDED_APPROVED:
                return DecisionOutcome(
                    view=view,
                    job=job,
                    feedback=await self._require_feedback(conn, organization_id, draft_id),
                    created=False,
                )
            if job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be approved.",
                    status=draft.status,
                )
            generated = await self._generated_body(conn, organization_id, draft) or draft.body
            verdict = approval_verdict(generated, draft.body)
            updated = await self._set_status(conn, organization_id, draft_id, DraftStatus.APPROVED)
            feedback = await self._insert_feedback(conn, organization_id, draft_id, verdict, review)
            return DecisionOutcome(
                view=replace(view, draft=updated), job=job, feedback=feedback, created=True
            )

    async def reject_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await self._lock(conn, organization_id, draft_id)
            if locked is None:
                return None
            view, job = locked
            draft = view.draft
            if draft.status in _DECIDED_APPROVED:
                raise DraftConflictError(
                    "DRAFT_ALREADY_APPROVED",
                    f"Draft '{draft_id}' was approved.",
                    status=draft.status,
                )
            if draft.status == DraftStatus.REJECTED.value:
                return DecisionOutcome(
                    view=view,
                    job=job,
                    feedback=await self._require_feedback(conn, organization_id, draft_id),
                    created=False,
                )
            if job is not None and job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be rejected.",
                    status=draft.status,
                )
            generated = await self._generated_body(conn, organization_id, draft) or draft.body
            verdict = rejection_verdict(generated, draft.body)
            updated = await self._set_status(conn, organization_id, draft_id, DraftStatus.REJECTED)
            feedback = await self._insert_feedback(conn, organization_id, draft_id, verdict, review)
            if job is not None:
                job, _ = await self._jobs.transition_job_state_on(
                    conn,
                    organization_id=organization_id,
                    job_id=job.id,
                    target_state=JobState.COMPLETED,
                    payload={
                        "decision": verdict.decision.value,
                        "draft_id": str(draft_id),
                        "feedback_id": str(feedback.id),
                    },
                )
                await conn.execute(
                    "UPDATE processing_job SET lease_expires_at = NULL"
                    " WHERE id = $1 AND organization_id = $2",
                    _to_uuid(job.id),
                    organization_id,
                )
                job.lease_expires_at = None
            return DecisionOutcome(
                view=replace(view, draft=updated, job_state=job.state if job else None),
                job=job,
                feedback=feedback,
                created=True,
            )

    async def _lock(
        self, conn: Any, organization_id: UUID, draft_id: UUID
    ) -> tuple[DraftView, Job | None] | None:
        """Lock the draft's job (first) and then the draft; None when the draft is unknown."""
        job_id = await conn.fetchval(
            "SELECT job_id FROM generated_draft WHERE id = $1 AND organization_id = $2",
            draft_id,
            organization_id,
        )
        job: Job | None = None
        if job_id is not None:
            job_row = await conn.fetchrow(
                f"SELECT {JOB_SELECT_COLUMNS} FROM processing_job"
                " WHERE id = $1 AND organization_id = $2 FOR UPDATE",
                job_id,
                organization_id,
            )
            if job_row is None:
                raise DraftConflictError(
                    "JOB_NOT_FOUND",
                    f"Job '{job_id}' of draft '{draft_id}' is gone.",
                    status="draft",
                )
            job = PostgresJobStore._row_to_job(job_row)  # noqa: SLF001
        row = await conn.fetchrow(
            _VIEW_SELECT + " WHERE d.id = $1 AND d.organization_id = $2 FOR UPDATE OF d",
            draft_id,
            organization_id,
        )
        if row is None:
            return None
        return _row_to_view(row), job

    async def _set_status(
        self, conn: Any, organization_id: UUID, draft_id: UUID, status: DraftStatus
    ) -> GeneratedDraft:
        row = await conn.fetchrow(
            "UPDATE generated_draft SET status = $3 WHERE id = $1 AND organization_id = $2"
            " RETURNING *",
            draft_id,
            organization_id,
            status.value,
        )
        assert row is not None
        return _row_to_draft(row)

    async def _insert_feedback(
        self,
        conn: Any,
        organization_id: UUID,
        draft_id: UUID,
        verdict: ReviewVerdict,
        review: ReviewInput,
    ) -> FeedbackRecord:
        row = await conn.fetchrow(
            """
            INSERT INTO feedback (id, organization_id, draft_id, reviewer, decision,
                                  edited_body, edit_distance, rating, comment, review_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (draft_id) DO NOTHING
            RETURNING *
            """,
            uuid4(),
            organization_id,
            draft_id,
            review.reviewer,
            verdict.decision.value,
            verdict.edited_body,
            verdict.edit_distance,
            review.rating,
            review.comment,
            review.review_ms,
        )
        if row is None:
            return await self._require_feedback(conn, organization_id, draft_id)
        return _row_to_feedback(row)

    async def _feedback(
        self, conn: Any, organization_id: UUID, draft_id: UUID
    ) -> FeedbackRecord | None:
        row = await conn.fetchrow(
            "SELECT * FROM feedback WHERE draft_id = $1 AND organization_id = $2",
            draft_id,
            organization_id,
        )
        return _row_to_feedback(row) if row is not None else None

    async def _require_feedback(
        self, conn: Any, organization_id: UUID, draft_id: UUID
    ) -> FeedbackRecord:
        feedback = await self._feedback(conn, organization_id, draft_id)
        if feedback is None:
            raise DraftConflictError(
                "FEEDBACK_MISSING",
                f"Draft '{draft_id}' was decided without a feedback row.",
                status="unknown",
            )
        return feedback

    async def _generated_body(
        self, conn: Any, organization_id: UUID, draft: GeneratedDraft
    ) -> str | None:
        """The body as generated, from the first ``draft_edited`` event; None if never edited."""
        value = await conn.fetchval(
            """
            SELECT payload->>'original_body' FROM processing_event
            WHERE organization_id = $1 AND message_id = $2 AND event_type = $3
              AND payload->>'draft_id' = $4 AND payload ? 'original_body'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            organization_id,
            _to_uuid(draft.message_id),
            DRAFT_EDITED_EVENT,
            str(draft.id),
        )
        return str(value) if value is not None else None

    async def _cited_chunks(
        self, conn: Any, organization_id: UUID, citations: Sequence[Mapping[str, Any]]
    ) -> tuple[CitedChunk, ...]:
        ids: list[UUID] = []
        for citation in citations:
            try:
                ids.append(UUID(str(citation.get("chunk_id"))))
            except ValueError:
                continue
        if not ids:
            return ()
        rows = await conn.fetch(
            "SELECT id, document_id, external_id, content, heading_path FROM knowledge_chunk"
            " WHERE organization_id = $1 AND id = ANY($2::uuid[])",
            organization_id,
            ids,
        )
        by_id = {str(r["id"]): r for r in rows}
        chunks: list[CitedChunk] = []
        for citation in citations:
            found = by_id.get(str(citation.get("chunk_id")))
            if found is None:
                continue
            chunks.append(
                CitedChunk(
                    citation_id=str(
                        citation.get("citation_id") or found["external_id"] or found["id"]
                    ),
                    chunk_id=str(found["id"]),
                    document_id=str(found["document_id"]),
                    external_id=found["external_id"],
                    content=found["content"],
                    heading_path=tuple(found["heading_path"] or ()),
                )
            )
        return tuple(chunks)

    async def _business_data(
        self, conn: Any, organization_id: UUID, job_id: UUID | str | None
    ) -> BusinessDataView | None:
        if job_id is None or str(job_id) == "":
            return None
        raw = await conn.fetchval(
            """
            SELECT payload FROM processing_event
            WHERE organization_id = $1 AND job_id = $2 AND state_to = $3
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            organization_id,
            _to_uuid(job_id),
            JobState.CONTEXT_READY.value,
        )
        payload = _json(raw)
        return BusinessDataView.from_context_payload(
            payload if isinstance(payload, Mapping) else None
        )


@dataclass
class _Entry:
    draft: GeneratedDraft
    original: NormalizedMessage
    category: str | None
    thread_summary: str | None
    business_data: BusinessDataView | None
    generated_body: str | None = None


class InMemoryReviewStore:
    """In-memory ReviewStore for unit and UI tests; applies the same rules as Postgres."""

    def __init__(self, job_store: InMemoryJobStore | None = None) -> None:
        self.jobs = job_store or InMemoryJobStore()
        self._entries: dict[UUID, _Entry] = {}
        self._feedback: dict[UUID, FeedbackRecord] = {}
        self._chunks: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._lock = asyncio.Lock()

    def add_draft(
        self,
        draft: GeneratedDraft,
        *,
        original: NormalizedMessage,
        category: str | None = None,
        thread_summary: str | None = None,
        business_data: BusinessDataView | None = None,
    ) -> None:
        """Seed a draft and the context the review screen shows with it."""
        self._entries[_to_uuid(draft.id)] = _Entry(
            draft=draft,
            original=original,
            category=category,
            thread_summary=thread_summary,
            business_data=business_data,
        )

    def add_chunk(
        self, chunk_id: UUID | str, *, content: str, heading_path: Sequence[str] = ()
    ) -> None:
        """Seed a knowledge chunk that drafts may cite by ``chunk_id``."""
        self._chunks[str(chunk_id)] = (content, tuple(heading_path))

    def feedback_rows(self, organization_id: UUID) -> list[FeedbackRecord]:
        """Every feedback row of the tenant (test inspection)."""
        return [f for f in self._feedback.values() if f.organization_id == organization_id]

    def _entry(self, organization_id: UUID, draft_id: UUID) -> _Entry | None:
        entry = self._entries.get(draft_id)
        if entry is None or _to_uuid(entry.draft.organization_id) != organization_id:
            return None
        return entry

    async def _job(self, entry: _Entry) -> Job | None:
        if entry.draft.job_id is None or str(entry.draft.job_id) == "":
            return None
        return await self.jobs.get_job(entry.draft.organization_id, entry.draft.job_id)

    async def _view(self, entry: _Entry) -> DraftView:
        job = await self._job(entry)
        return DraftView(
            draft=entry.draft,
            mailbox_id=_to_uuid(entry.original.mailbox_id),
            original_subject=entry.original.subject,
            sender_email=entry.original.sender.email,
            original_provider_message_id=entry.original.provider_message_id,
            category=entry.category,
            job_state=job.state if job is not None else None,
        )

    async def list_drafts(
        self,
        organization_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        mailbox_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> DraftPage:
        async with self._lock:
            entries = [
                e
                for e in self._entries.values()
                if _to_uuid(e.draft.organization_id) == organization_id
            ]
            views = [await self._view(e) for e in entries]
        matching = [
            v
            for v in views
            if (status is None or v.draft.status == status)
            and (category is None or v.category == category)
            and (mailbox_id is None or v.mailbox_id == mailbox_id)
            and (after is None or (v.draft.created_at, v.draft.id) < after)
        ]
        matching.sort(key=lambda v: (v.draft.created_at, v.draft.id), reverse=True)
        page = tuple(matching[:limit])
        next_cursor = (
            encode_cursor(page[-1].draft.created_at, page[-1].draft.id)
            if len(matching) > limit and page
            else None
        )
        return DraftPage(items=page, next_cursor=next_cursor)

    async def get_draft_detail(self, organization_id: UUID, draft_id: UUID) -> DraftDetail | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            chunks: list[CitedChunk] = []
            for citation in entry.draft.citations:
                stored = self._chunks.get(str(citation.get("chunk_id")))
                if stored is None:
                    continue
                content, heading_path = stored
                chunks.append(
                    CitedChunk(
                        citation_id=str(citation.get("citation_id") or citation.get("chunk_id")),
                        chunk_id=str(citation.get("chunk_id")),
                        document_id=(
                            str(citation["document_id"]) if citation.get("document_id") else None
                        ),
                        external_id=citation.get("external_id"),
                        content=content,
                        heading_path=heading_path,
                    )
                )
            return DraftDetail(
                view=await self._view(entry),
                original=entry.original,
                thread_summary=entry.thread_summary,
                cited_chunks=tuple(chunks),
                business_data=entry.business_data,
                feedback=self._feedback.get(draft_id),
            )

    async def edit_draft(
        self, organization_id: UUID, draft_id: UUID, *, body: str, subject: str | None = None
    ) -> DraftView | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            if entry.draft.status != DraftStatus.DRAFT.value:
                raise DraftConflictError(
                    "DRAFT_NOT_EDITABLE",
                    f"Draft '{draft_id}' is '{entry.draft.status}'; only a draft can be edited.",
                    status=entry.draft.status,
                )
            if entry.generated_body is None:
                entry.generated_body = entry.draft.body
            entry.draft = replace(
                entry.draft,
                body=body,
                subject=subject if subject is not None else entry.draft.subject,
            )
            return await self._view(entry)

    async def approve_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            draft = entry.draft
            job = await self._job(entry)
            if job is None:
                raise DraftConflictError(
                    "DRAFT_HAS_NO_JOB",
                    f"Draft '{draft_id}' has no processing job to dispatch.",
                    status=draft.status,
                )
            if draft.status == DraftStatus.REJECTED.value:
                raise DraftConflictError(
                    "DRAFT_ALREADY_REJECTED",
                    f"Draft '{draft_id}' was rejected.",
                    status=draft.status,
                )
            if draft.status in _DECIDED_APPROVED:
                return DecisionOutcome(
                    view=await self._view(entry),
                    job=job,
                    feedback=self._feedback[draft_id],
                    created=False,
                )
            if job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be approved.",
                    status=draft.status,
                )
            verdict = approval_verdict(entry.generated_body or draft.body, draft.body)
            entry.draft = replace(draft, status=DraftStatus.APPROVED.value)
            feedback = self._record(organization_id, draft_id, verdict, review)
            return DecisionOutcome(
                view=await self._view(entry), job=job, feedback=feedback, created=True
            )

    async def reject_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            draft = entry.draft
            job = await self._job(entry)
            if draft.status in _DECIDED_APPROVED:
                raise DraftConflictError(
                    "DRAFT_ALREADY_APPROVED",
                    f"Draft '{draft_id}' was approved.",
                    status=draft.status,
                )
            if draft.status == DraftStatus.REJECTED.value:
                return DecisionOutcome(
                    view=await self._view(entry),
                    job=job,
                    feedback=self._feedback[draft_id],
                    created=False,
                )
            if job is not None and job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be rejected.",
                    status=draft.status,
                )
            verdict = rejection_verdict(entry.generated_body or draft.body, draft.body)
            feedback_id = uuid4()
            if job is not None:
                # Transition first: it raises on an illegal state before anything is stored.
                job, _ = await self.jobs.transition_job_state(
                    organization_id=organization_id,
                    job_id=job.id,
                    target_state=JobState.COMPLETED,
                    payload={
                        "decision": verdict.decision.value,
                        "draft_id": str(draft_id),
                        "feedback_id": str(feedback_id),
                    },
                )
                job.lease_expires_at = None
            entry.draft = replace(draft, status=DraftStatus.REJECTED.value)
            feedback = self._record(organization_id, draft_id, verdict, review, feedback_id)
            return DecisionOutcome(
                view=await self._view(entry), job=job, feedback=feedback, created=True
            )

    def _record(
        self,
        organization_id: UUID,
        draft_id: UUID,
        verdict: ReviewVerdict,
        review: ReviewInput,
        feedback_id: UUID | None = None,
    ) -> FeedbackRecord:
        existing = self._feedback.get(draft_id)
        if existing is not None:  # UNIQUE (draft_id)
            return existing
        record = FeedbackRecord(
            id=feedback_id or uuid4(),
            organization_id=organization_id,
            draft_id=draft_id,
            decision=verdict.decision.value,
            edited_body=verdict.edited_body,
            edit_distance=verdict.edit_distance,
            rating=review.rating,
            reviewer=review.reviewer,
            review_ms=review.review_ms,
            comment=review.comment,
            created_at=datetime.now(UTC),
        )
        self._feedback[draft_id] = record
        return record

"""PostgreSQL and in-memory persistence stores for generated email drafts (R5.2, R16.4, R16.6).

Provides tenant-scoped storage and retrieval for generated drafts produced by
deterministic template replies or downstream LLM generation.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import GeneratedDraft

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _opt_uuid(val: UUID | str | None) -> UUID | None:
    if val is None or val == "":
        return None
    return val if isinstance(val, UUID) else UUID(str(val))


def _row_to_draft(row: asyncpg.Record) -> GeneratedDraft:
    raw_citations = row["citations"]
    if isinstance(raw_citations, str):
        try:
            citations_list = json.loads(raw_citations)
        except Exception:
            citations_list = []
    elif isinstance(raw_citations, list):
        citations_list = raw_citations
    else:
        citations_list = []

    return GeneratedDraft(
        id=row["id"],
        organization_id=row["organization_id"],
        job_id=row["job_id"],
        message_id=row["message_id"],
        thread_id=row["thread_id"],
        action=row["action"],
        subject=row["subject"],
        body=row["body"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        citations=citations_list,
        citation_mismatch=bool(row["citation_mismatch"]),
        model_name=row["model_name"],
        model_tier=row["model_tier"],
        escalation_reason=row["escalation_reason"],
        prompt_version=row["prompt_version"],
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cost_estimate=float(row["cost_estimate"] or 0.0),
        status=row["status"],
        provider_ref=row["provider_ref"],
        created_at=row["created_at"],
    )


@runtime_checkable
class DraftStore(Protocol):
    """Protocol defining persistence operations for generated email drafts."""

    async def create_draft(self, draft: GeneratedDraft) -> GeneratedDraft:
        """Persist a new generated draft."""
        ...

    async def get_draft(
        self,
        draft_id: UUID | str,
        organization_id: UUID | str,
    ) -> GeneratedDraft | None:
        """Retrieve a single draft by ID and tenant organization ID."""
        ...

    async def list_drafts_for_job(
        self,
        job_id: UUID | str,
        organization_id: UUID | str,
    ) -> list[GeneratedDraft]:
        """List all drafts associated with a specific processing job."""
        ...

    async def list_drafts_for_thread(
        self,
        thread_id: UUID | str,
        organization_id: UUID | str,
    ) -> list[GeneratedDraft]:
        """List all drafts associated with a specific email thread."""
        ...


class InMemoryDraftStore:
    """In-memory implementation of DraftStore for isolated testing."""

    def __init__(self) -> None:
        self._drafts: dict[UUID, GeneratedDraft] = {}

    async def create_draft(self, draft: GeneratedDraft) -> GeneratedDraft:
        draft_id = _to_uuid(draft.id)
        persisted = GeneratedDraft(
            id=draft_id,
            organization_id=_to_uuid(draft.organization_id),
            job_id=_opt_uuid(draft.job_id),
            message_id=_to_uuid(draft.message_id),
            thread_id=_to_uuid(draft.thread_id),
            action=draft.action,
            subject=draft.subject,
            body=draft.body,
            confidence=draft.confidence,
            citations=list(draft.citations),
            citation_mismatch=draft.citation_mismatch,
            model_name=draft.model_name,
            model_tier=draft.model_tier,
            escalation_reason=draft.escalation_reason,
            prompt_version=draft.prompt_version,
            input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens,
            cost_estimate=draft.cost_estimate,
            status=draft.status,
            provider_ref=draft.provider_ref,
            created_at=draft.created_at or datetime.now(UTC),
        )
        self._drafts[draft_id] = persisted
        return persisted

    async def get_draft(
        self,
        draft_id: UUID | str,
        organization_id: UUID | str,
    ) -> GeneratedDraft | None:
        d_id = _to_uuid(draft_id)
        org_id = _to_uuid(organization_id)
        draft = self._drafts.get(d_id)
        if draft and draft.organization_id == org_id:
            return draft
        return None

    async def list_drafts_for_job(
        self,
        job_id: UUID | str,
        organization_id: UUID | str,
    ) -> list[GeneratedDraft]:
        j_id = _to_uuid(job_id)
        org_id = _to_uuid(organization_id)
        return [
            d for d in self._drafts.values() if d.job_id == j_id and d.organization_id == org_id
        ]

    async def list_drafts_for_thread(
        self,
        thread_id: UUID | str,
        organization_id: UUID | str,
    ) -> list[GeneratedDraft]:
        t_id = _to_uuid(thread_id)
        org_id = _to_uuid(organization_id)
        return [
            d for d in self._drafts.values() if d.thread_id == t_id and d.organization_id == org_id
        ]


class PostgresDraftStore:
    """PostgreSQL implementation of DraftStore using asyncpg."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_draft(self, draft: GeneratedDraft) -> GeneratedDraft:
        """Insert a new draft into generated_draft table."""
        draft_id = _to_uuid(draft.id)
        org_id = _to_uuid(draft.organization_id)
        job_id = _opt_uuid(draft.job_id)
        msg_id = _to_uuid(draft.message_id)
        th_id = _to_uuid(draft.thread_id)

        citations_json = json.dumps(draft.citations or [])

        query = """
            INSERT INTO generated_draft (
                id, organization_id, job_id, message_id, thread_id,
                action, subject, body, confidence, citations,
                citation_mismatch, model_name, model_tier, escalation_reason,
                prompt_version, input_tokens, output_tokens, cost_estimate,
                status, provider_ref, created_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10::jsonb,
                $11, $12, $13, $14,
                $15, $16, $17, $18,
                $19, $20, $21
            )
            RETURNING *;
        """

        created_at = draft.created_at or datetime.now(UTC)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                draft_id,
                org_id,
                job_id,
                msg_id,
                th_id,
                draft.action,
                draft.subject,
                draft.body,
                draft.confidence,
                citations_json,
                draft.citation_mismatch,
                draft.model_name,
                draft.model_tier,
                draft.escalation_reason,
                draft.prompt_version,
                draft.input_tokens,
                draft.output_tokens,
                draft.cost_estimate,
                draft.status,
                draft.provider_ref,
                created_at,
            )

        if row is None:
            raise RuntimeError(f"Failed to insert generated_draft {draft_id}")

        return _row_to_draft(row)

    async def get_draft(
        self,
        draft_id: UUID | str,
        organization_id: UUID | str,
    ) -> GeneratedDraft | None:
        """Fetch draft by id and tenant organization_id."""
        query = """
            SELECT * FROM generated_draft
            WHERE id = $1 AND organization_id = $2;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, _to_uuid(draft_id), _to_uuid(organization_id))

        if row is None:
            return None
        return _row_to_draft(row)

    async def list_drafts_for_job(
        self,
        job_id: UUID | str,
        organization_id: UUID | str,
    ) -> list[GeneratedDraft]:
        """Fetch all drafts for a job within a tenant organization."""
        query = """
            SELECT * FROM generated_draft
            WHERE job_id = $1 AND organization_id = $2
            ORDER BY created_at DESC;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, _to_uuid(job_id), _to_uuid(organization_id))

        return [_row_to_draft(r) for r in rows]

    async def list_drafts_for_thread(
        self,
        thread_id: UUID | str,
        organization_id: UUID | str,
    ) -> list[GeneratedDraft]:
        """Fetch all drafts for a thread within a tenant organization."""
        query = """
            SELECT * FROM generated_draft
            WHERE thread_id = $1 AND organization_id = $2
            ORDER BY created_at DESC;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, _to_uuid(thread_id), _to_uuid(organization_id))

        return [_row_to_draft(r) for r in rows]

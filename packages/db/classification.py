"""Classification persistence store (R5.2, R6.7, design.md §6.1).

Provides durable PostgreSQL and in-memory backends for persisting triage classification
results to the classification_result table with strict multi-tenant organization scoping.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import asyncpg

from packages.domain.entities import Classification

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _to_json_val(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    return str(val)


def _from_json_val(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        except Exception:
            return {"raw_text": val}
    if isinstance(val, dict):
        return val
    return {"data": val}


@dataclass(frozen=True)
class ClassificationResultRow:
    """Record matching the classification_result table schema (R6.7, design.md §6.1)."""

    id: UUID
    organization_id: UUID
    message_id: UUID
    category: str
    intent: str | None
    priority: str
    reply_required: bool
    retrieval_required: bool
    confidence: float
    decided_by: str  # 'rule' | 'ml' | 'llm' | 'default'
    model_name: str | None
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_domain_classification(self) -> Classification:
        """Convert database record to pure domain Classification entity."""
        workflow_hint = "ai"
        if not self.reply_required:
            workflow_hint = "none"
        elif isinstance(self.raw, dict) and "workflow_hint" in self.raw:
            workflow_hint = str(self.raw["workflow_hint"])

        return Classification(
            category=self.category,
            intent=self.intent,
            priority=self.priority,
            reply_required=self.reply_required,
            workflow_hint=workflow_hint,
            retrieval_required=self.retrieval_required,
            confidence=self.confidence,
            decided_by=self.decided_by,
            latency_ms=self.latency_ms,
            model=self.model_name,
            raw=dict(self.raw),
        )

    @classmethod
    def from_domain(
        cls,
        organization_id: UUID | str,
        message_id: UUID | str,
        classification: Classification,
        classification_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> ClassificationResultRow:
        """Construct a ClassificationResultRow from a domain Classification entity."""
        raw_payload = dict(classification.raw)
        if "workflow_hint" not in raw_payload:
            raw_payload["workflow_hint"] = classification.workflow_hint

        return cls(
            id=classification_id or uuid4(),
            organization_id=_to_uuid(organization_id),
            message_id=_to_uuid(message_id),
            category=classification.category,
            intent=classification.intent,
            priority=classification.priority,
            reply_required=classification.reply_required,
            retrieval_required=classification.retrieval_required,
            confidence=round(float(classification.confidence), 3),
            decided_by=classification.decided_by,
            model_name=classification.model,
            latency_ms=classification.latency_ms,
            raw=raw_payload,
            created_at=created_at or datetime.now(UTC),
        )


@runtime_checkable
class ClassificationStore(Protocol):
    """Protocol for storing and retrieving classification results."""

    async def save_classification(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
        classification: Classification,
        classification_id: UUID | None = None,
    ) -> ClassificationResultRow:
        """Persist a classification result with multi-tenant organization scoping."""
        ...

    async def get_classification(
        self,
        organization_id: UUID | str,
        classification_id: UUID | str,
    ) -> ClassificationResultRow | None:
        """Fetch a specific classification result by ID."""
        ...

    async def get_latest_classification_by_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> ClassificationResultRow | None:
        """Fetch the most recent classification for a given message."""
        ...

    async def list_classifications_by_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[ClassificationResultRow]:
        """List all classification attempts for a message in chronological order."""
        ...


class PostgresClassificationStore(ClassificationStore):
    """PostgreSQL implementation of ClassificationStore backed by asyncpg."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    def _row_to_record(self, row: asyncpg.Record) -> ClassificationResultRow:
        return ClassificationResultRow(
            id=row["id"],
            organization_id=row["organization_id"],
            message_id=row["message_id"],
            category=row["category"],
            intent=row["intent"],
            priority=row["priority"],
            reply_required=row["reply_required"],
            retrieval_required=row["retrieval_required"],
            confidence=float(row["confidence"]),
            decided_by=row["decided_by"],
            model_name=row["model_name"],
            latency_ms=row["latency_ms"] or 0,
            raw=_from_json_val(row["raw"]),
            created_at=row["created_at"],
        )

    async def save_classification(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
        classification: Classification,
        classification_id: UUID | None = None,
    ) -> ClassificationResultRow:
        """Insert a classification record into classification_result."""
        org_id = _to_uuid(organization_id)
        msg_id = _to_uuid(message_id)
        cls_id = classification_id or uuid4()
        now = datetime.now(UTC)

        raw_payload = dict(classification.raw)
        if "workflow_hint" not in raw_payload:
            raw_payload["workflow_hint"] = classification.workflow_hint
        raw_json = _to_json_val(raw_payload)

        query = """
            INSERT INTO classification_result (
                id, organization_id, message_id, category, intent, priority,
                reply_required, retrieval_required, confidence, decided_by,
                model_name, latency_ms, raw, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14
            )
            RETURNING *
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                cls_id,
                org_id,
                msg_id,
                classification.category,
                classification.intent,
                classification.priority,
                classification.reply_required,
                classification.retrieval_required,
                round(float(classification.confidence), 3),
                classification.decided_by,
                classification.model,
                classification.latency_ms,
                raw_json,
                now,
            )
            if row is None:
                raise RuntimeError("Failed to insert classification_result record")
            return self._row_to_record(row)

    async def get_classification(
        self,
        organization_id: UUID | str,
        classification_id: UUID | str,
    ) -> ClassificationResultRow | None:
        """Fetch a specific classification result by ID."""
        query = """
            SELECT * FROM classification_result
            WHERE organization_id = $1 AND id = $2
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, _to_uuid(organization_id), _to_uuid(classification_id))
            return self._row_to_record(row) if row else None

    async def get_latest_classification_by_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> ClassificationResultRow | None:
        """Fetch the most recent classification for a message."""
        query = """
            SELECT * FROM classification_result
            WHERE organization_id = $1 AND message_id = $2
            ORDER BY created_at DESC
            LIMIT 1
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, _to_uuid(organization_id), _to_uuid(message_id))
            return self._row_to_record(row) if row else None

    async def list_classifications_by_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[ClassificationResultRow]:
        """List all classification records for a message in chronological order."""
        query = """
            SELECT * FROM classification_result
            WHERE organization_id = $1 AND message_id = $2
            ORDER BY created_at ASC
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, _to_uuid(organization_id), _to_uuid(message_id))
            return [self._row_to_record(r) for r in rows]


class InMemoryClassificationStore(ClassificationStore):
    """In-memory thread-safe implementation of ClassificationStore for tests."""

    def __init__(self) -> None:
        self._records: list[ClassificationResultRow] = []
        self._lock = asyncio.Lock()

    async def save_classification(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
        classification: Classification,
        classification_id: UUID | None = None,
    ) -> ClassificationResultRow:
        row = ClassificationResultRow.from_domain(
            organization_id=organization_id,
            message_id=message_id,
            classification=classification,
            classification_id=classification_id,
        )
        async with self._lock:
            self._records.append(row)
        return row

    async def get_classification(
        self,
        organization_id: UUID | str,
        classification_id: UUID | str,
    ) -> ClassificationResultRow | None:
        org_id = _to_uuid(organization_id)
        c_id = _to_uuid(classification_id)
        async with self._lock:
            for r in self._records:
                if r.organization_id == org_id and r.id == c_id:
                    return r
        return None

    async def get_latest_classification_by_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> ClassificationResultRow | None:
        org_id = _to_uuid(organization_id)
        m_id = _to_uuid(message_id)
        async with self._lock:
            matching = [
                r for r in self._records if r.organization_id == org_id and r.message_id == m_id
            ]
            if not matching:
                return None
            matching.sort(key=lambda x: x.created_at, reverse=True)
            return matching[0]

    async def list_classifications_by_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[ClassificationResultRow]:
        org_id = _to_uuid(organization_id)
        m_id = _to_uuid(message_id)
        async with self._lock:
            matching = [
                r for r in self._records if r.organization_id == org_id and r.message_id == m_id
            ]
            matching.sort(key=lambda x: x.created_at)
            return list(matching)

    async def clear(self) -> None:
        """Clear all stored classification records."""
        async with self._lock:
            self._records.clear()

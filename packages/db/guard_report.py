"""AgentMailGuard report of a C3 job (task 9.7, ADR-0018 decision 7, R25.5, R25.6).

The row is written on the ``persist_drafted`` connection, in its transaction (AGENTS.md §4), and
read by ``GET /v1/jobs/{id}/guard`` scoped by organization.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import asyncpg


def _uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass
class GuardReportRecord:
    """One job's guard report."""

    organization_id: UUID | str
    job_id: UUID | str
    report: dict[str, Any]
    schema_version: int
    inbound_action: str | None = None
    final_action: str | None = None
    guard_mode: str = "C3"
    model_profile: str | None = None
    guard_llm_calls: int = 0
    timings_ms: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


INSERT_GUARD_REPORT_SQL = """
    INSERT INTO guard_report (
        id, organization_id, job_id, guard_mode, model_profile, schema_version,
        inbound_action, final_action, report, guard_llm_calls, timings_ms, created_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::jsonb, $12)
    ON CONFLICT (job_id) DO NOTHING
"""


async def insert_guard_report(conn: asyncpg.Connection, record: GuardReportRecord) -> None:
    """Write ``record`` on ``conn`` (the caller's transaction); a second write is a no-op."""
    await conn.execute(
        INSERT_GUARD_REPORT_SQL,
        record.id,
        _uuid(record.organization_id),
        _uuid(record.job_id),
        record.guard_mode,
        record.model_profile,
        record.schema_version,
        record.inbound_action,
        record.final_action,
        json.dumps(record.report, default=str),
        record.guard_llm_calls,
        json.dumps(record.timings_ms),
        record.created_at,
    )


def _from_row(row: asyncpg.Record) -> GuardReportRecord:
    def _json(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    return GuardReportRecord(
        id=row["id"],
        organization_id=row["organization_id"],
        job_id=row["job_id"],
        guard_mode=row["guard_mode"],
        model_profile=row["model_profile"],
        schema_version=row["schema_version"],
        inbound_action=row["inbound_action"],
        final_action=row["final_action"],
        report=_json(row["report"]),
        guard_llm_calls=row["guard_llm_calls"],
        timings_ms=_json(row["timings_ms"]) or {},
        created_at=row["created_at"],
    )


@runtime_checkable
class GuardReportStore(Protocol):
    async def get_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GuardReportRecord | None:
        """The job's report in its organization, or None (a C0 job, or another tenant's)."""
        ...


class PostgresGuardReportStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GuardReportRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM guard_report WHERE job_id = $1 AND organization_id = $2",
                _uuid(job_id),
                _uuid(organization_id),
            )
        return _from_row(row) if row is not None else None


class InMemoryGuardReportStore:
    def __init__(self) -> None:
        self._rows: dict[str, GuardReportRecord] = {}

    def add(self, record: GuardReportRecord) -> None:
        self._rows.setdefault(str(record.job_id), record)

    async def get_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GuardReportRecord | None:
        record = self._rows.get(str(job_id))
        if record is None or str(record.organization_id) != str(organization_id):
            return None
        return record

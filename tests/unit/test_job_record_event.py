"""JobStore.record_event appends a non-transition event to a job's timeline (R18.6, R21).

The ai-worker records a ``context_built`` diagnostics event after it builds a job's context. No
existing store method wrote an event without a state change, so the store gains one: it records
the job's current state as both ``state_from`` and ``state_to`` (the timeline API requires
``state_to``, and the event changes no state).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.db.job import InMemoryJobStore, PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from services.api.schemas.jobs import ProcessingEventResponse


async def _queued_job(jobs: InMemoryJobStore, org_id: UUID) -> Job:
    job, _ = await jobs.create_job(
        Job(
            organization_id=org_id,
            message_id=uuid4(),
            state=JobState.QUEUED.value,
            idempotency_key=f"k-{uuid4()}",
            trace_id="trace-1",
        )
    )
    return job


async def test_in_memory_event_carries_the_current_state_and_changes_none() -> None:
    jobs, org_id = InMemoryJobStore(), uuid4()
    job = await _queued_job(jobs, org_id)

    event = await jobs.record_event(org_id, job.id, "context_built", {"retrieved": []})

    assert event.event_type == "context_built"
    assert event.state_from == event.state_to == JobState.QUEUED.value
    assert event.payload == {"retrieved": []}
    assert (event.job_id, event.message_id, event.trace_id) == (job.id, job.message_id, "trace-1")
    stored = await jobs.get_job(org_id, job.id)
    assert stored is not None and stored.state == JobState.QUEUED.value


async def test_in_memory_event_joins_the_timeline_in_order() -> None:
    jobs, org_id = InMemoryJobStore(), uuid4()
    job = await _queued_job(jobs, org_id)
    await jobs.record_event(org_id, job.id, "context_built")
    await jobs.transition_job_state(org_id, job.id, JobState.CONTEXT_READY)

    events = await jobs.list_events_for_job(org_id, job.id)

    assert [(e.event_type, e.state_to) for e in events] == [
        ("state_transition", "QUEUED"),
        ("context_built", "QUEUED"),
        ("state_transition", "CONTEXT_READY"),
    ]
    assert events[1].payload == {}
    assert len({e.id for e in events}) == 3


async def test_in_memory_event_is_tenant_scoped() -> None:
    jobs, org_id = InMemoryJobStore(), uuid4()
    job = await _queued_job(jobs, org_id)

    with pytest.raises(KeyError, match="not found"):
        await jobs.record_event(uuid4(), job.id, "context_built")
    with pytest.raises(KeyError, match="not found"):
        await jobs.record_event(org_id, uuid4(), "context_built")

    assert len(await jobs.list_events_for_job(org_id, job.id)) == 1


async def test_event_stays_valid_in_the_timeline_api_schema() -> None:
    """ProcessingEventResponse requires state_to; a null one would fail the timeline endpoint."""
    jobs, org_id = InMemoryJobStore(), uuid4()
    job = await _queued_job(jobs, org_id)
    event = await jobs.record_event(org_id, job.id, "context_built", {"summary_triggered": False})

    response = ProcessingEventResponse(
        id=event.id,
        job_id=UUID(str(event.job_id)),
        message_id=UUID(str(event.message_id)),
        organization_id=UUID(str(event.organization_id)),
        event_type=event.event_type,
        state_from=event.state_from,
        state_to=event.state_to or "",
        payload=event.payload,
        trace_id=event.trace_id,
        created_at=event.created_at,
    )

    assert response.event_type == "context_built" and response.state_to == "QUEUED"


class RecordingConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.row


class RecordingPool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.connection = RecordingConnection(row)

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self.connection


def _event_row(org_id: UUID, job_id: UUID) -> dict[str, Any]:
    return {
        "id": 41,
        "job_id": job_id,
        "message_id": uuid4(),
        "organization_id": org_id,
        "event_type": "context_built",
        "state_from": "CONTEXT_READY",
        "state_to": "CONTEXT_READY",
        "payload": json.dumps({"retrieved": []}),
        "trace_id": "t-1",
        "created_at": datetime.now(UTC),
    }


async def test_postgres_event_is_one_statement_scoped_to_the_organization() -> None:
    org_id, job_id = uuid4(), uuid4()
    pool = RecordingPool(_event_row(org_id, job_id))

    event = await PostgresJobStore(pool).record_event(
        org_id, job_id, "context_built", {"retrieved": []}
    )

    ((query, args),) = pool.connection.calls
    assert "INSERT INTO processing_event" in query
    assert "j.id = $1 AND j.organization_id = $2" in query  # tenant scope (R5.3)
    assert "j.state, j.state" in query  # state_from = state_to = the row's own state
    assert args == (job_id, org_id, "context_built", json.dumps({"retrieved": []}))
    assert (event.id, event.event_type, event.payload) == (41, "context_built", {"retrieved": []})


async def test_postgres_event_for_an_unknown_job_raises() -> None:
    pool = RecordingPool(None)

    with pytest.raises(KeyError, match="not found"):
        await PostgresJobStore(pool).record_event(uuid4(), uuid4(), "context_built")

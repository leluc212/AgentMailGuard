"""Draft persistence unit of work: draft + GENERATING -> DRAFTED together (R16.4, R18.1, R18.5)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState


async def _setup(state: JobState) -> tuple[InMemoryDraftPersistence, InMemoryJobStore, Job]:
    jobs = InMemoryJobStore()
    job, _ = await jobs.create_job(
        Job(organization_id=uuid4(), state=state.value, idempotency_key=f"k-{uuid4()}")
    )
    return InMemoryDraftPersistence(jobs, InMemoryDraftStore()), jobs, job


def _draft(job: Job) -> GeneratedDraft:
    return GeneratedDraft(
        organization_id=job.organization_id,
        job_id=job.id,
        message_id=uuid4(),
        thread_id=uuid4(),
        body="Hello",
        model_name="model-routine",
        model_tier="routine",
        escalation_reason="none",
        prompt_version="p.v1",
        input_tokens=10,
        output_tokens=5,
        cost_estimate=0.000001,
    )


async def test_persist_moves_generating_job_to_drafted() -> None:
    persistence, jobs, job = await _setup(JobState.GENERATING)
    draft = _draft(job)

    outcome = await persistence.persist_drafted(draft)

    assert outcome.created is True
    assert outcome.draft.id == draft.id
    assert outcome.job.state == JobState.DRAFTED.value
    assert outcome.job.result_ref == {"draft_id": str(draft.id)}
    assert outcome.event is not None
    assert (outcome.event.state_from, outcome.event.state_to) == ("GENERATING", "DRAFTED")
    assert outcome.event.payload["draft_id"] == str(draft.id)
    assert outcome.event.payload["cost_estimate"] == 0.000001
    stored = await persistence.find_draft_for_job(job.organization_id, job.id)
    assert stored is not None and stored.id == draft.id


async def test_job_not_generating_is_refused_without_a_draft() -> None:
    persistence, _, job = await _setup(JobState.CONTEXT_READY)

    with pytest.raises(IllegalStateTransitionError):
        await persistence.persist_drafted(_draft(job))

    assert await persistence.find_draft_for_job(job.organization_id, job.id) is None


async def test_already_drafted_job_returns_the_existing_draft() -> None:
    persistence, jobs, job = await _setup(JobState.GENERATING)
    first = await persistence.persist_drafted(_draft(job))
    events_before = len(await jobs.list_events_for_job(job.organization_id, job.id))

    second = await persistence.persist_drafted(_draft(job))

    assert second.created is False
    assert second.event is None
    assert second.draft.id == first.draft.id
    assert len(await jobs.list_events_for_job(job.organization_id, job.id)) == events_before


async def test_draft_for_another_tenant_is_refused() -> None:
    persistence, _, job = await _setup(JobState.GENERATING)
    foreign = _draft(job)
    foreign.organization_id = uuid4()

    with pytest.raises(KeyError):
        await persistence.persist_drafted(foreign)


async def test_draft_without_job_is_refused() -> None:
    persistence, _, job = await _setup(JobState.GENERATING)
    orphan = _draft(job)
    orphan.job_id = None

    with pytest.raises(ValueError, match="job_id"):
        await persistence.persist_drafted(orphan)

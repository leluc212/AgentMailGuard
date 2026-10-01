"""Draft persistence against live PostgreSQL in rag_email_test (R16.4, R18.5, R19.4, R19.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed(pool: asyncpg.Pool, state: JobState) -> tuple[Job, uuid.UUID, uuid.UUID]:
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Box', 'active')",
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'c@example.com', 'C', '[]', 'Hi', 'Hi', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:6]}",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=state.value,
            idempotency_key=f"draft-persist-{uuid.uuid4()}",
        )
    )
    return job, msg_id, thread_id


def _draft(job: Job, msg_id: uuid.UUID, thread_id: uuid.UUID, cost: float | None) -> GeneratedDraft:
    return GeneratedDraft(
        organization_id=job.organization_id,
        job_id=job.id,
        message_id=msg_id,
        thread_id=thread_id,
        subject="Re: Hi",
        body="Hello from the model",
        confidence=0.9,
        citations=[{"citation_id": "DOC-1", "chunk_id": "c1"}],
        citation_mismatch=False,
        model_name="model-routine",
        model_tier="routine",
        escalation_reason="none",
        prompt_version="p.v1",
        input_tokens=1_200,
        output_tokens=300,
        cost_estimate=cost,
    )


async def _draft_count(pool: asyncpg.Pool, job_id: object) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval("SELECT count(*) FROM generated_draft WHERE job_id = $1", job_id)
        )


async def test_draft_and_transition_commit_together(db_pool: asyncpg.Pool) -> None:
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    outcome = await PostgresDraftPersistence(db_pool).persist_drafted(
        _draft(job, msg_id, thread_id, 0.00036)
    )

    assert outcome.created is True
    assert outcome.job.state == JobState.DRAFTED.value
    assert outcome.job.result_ref == {"draft_id": str(outcome.draft.id)}
    assert outcome.draft.cost_estimate == pytest.approx(0.00036)
    assert outcome.draft.citations == [{"citation_id": "DOC-1", "chunk_id": "c1"}]
    events = await PostgresJobStore(db_pool).list_events_for_job(job.organization_id, job.id)
    assert [(e.state_from, e.state_to) for e in events][-1] == ("GENERATING", "DRAFTED")
    assert events[-1].payload["draft_id"] == str(outcome.draft.id)


async def test_illegal_state_rolls_back_draft_insert(db_pool: asyncpg.Pool) -> None:
    """Review Focus 5: the job left GENERATING; no orphan draft may survive."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.CONTEXT_READY)

    with pytest.raises(IllegalStateTransitionError):
        await PostgresDraftPersistence(db_pool).persist_drafted(
            _draft(job, msg_id, thread_id, 0.0001)
        )

    assert await _draft_count(db_pool, job.id) == 0
    stored = await PostgresJobStore(db_pool).get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.CONTEXT_READY.value


async def test_unknown_cost_round_trips_as_null(db_pool: asyncpg.Pool) -> None:
    """Review Focus 3: an unpriced model stores NULL, and reads back as None."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    persistence = PostgresDraftPersistence(db_pool)
    await persistence.persist_drafted(_draft(job, msg_id, thread_id, None))

    stored = await persistence.find_draft_for_job(job.organization_id, job.id)
    assert stored is not None and stored.cost_estimate is None


async def test_redelivery_returns_existing_draft(db_pool: asyncpg.Pool) -> None:
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    persistence = PostgresDraftPersistence(db_pool)
    first = await persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001))

    second = await persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001))

    assert second.created is False and second.event is None
    assert second.draft.id == first.draft.id
    assert await _draft_count(db_pool, job.id) == 1


async def test_concurrent_persist_yields_one_draft(db_pool: asyncpg.Pool) -> None:
    """Review Focus 2: two workers racing on one job leave exactly one draft."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    persistence = PostgresDraftPersistence(db_pool)

    outcomes = await asyncio.gather(
        persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001)),
        persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001)),
    )

    assert sorted(o.created for o in outcomes) == [False, True]
    assert outcomes[0].draft.id == outcomes[1].draft.id
    assert await _draft_count(db_pool, job.id) == 1


async def test_unique_index_rejects_a_second_draft_for_a_job(db_pool: asyncpg.Pool) -> None:
    """The truth layer holds even for a writer that bypasses the unit of work."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    await PostgresDraftPersistence(db_pool).persist_drafted(_draft(job, msg_id, thread_id, 0.0))

    async with db_pool.acquire() as conn:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO generated_draft (id, organization_id, job_id, message_id, thread_id,"
                " action, body) VALUES ($1, $2, $3, $4, $5, 'reply', 'dup')",
                uuid.uuid4(),
                job.organization_id,
                job.id,
                msg_id,
                thread_id,
            )


async def test_drafted_job_releases_its_claim_lease_postgres(db_pool: asyncpg.Pool) -> None:
    """The lease clears in the same transaction as GENERATING -> DRAFTED (4.13b)."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE processing_job SET lease_expires_at = now() + interval '5 minutes'"
            " WHERE id = $1",
            job.id,
        )

    outcome = await PostgresDraftPersistence(db_pool).persist_drafted(
        _draft(job, msg_id, thread_id, 0.0001)
    )

    assert outcome.job.lease_expires_at is None
    async with db_pool.acquire() as conn:
        lease = await conn.fetchval(
            "SELECT lease_expires_at FROM processing_job WHERE id = $1", job.id
        )
    assert lease is None

"""DraftingService end to end on live PostgreSQL in rag_email_test (R18.1, R16.4, R19.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import asyncpg
import pytest

from packages.core.settings import AppSettings, ModelPricing
from packages.db.connection import create_pool_from_settings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from services.ai_worker.drafting import DraftingService

REPLY = {
    "action": "reply",
    "draft": "Hello, open Settings and choose Reset Password.",
    "confidence": 0.93,
    "knowledge_chunks": ["DOC-125-08", "DOC-404-00"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_context_ready(pool: asyncpg.Pool) -> tuple[Job, ContextPackage]:
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
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'c@example.com', 'C', '[]',
                      'Password reset', 'I forgot my password.', now())
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
            state=JobState.CONTEXT_READY.value,
            idempotency_key=f"drafting-{uuid.uuid4()}",
        )
    )
    message = NormalizedMessage(
        message_id=msg_id,
        thread_id=thread_id,
        mailbox_id=mbx_id,
        organization_id=org_id,
        provider="gmail",
        provider_message_id=f"prov-{msg_id.hex[:6]}",
        sender=EmailAddress(email="c@example.com", name="C"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    context = ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=[
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Open Settings and choose Reset Password.",
                external_id="DOC-125-08",
            )
        ],
    )
    return job, context


def _service(pool: asyncpg.Pool) -> DraftingService:
    return DraftingService(
        generator=SinglePassGenerator(
            llm_provider=FakeLLMProvider(default_response=REPLY),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=PostgresJobStore(pool),
        persistence=PostgresDraftPersistence(pool),
        price_table={"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)},
    )


async def test_context_ready_job_ends_drafted_with_full_record(db_pool: asyncpg.Pool) -> None:
    job, context = await _seed_context_ready(db_pool)

    outcome = await _service(db_pool).draft(job, context, category="technical_support")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM generated_draft WHERE job_id = $1", job.id)
        state = await conn.fetchval("SELECT state FROM processing_job WHERE id = $1", job.id)
    assert state == JobState.DRAFTED.value
    assert row is not None
    assert row["id"] == outcome.draft.id
    assert row["body"] == REPLY["draft"]
    assert row["subject"] == "Re: Password reset"
    assert row["model_name"] == "fake-fast-model"
    assert row["model_tier"] == "routine"
    assert row["escalation_reason"] == "none"
    assert row["prompt_version"]
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0
    assert row["cost_estimate"] is not None and row["cost_estimate"] > 0
    assert row["citation_mismatch"] is True  # DOC-404-00 was never supplied
    assert '"DOC-404-00"' not in row["citations"]


async def test_concurrent_deliveries_leave_one_draft(db_pool: asyncpg.Pool) -> None:
    """R19.7 / design §9: two deliveries of one job never produce two drafts."""
    job, context = await _seed_context_ready(db_pool)
    service = _service(db_pool)
    await service.job_store.transition_job_state(
        organization_id=job.organization_id, job_id=job.id, target_state=JobState.GENERATING
    )

    outcomes = await asyncio.gather(service.draft(job, context), service.draft(job, context))

    assert sorted(o.created for o in outcomes) == [False, True]
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM generated_draft WHERE job_id = $1", job.id
        )
    assert count == 1

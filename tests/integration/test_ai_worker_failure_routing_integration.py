"""Generation failure routing on a real broker and Postgres (4.13a; closes 4.9).

Scratch vhost: retry queues are declared with 1/2/3 s TTLs, which would 406 against the
shared test vhost's default-TTL queues.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.core.settings import (
    AppSettings,
    BrokerSettings,
    RetryLadderSettings,
    SummarizationSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.protocol import LLMResult, LLMTimeoutError
from packages.llm.router import ComplexityRouter
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.drafting import DraftingService
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
LANE = "email.support.normal"
REPLY = {
    "action": "reply",
    "draft": "Hello, open Settings and choose Reset Password.",
    "confidence": 0.93,
    "knowledge_chunks": [],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
INVALID = {"action": "reply"}


class _ScriptedProvider(FakeLLMProvider):
    def __init__(self, *script: Any) -> None:
        super().__init__()
        self.script = list(script)
        self.calls = 0

    async def generate(self, **kwargs: Any) -> LLMResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        self._default_response = dict(step)
        self._explicit_default = True
        return replace(await super().generate(**kwargs), raw_finish_reason="stop")


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "aiworker") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


async def _seed_queued_job(pool: asyncpg.Pool) -> Job:
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
            state=JobState.QUEUED.value,
            idempotency_key=f"aiw-{uuid.uuid4()}",
        )
    )
    return job


def _consumer(
    broker: BrokerSettings, pool: asyncpg.Pool, provider: _ScriptedProvider
) -> AIWorkerConsumer:
    jobs, messages = PostgresJobStore(pool), PostgresMessageStore(pool)
    return AIWorkerConsumer(
        LANE,
        job_store=jobs,
        message_store=messages,
        context_builder=ContextBuilder(
            thread_assembler=ThreadContextAssembler(
                settings=SummarizationSettings(),
                thread_state_store=PostgresThreadStateStore(pool),
                message_store=messages,
            ),
            job_store=jobs,
        ),
        router=ComplexityRouter(),
        drafting=DraftingService(
            generator=SinglePassGenerator(
                llm_provider=provider,
                profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
            ),
            job_store=jobs,
            persistence=PostgresDraftPersistence(pool),
            price_table={},
        ),
        broker_settings=broker,
        retry_settings=FAST_RETRY,
        prefetch_count=1,
    )


def _envelope(job: Job) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(job.organization_id),
        message_id=str(job.message_id),
        classification={"category": "support", "priority": "normal", "retrieval_required": False},
    )


async def _publish(broker: BrokerSettings, envelope: JobEnvelope) -> None:
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(broker.exchange_email_route, LANE, envelope)
    finally:
        await publisher.close()


async def _wait_for_state(pool: asyncpg.Pool, job: Job, state: JobState, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    current = None
    while asyncio.get_running_loop().time() < deadline:
        stored = await PostgresJobStore(pool).get_job(job.organization_id, job.id)
        current = stored.state if stored else None
        if current == state.value:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job.id} ended {current}, expected {state.value}")


async def _draft_count(pool: asyncpg.Pool, job: Job) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval("SELECT count(*) FROM generated_draft WHERE job_id = $1", job.id)
        )


async def _queue_is_empty(channel: AbstractChannel, name: str) -> bool:
    """True when a fetch finds no ready message.

    The passive-declare ``message_count`` proved unreliable here (it read 0 for a queue holding
    a freshly published message), so emptiness is checked by actually fetching.
    """
    queue = await channel.declare_queue(name, passive=True)
    return await queue.get(no_ack=True, fail=False, timeout=5) is None


async def test_transient_failure_retries_then_drafts_once(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Review Focus 4: timeout -> retry ladder -> redelivery -> exactly one draft, no DLQ."""
    job = await _seed_queued_job(pool)
    provider = _ScriptedProvider(LLMTimeoutError("upstream timeout"), REPLY)
    consumer = _consumer(broker, pool, provider)
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await consumer.stop()

    assert provider.calls == 2
    assert await _draft_count(pool, job) == 1
    assert await _queue_is_empty(channel, broker.queue_dead_letter)


async def test_invalid_output_twice_goes_straight_to_dead_letter(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Option 1: no retry ladder; DLQ with the reason; job DEAD_LETTER; no draft."""
    job = await _seed_queued_job(pool)
    provider = _ScriptedProvider(INVALID, INVALID)
    consumer = _consumer(broker, pool, provider)
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DEAD_LETTER, timeout_s=15)
    finally:
        await consumer.stop()

    assert provider.calls == 2
    assert await _draft_count(pool, job) == 0
    dlq = await channel.declare_queue(broker.queue_dead_letter, passive=True)
    message = await dlq.get(no_ack=True, fail=True, timeout=5)
    reason = str((message.headers or {}).get("x-failure-reason"))
    assert "UnvalidatedDraftError" in reason


async def test_redelivered_drafted_job_is_acked_and_dropped(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Review Focus 2: a redelivery after DRAFTED costs no model call and never dead-letters."""
    job = await _seed_queued_job(pool)
    provider = _ScriptedProvider(REPLY)
    consumer = _consumer(broker, pool, provider)
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=15)
        await _publish(broker, _envelope(job))
        await asyncio.sleep(2.0)  # let the consumer take and ack the second delivery
    finally:
        await consumer.stop()

    # The second delivery was consumed and acked: a delivered-but-unacked message would be
    # requeued by consumer.stop() and show up here.
    assert await _queue_is_empty(channel, LANE)
    assert provider.calls == 1
    assert await _draft_count(pool, job) == 1
    assert await _queue_is_empty(channel, broker.queue_dead_letter)

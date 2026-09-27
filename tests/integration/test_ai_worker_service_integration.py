"""The composed AI worker on a real broker and Postgres (4.13b; R11.7, R19.7, R22.8)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.settings import (
    AIWorkerSettings,
    AppSettings,
    BrokerSettings,
    RetryLadderSettings,
    SummarizationSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.llm.protocol import LLMResult
from packages.observability.health import HealthRegistry
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.main import build_consumers
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
LANE = "email.support.normal"


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "aisvc") as fast:
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


async def _seed_thread(pool: asyncpg.Pool, message_count: int) -> Job:
    org_id, mbx_id, thread_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    start = datetime.now(UTC) - timedelta(hours=message_count)
    last_msg = uuid.uuid4()
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
        for i in range(message_count):
            msg_id = last_msg if i == message_count - 1 else uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO email_message (
                    id, organization_id, mailbox_id, thread_id, provider_message_id,
                    direction, sender_email, sender_name, recipients, subject, body_text,
                    received_at
                ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'c@example.com', 'C', '[]',
                          'Setup help', $6, $7)
                """,
                msg_id,
                org_id,
                mbx_id,
                thread_id,
                f"prov-{msg_id.hex[:8]}",
                f"Message {i} about the setup.",
                start + timedelta(hours=i),
            )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=last_msg,
            thread_id=thread_id,
            state=JobState.QUEUED.value,
            idempotency_key=f"aisvc-{uuid.uuid4()}",
        )
    )
    return job


def _envelope(job: Job) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(job.organization_id),
        message_id=str(job.message_id),
        classification={"category": "support", "priority": "normal", "retrieval_required": True},
    )


async def _resources(
    broker: BrokerSettings, pool: asyncpg.Pool, metrics: PipelineMetrics
) -> WorkerResources:
    settings = AIWorkerSettings(
        broker=broker,
        retry=FAST_RETRY,
        summarization=SummarizationSettings(min_messages_threshold=4),
    )
    connection = await aio_pika.connect_robust(broker.url)
    publisher = MessagePublisher(
        broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
    )
    return WorkerResources(
        settings=settings,
        db_pool=pool,
        connection=connection,
        publisher=publisher,
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=metrics,
    )


def _lane_consumer(consumers: list[AIWorkerConsumer]) -> AIWorkerConsumer:
    return next(c for c in consumers if c.queue_name == LANE)


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


def _count(metrics: PipelineMetrics, name: str, **labels: str) -> float:
    total = 0.0
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
    return total


async def test_composed_worker_measures_every_request(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """A long-thread job through the built components moves every telemetry series."""
    metrics = create_pipeline_metrics()
    res = await _resources(broker, pool, metrics)
    job = await _seed_thread(pool, message_count=6)
    consumer = _lane_consumer(build_consumers(res, token_counter=TokenCounter()))
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await consumer.stop()
        await res.connection.close()

    assert _count(metrics, "llm_context_tokens_count", kind="summarize") == 1
    assert _count(metrics, "llm_context_tokens_count", kind="generate") == 1
    assert (
        _count(
            metrics,
            "emails_generated_total",
            organization=str(job.organization_id),
            category="support",
        )
        == 1
    )
    assert await _draft_count(pool, job) == 1


class _BlockingFake(FakeLLMProvider):
    """Schema-aware fake whose draft call blocks until released (a worker stuck mid-generation)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, **kwargs: Any) -> LLMResult:
        if "draft" in set((kwargs.get("schema") or {}).get("required", [])):
            self.entered.set()
            await self.release.wait()
        return await super().generate(**kwargs)


async def test_worker_killed_mid_generation_yields_one_draft(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Review Focus 1 (design §9, R19.7, R22.8): kill -> redelivery -> exactly one draft."""
    job = await _seed_thread(pool, message_count=1)
    stuck = _BlockingFake()
    res_a = await _resources(broker, pool, create_pipeline_metrics())
    worker_a = _lane_consumer(
        build_consumers(res_a, llm_provider=stuck, token_counter=TokenCounter())
    )
    await worker_a.start()
    await _publish(broker, _envelope(job))
    await asyncio.wait_for(stuck.entered.wait(), timeout=15)

    # The kill: the broker sees the connection drop with the delivery unacked and requeues it.
    await res_a.connection.close()

    res_b = await _resources(broker, pool, create_pipeline_metrics())
    worker_b = _lane_consumer(build_consumers(res_b, token_counter=TokenCounter()))
    await worker_b.start()
    try:
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await worker_b.stop()
        await res_b.connection.close()
        # Closing A's connection was the kill: it cancelled A's in-flight call (the call never
        # returns, so there is no stale persist to race) and released A's channel. Calling
        # worker_a.stop() on that closed connection would block forever, so A is left as is.

    assert await _draft_count(pool, job) == 1

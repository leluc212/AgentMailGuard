"""Live RabbitMQ integration tests for worker-level micro-batching (R3.6, R3.7).

Validates that micro-batching pulls N jobs together across AMQP transport,
executes independent inference per email, guarantees zero prompt cross-contamination,
and commits per-message manual acknowledgements.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from packages.broker.batch_consumer import BaseBatchConsumer, BatchItem
from packages.broker.envelope import JobEnvelope
from packages.broker.prompt_safety import (
    PromptExecutionRecord,
    assert_prompt_isolation,
)
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import BrokerSettings
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import ChatMessage

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    """Provide a dedicated robust connection and channel for testing."""
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await conn.channel()
    yield channel
    if not channel.is_closed:
        await channel.close()
    if not conn.is_closed:
        await conn.close()


class LiveBatchWorker(BaseBatchConsumer):
    """Test worker processing email jobs from RabbitMQ in micro-batches."""

    def __init__(
        self,
        queue_name: str,
        llm: FakeLLMProvider,
        batch_size: int = 5,
        batch_timeout_s: float = 0.1,
        broker_settings: BrokerSettings | None = None,
        publisher: MessagePublisher | None = None,
    ) -> None:
        b_cfg = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=queue_name,
            broker_settings=b_cfg,
            batch_size=batch_size,
            batch_timeout_s=batch_timeout_s,
            prefetch_count=10,
        )
        self.llm = llm
        if publisher:
            self._publisher = publisher
        self.batch_sizes_observed: list[int] = []
        self.recorded_prompts: list[PromptExecutionRecord] = []
        self.completed_jobs: list[str] = []

    async def pre_batch_hook(self, items: list[BatchItem]) -> None:
        """Record batch size observed by worker loop."""
        self.batch_sizes_observed.append(len(items))

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        """Execute independent per-job inference call (R3.7)."""
        payload = envelope.payload or {}
        subject = payload.get("subject", "No subject")
        sender = payload.get("sender_email", "unknown@test.com")
        body = payload.get("body_text", "")

        # Independent prompt construction: strictly restricted to this single email
        prompt_content = (
            f"Generate support response for message {envelope.message_id}.\n"
            f"Sender: {sender}\nSubject: {subject}\nBody: {body}"
        )
        messages = [
            ChatMessage(role="system", content="You are a helpful customer support agent."),
            ChatMessage(role="user", content=prompt_content),
        ]

        await self.llm.generate(messages=messages)
        self.recorded_prompts.append(PromptExecutionRecord.from_call(envelope, messages))
        self.completed_jobs.append(envelope.job_id)


@pytest.mark.asyncio
async def test_live_amqp_worker_micro_batching_and_prompt_isolation(
    broker_channel: AbstractChannel,
) -> None:
    """Verify live RabbitMQ micro-batch pulling and prompt isolation (R3.6, R3.7)."""
    b_cfg = BrokerSettings()
    await setup_topology(broker_channel, broker_settings=b_cfg)

    queue_name = "email.support.normal"
    q = await broker_channel.get_queue(queue_name)
    await q.purge()

    publisher = MessagePublisher(broker_settings=b_cfg, channel=broker_channel)
    fake_llm = FakeLLMProvider()

    worker = LiveBatchWorker(
        queue_name=queue_name,
        llm=fake_llm,
        batch_size=5,
        batch_timeout_s=0.1,
        broker_settings=b_cfg,
        publisher=publisher,
    )
    await worker.start()

    try:
        org_id = str(uuid4())

        # Publish a burst of 10 distinct emails into email.support.normal
        email_data = [
            (
                f"msg-supp-{i}",
                f"Support Request #{i}: Question about billing",
                f"user{i}@corp.io",
                f"Inquiry body content {i} details",
            )
            for i in range(10)
        ]

        for msg_id, sub, sender, body in email_data:
            envelope = JobEnvelope(
                idempotency_key=f"idem-burst-{msg_id}-{uuid4()}",
                job_type="generate_reply",
                organization_id=org_id,
                message_id=msg_id,
                payload={"subject": sub, "sender_email": sender, "body_text": body},
            )
            await publisher.publish(
                exchange_name=b_cfg.exchange_email_route,
                routing_key=queue_name,
                envelope=envelope,
            )

        # Wait for worker to consume and process all 10 messages in micro-batches
        for _ in range(50):
            if len(worker.completed_jobs) >= 10:
                break
            await asyncio.sleep(0.1)

        # 1. Assert all 10 jobs completed
        assert len(worker.completed_jobs) == 10

        # 2. Assert micro-batching occurred (batches aggregated up to 5 jobs - R3.6)
        assert sum(worker.batch_sizes_observed) == 10
        assert any(size > 1 for size in worker.batch_sizes_observed), (
            "Expected multi-item micro-batches"
        )
        assert all(size <= 5 for size in worker.batch_sizes_observed)

        # 3. Assert exactly 10 independent inference calls executed (R3.7)
        assert len(fake_llm.recorded_calls) == 10
        assert len(worker.recorded_prompts) == 10

        # 4. Assert strict prompt isolation across all 10 processed emails (R3.7)
        assert_prompt_isolation(worker.recorded_prompts)

        # 5. Verify pairwise cross-contamination across all pairs (i != j)
        for i, prompt_i in enumerate(worker.recorded_prompts):
            for j, prompt_j in enumerate(worker.recorded_prompts):
                if i != j:
                    assert prompt_j.message_id not in prompt_i.prompt_text
                    assert prompt_j.subject not in prompt_i.prompt_text
                    assert prompt_j.sender_email not in prompt_i.prompt_text

        # 6. Verify queue is empty (all messages individually ACKed after side effects - R3.3)
        queue_info = await q.declare()
        assert queue_info.message_count == 0

    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_live_amqp_partial_batch_timeout_flushing(
    broker_channel: AbstractChannel,
) -> None:
    """Verify partial batches flush cleanly upon timeout when fewer than N items arrive."""
    b_cfg = BrokerSettings()
    await setup_topology(broker_channel, broker_settings=b_cfg)

    queue_name = "email.billing.normal"
    q = await broker_channel.get_queue(queue_name)
    await q.purge()

    publisher = MessagePublisher(broker_settings=b_cfg, channel=broker_channel)
    fake_llm = FakeLLMProvider()

    worker = LiveBatchWorker(
        queue_name=queue_name,
        llm=fake_llm,
        batch_size=5,
        batch_timeout_s=0.25,  # 250ms timeout
        broker_settings=b_cfg,
        publisher=publisher,
    )
    await worker.start()

    try:
        org_id = str(uuid4())

        # Publish only 3 messages (less than batch_size=5)
        for i in range(3):
            envelope = JobEnvelope(
                idempotency_key=f"idem-partial-{i}-{uuid4()}",
                job_type="generate_reply",
                organization_id=org_id,
                message_id=f"msg-partial-{i}",
                payload={
                    "subject": f"Invoice query {i}",
                    "sender_email": f"bill{i}@org.com",
                    "body_text": f"Body {i}",
                },
            )
            await publisher.publish(
                exchange_name=b_cfg.exchange_email_route,
                routing_key=queue_name,
                envelope=envelope,
            )

        # Wait for timeout flush
        for _ in range(30):
            if len(worker.completed_jobs) >= 3:
                break
            await asyncio.sleep(0.1)

        assert len(worker.completed_jobs) == 3
        assert worker.batch_sizes_observed == [3]

        queue_info = await q.declare()
        assert queue_info.message_count == 0
    finally:
        await worker.stop()

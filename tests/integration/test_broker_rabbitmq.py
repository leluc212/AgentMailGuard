"""Integration tests for RabbitMQ broker topology, publisher, and consumer (R3.1–R3.5, R3.8).

Runs against the live RabbitMQ container on localhost:5672.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import BrokerSettings, RetryLadderSettings

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    """Provide a dedicated robust connection and channel for tests."""
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await conn.channel()
    yield channel
    if not channel.is_closed:
        await channel.close()
    if not conn.is_closed:
        await conn.close()


@pytest.mark.asyncio
async def test_topology_idempotent_declaration(broker_channel: AbstractChannel) -> None:
    """Verify setup_topology declares all exchanges and queues idempotently (R3.1, R3.2)."""
    settings = BrokerSettings()
    retry_cfg = RetryLadderSettings()

    # Run 1: Initial declaration
    topo1 = await setup_topology(broker_channel, settings, retry_cfg)
    assert len(topo1.exchanges) >= 11
    assert len(topo1.queues) >= 9

    # Verify primary exchanges exist
    expected_exchanges = [
        settings.exchange_mail_ingest,
        settings.exchange_email_process,
        settings.exchange_email_triage,
        settings.exchange_email_route,
        settings.exchange_email_dispatch,
        settings.exchange_knowledge_ingest,
        settings.exchange_retry,
        settings.exchange_dlx,
        f"{settings.exchange_retry}.30s",
        f"{settings.exchange_retry}.5m",
        f"{settings.exchange_retry}.30m",
    ]
    for ex_name in expected_exchanges:
        assert ex_name in topo1.exchanges

    # Verify queues exist
    expected_queues = [
        settings.queue_mail_sync,
        settings.queue_normalize,
        settings.queue_triage,
        settings.queue_dispatch,
        settings.queue_knowledge,
        settings.queue_dead_letter,
        "email.retry.30s",
        "email.retry.5m",
        "email.retry.30m",
    ]
    for q_name in expected_queues:
        assert q_name in topo1.queues

    # Run 2: Idempotent re-declaration
    topo2 = await setup_topology(broker_channel, settings, retry_cfg)
    assert len(topo2.exchanges) == len(topo1.exchanges)
    assert len(topo2.queues) == len(topo1.queues)


@pytest.mark.asyncio
async def test_persistent_publish_and_manual_ack_consume(broker_channel: AbstractChannel) -> None:
    """Verify persistent message publish, consumption, and manual ACK (R3.1, R3.3)."""
    settings = BrokerSettings()
    await setup_topology(broker_channel, settings)

    test_org_id = str(uuid.uuid4())
    test_msg_id = str(uuid.uuid4())
    test_job_id = str(uuid.uuid4())

    processed_jobs: list[JobEnvelope] = []
    process_event = asyncio.Event()

    class NormalizerConsumer(BaseConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            processed_jobs.append(envelope)
            process_event.set()

    # Purge queue before test for clean isolation
    norm_q = await broker_channel.get_queue(settings.queue_normalize)
    await norm_q.purge()

    consumer = NormalizerConsumer(queue_name=settings.queue_normalize, prefetch_count=5)
    await consumer.start()

    publisher = MessagePublisher(broker_settings=settings)
    await publisher.connect()

    try:
        envelope = JobEnvelope(
            job_id=test_job_id,
            idempotency_key=f"{test_org_id}:{test_msg_id}:norm",
            job_type="normalize",
            organization_id=test_org_id,
            message_id=test_msg_id,
            thread_id=str(uuid.uuid4()),
            classification={"intent": "question"},
        )

        await publisher.publish(
            exchange_name=settings.exchange_email_process,
            routing_key=settings.queue_normalize,
            envelope=envelope,
        )

        # Wait for consumer to process
        await asyncio.wait_for(process_event.wait(), timeout=5.0)

        assert len(processed_jobs) == 1
        assert processed_jobs[0].job_id == test_job_id
        assert processed_jobs[0].organization_id == test_org_id
        assert processed_jobs[0].classification["intent"] == "question"

    finally:
        await consumer.stop()
        await publisher.close()


@pytest.mark.asyncio
async def test_transient_failure_routes_to_retry_ladder(broker_channel: AbstractChannel) -> None:
    """Verify transient failures trigger retry ladder routing and metadata (R7.2, R19.5)."""
    settings = BrokerSettings()
    await setup_topology(broker_channel, settings)

    test_job_id = str(uuid.uuid4())
    retry_queue_name = "email.retry.30s"
    # Purge queues before test
    norm_q = await broker_channel.get_queue(settings.queue_normalize)
    await norm_q.purge()
    retry_q = await broker_channel.get_queue(retry_queue_name)
    await retry_q.purge()

    class FailingConsumer(BaseConsumer):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.failed_once = False

        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            if not self.failed_once:
                self.failed_once = True
                raise TransientError("Provider API timeout")

    consumer = FailingConsumer(queue_name=settings.queue_normalize)
    await consumer.start()

    publisher = MessagePublisher(broker_settings=settings)
    await publisher.connect()

    try:
        envelope = JobEnvelope(
            job_id=test_job_id,
            idempotency_key=f"test:{test_job_id}:retry",
            job_type="normalize",
            organization_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
            thread_id=str(uuid.uuid4()),
            attempt=0,
        )

        await publisher.publish(
            exchange_name=settings.exchange_email_process,
            routing_key=settings.queue_normalize,
            envelope=envelope,
        )

        # Wait for consumer to process failure and route to retry queue
        await asyncio.sleep(1.0)

        # Inspect retry queue
        msg = await retry_q.get(no_ack=True, timeout=2.0)
        assert msg is not None

        retried_envelope = JobEnvelope.from_message(msg)
        assert retried_envelope.job_id == test_job_id
        assert retried_envelope.attempt == 1

        # Verify retry headers
        assert msg.headers["x-original-routing-key"] == settings.queue_normalize
        assert "TransientError" in str(msg.headers.get("x-failure-reason", ""))

    finally:
        await consumer.stop()
        await publisher.close()


@pytest.mark.asyncio
async def test_terminal_failure_routes_to_dead_letter_queue(
    broker_channel: AbstractChannel,
) -> None:
    """Verify fatal or exhausted failures route to dlx.email / email.dead_letter (R3.5, R19.6)."""
    settings = BrokerSettings()
    await setup_topology(broker_channel, settings)

    test_job_id = str(uuid.uuid4())
    norm_q = await broker_channel.get_queue(settings.queue_normalize)
    await norm_q.purge()
    dlx_queue = await broker_channel.get_queue(settings.queue_dead_letter)
    await dlx_queue.purge()

    class FatalConsumer(BaseConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            raise FatalError("Corrupt MIME encoding cannot be parsed")

    consumer = FatalConsumer(queue_name=settings.queue_normalize)
    await consumer.start()

    publisher = MessagePublisher(broker_settings=settings)
    await publisher.connect()

    try:
        envelope = JobEnvelope(
            job_id=test_job_id,
            idempotency_key=f"test:{test_job_id}:fatal",
            job_type="normalize",
            organization_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
            thread_id=str(uuid.uuid4()),
            attempt=0,
        )

        await publisher.publish(
            exchange_name=settings.exchange_email_process,
            routing_key=settings.queue_normalize,
            envelope=envelope,
        )

        # Wait for dead-lettering
        await asyncio.sleep(1.0)

        # Inspect dead-letter queue
        dlx_msg = await dlx_queue.get(no_ack=True, timeout=2.0)
        assert dlx_msg is not None

        dead_envelope = JobEnvelope.from_message(dlx_msg)
        assert dead_envelope.job_id == test_job_id
        assert dlx_msg.headers["x-original-routing-key"] == settings.queue_normalize
        assert "FatalError: Corrupt MIME" in str(dlx_msg.headers.get("x-failure-reason", ""))

    finally:
        await consumer.stop()
        await publisher.close()


@pytest.mark.asyncio
async def test_retry_ladder_ttl_and_dlx_redelivery_preserves_routing_key(
    broker_channel: AbstractChannel,
) -> None:
    """Verify queue TTL expiry dead-letters back to origin exchange with key preserved (R7.2)."""
    settings = BrokerSettings()
    await setup_topology(broker_channel, settings)

    test_uid = uuid.uuid4().hex[:8]
    test_routing_key = f"email.billing.{test_uid}"

    # 1. Target worker queue bound to topic exchange email.route
    worker_queue_name = f"test.worker.{test_uid}"
    worker_q = await broker_channel.declare_queue(worker_queue_name, auto_delete=True)
    await worker_q.bind(
        await broker_channel.get_exchange(settings.exchange_email_route),
        routing_key=f"email.billing.{test_uid}",
    )

    # 2. Short-TTL retry queue (500ms) with DLX pointing to email.route
    retry_q_name = f"test.retry.500ms.{test_uid}"
    retry_fanout_name = f"retry.email.fanout.{test_uid}"

    retry_fanout = await broker_channel.declare_exchange(
        retry_fanout_name, aio_pika.ExchangeType.FANOUT, auto_delete=True
    )
    retry_q = await broker_channel.declare_queue(
        retry_q_name,
        auto_delete=True,
        arguments={
            "x-message-ttl": 500,
            "x-dead-letter-exchange": settings.exchange_email_route,
        },
    )
    await retry_q.bind(retry_fanout)

    # 3. Publish message through the fanout exchange with origin routing key
    envelope = JobEnvelope(
        job_id=str(uuid.uuid4()),
        idempotency_key=f"test:{test_uid}:ladder",
        job_type="generate_reply",
        organization_id=str(uuid.uuid4()),
        message_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        attempt=1,
    )
    msg = envelope.to_message(headers={"x-original-routing-key": test_routing_key})
    await retry_fanout.publish(msg, routing_key=test_routing_key)

    # 4. Wait for TTL (500ms) to expire and trigger dead-lettering into email.route
    await asyncio.sleep(1.2)

    # 5. Verify message arrived at the worker queue via email.route topic exchange
    redelivered_msg = await worker_q.get(no_ack=True, timeout=2.0)
    assert redelivered_msg is not None
    assert redelivered_msg.routing_key == test_routing_key

    received_envelope = JobEnvelope.from_message(redelivered_msg)
    assert received_envelope.job_id == envelope.job_id
    assert received_envelope.attempt == 1

    # Cleanup temporary queues and exchanges
    await worker_q.delete()
    await retry_q.delete()
    await retry_fanout.delete()

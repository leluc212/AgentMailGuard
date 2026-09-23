"""Integration tests for queue metrics against live RabbitMQ 3.13 (R7.5, R21.4, R20.5).

Tests verify:
1. QueueMonitor correctly samples real queue depths via passive declaration.
2. BaseConsumer records queue_wait_ms when consuming messages with real enqueued_at offsets.
3. Prometheus /metrics endpoint exposes queue_depth and queue_wait_ms with correct labels.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection
from prometheus_client import CollectorRegistry

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.queue_monitor import QueueMonitor
from packages.broker.topology import setup_topology
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.observability.metrics import (
    create_pipeline_metrics,
    generate_metrics_payload,
)

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def broker_conn() -> AsyncGenerator[AbstractRobustConnection, None]:
    """Provide a dedicated robust connection for tests."""
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    yield conn
    if not conn.is_closed:
        await conn.close()


@pytest.fixture
async def broker_channel(
    broker_conn: AbstractRobustConnection,
) -> AsyncGenerator[AbstractChannel, None]:
    """Provide a dedicated channel for tests."""
    channel = await broker_conn.channel()
    yield channel
    if not channel.is_closed:
        await channel.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_gauge_value(metrics: Any, metric_name: str, labels: dict[str, str]) -> float:
    """Extract gauge value from metrics registry."""
    for metric_family in metrics.registry.collect():
        if metric_family.name == metric_name:
            for sample in metric_family.samples:
                if sample.name == metric_name and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
    return -1.0


def _get_histogram_sum(metrics: Any, metric_name: str, labels: dict[str, str]) -> float:
    """Extract the _sum value from a Histogram metric."""
    for metric_family in metrics.registry.collect():
        if metric_family.name == metric_name:
            for sample in metric_family.samples:
                if sample.name == f"{metric_name}_sum" and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
    return 0.0


def _get_histogram_count(metrics: Any, metric_name: str, labels: dict[str, str]) -> float:
    """Extract the _count value from a Histogram metric."""
    for metric_family in metrics.registry.collect():
        if metric_family.name == metric_name:
            for sample in metric_family.samples:
                if sample.name == f"{metric_name}_count" and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Test 1: Live queue depth sampling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_queue_depth_sampling(
    broker_conn: AbstractRobustConnection,
    broker_channel: AbstractChannel,
) -> None:
    """Verify QueueMonitor reads real message counts via passive declaration (R7.5, R21.4)."""
    settings = BrokerSettings()
    retry_settings = RetryLadderSettings()
    await setup_topology(broker_channel, settings, retry_settings)

    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    # Use unique test queues from the declared topology
    test_queues = ["email.support.normal", settings.queue_dead_letter]

    # Purge queues first to ensure clean state
    for q_name in test_queues:
        q = await broker_channel.declare_queue(q_name, passive=True)
        await q.purge()

    # Publish 3 messages to email.support.normal
    publisher = MessagePublisher(broker_settings=settings, connection=broker_conn)
    for i in range(3):
        envelope = JobEnvelope(
            idempotency_key=f"depth-test-support-{i}-{uuid.uuid4().hex[:8]}",
            job_type="triage",
            organization_id=str(uuid.uuid4()),
        )
        await publisher.publish(
            exchange_name=settings.exchange_email_route,
            routing_key="email.support.normal",
            envelope=envelope,
        )

    # Publish 2 messages to dead letter queue
    for i in range(2):
        envelope = JobEnvelope(
            idempotency_key=f"depth-test-dlq-{i}-{uuid.uuid4().hex[:8]}",
            job_type="triage",
            organization_id=str(uuid.uuid4()),
        )
        await publisher.publish(
            exchange_name=settings.exchange_dlx,
            routing_key="email.dead_letter",
            envelope=envelope,
        )

    # Allow messages to settle
    await asyncio.sleep(0.2)

    # Create QueueMonitor targeting only our test queues
    monitor = QueueMonitor(
        connection=broker_conn,
        metrics=metrics,
        queues=test_queues,
    )

    results = await monitor.sample_queue_depths()

    # Verify depths
    assert results["email.support.normal"] == 3, (
        f"Expected 3 messages in email.support.normal, got {results['email.support.normal']}"
    )
    dlq_name = settings.queue_dead_letter
    assert results[dlq_name] == 2, (
        f"Expected 2 messages in {dlq_name}, got {results[dlq_name]}"
    )

    # Verify Prometheus gauges
    depth_support = _get_gauge_value(metrics, "queue_depth", {"queue": "email.support.normal"})
    assert depth_support == 3.0

    depth_dlq = _get_gauge_value(metrics, "queue_depth", {"queue": settings.queue_dead_letter})
    assert depth_dlq == 2.0

    # Cleanup: purge test queues
    for q_name in test_queues:
        q = await broker_channel.declare_queue(q_name, passive=True)
        await q.purge()


# ---------------------------------------------------------------------------
# Test 2: Live queue wait time and /metrics endpoint
# ---------------------------------------------------------------------------


class _WaitTimeTestConsumer(BaseConsumer):
    """Test consumer that records consumed envelopes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.consumed: list[JobEnvelope] = []

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        self.consumed.append(envelope)


@pytest.mark.asyncio
async def test_live_queue_wait_time_and_metrics_endpoint(
    broker_conn: AbstractRobustConnection,
    broker_channel: AbstractChannel,
) -> None:
    """Verify queue_wait_ms is recorded upon consumption and appears in /metrics (R7.5, R21.4)."""
    settings = BrokerSettings()
    retry_settings = RetryLadderSettings()
    await setup_topology(broker_channel, settings, retry_settings)

    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    test_queue = settings.queue_normalize

    # Purge
    q = await broker_channel.declare_queue(test_queue, passive=True)
    await q.purge()

    # Publish a message with enqueued_at 600ms in the past
    past_enqueued = datetime.now(UTC) - timedelta(milliseconds=600)
    envelope = JobEnvelope(
        idempotency_key=f"wait-test-{uuid.uuid4().hex[:8]}",
        job_type="normalize",
        organization_id=str(uuid.uuid4()),
        enqueued_at=past_enqueued,
    )
    publisher = MessagePublisher(broker_settings=settings, connection=broker_conn)
    await publisher.publish(
        exchange_name=settings.exchange_email_process,
        routing_key=test_queue,
        envelope=envelope,
    )

    await asyncio.sleep(0.1)

    # Create consumer
    consumer = _WaitTimeTestConsumer(
        queue_name=test_queue,
        broker_settings=settings,
        retry_settings=retry_settings,
        connection=broker_conn,
        metrics=metrics,
    )
    await consumer.start()

    # Wait for consumption
    for _ in range(20):
        await asyncio.sleep(0.1)
        if consumer.consumed:
            break

    await consumer.stop()

    # Verify message was consumed
    assert len(consumer.consumed) >= 1, "Expected at least 1 consumed message"

    # Verify wait time was recorded
    count = _get_histogram_count(metrics, "queue_wait_ms", {"queue": test_queue})
    assert count >= 1, f"Expected count >= 1, got {count}"

    sum_val = _get_histogram_sum(metrics, "queue_wait_ms", {"queue": test_queue})
    # Should be at least 500ms (allowing for jitter)
    assert sum_val >= 500.0, f"Expected sum >= 500ms, got {sum_val}"

    # Verify /metrics endpoint contains the metrics
    payload_bytes, content_type = generate_metrics_payload(reg)
    payload_text = payload_bytes.decode("utf-8")

    assert "queue_depth" in payload_text, "queue_depth not found in /metrics payload"
    assert "queue_wait_ms" in payload_text, "queue_wait_ms not found in /metrics payload"

    # Verify specific queue label appears
    assert f'queue="{test_queue}"' in payload_text, (
        f"queue label '{test_queue}' not found in /metrics payload"
    )

    # Cleanup
    q = await broker_channel.declare_queue(test_queue, passive=True)
    await q.purge()

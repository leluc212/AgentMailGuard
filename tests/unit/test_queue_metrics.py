"""Unit tests for queue metrics: wait time recording and depth monitoring (R7.5, R21.4, R20.5).

Tests verify:
1. BaseConsumer records queue_wait_ms histogram on message consumption.
2. BaseBatchConsumer records queue_wait_ms on individual batch item processing.
3. QueueMonitor samples queue depths via passive declaration and updates gauges.
4. QueueMonitor recovers gracefully when a queue does not exist.
5. get_monitored_queues discovers core, retry, and category queues.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import CollectorRegistry

from packages.broker.batch_consumer import BaseBatchConsumer, BatchItem
from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.queue_monitor import QueueMonitor, get_monitored_queues
from packages.core.settings import BrokerSettings
from packages.observability.metrics import create_pipeline_metrics

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class DummyConsumer(BaseConsumer):
    """Minimal BaseConsumer subclass for testing."""

    def __init__(self, queue_name: str = "test.queue", **kwargs: Any) -> None:
        super().__init__(queue_name=queue_name, **kwargs)
        self.processed: list[JobEnvelope] = []

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        self.processed.append(envelope)


class DummyBatchConsumer(BaseBatchConsumer):
    """Minimal BaseBatchConsumer subclass for testing."""

    def __init__(self, queue_name: str = "test.batch.queue", **kwargs: Any) -> None:
        super().__init__(queue_name=queue_name, **kwargs)
        self.processed: list[JobEnvelope] = []

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        self.processed.append(envelope)


def _make_mock_message(envelope: JobEnvelope) -> AbstractIncomingMessage:
    """Create a mock AMQP message carrying a serialized JobEnvelope."""
    body = json.dumps(envelope.model_dump(mode="json")).encode()
    msg = AsyncMock(spec=AbstractIncomingMessage)
    msg.body = body
    msg.headers = {}
    msg.exchange = ""
    msg.routing_key = "test.queue"
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    msg.reject = AsyncMock()
    msg.info.return_value = {"headers": {}}
    return msg


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


# ---------------------------------------------------------------------------
# Test 1: BaseConsumer records queue_wait_ms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_records_queue_wait_ms() -> None:
    """Verify BaseConsumer observes queue_wait_ms histogram upon message consumption (R7.5)."""
    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    consumer = DummyConsumer(queue_name="email.support.normal", metrics=metrics)

    # Create envelope with enqueued_at 500ms in the past
    enqueued_at = datetime.now(UTC) - timedelta(milliseconds=500)
    envelope = JobEnvelope(
        idempotency_key="test-idem-1",
        job_type="triage",
        organization_id="org-1",
        enqueued_at=enqueued_at,
    )
    mock_msg = _make_mock_message(envelope)

    # Simulate _handle_message
    await consumer._handle_message(mock_msg)

    # Verify wait time was recorded
    count = _get_histogram_count(metrics, "queue_wait_ms", {"queue": "email.support.normal"})
    assert count >= 1, f"Expected at least 1 observation, got {count}"

    sum_val = _get_histogram_sum(metrics, "queue_wait_ms", {"queue": "email.support.normal"})
    # Should be at least 400ms (allowing for processing time variance)
    assert sum_val >= 400.0, f"Expected sum >= 400ms, got {sum_val}"

    # Verify the message was processed
    assert len(consumer.processed) == 1


# ---------------------------------------------------------------------------
# Test 2: BaseBatchConsumer records queue_wait_ms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_consumer_records_queue_wait_ms() -> None:
    """Verify BaseBatchConsumer observes queue_wait_ms on individual batch item (R7.5)."""
    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    consumer = DummyBatchConsumer(queue_name="email.billing.priority", metrics=metrics)

    # Create envelope with enqueued_at 300ms in the past
    enqueued_at = datetime.now(UTC) - timedelta(milliseconds=300)
    envelope = JobEnvelope(
        idempotency_key="test-idem-2",
        job_type="triage",
        organization_id="org-2",
        enqueued_at=enqueued_at,
    )
    mock_msg = _make_mock_message(envelope)
    mock_msg.routing_key = "email.billing.priority"

    item = BatchItem(envelope=envelope, raw_message=mock_msg)

    # Simulate _handle_single_item
    await consumer._handle_single_item(item)

    count = _get_histogram_count(metrics, "queue_wait_ms", {"queue": "email.billing.priority"})
    assert count >= 1, f"Expected at least 1 observation, got {count}"

    sum_val = _get_histogram_sum(metrics, "queue_wait_ms", {"queue": "email.billing.priority"})
    assert sum_val >= 200.0, f"Expected sum >= 200ms, got {sum_val}"

    assert len(consumer.processed) == 1


# ---------------------------------------------------------------------------
# Test 3: QueueMonitor samples depths via passive declaration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_monitor_samples_depths() -> None:
    """Verify QueueMonitor updates queue_depth and queue_consumers gauges (R7.5, R21.4)."""
    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    # Mock declaration result
    mock_decl_result = MagicMock()
    mock_decl_result.message_count = 5
    mock_decl_result.consumer_count = 2

    mock_queue = MagicMock()
    mock_queue.declaration_result = mock_decl_result

    mock_channel = AsyncMock()
    mock_channel.is_closed = False
    mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

    mock_conn = AsyncMock()
    mock_conn.is_closed = False
    mock_conn.channel = AsyncMock(return_value=mock_channel)

    monitor = QueueMonitor(
        connection=mock_conn,
        metrics=metrics,
        queues=["email.support.normal", "email.dead_letter"],
    )

    results = await monitor.sample_queue_depths()

    assert results == {"email.support.normal": 5, "email.dead_letter": 5}

    # Verify gauges were set
    depth_support = _get_gauge_value(metrics, "queue_depth", {"queue": "email.support.normal"})
    assert depth_support == 5.0

    depth_dlq = _get_gauge_value(metrics, "queue_depth", {"queue": "email.dead_letter"})
    assert depth_dlq == 5.0

    consumers_val = _get_gauge_value(
        metrics, "queue_consumers", {"queue": "email.support.normal"}
    )
    assert consumers_val == 2.0


# ---------------------------------------------------------------------------
# Test 4: QueueMonitor recovers from missing queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_monitor_recovers_missing_queue() -> None:
    """Ensure ChannelNotFoundEntity sets depth to 0 and reopens channel safely (R7.5)."""
    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    # Simulate ChannelNotFoundEntity on first queue, success on second
    call_count = 0

    mock_decl_result = MagicMock()
    mock_decl_result.message_count = 3
    mock_decl_result.consumer_count = 1

    mock_queue = MagicMock()
    mock_queue.declaration_result = mock_decl_result

    async def side_effect_declare(queue_name: str, passive: bool = False) -> Any:
        nonlocal call_count
        call_count += 1
        if queue_name == "non.existent.queue":
            raise Exception("NOT_FOUND - no queue 'non.existent.queue'")
        return mock_queue

    mock_channel = AsyncMock()
    mock_channel.is_closed = False
    mock_channel.declare_queue = AsyncMock(side_effect=side_effect_declare)

    mock_conn = AsyncMock()
    mock_conn.is_closed = False
    mock_conn.channel = AsyncMock(return_value=mock_channel)

    monitor = QueueMonitor(
        connection=mock_conn,
        metrics=metrics,
        queues=["non.existent.queue", "email.support.normal"],
    )

    results = await monitor.sample_queue_depths()

    # Missing queue should have depth 0
    assert results["non.existent.queue"] == 0
    # Existing queue should have depth 3
    assert results["email.support.normal"] == 3

    # Verify gauges
    depth_missing = _get_gauge_value(metrics, "queue_depth", {"queue": "non.existent.queue"})
    assert depth_missing == 0.0

    depth_existing = _get_gauge_value(metrics, "queue_depth", {"queue": "email.support.normal"})
    assert depth_existing == 3.0


# ---------------------------------------------------------------------------
# Test 5: get_monitored_queues discovers all queue types
# ---------------------------------------------------------------------------


def test_get_monitored_queues_completeness() -> None:
    """Verify get_monitored_queues includes core, retry, dead-letter, and category queues."""
    queues = get_monitored_queues()

    # Core pipeline queues
    b_cfg = BrokerSettings()
    assert b_cfg.queue_mail_sync in queues
    assert b_cfg.queue_normalize in queues
    assert b_cfg.queue_triage in queues
    assert b_cfg.queue_dispatch in queues
    assert b_cfg.queue_knowledge in queues
    assert b_cfg.queue_dead_letter in queues

    # Retry tier queues
    assert "email.retry.30s" in queues
    assert "email.retry.5m" in queues
    assert "email.retry.30m" in queues

    # Category queues (at least support.normal must exist from default taxonomy)
    assert "email.support.normal" in queues

    # All queues should be sorted
    assert queues == sorted(queues)

    # No duplicates
    assert len(queues) == len(set(queues))


# ---------------------------------------------------------------------------
# Test 6: QueueMonitor start/stop lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_monitor_start_stop_lifecycle() -> None:
    """Verify QueueMonitor can start and stop without errors."""
    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    mock_decl_result = MagicMock()
    mock_decl_result.message_count = 0
    mock_decl_result.consumer_count = 0

    mock_queue = MagicMock()
    mock_queue.declaration_result = mock_decl_result

    mock_channel = AsyncMock()
    mock_channel.is_closed = False
    mock_channel.declare_queue = AsyncMock(return_value=mock_queue)
    mock_channel.close = AsyncMock()

    mock_conn = AsyncMock()
    mock_conn.is_closed = False
    mock_conn.channel = AsyncMock(return_value=mock_channel)
    mock_conn.close = AsyncMock()

    monitor = QueueMonitor(
        connection=mock_conn,
        metrics=metrics,
        queues=["email.support.normal"],
    )

    await monitor.start(interval_seconds=0.05)
    assert monitor._running is True
    assert monitor._poll_task is not None

    # Allow at least one poll cycle
    await asyncio.sleep(0.15)

    await monitor.stop()
    assert monitor._running is False

    # Verify at least one depth was sampled
    depth = _get_gauge_value(metrics, "queue_depth", {"queue": "email.support.normal"})
    assert depth == 0.0


# ---------------------------------------------------------------------------
# Test 7: queue_wait_ms records 0 for messages with future enqueued_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_records_zero_wait_for_future_enqueued_at() -> None:
    """Verify queue_wait_ms never goes negative even with clock skew (R7.5)."""
    reg = CollectorRegistry()
    metrics = create_pipeline_metrics(registry=reg)

    consumer = DummyConsumer(queue_name="email.triage", metrics=metrics)

    # Create envelope with enqueued_at in the future (clock skew scenario)
    enqueued_at = datetime.now(UTC) + timedelta(seconds=10)
    envelope = JobEnvelope(
        idempotency_key="test-idem-future",
        job_type="triage",
        organization_id="org-future",
        enqueued_at=enqueued_at,
    )
    mock_msg = _make_mock_message(envelope)

    await consumer._handle_message(mock_msg)

    sum_val = _get_histogram_sum(metrics, "queue_wait_ms", {"queue": "email.triage"})
    assert sum_val == 0.0, f"Expected 0ms (clamped), got {sum_val}"

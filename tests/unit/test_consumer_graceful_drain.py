"""Graceful shutdown drain ordering for broker consumers (RA.6, R20.8, R3.3)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelInvalidStateError

from packages.broker.batch_consumer import BaseBatchConsumer
from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.observability.health import HealthRegistry
from packages.observability.shutdown import GracefulShutdownCoordinator


class FakeChannel:
    def __init__(self) -> None:
        self.is_closed = False
        self.close_calls = 0

    async def close(self, exc: Any = None) -> None:
        self.close_calls += 1
        self.is_closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.is_closed = False
        self.close_calls = 0

    async def close(self, exc: Any = None) -> None:
        self.close_calls += 1
        self.is_closed = True


class FakeQueue:
    def __init__(self) -> None:
        self.cancelled_tags: list[str] = []

    async def cancel(self, consumer_tag: str, timeout: Any = None, nowait: bool = False) -> None:
        self.cancelled_tags.append(consumer_tag)


class ChannelBoundMessage:
    """Delivery whose ack fails once its channel is closed, like aio_pika.IncomingMessage."""

    def __init__(self, envelope: JobEnvelope, channel: FakeChannel) -> None:
        self.body = envelope.model_dump_json().encode("utf-8")
        self.exchange = "email.process"
        self.routing_key = "email.normalize"
        self.headers: dict[str, Any] = {}
        self._channel = channel
        self.acked = False

    async def ack(self, multiple: bool = False) -> None:
        if self._channel.is_closed:
            raise ChannelInvalidStateError("channel closed before ack")
        self.acked = True


def make_envelope(job_id: str = "job-drain") -> JobEnvelope:
    return JobEnvelope(
        job_id=job_id,
        idempotency_key=f"idem-{job_id}",
        job_type="normalize",
        organization_id=str(uuid4()),
    )


def make_publisher() -> MessagePublisher:
    publisher = MagicMock(spec=MessagePublisher)
    publisher.publish_to_retry = AsyncMock()
    publisher.publish_to_dead_letter = AsyncMock()
    return cast(MessagePublisher, publisher)


class GatedConsumer(BaseConsumer):
    """process_job blocks until the test releases it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(queue_name="test.drain.queue", **kwargs)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        self.started.set()
        await self.release.wait()
        self.finished = True


class GatedBatchConsumer(BaseBatchConsumer):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            queue_name="test.drain.batch.queue",
            prefetch_count=4,
            batch_size=2,
            batch_timeout_s=0.01,
            **kwargs,
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        self.started.set()
        await self.release.wait()


def wire_fakes(
    consumer: BaseConsumer,
    connection: FakeConnection | None = None,
) -> tuple[FakeChannel, FakeQueue]:
    """Put the consumer in its post-start() state without a broker."""
    channel, queue = FakeChannel(), FakeQueue()
    consumer._channel = cast(Any, channel)
    consumer._queue = cast(Any, queue)
    consumer._consumer_tag = "ctag-test"
    consumer._is_consuming = True
    consumer._publisher = make_publisher()
    if connection is not None:
        consumer._connection = cast(Any, connection)
    return channel, queue


@pytest.mark.asyncio
async def test_in_flight_job_finishing_after_shutdown_is_acked() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0, health_registry=HealthRegistry("t"))
    consumer = GatedConsumer(shutdown_coordinator=coord)
    channel, queue = wire_fakes(consumer)
    msg = ChannelBoundMessage(make_envelope(), channel)

    job = asyncio.create_task(consumer._handle_message(cast(AbstractIncomingMessage, msg)))
    await consumer.started.wait()

    shutdown = asyncio.create_task(coord.trigger_shutdown("TEST_SIGTERM"))
    await asyncio.sleep(0.05)

    # Drain phase: subscription cancelled, channel still open for the in-flight ack
    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.is_closed is False
    assert coord.active_jobs_count == 1

    consumer.release.set()
    await shutdown
    (result,) = await asyncio.gather(job, return_exceptions=True)

    assert result is None
    assert consumer.finished is True
    assert msg.acked is True
    assert channel.close_calls == 1  # closed in the cleanup phase, after the ack
    assert consumer._publisher is not None
    retry_mock = cast(AsyncMock, consumer._publisher.publish_to_retry)
    retry_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_closes_channel_when_drain_times_out() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=0.05)
    consumer = GatedConsumer(shutdown_coordinator=coord)
    channel, _ = wire_fakes(consumer)
    msg = ChannelBoundMessage(make_envelope("job-stuck"), channel)

    job = asyncio.create_task(consumer._handle_message(cast(AbstractIncomingMessage, msg)))
    await consumer.started.wait()

    await coord.trigger_shutdown("TEST_SIGTERM")

    # Timeout path: channel closed anyway so RabbitMQ requeues the unacked delivery
    assert channel.is_closed is True
    assert msg.acked is False
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_consuming_is_idempotent_and_keeps_channel_open() -> None:
    consumer = GatedConsumer()
    channel, queue = wire_fakes(consumer)

    await consumer.stop_consuming()
    await consumer.stop_consuming()

    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.is_closed is False


@pytest.mark.asyncio
async def test_stop_wrapper_cancels_then_closes_owned_connection_once() -> None:
    connection = FakeConnection()
    consumer = GatedConsumer()
    channel, queue = wire_fakes(consumer, connection=connection)

    await consumer.stop()
    await consumer.stop()

    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.close_calls == 1
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_close_leaves_external_connection_open() -> None:
    connection = FakeConnection()
    consumer = GatedConsumer(connection=cast(Any, connection))
    channel, _ = wire_fakes(consumer)

    await consumer.close()

    assert channel.is_closed is True
    assert connection.close_calls == 0


@pytest.mark.asyncio
async def test_batch_in_flight_job_finishing_after_shutdown_is_acked() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0)
    consumer = GatedBatchConsumer(shutdown_coordinator=coord)
    channel, queue = wire_fakes(consumer)
    msg = ChannelBoundMessage(make_envelope("job-batch"), channel)

    await consumer._on_message_delivered(cast(AbstractIncomingMessage, msg))
    consumer._batch_loop_task = asyncio.create_task(consumer._batch_worker_loop())
    await consumer.started.wait()

    shutdown = asyncio.create_task(coord.trigger_shutdown("TEST_SIGTERM"))
    await asyncio.sleep(0.05)
    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.is_closed is False

    consumer.release.set()
    await shutdown

    assert msg.acked is True
    assert channel.close_calls == 1
    assert consumer._batch_loop_task.done()


@pytest.mark.asyncio
async def test_coordinator_runs_drain_then_waits_for_jobs_then_cleanup() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0)
    events: list[str] = []

    async def on_drain() -> None:
        events.append("drain")

    async def on_cleanup() -> None:
        events.append("cleanup")

    coord.register_drain_callback(on_drain)
    coord.register_cleanup_callback(on_cleanup)

    async def job() -> None:
        with coord.track_job():
            await asyncio.sleep(0.05)
            events.append("job_done")

    task = asyncio.create_task(job())
    await asyncio.sleep(0)
    await coord.trigger_shutdown("TEST")
    await task

    assert events == ["drain", "job_done", "cleanup"]

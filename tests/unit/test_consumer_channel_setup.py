"""Channel setup and ack placement for consumers and publishers (RA.5, R3.1, R3.3)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelInvalidStateError

from packages.broker.batch_consumer import BaseBatchConsumer
from packages.broker.consumer import BaseConsumer, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings


class _RecordingChannel:
    def __init__(self) -> None:
        self.is_closed = False
        self.qos: int | None = None
        self.declared: list[tuple[str, dict[str, Any]]] = []

    async def set_qos(self, prefetch_count: int) -> None:
        self.qos = prefetch_count

    async def declare_queue(self, name: str, **kwargs: Any) -> AsyncMock:
        self.declared.append((name, kwargs))
        queue = AsyncMock()
        queue.consume = AsyncMock(return_value="ctag-1")
        return queue

    async def close(self) -> None:
        self.is_closed = True


class _RecordingConnection:
    def __init__(self) -> None:
        self.is_closed = False
        self.channel_kwargs: list[dict[str, Any]] = []
        self.channel_obj = _RecordingChannel()

    async def channel(self, **kwargs: Any) -> _RecordingChannel:
        self.channel_kwargs.append(kwargs)
        return self.channel_obj


class _OkConsumer(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        return None


class _OkBatchConsumer(BaseBatchConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        return None


class _FlakyConsumer(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        raise TransientError("flaky")


class _FlakyBatchConsumer(BaseBatchConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        raise TransientError("flaky")


def _envelope_message(ack: AsyncMock) -> MagicMock:
    envelope = JobEnvelope(
        idempotency_key=f"k-{uuid4()}", job_type="normalize", organization_id=str(uuid4())
    )
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = envelope.model_dump_json().encode("utf-8")
    msg.exchange = "email.process"
    msg.routing_key = "email.normalize"
    msg.headers = {}
    msg.ack = ack
    return msg


def _mock_publisher() -> MagicMock:
    pub = MagicMock(spec=MessagePublisher)
    pub.publish_to_retry = AsyncMock()
    pub.publish_to_dead_letter = AsyncMock()
    pub.settings = BrokerSettings()
    return pub


async def test_consumer_start_uses_raising_channel_and_passive_declare() -> None:
    conn = _RecordingConnection()
    consumer = _OkConsumer(queue_name="email.normalize", connection=cast(Any, conn))

    await consumer.start()

    assert conn.channel_kwargs == [{"on_return_raises": True}]
    assert conn.channel_obj.declared == [("email.normalize", {"passive": True})]


async def test_batch_consumer_start_uses_raising_channel_and_passive_declare() -> None:
    conn = _RecordingConnection()
    consumer = _OkBatchConsumer(
        queue_name="email.normalize",
        connection=cast(Any, conn),
        batch_size=2,
        batch_timeout_s=0.01,
    )

    await consumer.start()
    consumer._is_consuming = False
    assert consumer._batch_loop_task is not None
    await asyncio.wait_for(consumer._batch_loop_task, timeout=2)

    assert conn.channel_kwargs == [{"on_return_raises": True}]
    assert conn.channel_obj.declared == [("email.normalize", {"passive": True})]


async def test_publisher_connect_uses_raising_channel() -> None:
    conn = _RecordingConnection()
    publisher = MessagePublisher(connection=cast(Any, conn))

    await publisher.connect()

    assert conn.channel_kwargs == [{"on_return_raises": True}]


async def test_failed_ack_does_not_publish_a_retry() -> None:
    consumer = _OkConsumer(queue_name="email.normalize")
    pub = _mock_publisher()
    consumer._publisher = pub
    msg = _envelope_message(AsyncMock(side_effect=ChannelInvalidStateError("closed")))

    await consumer._handle_message(msg)  # must not raise

    pub.publish_to_retry.assert_not_awaited()
    pub.publish_to_dead_letter.assert_not_awaited()


async def test_failed_ack_after_retry_publish_is_contained() -> None:
    consumer = _FlakyConsumer(queue_name="email.normalize")
    pub = _mock_publisher()
    consumer._publisher = pub
    msg = _envelope_message(AsyncMock(side_effect=ChannelInvalidStateError("closed")))

    await consumer._handle_message(msg)  # must not raise

    pub.publish_to_retry.assert_awaited_once()


async def test_batch_failed_ack_does_not_publish_a_retry() -> None:
    consumer = _OkBatchConsumer(queue_name="email.normalize", batch_size=1, batch_timeout_s=0.01)
    pub = _mock_publisher()
    consumer._publisher = pub
    msg = _envelope_message(AsyncMock(side_effect=ChannelInvalidStateError("closed")))
    consumer._is_consuming = True

    await consumer._on_message_delivered(msg)
    task = asyncio.create_task(consumer._batch_worker_loop())
    await asyncio.sleep(0.05)
    consumer._is_consuming = False
    await asyncio.wait_for(task, timeout=2)

    pub.publish_to_retry.assert_not_awaited()
    pub.publish_to_dead_letter.assert_not_awaited()


async def test_batch_failed_ack_after_retry_publish_is_contained() -> None:
    consumer = _FlakyBatchConsumer(queue_name="email.normalize", batch_size=1, batch_timeout_s=0.01)
    pub = _mock_publisher()
    consumer._publisher = pub
    msg = _envelope_message(AsyncMock(side_effect=ChannelInvalidStateError("closed")))
    consumer._is_consuming = True

    await consumer._on_message_delivered(msg)
    task = asyncio.create_task(consumer._batch_worker_loop())
    await asyncio.sleep(0.05)
    consumer._is_consuming = False
    await asyncio.wait_for(task, timeout=2)

    pub.publish_to_retry.assert_awaited_once()

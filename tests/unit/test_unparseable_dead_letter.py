"""Unparseable deliveries are dead-lettered with their raw body (RA.4, R3.5, R3.3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher


def _raw_message(
    body: bytes,
    exchange: str = "email.process",
    routing_key: str = "email.normalize",
    headers: dict[str, Any] | None = None,
) -> MagicMock:
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = body
    msg.exchange = exchange
    msg.routing_key = routing_key
    msg.headers = headers or {}
    msg.content_type = "application/json"
    msg.content_encoding = None
    msg.message_id = "m-1"
    msg.correlation_id = None
    msg.timestamp = None  # a MagicMock timestamp would break aio_pika.Message encoding
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    return msg


class _NeverCalled(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        raise AssertionError("process_job must not run for unparseable input")


async def test_base_consumer_dead_letters_unparseable_with_raw_body() -> None:
    consumer = _NeverCalled(queue_name="email.normalize")
    pub = MagicMock(spec=MessagePublisher)
    pub.publish_raw_to_dead_letter = AsyncMock()
    consumer._publisher = pub
    msg = _raw_message(b"NOT JSON {")

    await consumer._handle_message(msg)

    pub.publish_raw_to_dead_letter.assert_awaited_once()
    kw = pub.publish_raw_to_dead_letter.await_args.kwargs
    assert kw["message"] is msg
    assert kw["failure_reason"].startswith("EnvelopeParseError")
    assert kw["origin_exchange"] == "email.process"
    assert kw["origin_routing_key"] == "email.normalize"
    msg.ack.assert_awaited_once()
    msg.nack.assert_not_awaited()


async def test_base_consumer_requeues_when_dead_letter_publish_fails() -> None:
    consumer = _NeverCalled(queue_name="email.normalize")
    pub = MagicMock(spec=MessagePublisher)
    pub.publish_raw_to_dead_letter = AsyncMock(side_effect=ConnectionError("broker gone"))
    consumer._publisher = pub
    msg = _raw_message(b"\xff\xfe")

    await consumer._handle_message(msg)

    msg.ack.assert_not_awaited()
    msg.nack.assert_awaited_once_with(requeue=True)


async def test_publish_raw_to_dead_letter_preserves_body_and_headers() -> None:
    publisher = MessagePublisher()
    exchange = AsyncMock()
    publisher._get_exchange = AsyncMock(return_value=exchange)  # type: ignore[method-assign]
    msg = _raw_message(b"NOT JSON {", headers={"trace_id": "t-1"})

    await publisher.publish_raw_to_dead_letter(
        message=msg,
        failure_reason="EnvelopeParseError: JSONDecodeError: x",
        origin_exchange="email.process",
        origin_routing_key="email.normalize",
    )

    publisher._get_exchange.assert_awaited_once_with("dlx.email")
    published = exchange.publish.call_args[0][0]
    assert published.body == b"NOT JSON {"
    assert published.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert published.headers["trace_id"] == "t-1"
    assert published.headers["x-original-routing-key"] == "email.normalize"
    assert published.headers["x-failure-reason"].startswith("EnvelopeParseError")
    assert "x-failed-at" in published.headers
    assert exchange.publish.call_args[1]["routing_key"] == "email.normalize"

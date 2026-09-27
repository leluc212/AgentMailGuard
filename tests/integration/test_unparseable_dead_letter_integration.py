"""An unparseable delivery reaches email.dead_letter with its original body (RA.4, R3.5)."""

from __future__ import annotations

import asyncio

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from tests.integration.isolation import scratch_vhost


async def _wait(queue: AbstractQueue, timeout_s: float) -> AbstractIncomingMessage:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        msg = await queue.get(no_ack=True, fail=False)
        if msg is not None:
            return msg
        await asyncio.sleep(0.1)
    raise AssertionError(f"no message on {queue.name} within {timeout_s}s")


class _NeverCalled(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        raise AssertionError("must not be called")


async def test_unparseable_message_is_dead_lettered_with_raw_body() -> None:
    async with scratch_vhost(AppSettings().broker, "unparseable") as broker:
        conn = await aio_pika.connect_robust(broker.url)
        consumer = _NeverCalled(queue_name=broker.queue_normalize, broker_settings=broker)
        try:
            channel = await conn.channel()
            await setup_topology(channel, broker)
            await consumer.start()
            ex = await channel.get_exchange(broker.exchange_email_process)
            await ex.publish(
                aio_pika.Message(
                    b"NOT JSON {",
                    headers={"trace_id": "t-1"},
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=broker.queue_normalize,
            )
            dlq = await channel.get_queue(broker.queue_dead_letter)
            msg = await _wait(dlq, timeout_s=5)
            norm = await channel.get_queue(broker.queue_normalize)
            leftover = await norm.get(no_ack=True, fail=False)
        finally:
            await consumer.stop()
            await conn.close()

    assert msg.body == b"NOT JSON {"
    assert msg.headers["trace_id"] == "t-1"
    assert msg.headers["x-original-exchange"] == "email.process"
    assert msg.headers["x-original-routing-key"] == "email.normalize"
    assert str(msg.headers["x-failure-reason"]).startswith("EnvelopeParseError")
    assert leftover is None

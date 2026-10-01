"""Live retry-return routing on a throwaway vhost (RA.3, R3.5, R19.5).

A scratch vhost is required: the tests declare retry queues with 1/2/3 s TTLs, which
would conflict (406) with the default-TTL queues in the shared test vhost.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractQueue

from packages.broker.consumer import BaseConsumer, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import RETRY_ORIGIN_EXCHANGE_HEADER, MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings
from tests.integration.isolation import management_url, scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
ORIGIN_QUEUES = [
    ("mail.ingest", "mail.sync.requested"),
    ("email.process", "email.normalize"),
    ("email.triage", "email.triage"),
    ("email.route", "email.billing.priority"),
    ("email.dispatch", "email.dispatch"),
    ("knowledge.ingest", "knowledge.ingest"),
]


@pytest.fixture
async def fast_broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "retry") as broker:
        yield broker


@pytest.fixture
async def topo_channel(fast_broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(fast_broker.url)
    channel = await conn.channel()
    await setup_topology(channel, fast_broker, FAST_RETRY)
    try:
        yield channel
    finally:
        await conn.close()  # before the vhost fixture deletes the vhost


async def wait_for_message(queue: AbstractQueue, timeout_s: float) -> AbstractIncomingMessage:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        msg = await queue.get(no_ack=True, fail=False)
        if msg is not None:
            return msg
        await asyncio.sleep(0.1)
    raise AssertionError(f"no message on {queue.name} within {timeout_s}s")


def make_envelope(attempt: int) -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"it:{uuid4()}",
        job_type="normalize",
        organization_id=str(uuid4()),
        message_id=str(uuid4()),
        attempt=attempt,
    )


async def test_retry_queues_dead_letter_into_retry_return(
    fast_broker: BrokerSettings, topo_channel: AbstractChannel
) -> None:
    vh = quote(fast_broker.vhost, safe="")
    async with httpx.AsyncClient(
        base_url=management_url(fast_broker),
        auth=(fast_broker.user, fast_broker.password),
        timeout=10.0,
    ) as http:
        for suffix, ttl in (("30s", 1000), ("5m", 2000), ("30m", 3000)):
            q = (await http.get(f"/api/queues/{vh}/email.retry.{suffix}")).json()
            assert q["arguments"] == {
                "x-message-ttl": ttl,
                "x-dead-letter-exchange": "retry.return",
            }
        ex = (await http.get(f"/api/exchanges/{vh}/retry.return")).json()
        assert ex["type"] == "headers"
        assert ex["arguments"] == {"alternate-exchange": "dlx.email"}
        binds = (await http.get(f"/api/exchanges/{vh}/retry.return/bindings/source")).json()
    got = {
        (b["destination"], b["destination_type"], b["arguments"][RETRY_ORIGIN_EXCHANGE_HEADER])
        for b in binds
    }
    assert got == {(origin, "exchange", origin) for origin, _ in ORIGIN_QUEUES}


@pytest.mark.parametrize(("origin_exchange", "origin_queue"), ORIGIN_QUEUES)
async def test_expired_retry_returns_to_origin_queue(
    fast_broker: BrokerSettings,
    topo_channel: AbstractChannel,
    origin_exchange: str,
    origin_queue: str,
) -> None:
    publisher = MessagePublisher(broker_settings=fast_broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        env = make_envelope(attempt=1)
        await publisher.publish_to_retry(
            envelope=env,
            tier_delay_s=FAST_RETRY.tier_1_delay_s,
            origin_exchange=origin_exchange,
            origin_routing_key=origin_queue,
            failure_reason="TransientError: it",
        )
        msg = await wait_for_message(await topo_channel.get_queue(origin_queue), timeout_s=10)
    finally:
        await publisher.close()

    assert JobEnvelope.from_message(msg).job_id == env.job_id
    assert msg.routing_key == origin_queue
    assert msg.exchange == fast_broker.exchange_retry_return  # DLX replaces the exchange name
    assert msg.headers[RETRY_ORIGIN_EXCHANGE_HEADER] == origin_exchange  # headers survive DLX
    assert "x-death" in msg.headers
    dlq = await topo_channel.get_queue(fast_broker.queue_dead_letter)
    assert await dlq.get(no_ack=True, fail=False) is None


async def test_unmatched_retry_origin_goes_to_dead_letter_not_dropped(
    fast_broker: BrokerSettings, topo_channel: AbstractChannel
) -> None:
    publisher = MessagePublisher(broker_settings=fast_broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        env = make_envelope(attempt=1)
        await publisher.publish_to_retry(
            envelope=env,
            tier_delay_s=1,
            origin_exchange="no.such.exchange",
            origin_routing_key="email.normalize",
            failure_reason="TransientError: it",
        )
        dlq = await topo_channel.get_queue(fast_broker.queue_dead_letter)
        msg = await wait_for_message(dlq, timeout_s=10)
    finally:
        await publisher.close()
    assert JobEnvelope.from_message(msg).job_id == env.job_id
    assert "x-death" in msg.headers


async def test_transient_failures_are_redelivered_to_email_normalize(
    fast_broker: BrokerSettings, topo_channel: AbstractChannel
) -> None:
    deliveries: list[tuple[int, str | None, str | None, Any]] = []
    done = asyncio.Event()

    class FlakyNormalizer(BaseConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            deliveries.append(
                (
                    envelope.attempt,
                    raw_message.exchange,
                    raw_message.routing_key,
                    (raw_message.headers or {}).get(RETRY_ORIGIN_EXCHANGE_HEADER),
                )
            )
            if envelope.attempt < 2:
                raise TransientError(f"provider timeout #{envelope.attempt}")
            done.set()

    consumer = FlakyNormalizer(
        queue_name=fast_broker.queue_normalize,
        broker_settings=fast_broker,
        retry_settings=FAST_RETRY,
        prefetch_count=1,
    )
    publisher = MessagePublisher(broker_settings=fast_broker, retry_settings=FAST_RETRY)
    await consumer.start()
    await publisher.connect()
    try:
        await publisher.publish(
            exchange_name=fast_broker.exchange_email_process,
            routing_key=fast_broker.queue_normalize,
            envelope=make_envelope(attempt=0),
        )
        await asyncio.wait_for(done.wait(), timeout=20)  # ~1 s (tier 1) + ~2 s (tier 2)
    finally:
        await consumer.stop()
        await publisher.close()

    assert [d[0] for d in deliveries] == [0, 1, 2]
    assert deliveries[0][1] == "email.process"
    for _attempt, exchange, routing_key, origin in deliveries[1:]:
        assert exchange == fast_broker.exchange_retry_return
        assert routing_key == fast_broker.queue_normalize
        assert origin == "email.process"  # the second failure resolved its origin via header
    dlq = await topo_channel.get_queue(fast_broker.queue_dead_letter)
    assert await dlq.get(no_ack=True, fail=False) is None

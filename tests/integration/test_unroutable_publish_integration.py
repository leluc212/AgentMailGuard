"""Unroutable mandatory publishes raise instead of vanishing (RA.5, R3.1, R7.1)."""

from __future__ import annotations

from uuid import uuid4

import aio_pika
import pytest
from aio_pika.exceptions import PublishError

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from tests.integration.isolation import scratch_vhost


def _envelope() -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"unroutable:{uuid4()}",
        job_type="generate_reply",
        organization_id=str(uuid4()),
    )


async def test_unroutable_publish_raises() -> None:
    async with scratch_vhost(AppSettings().broker, "unroutable") as broker:
        conn = await aio_pika.connect_robust(broker.url)
        publisher = MessagePublisher(broker_settings=broker)
        try:
            await setup_topology(await conn.channel(), broker)
            await publisher.connect()
            with pytest.raises(PublishError):
                await publisher.publish(
                    exchange_name=broker.exchange_email_route,
                    routing_key="email.not_a_category.normal",
                    envelope=_envelope(),
                )
            # The channel survives a return: a routable publish still succeeds.
            await publisher.publish(
                exchange_name=broker.exchange_email_route,
                routing_key="email.billing.normal",
                envelope=_envelope(),
            )
        finally:
            await publisher.close()
            await conn.close()

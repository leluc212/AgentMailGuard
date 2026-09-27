"""Retry return routing: origin header, tier mapping, topology (RA.3, R3.5, R19.5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelPreconditionFailed

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import (
    RETRY_ORIGIN_EXCHANGE_HEADER,
    MessagePublisher,
    resolve_origin_exchange,
)
from packages.broker.topology import RetryTopologyMigrationError, setup_topology
from packages.core.settings import BrokerSettings, RetryLadderSettings

ORIGINS = (
    "mail.ingest",
    "email.process",
    "email.triage",
    "email.route",
    "email.dispatch",
    "knowledge.ingest",
)


def _envelope(attempt: int = 1) -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"k-{uuid4()}",
        job_type="normalize",
        organization_id=str(uuid4()),
        attempt=attempt,
    )


def _message(exchange: str, headers: dict[str, Any] | None = None) -> AbstractIncomingMessage:
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.exchange = exchange
    msg.headers = headers or {}
    return msg


async def test_publish_to_retry_sets_retry_origin_header() -> None:
    publisher = MessagePublisher()
    exchange = AsyncMock()
    publisher._get_exchange = AsyncMock(return_value=exchange)  # type: ignore[method-assign]

    await publisher.publish_to_retry(
        envelope=_envelope(),
        tier_delay_s=30,
        origin_exchange="email.process",
        origin_routing_key="email.normalize",
        failure_reason="TransientError: x",
    )

    publisher._get_exchange.assert_awaited_once_with("retry.email.30s")
    msg = exchange.publish.call_args[0][0]
    assert exchange.publish.call_args[1]["routing_key"] == "email.normalize"
    assert msg.headers[RETRY_ORIGIN_EXCHANGE_HEADER] == "email.process"
    assert not RETRY_ORIGIN_EXCHANGE_HEADER.startswith("x-")  # headers exchange skips x-*
    assert msg.headers["x-original-exchange"] == "email.process"


@pytest.mark.parametrize(("delay", "suffix"), [(30, "30s"), (300, "5m"), (1800, "30m")])
def test_retry_tier_mapping_default_settings(delay: int, suffix: str) -> None:
    assert MessagePublisher().retry_tier_suffix(delay) == suffix


@pytest.mark.parametrize(("delay", "suffix"), [(1, "30s"), (2, "5m"), (3, "30m")])
def test_retry_tier_mapping_follows_retry_settings(delay: int, suffix: str) -> None:
    fast = RetryLadderSettings(tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3)
    assert MessagePublisher(retry_settings=fast).retry_tier_suffix(delay) == suffix


def test_resolve_origin_exchange_prefers_header_after_retry_return() -> None:
    s = BrokerSettings()
    via_return = _message("retry.return", {RETRY_ORIGIN_EXCHANGE_HEADER: "email.process"})
    assert resolve_origin_exchange(via_return, s) == "email.process"
    via_bytes = _message("retry.return", {RETRY_ORIGIN_EXCHANGE_HEADER: b"email.triage"})
    assert resolve_origin_exchange(via_bytes, s) == "email.triage"
    direct = _message("email.triage", {RETRY_ORIGIN_EXCHANGE_HEADER: "email.process"})
    assert resolve_origin_exchange(direct, s) == "email.triage"  # header trusted only via DLX


def _recording_channel() -> tuple[MagicMock, dict[str, AsyncMock], dict[str, dict[str, Any]]]:
    exchanges: dict[str, AsyncMock] = {}
    queue_calls: dict[str, dict[str, Any]] = {}

    async def declare_exchange(name: str, type_: Any = None, **kw: Any) -> AsyncMock:
        ex = exchanges.setdefault(name, AsyncMock())
        ex.declared_type, ex.declared_kwargs = type_, kw
        return ex

    async def declare_queue(name: str, **kw: Any) -> AsyncMock:
        queue_calls[name] = kw
        return AsyncMock()

    channel = MagicMock()
    channel.declare_exchange = AsyncMock(side_effect=declare_exchange)
    channel.declare_queue = AsyncMock(side_effect=declare_queue)
    return channel, exchanges, queue_calls


async def test_setup_topology_declares_retry_return_routing() -> None:
    channel, exchanges, queue_calls = _recording_channel()

    await setup_topology(channel, BrokerSettings(), RetryLadderSettings())

    rr = exchanges["retry.return"]
    assert rr.declared_type == aio_pika.ExchangeType.HEADERS
    assert rr.declared_kwargs["arguments"] == {"alternate-exchange": "dlx.email"}
    for suffix, ttl in (("30s", 30_000), ("5m", 300_000), ("30m", 1_800_000)):
        assert queue_calls[f"email.retry.{suffix}"]["arguments"] == {
            "x-message-ttl": ttl,
            "x-dead-letter-exchange": "retry.return",
        }
    for origin in ORIGINS:
        exchanges[origin].bind.assert_any_await(
            rr,
            routing_key="",
            arguments={"x-match": "all", RETRY_ORIGIN_EXCHANGE_HEADER: origin},
        )


async def test_setup_topology_explains_stale_retry_queue_arguments() -> None:
    channel, _, _ = _recording_channel()

    async def declare_queue(name: str, **kw: Any) -> AsyncMock:
        if name == "email.retry.30s":
            raise ChannelPreconditionFailed(
                "PRECONDITION_FAILED - inequivalent arg 'x-dead-letter-exchange'"
            )
        return AsyncMock()

    channel.declare_queue = AsyncMock(side_effect=declare_queue)

    with pytest.raises(RetryTopologyMigrationError, match="make broker-migrate-retry"):
        await setup_topology(channel, BrokerSettings(), RetryLadderSettings())

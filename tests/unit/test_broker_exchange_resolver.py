"""BrokerSettings.exchange_for_queue mirrors setup_topology bindings (RA.3, R3.1, R18.7)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.broker.topology import setup_topology
from packages.core.settings import BrokerSettings


@pytest.mark.parametrize(
    ("queue", "exchange"),
    [
        ("mail.sync.requested", "mail.ingest"),
        ("email.normalize", "email.process"),
        ("email.triage", "email.triage"),
        ("email.dispatch", "email.dispatch"),
        ("knowledge.ingest", "knowledge.ingest"),
        ("email.support.normal", "email.route"),
        ("email.billing.priority", "email.route"),
        ("email.general_inquiry.normal", "email.route"),
    ],
)
def test_known_queues_resolve_to_their_bound_exchange(queue: str, exchange: str) -> None:
    assert BrokerSettings().exchange_for_queue(queue) == exchange


@pytest.mark.parametrize(
    "queue",
    [None, "", "email.retry.30s", "email.dead_letter", "email.support.urgent", "unknown.queue"],
)
def test_unroutable_queues_resolve_to_none(queue: str | None) -> None:
    assert BrokerSettings().exchange_for_queue(queue) is None


def test_resolver_follows_overridden_names() -> None:
    cfg = BrokerSettings(queue_normalize="n.q", exchange_email_process="n.ex")
    assert cfg.exchange_for_queue("n.q") == "n.ex"


class _RecordingChannel:
    """Minimal aio-pika channel double recording (queue, exchange, routing_key) bindings."""

    def __init__(self) -> None:
        self.bindings: list[tuple[str, str, str | None]] = []

    async def declare_exchange(self, name: str, *_: Any, **__: Any) -> MagicMock:
        ex = MagicMock()
        ex.name = name
        ex.bind = AsyncMock()
        return ex

    async def declare_queue(self, name: str, **_: Any) -> MagicMock:
        q = MagicMock()

        async def bind(exchange: Any, routing_key: str | None = None) -> None:
            self.bindings.append((name, exchange.name, routing_key))

        q.bind = bind
        return q


async def test_resolver_agrees_with_every_direct_topology_binding() -> None:
    cfg = BrokerSettings()
    channel = _RecordingChannel()
    await setup_topology(channel, cfg)  # type: ignore[arg-type]
    checked = 0
    for queue, exchange, key in channel.bindings:
        if key != queue or queue.startswith("email.retry.") or exchange == cfg.exchange_dlx:
            continue
        assert cfg.exchange_for_queue(queue) == exchange, (queue, exchange)
        checked += 1
    assert checked >= 5 + 2  # 5 stage queues + at least one category x 2 lanes

"""Idempotent RabbitMQ topology declaration (R3.1, R3.2, R3.5, R3.8, R3.9).

Declares durable exchanges, durable queues, retry queues with TTL+DLX,
and dead-letter queues per specs/design.md §7.1 and §7.2.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractQueue,
)
from aio_pika.exceptions import ChannelPreconditionFailed

from packages.broker.publisher import RETRY_ORIGIN_EXCHANGE_HEADER, RETRY_TIER_SUFFIXES
from packages.broker.routing import (
    is_queue_consumed,
    load_categories_from_yaml,
)
from packages.core.settings import (
    BrokerSettings,
    CategoryRoutingSettings,
    RetryLadderSettings,
)
from packages.domain.taxonomy import get_default_registry

logger = logging.getLogger(__name__)


class RetryTopologyMigrationError(RuntimeError):
    """A retry queue exists on the broker with outdated arguments (RabbitMQ 406)."""

    def __init__(self, queue_name: str) -> None:
        super().__init__(
            f"Retry queue '{queue_name}' exists with outdated arguments (queue arguments are "
            "immutable in RabbitMQ). Run `make broker-migrate-retry` once to delete the empty "
            "old retry queues, then start the stack again."
        )
        self.queue_name = queue_name


@dataclass(frozen=True)
class BrokerTopology:
    """References to all declared exchanges and queues in the messaging topology."""

    # Exchanges
    exchanges: dict[str, AbstractExchange]

    # Queues
    queues: dict[str, AbstractQueue]

    # Category and priority queues (R7.1, R7.4)
    category_queues: dict[str, AbstractQueue] = field(default_factory=dict)

    # Category queues that have no configured consumer (R7.6)
    unconsumed_queues: list[str] = field(default_factory=list)


async def setup_topology(
    channel: AbstractChannel,
    broker_settings: BrokerSettings | None = None,
    retry_settings: RetryLadderSettings | None = None,
    routing_settings: CategoryRoutingSettings | None = None,
) -> BrokerTopology:
    """Declare all messaging exchanges, queues, and bindings idempotently (R3.2).

    Parameters
    ----------
    channel : AbstractChannel
        An open aio-pika channel.
    broker_settings : BrokerSettings | None
        Broker configuration specifying exchange and queue names.
    retry_settings : RetryLadderSettings | None
        Retry ladder configuration specifying delay intervals.

    Returns
    -------
    BrokerTopology
        Named map of all declared durable exchanges and queues.
    """
    b_cfg = broker_settings or BrokerSettings()
    r_cfg = retry_settings or RetryLadderSettings()
    rt_cfg = routing_settings or CategoryRoutingSettings()

    if rt_cfg.categories_config_path:
        load_categories_from_yaml(rt_cfg.categories_config_path)

    logger.info("Declaring RabbitMQ topology idempotently...")

    exchanges: dict[str, AbstractExchange] = {}
    queues: dict[str, AbstractQueue] = {}

    # -------------------------------------------------------------------------
    # 1. Declare Primary Exchanges (design.md §7.1)
    # -------------------------------------------------------------------------
    # Direct exchanges
    exchanges[b_cfg.exchange_mail_ingest] = await channel.declare_exchange(
        b_cfg.exchange_mail_ingest, aio_pika.ExchangeType.DIRECT, durable=True
    )
    exchanges[b_cfg.exchange_email_process] = await channel.declare_exchange(
        b_cfg.exchange_email_process, aio_pika.ExchangeType.DIRECT, durable=True
    )
    exchanges[b_cfg.exchange_email_triage] = await channel.declare_exchange(
        b_cfg.exchange_email_triage, aio_pika.ExchangeType.DIRECT, durable=True
    )
    exchanges[b_cfg.exchange_email_dispatch] = await channel.declare_exchange(
        b_cfg.exchange_email_dispatch, aio_pika.ExchangeType.DIRECT, durable=True
    )
    exchanges[b_cfg.exchange_knowledge_ingest] = await channel.declare_exchange(
        b_cfg.exchange_knowledge_ingest, aio_pika.ExchangeType.DIRECT, durable=True
    )
    exchanges[b_cfg.exchange_retry] = await channel.declare_exchange(
        b_cfg.exchange_retry, aio_pika.ExchangeType.DIRECT, durable=True
    )

    # Topic exchanges
    exchanges[b_cfg.exchange_email_route] = await channel.declare_exchange(
        b_cfg.exchange_email_route, aio_pika.ExchangeType.TOPIC, durable=True
    )
    exchanges[b_cfg.exchange_dlx] = await channel.declare_exchange(
        b_cfg.exchange_dlx, aio_pika.ExchangeType.TOPIC, durable=True
    )

    # Retry tier fanout exchanges (preserves routing key on TTL dead-letter redelivery)
    retry_tier_fanout_exchanges: dict[str, AbstractExchange] = {}
    for tier_suffix in RETRY_TIER_SUFFIXES:
        fanout_name = f"{b_cfg.exchange_retry}.{tier_suffix}"
        ex = await channel.declare_exchange(fanout_name, aio_pika.ExchangeType.FANOUT, durable=True)
        exchanges[fanout_name] = ex
        retry_tier_fanout_exchanges[tier_suffix] = ex

    # Retry return exchange (design.md §7.2): expired retry messages are dead-lettered here,
    # keeping their original routing key and headers, and routed back to the exchange named
    # in the retry-origin-exchange header. Unmatched messages go to the alternate exchange
    # (dlx.email) instead of being dropped.
    retry_return = await channel.declare_exchange(
        b_cfg.exchange_retry_return,
        aio_pika.ExchangeType.HEADERS,
        durable=True,
        arguments={"alternate-exchange": b_cfg.exchange_dlx},
    )
    exchanges[b_cfg.exchange_retry_return] = retry_return

    retry_origin_exchanges = (
        b_cfg.exchange_mail_ingest,
        b_cfg.exchange_email_process,
        b_cfg.exchange_email_triage,
        b_cfg.exchange_email_route,
        b_cfg.exchange_email_dispatch,
        b_cfg.exchange_knowledge_ingest,
    )
    for origin_name in retry_origin_exchanges:
        # destination.bind(source): messages flow retry.return -> origin exchange
        await exchanges[origin_name].bind(
            retry_return,
            routing_key="",
            arguments={"x-match": "all", RETRY_ORIGIN_EXCHANGE_HEADER: origin_name},
        )

    # -------------------------------------------------------------------------
    # 2. Queue Common Arguments (Quorum queue support per R3.9)
    # -------------------------------------------------------------------------
    common_args: dict[str, Any] = {}
    if b_cfg.use_quorum_queues:
        common_args["x-queue-type"] = "quorum"

    # -------------------------------------------------------------------------
    # 3. Declare Core Stage Queues and Bindings
    # -------------------------------------------------------------------------
    # Mail Ingest Queue
    q_mail_sync = await channel.declare_queue(
        b_cfg.queue_mail_sync, durable=True, arguments=common_args
    )
    await q_mail_sync.bind(exchanges[b_cfg.exchange_mail_ingest], routing_key=b_cfg.queue_mail_sync)
    queues[b_cfg.queue_mail_sync] = q_mail_sync

    # Email Normalization Queue
    q_normalize = await channel.declare_queue(
        b_cfg.queue_normalize, durable=True, arguments=common_args
    )
    await q_normalize.bind(
        exchanges[b_cfg.exchange_email_process], routing_key=b_cfg.queue_normalize
    )
    queues[b_cfg.queue_normalize] = q_normalize

    # Triage Queue
    q_triage = await channel.declare_queue(b_cfg.queue_triage, durable=True, arguments=common_args)
    await q_triage.bind(exchanges[b_cfg.exchange_email_triage], routing_key=b_cfg.queue_triage)
    queues[b_cfg.queue_triage] = q_triage

    # Email Dispatch Queue
    q_dispatch = await channel.declare_queue(
        b_cfg.queue_dispatch, durable=True, arguments=common_args
    )
    await q_dispatch.bind(
        exchanges[b_cfg.exchange_email_dispatch], routing_key=b_cfg.queue_dispatch
    )
    queues[b_cfg.queue_dispatch] = q_dispatch

    # Knowledge Ingestion Queue
    q_knowledge = await channel.declare_queue(
        b_cfg.queue_knowledge, durable=True, arguments=common_args
    )
    await q_knowledge.bind(
        exchanges[b_cfg.exchange_knowledge_ingest], routing_key=b_cfg.queue_knowledge
    )
    queues[b_cfg.queue_knowledge] = q_knowledge

    # Terminal Dead-Letter Queue (bound to dlx.email with wildcard #)
    q_dlx = await channel.declare_queue(
        b_cfg.queue_dead_letter, durable=True, arguments=common_args
    )
    await q_dlx.bind(exchanges[b_cfg.exchange_dlx], routing_key="#")
    queues[b_cfg.queue_dead_letter] = q_dlx

    # -------------------------------------------------------------------------
    # 4. Declare Retry Queues with TTL + DLX (design.md §7.2)
    # -------------------------------------------------------------------------
    # Delays: 30s (30,000ms), 5m (300,000ms), 30m (1,800,000ms)
    retry_tiers = list(
        zip(
            RETRY_TIER_SUFFIXES,
            (
                r_cfg.tier_1_delay_s * 1000,
                r_cfg.tier_2_delay_s * 1000,
                r_cfg.tier_3_delay_s * 1000,
            ),
            strict=True,
        )
    )

    for tier_suffix, ttl_ms in retry_tiers:
        queue_name = f"email.retry.{tier_suffix}"
        retry_args: dict[str, Any] = {
            "x-message-ttl": ttl_ms,
            "x-dead-letter-exchange": b_cfg.exchange_retry_return,
        }
        if b_cfg.use_quorum_queues:
            retry_args["x-queue-type"] = "quorum"

        try:
            q_retry = await channel.declare_queue(queue_name, durable=True, arguments=retry_args)
        except ChannelPreconditionFailed as exc:
            raise RetryTopologyMigrationError(queue_name) from exc

        # Bind to direct retry.email exchange
        await q_retry.bind(exchanges[b_cfg.exchange_retry], routing_key=f"retry.{tier_suffix}")
        await q_retry.bind(exchanges[b_cfg.exchange_retry], routing_key=queue_name)

        # Bind to companion fanout exchange
        await q_retry.bind(retry_tier_fanout_exchanges[tier_suffix])

        queues[queue_name] = q_retry

    # -------------------------------------------------------------------------
    # 5. Declare Category-Aware Routing Queues and Bindings (R7.1, R7.2, R7.4)
    # -------------------------------------------------------------------------
    category_queues: dict[str, AbstractQueue] = {}
    unconsumed_queues: list[str] = []

    registry = get_default_registry()
    all_categories = registry.all_categories()
    priority_lanes = rt_cfg.priority_lanes or ["normal", "priority"]

    for category in all_categories:
        for lane in priority_lanes:
            queue_name = f"email.{category}.{lane}"
            q_cat = await channel.declare_queue(queue_name, durable=True, arguments=common_args)
            # Bind to topic exchange email.route with routing key email.<category>.<lane>
            await q_cat.bind(exchanges[b_cfg.exchange_email_route], routing_key=queue_name)
            queues[queue_name] = q_cat
            category_queues[queue_name] = q_cat

    # -------------------------------------------------------------------------
    # 6. Check for Unconsumed Category Queues (R7.6)
    # -------------------------------------------------------------------------
    for q_name in category_queues:
        if not is_queue_consumed(q_name, rt_cfg.configured_consumers):
            logger.warning(
                "Category queue '%s' has no configured consumer (R7.6). Messages may accumulate.",
                q_name,
            )
            unconsumed_queues.append(q_name)

    logger.info(
        "RabbitMQ topology successfully declared: %d exchanges, %d queues (%d category queues)",
        len(exchanges),
        len(queues),
        len(category_queues),
    )
    return BrokerTopology(
        exchanges=exchanges,
        queues=queues,
        category_queues=category_queues,
        unconsumed_queues=unconsumed_queues,
    )

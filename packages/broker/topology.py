"""Idempotent RabbitMQ topology declaration (R3.1, R3.2, R3.5, R3.8, R3.9).

Declares durable exchanges, durable queues, retry queues with TTL+DLX,
and dead-letter queues per specs/design.md §7.1 and §7.2.
"""

import logging
from dataclasses import dataclass
from typing import Any

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractQueue,
)

from packages.core.settings import BrokerSettings, RetryLadderSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerTopology:
    """References to all declared exchanges and queues in the messaging topology."""

    # Exchanges
    exchanges: dict[str, AbstractExchange]

    # Queues
    queues: dict[str, AbstractQueue]


async def setup_topology(
    channel: AbstractChannel,
    broker_settings: BrokerSettings | None = None,
    retry_settings: RetryLadderSettings | None = None,
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
    for tier_suffix in ["30s", "5m", "30m"]:
        fanout_name = f"{b_cfg.exchange_retry}.{tier_suffix}"
        ex = await channel.declare_exchange(fanout_name, aio_pika.ExchangeType.FANOUT, durable=True)
        exchanges[fanout_name] = ex
        retry_tier_fanout_exchanges[tier_suffix] = ex

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
    retry_tiers = [
        ("30s", r_cfg.tier_1_delay_s * 1000),
        ("5m", r_cfg.tier_2_delay_s * 1000),
        ("30m", r_cfg.tier_3_delay_s * 1000),
    ]

    for tier_suffix, ttl_ms in retry_tiers:
        queue_name = f"email.retry.{tier_suffix}"
        retry_args: dict[str, Any] = {
            "x-message-ttl": ttl_ms,
            "x-dead-letter-exchange": b_cfg.exchange_email_route,
        }
        if b_cfg.use_quorum_queues:
            retry_args["x-queue-type"] = "quorum"

        q_retry = await channel.declare_queue(queue_name, durable=True, arguments=retry_args)

        # Bind to direct retry.email exchange
        await q_retry.bind(exchanges[b_cfg.exchange_retry], routing_key=f"retry.{tier_suffix}")
        await q_retry.bind(exchanges[b_cfg.exchange_retry], routing_key=queue_name)

        # Bind to companion fanout exchange
        await q_retry.bind(retry_tier_fanout_exchanges[tier_suffix])

        queues[queue_name] = q_retry

    logger.info(
        "RabbitMQ topology successfully declared: %d exchanges, %d queues",
        len(exchanges),
        len(queues),
    )
    return BrokerTopology(exchanges=exchanges, queues=queues)

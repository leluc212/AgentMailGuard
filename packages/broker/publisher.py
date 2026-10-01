"""Persistent message publisher for RabbitMQ (R3.1, R3.5, R7.2).

Ensures all published messages carry persistent delivery mode, OpenTelemetry trace context,
and standardized job envelopes.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)

from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings, RetryLadderSettings

logger = logging.getLogger(__name__)

RETRY_ORIGIN_EXCHANGE_HEADER = "retry-origin-exchange"
"""Header matched by the retry.return headers exchange.

Deliberately NOT ``x-`` prefixed: with ``x-match: all`` RabbitMQ skips ``x-*`` headers when
matching (rabbit_exchange_type_headers: ``match({<<"x-", _/binary>>, _, _}, _) -> skip``).
"""

RETRY_TIER_SUFFIXES: tuple[str, str, str] = ("30s", "5m", "30m")


def resolve_origin_exchange(
    message: AbstractIncomingMessage, broker_settings: BrokerSettings
) -> str:
    """Return the exchange a delivery logically belongs to.

    Dead-lettering replaces the message's exchange with the DLX name, so a retried delivery
    arrives with ``exchange == retry.return``; its true origin travels in the
    ``retry-origin-exchange`` header set by ``publish_to_retry``.
    """
    exchange = message.exchange or ""
    if exchange == broker_settings.exchange_retry_return:
        origin = (message.headers or {}).get(RETRY_ORIGIN_EXCHANGE_HEADER)
        if isinstance(origin, (bytes, bytearray)):
            origin = origin.decode("utf-8", errors="replace")
        if origin:
            return str(origin)
    return exchange


class MessagePublisher:
    """Robust message publisher handling persistent delivery, retries, and dead-lettering."""

    def __init__(
        self,
        broker_settings: BrokerSettings | None = None,
        connection: AbstractRobustConnection | None = None,
        channel: AbstractChannel | None = None,
        retry_settings: RetryLadderSettings | None = None,
    ) -> None:
        self.settings = broker_settings or BrokerSettings()
        self.retry_settings = retry_settings or RetryLadderSettings()
        self._external_conn = connection is not None
        self._external_channel = channel is not None
        self._connection = connection
        self._channel = channel
        self._exchanges: dict[str, AbstractExchange] = {}

    def retry_tier_suffix(self, tier_delay_s: int) -> str:
        """Map a tier delay to the retry queue whose TTL was declared from the same settings."""
        r = self.retry_settings
        if tier_delay_s <= r.tier_1_delay_s:
            return RETRY_TIER_SUFFIXES[0]
        if tier_delay_s <= r.tier_2_delay_s:
            return RETRY_TIER_SUFFIXES[1]
        return RETRY_TIER_SUFFIXES[2]

    @property
    def broker_settings(self) -> BrokerSettings:
        """Alias for settings to maintain backwards and cross-module compatibility."""
        return self.settings

    async def connect(self) -> None:
        """Establish connection and channel if not provided externally."""
        if self._connection is None or self._connection.is_closed:
            self._connection = await aio_pika.connect_robust(self.settings.url)
            logger.info("Connected publisher to RabbitMQ at %s", self.settings.host)

        if self._channel is None or self._channel.is_closed:
            # Unroutable mandatory publishes must raise PublishError, never vanish (R3.1).
            self._channel = await self._connection.channel(on_return_raises=True)
            logger.info("Publisher AMQP channel created")

    async def close(self) -> None:
        """Close internally managed channel and connection."""
        if not self._external_channel and self._channel and not self._channel.is_closed:
            await self._channel.close()
            self._channel = None

        if not self._external_conn and self._connection and not self._connection.is_closed:
            await self._connection.close()
            self._connection = None

        self._exchanges.clear()
        logger.info("Publisher AMQP resources closed")

    async def _get_exchange(self, exchange_name: str) -> AbstractExchange:
        """Retrieve exchange reference, declaring passively if necessary."""
        if self._channel is None or self._channel.is_closed:
            await self.connect()

        assert self._channel is not None

        if exchange_name not in self._exchanges:
            # Look up existing exchange on channel
            ex = await self._channel.get_exchange(exchange_name, ensure=False)
            self._exchanges[exchange_name] = ex

        return self._exchanges[exchange_name]

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        """Publish a persistent JobEnvelope to a target exchange (R3.1).

        Parameters
        ----------
        exchange_name : str
            Name of target exchange.
        routing_key : str
            Routing key for destination matching.
        envelope : JobEnvelope
            Standardized job envelope.
        headers : dict[str, Any] | None
            Optional extra AMQP headers.
        """
        exchange = await self._get_exchange(exchange_name)
        message = envelope.to_message(headers=headers)

        await exchange.publish(message, routing_key=routing_key)
        logger.debug(
            "Published job %s (type=%s, attempt=%d) to exchange '%s' with key '%s'",
            envelope.job_id,
            envelope.job_type,
            envelope.attempt,
            exchange_name,
            routing_key,
        )

    async def publish_to_retry(
        self,
        envelope: JobEnvelope,
        tier_delay_s: int,
        origin_exchange: str,
        origin_routing_key: str,
        failure_reason: str,
    ) -> None:
        """Publish a message to the retry ladder with TTL and return DLX headers (R7.2, R19.5).

        Parameters
        ----------
        envelope : JobEnvelope
            Job envelope with incremented attempt count.
        tier_delay_s : int
            Delay interval in seconds (e.g. 30, 300, 1800).
        origin_exchange : str
            Original exchange the message was consumed from.
        origin_routing_key : str
            Original routing key to be preserved upon redelivery.
        failure_reason : str
            Diagnostic error message causing retry.
        """
        tier_suffix = self.retry_tier_suffix(tier_delay_s)
        retry_exchange = f"{self.settings.exchange_retry}.{tier_suffix}"

        headers = {
            "x-original-exchange": origin_exchange,
            "x-original-routing-key": origin_routing_key,
            "x-failure-reason": failure_reason,
            "x-attempt": envelope.attempt,
            # Matched by retry.return (headers exchange) after TTL dead-lettering.
            RETRY_ORIGIN_EXCHANGE_HEADER: origin_exchange,
        }

        # Publishing to the fanout retry exchange preserves origin_routing_key
        # so that when queue TTL expires, DLX republishes with origin_routing_key.
        await self.publish(
            exchange_name=retry_exchange,
            routing_key=origin_routing_key,
            envelope=envelope,
            headers=headers,
        )
        logger.warning(
            "Job %s scheduled for retry in tier %s (delay=%ds, attempt=%d): %s",
            envelope.job_id,
            tier_suffix,
            tier_delay_s,
            envelope.attempt,
            failure_reason,
        )

    async def publish_to_dead_letter(
        self,
        envelope: JobEnvelope,
        failure_reason: str,
        origin_routing_key: str,
        origin_exchange: str,
        extra_headers: dict[str, Any] | None = None,
    ) -> None:
        """Route an unrecoverable job to terminal dead-letter exchange (R3.5, R19.6).

        Preserves original routing key, original exchange, attempt count, failure reason,
        and timestamp in AMQP message headers.
        """
        headers: dict[str, Any] = {
            "x-original-exchange": origin_exchange,
            "x-original-routing-key": origin_routing_key,
            "x-failure-reason": failure_reason,
            "x-attempt": envelope.attempt,
            "x-failed-at": datetime.now(UTC).isoformat(),
        }
        if extra_headers:
            headers.update(extra_headers)

        await self.publish(
            exchange_name=self.settings.exchange_dlx,
            routing_key=origin_routing_key,
            envelope=envelope,
            headers=headers,
        )
        logger.error(
            "Job %s dead-lettered after %d attempts: %s",
            envelope.job_id,
            envelope.attempt,
            failure_reason,
        )

    async def publish_raw_to_dead_letter(
        self,
        message: AbstractIncomingMessage,
        failure_reason: str,
        origin_exchange: str,
        origin_routing_key: str,
    ) -> None:
        """Dead-letter a delivery whose body could not be parsed into a JobEnvelope (R3.5).

        The original body bytes and headers are preserved verbatim so the payload can be
        inspected or replayed; diagnostic headers are added on top. ``expiration`` is not
        copied, so the dead-letter copy never expires.
        """
        headers: dict[str, Any] = dict(message.headers or {})
        headers.update(
            {
                "x-original-exchange": origin_exchange,
                "x-original-routing-key": origin_routing_key,
                "x-failure-reason": failure_reason,
                "x-failed-at": datetime.now(UTC).isoformat(),
            }
        )
        dead_message = aio_pika.Message(
            body=message.body,
            headers=headers,
            content_type=message.content_type,
            content_encoding=message.content_encoding,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            timestamp=message.timestamp,
        )
        exchange = await self._get_exchange(self.settings.exchange_dlx)
        # dlx.email is a topic exchange with a "#" binding: any key (including "") routes.
        await exchange.publish(dead_message, routing_key=origin_routing_key)
        logger.error(
            "Unparseable message from '%s' (key '%s') dead-lettered: %s",
            origin_exchange,
            origin_routing_key,
            failure_reason,
        )

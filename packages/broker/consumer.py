"""Consumer base class with manual ack, prefetch QoS, and retry ladder routing (R3.3, R3.4, R3.5).

Enforces manual acknowledgment only after side effects commit, bounded prefetch per consumer,
and automated retry routing through the 3-tier TTL+DLX ladder.
"""

import logging
from abc import ABC, abstractmethod
from contextlib import nullcontext

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings, RetryLadderSettings, WorkerConcurrencySettings
from packages.observability.context import bind_log_context
from packages.observability.metrics import get_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import extract_trace_context, trace_span

logger = logging.getLogger(__name__)


class TransientError(Exception):
    """Signals a temporary failure eligible for retry ladder backoff (R19.5)."""


class FatalError(Exception):
    """Signals an unrecoverable failure immediately routed to dead-letter exchange (R3.5)."""


class BaseConsumer(ABC):
    """Base AMQP consumer providing manual ack, bounded prefetch, and retry ladder orchestration."""

    def __init__(
        self,
        queue_name: str,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
    ) -> None:
        self.queue_name = queue_name
        self.broker_settings = broker_settings or BrokerSettings()
        self.retry_settings = retry_settings or RetryLadderSettings()
        self.shutdown_coordinator = shutdown_coordinator

        concurrency_cfg = WorkerConcurrencySettings()
        self.prefetch_count = (
            prefetch_count if prefetch_count is not None else concurrency_cfg.default_prefetch
        )

        self._connection = connection
        self._external_conn = connection is not None
        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None
        self._consumer_tag: str | None = None
        self._publisher: MessagePublisher | None = None
        self._is_consuming: bool = False

        if self.shutdown_coordinator is not None:
            self.shutdown_coordinator.register_drain_callback(self.stop)

    @abstractmethod
    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        """Process a single job envelope to completion.

        Must be implemented by concrete service consumers. If this method raises
        TransientError (or any non-FatalError exception by default), the message
        is retried via the retry ladder. If it raises FatalError or exceeds max retries,
        it is routed to the terminal dead-letter queue.

        Parameters
        ----------
        envelope : JobEnvelope
            Deserialized and validated job envelope.
        raw_message : AbstractIncomingMessage
            Underlying AMQP message for access to headers and properties.
        """

    def is_transient_error(self, exc: Exception) -> bool:
        """Determine whether an exception qualifies for retry backoff."""
        return not isinstance(exc, FatalError)

    def get_retry_delay_s(self, attempt: int) -> int:
        """Calculate delay in seconds for the given retry attempt tier."""
        if attempt <= 1:
            return self.retry_settings.tier_1_delay_s
        elif attempt == 2:
            return self.retry_settings.tier_2_delay_s
        else:
            return self.retry_settings.tier_3_delay_s

    async def start(self) -> None:
        """Connect, configure prefetch QoS, and start consuming messages (R3.3, R3.4)."""
        if self._is_consuming:
            logger.warning("Consumer for %s already running", self.queue_name)
            return

        if self._connection is None or self._connection.is_closed:
            self._connection = await aio_pika.connect_robust(self.broker_settings.url)

        self._channel = await self._connection.channel()

        # Enforce bounded prefetch QoS per consumer (R3.4)
        await self._channel.set_qos(prefetch_count=self.prefetch_count)
        logger.info(
            "Configured prefetch QoS (%d) for queue '%s'", self.prefetch_count, self.queue_name
        )

        # Initialize publisher for retries and dead letters sharing the connection
        self._publisher = MessagePublisher(
            broker_settings=self.broker_settings,
            connection=self._connection,
            channel=self._channel,
        )

        self._queue = await self._channel.get_queue(self.queue_name, ensure=False)

        # Start consuming with manual acknowledgement (no_ack=False, R3.3)
        self._consumer_tag = await self._queue.consume(self._handle_message, no_ack=False)
        self._is_consuming = True
        logger.info("Consumer started on queue '%s' (tag=%s)", self.queue_name, self._consumer_tag)

    async def stop(self) -> None:
        """Cancel subscription and gracefully close channels."""
        if not self._is_consuming:
            return

        if self._queue and self._consumer_tag:
            try:
                await self._queue.cancel(self._consumer_tag)
            except Exception as err:
                logger.warning("Error cancelling consumer tag %s: %s", self._consumer_tag, err)

        if self._channel and not self._channel.is_closed:
            await self._channel.close()

        if not self._external_conn and self._connection and not self._connection.is_closed:
            await self._connection.close()

        self._is_consuming = False
        logger.info("Consumer stopped on queue '%s'", self.queue_name)

    async def _handle_message(self, message: AbstractIncomingMessage) -> None:
        """Handle incoming delivery with manual ack, tracing, and retry ladder coordination."""
        try:
            envelope = JobEnvelope.from_message(message)
        except Exception as parse_err:
            logger.error("Failed to parse JobEnvelope from message: %s", parse_err)
            # Cannot parse: route raw rejection to dead letter and ack to clear queue
            await message.ack()
            return

        origin_exchange = message.exchange or ""
        origin_routing_key = message.routing_key or self.queue_name

        # 1. Restore OpenTelemetry trace context across AMQP hop (R21.1)
        headers_dict = dict(message.headers) if message.headers else {}
        parent_ctx = extract_trace_context(headers_dict)

        # 2. Track in-flight job for graceful shutdown draining (R20.8)
        track_ctx = (
            self.shutdown_coordinator.track_job() if self.shutdown_coordinator else nullcontext()
        )

        # 3. Bind correlation logging context and open OpenTelemetry child span (R21.1–R21.3)
        with (
            bind_log_context(
                trace_id=envelope.trace_id,
                message_id=envelope.message_id,
                thread_id=envelope.thread_id,
                job_id=envelope.job_id,
                organization_id=envelope.organization_id,
            ),
            trace_span(
                "broker.consume",
                parent_context=parent_ctx,
                attributes={
                    "messaging.system": "rabbitmq",
                    "messaging.destination": self.queue_name,
                    "messaging.job_id": envelope.job_id,
                    "messaging.job_type": envelope.job_type,
                    "organization.id": envelope.organization_id,
                    "messaging.attempt": envelope.attempt,
                },
            ),
            track_ctx,
        ):
            try:
                # 4. Execute consumer processing
                await self.process_job(envelope, message)

                # 5. Manual ACK: ONLY after all processing and side effects commit (R3.3)
                await message.ack()
                logger.debug(
                    "Job %s successfully processed and ACKed on queue '%s'",
                    envelope.job_id,
                    self.queue_name,
                )

            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                should_retry = self.is_transient_error(exc) and (
                    envelope.attempt < self.retry_settings.max_retries
                )

                assert self._publisher is not None
                metrics = get_metrics()

                if should_retry:
                    next_attempt = envelope.attempt + 1
                    delay_s = self.get_retry_delay_s(next_attempt)

                    retry_envelope = envelope.model_copy(update={"attempt": next_attempt})

                    # Determine effective origin exchange for DLX redelivery
                    effective_exchange = (
                        origin_exchange or self.broker_settings.exchange_email_route
                    )

                    # Publish to retry ladder
                    await self._publisher.publish_to_retry(
                        envelope=retry_envelope,
                        tier_delay_s=delay_s,
                        origin_exchange=effective_exchange,
                        origin_routing_key=origin_routing_key,
                        failure_reason=reason,
                    )

                    # Ack original message so it doesn't block prefetch or queue
                    await message.ack()

                    # Record retry metric (R21.4)
                    metrics.retry_jobs_total.labels(queue=self.queue_name, tier=f"{delay_s}s").inc()

                    logger.info(
                        "Job %s failed transiently; routed to retry tier %ds (attempt %d/%d)",
                        envelope.job_id,
                        delay_s,
                        next_attempt,
                        self.retry_settings.max_retries,
                    )
                else:
                    # Terminal failure: route to dead-letter exchange (R3.5, R19.6)
                    await self._publisher.publish_to_dead_letter(
                        envelope=envelope,
                        failure_reason=reason,
                        origin_routing_key=origin_routing_key,
                        origin_exchange=origin_exchange,
                    )

                    # Ack original message to prevent infinite redelivery
                    await message.ack()

                    # Record dead-letter metric (R21.4)
                    metrics.failed_jobs_total.labels(
                        queue=self.queue_name,
                        job_type=envelope.job_type,
                        error_type=type(exc).__name__,
                    ).inc()

                    logger.error(
                        "Job %s failed terminally (%s); routed to dead-letter exchange",
                        envelope.job_id,
                        reason,
                    )

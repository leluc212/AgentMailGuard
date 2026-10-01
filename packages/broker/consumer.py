"""Consumer base class with manual ack, prefetch QoS, and retry ladder routing (R3.3, R3.4, R3.5).

Enforces manual acknowledgment only after side effects commit, bounded prefetch per consumer,
and automated retry routing through the 3-tier TTL+DLX ladder.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from packages.broker.backoff import resolve_retry_tier_delay
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher, resolve_origin_exchange
from packages.broker.retry import (
    handle_job_recovery,
    handle_job_terminal_failure,
    handle_job_transient_failure,
    is_valid_uuid,
)
from packages.core.settings import BrokerSettings, RetryLadderSettings, WorkerConcurrencySettings
from packages.observability.context import bind_log_context
from packages.observability.metrics import PipelineMetrics, get_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import extract_trace_context, trace_span

if TYPE_CHECKING:
    from packages.db.job import JobStoreProtocol

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
        job_store: JobStoreProtocol | None = None,
        lease_timeout_s: int = 300,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self.queue_name = queue_name
        self.broker_settings = broker_settings or BrokerSettings()
        self.retry_settings = retry_settings or RetryLadderSettings()
        self.shutdown_coordinator = shutdown_coordinator
        self.job_store = job_store
        self.lease_timeout_s = lease_timeout_s
        self.metrics = metrics or get_metrics()

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
            # R20.8 ordering: stop new deliveries during drain, but keep the channel open so
            # in-flight jobs can still ack/publish retries on it; close it only in cleanup,
            # after the coordinator has waited for tracked jobs.
            self.shutdown_coordinator.register_drain_callback(self.stop_consuming)
            self.shutdown_coordinator.register_cleanup_callback(self.close)

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

    def retry_after_s(self, exc: Exception) -> float | None:
        """Provider-requested wait carried by ``exc`` (Retry-After), or None (R17.5).

        A non-None value picks the first retry tier >= it instead of the attempt tier.
        """
        return None

    async def prepare_delivery(self, envelope: JobEnvelope) -> None:
        """Run before ``process_job``: recover a RETRY_PENDING job and take a claim lease.

        Requirements: R19.5 (RETRY_PENDING -> GENERATING on redelivery), R19.8 (lease on
        claim). Consumers whose job claims itself (the dispatch-worker, design.md §5.8)
        override this.
        """
        await handle_job_recovery(envelope=envelope, job_store=self.job_store)
        if self.job_store is not None and is_valid_uuid(envelope.job_id):
            try:
                await self.job_store.acquire_lease(
                    organization_id=envelope.organization_id,
                    job_id=UUID(envelope.job_id),
                    lease_timeout_s=self.lease_timeout_s,
                )
            except Exception as lease_err:
                logger.warning("Failed to acquire lease for job %s: %s", envelope.job_id, lease_err)

    def get_retry_delay_s(self, attempt: int) -> int:
        """Calculate delay in seconds for the given retry attempt tier (R3.4, R7.2)."""
        return resolve_retry_tier_delay(attempt, self.retry_settings)

    async def _dead_letter_unparseable(
        self, message: AbstractIncomingMessage, parse_err: Exception
    ) -> None:
        """Dead-letter an unparseable delivery with its raw body, then ack (R3.5).

        Never acks without a successful dead-letter publish: on publish failure the message
        is requeued so it is not lost.
        """
        reason = f"EnvelopeParseError: {type(parse_err).__name__}: {parse_err}"
        logger.error("Failed to parse JobEnvelope on queue '%s': %s", self.queue_name, parse_err)
        try:
            if self._publisher is None:
                raise RuntimeError("consumer publisher is not initialised")
            await self._publisher.publish_raw_to_dead_letter(
                message=message,
                failure_reason=reason,
                origin_exchange=resolve_origin_exchange(message, self.broker_settings),
                origin_routing_key=message.routing_key or self.queue_name,
            )
        except Exception:
            logger.exception(
                "Could not dead-letter unparseable message on '%s'; requeueing", self.queue_name
            )
            await message.nack(requeue=True)
            return
        await message.ack()

    async def _ack_or_warn(self, message: AbstractIncomingMessage, envelope: JobEnvelope) -> bool:
        """Ack a delivery; if the ack itself fails (channel lost), log and return False (R3.3).

        Never publishes anything: the broker redelivers the unacked message on its own, so a
        retry published here would process the job twice.
        """
        try:
            await message.ack()
        except Exception as ack_err:
            logger.warning(
                "Ack failed for job %s on queue '%s' (%s); the broker will redeliver it",
                envelope.job_id,
                self.queue_name,
                ack_err,
            )
            return False
        return True

    async def start(self) -> None:
        """Connect, configure prefetch QoS, and start consuming messages (R3.3, R3.4)."""
        if self._is_consuming:
            logger.warning("Consumer for %s already running", self.queue_name)
            return

        if self._connection is None or self._connection.is_closed:
            self._connection = await aio_pika.connect_robust(self.broker_settings.url)

        self._channel = await self._connection.channel(on_return_raises=True)

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
            retry_settings=self.retry_settings,
        )

        # Passive declare: fails if the topology was never declared (R3.2), and on a
        # RobustChannel registers the queue so consuming resumes after a reconnect.
        self._queue = await self._channel.declare_queue(self.queue_name, passive=True)

        # Start consuming with manual acknowledgement (no_ack=False, R3.3)
        self._consumer_tag = await self._queue.consume(self._handle_message, no_ack=False)
        self._is_consuming = True
        logger.info("Consumer started on queue '%s' (tag=%s)", self.queue_name, self._consumer_tag)

    async def stop_consuming(self) -> None:
        """Cancel the broker subscription so no new deliveries arrive (R20.8 drain step).

        The channel stays open: RabbitMQ requires acks on the channel that received the
        delivery, and closing it would cancel in-flight callbacks. Idempotent.
        """
        if not self._is_consuming:
            return
        self._is_consuming = False

        if self._queue is not None and self._consumer_tag is not None:
            try:
                await self._queue.cancel(self._consumer_tag)
            except Exception as err:
                logger.warning("Error cancelling consumer tag %s: %s", self._consumer_tag, err)

        logger.info(
            "Consumer cancelled on queue '%s'; channel kept open for in-flight acks",
            self.queue_name,
        )

    async def close(self) -> None:
        """Close the channel and owned connection (R20.8 cleanup step). Idempotent.

        Call only after in-flight jobs have drained: closing the channel makes aiormq cancel
        any consumer callback still running and makes RabbitMQ requeue its unacked delivery.
        """
        await self.stop_consuming()
        await self._await_local_drain()

        if self._channel is not None and not self._channel.is_closed:
            try:
                await self._channel.close()
            except Exception as err:
                logger.warning("Error closing channel for queue '%s': %s", self.queue_name, err)

        if (
            not self._external_conn
            and self._connection is not None
            and not self._connection.is_closed
        ):
            await self._connection.close()

        logger.info("Consumer closed on queue '%s'", self.queue_name)

    async def stop(self) -> None:
        """Backwards-compatible stop: cancel the subscription, then close immediately.

        Does not wait for in-flight jobs; register with a GracefulShutdownCoordinator for that.
        """
        await self.close()

    async def _await_local_drain(self) -> None:
        """Hook: wait for work buffered inside this consumer before the channel closes.

        BaseConsumer buffers nothing (each delivery runs in its own callback task).
        """
        return None

    async def _handle_message(self, message: AbstractIncomingMessage) -> None:
        """Handle incoming delivery with manual ack, tracing, and retry ladder coordination."""
        try:
            envelope = JobEnvelope.from_message(message)
        except Exception as parse_err:
            await self._dead_letter_unparseable(message, parse_err)
            return

        origin_exchange = resolve_origin_exchange(message, self.broker_settings)
        origin_routing_key = message.routing_key or self.queue_name

        # Record queue wait time metric (R7.5, R21.4)
        wait_ms = max(0.0, (datetime.now(UTC) - envelope.enqueued_at).total_seconds() * 1000.0)
        self.metrics.queue_wait_ms.labels(queue=self.queue_name).observe(wait_ms)

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
                # 3.5 / 3.6 Redelivery recovery and claim lease (R19.5, R19.8), overridable
                await self.prepare_delivery(envelope)

                # 4. Execute consumer processing
                await self.process_job(envelope, message)
            except Exception as exc:
                should_retry = self.is_transient_error(exc) and (
                    envelope.attempt < self.retry_settings.max_retries
                )

                assert self._publisher is not None

                if should_retry:
                    await handle_job_transient_failure(
                        envelope=envelope,
                        exception=exc,
                        publisher=self._publisher,
                        retry_settings=self.retry_settings,
                        origin_exchange=origin_exchange,
                        origin_routing_key=origin_routing_key,
                        queue_name=self.queue_name,
                        job_store=self.job_store,
                        retry_after_s=self.retry_after_s(exc),
                    )
                    # Ack original message so it doesn't block prefetch or queue
                    await self._ack_or_warn(message, envelope)
                else:
                    await handle_job_terminal_failure(
                        envelope=envelope,
                        exception=exc,
                        publisher=self._publisher,
                        origin_exchange=origin_exchange,
                        origin_routing_key=origin_routing_key,
                        queue_name=self.queue_name,
                        job_store=self.job_store,
                    )
                    # Ack original message to prevent infinite redelivery
                    await self._ack_or_warn(message, envelope)
                return

            # 5. Manual ACK only after all side effects commit (R3.3). Outside the try: if
            # the ack itself fails (channel lost), the broker redelivers the unacked message,
            # so publishing a retry as well would process the job twice.
            if await self._ack_or_warn(message, envelope):
                logger.debug(
                    "Job %s successfully processed and ACKed on queue '%s'",
                    envelope.job_id,
                    self.queue_name,
                )

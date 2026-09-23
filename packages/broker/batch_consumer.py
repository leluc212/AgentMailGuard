"""Worker-level micro-batch consumer base class (R3.6, R3.7, design.md §7.4).

Enables workers to pull N jobs together from RabbitMQ to amortize round-trips and keep
provider connections warm, while enforcing strict independent per-job inference execution
and per-message fault isolation (R3.3, R3.7).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import aio_pika
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustConnection,
)

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.retry import (
    handle_job_recovery,
    handle_job_terminal_failure,
    handle_job_transient_failure,
    is_valid_uuid,
)
from packages.core.settings import BrokerSettings, RetryLadderSettings, WorkerConcurrencySettings
from packages.observability.context import bind_log_context
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import extract_trace_context, trace_span

if TYPE_CHECKING:
    from packages.db.job import JobStoreProtocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchItem:
    """A single job delivery within a micro-batch."""

    envelope: JobEnvelope
    raw_message: AbstractIncomingMessage


class BaseBatchConsumer(BaseConsumer):
    """Base consumer supporting worker micro-batching with independent job processing (R3.6, R3.7).

    Pulls up to `batch_size` jobs together from RabbitMQ within `batch_timeout_s`.
    Subclasses implement `process_job(envelope, raw_message)` for each item.
    Prompts are never concatenated; each email receives its own isolated inference call.
    """

    def __init__(
        self,
        queue_name: str,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        batch_size: int | None = None,
        batch_timeout_s: float | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        job_store: JobStoreProtocol | None = None,
        lease_timeout_s: int = 300,
    ) -> None:
        concurrency_cfg = WorkerConcurrencySettings()
        effective_prefetch = (
            prefetch_count if prefetch_count is not None else concurrency_cfg.default_prefetch
        )
        effective_batch_size = (
            batch_size if batch_size is not None else concurrency_cfg.micro_batch_size
        )

        # Enforce prefetch >= batch_size to prevent buffer starvation
        if effective_prefetch < effective_batch_size:
            effective_prefetch = effective_batch_size

        super().__init__(
            queue_name=queue_name,
            broker_settings=broker_settings,
            retry_settings=retry_settings,
            prefetch_count=effective_prefetch,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=job_store,
            lease_timeout_s=lease_timeout_s,
        )

        self.batch_size = effective_batch_size
        self.batch_timeout_s = (
            batch_timeout_s
            if batch_timeout_s is not None
            else (concurrency_cfg.micro_batch_timeout_ms / 1000.0)
        )

        self._inbound_queue: asyncio.Queue[AbstractIncomingMessage] = asyncio.Queue()
        self._batch_loop_task: asyncio.Task[None] | None = None
        self._drain_complete: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """Connect, configure prefetch QoS, and launch micro-batch consumer loop (R3.4, R3.6)."""
        if self._is_consuming:
            logger.warning("Batch consumer for %s already running", self.queue_name)
            return

        if self._connection is None or self._connection.is_closed:
            self._connection = await aio_pika.connect_robust(self.broker_settings.url)

        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self.prefetch_count)

        self._publisher = MessagePublisher(
            broker_settings=self.broker_settings,
            connection=self._connection,
            channel=self._channel,
        )

        self._queue = await self._channel.get_queue(self.queue_name, ensure=False)
        self._drain_complete.clear()

        # Route inbound deliveries directly to internal queue for batch aggregation
        self._consumer_tag = await self._queue.consume(
            self._on_message_delivered,
            no_ack=False,
        )
        self._is_consuming = True

        # Launch background micro-batch aggregation loop
        self._batch_loop_task = asyncio.create_task(
            self._batch_worker_loop(),
            name=f"batch-loop-{self.queue_name}",
        )
        logger.info(
            "Batch consumer started on queue '%s' (batch_size=%d, timeout=%.3fs, tag=%s)",
            self.queue_name,
            self.batch_size,
            self.batch_timeout_s,
            self._consumer_tag,
        )

    async def _on_message_delivered(self, message: AbstractIncomingMessage) -> None:
        """Receive message from AMQP channel and buffer into memory queue."""
        await self._inbound_queue.put(message)

    async def _batch_worker_loop(self) -> None:
        """Background loop assembling and executing micro-batches of up to N jobs (R3.6)."""
        try:
            while self._is_consuming or not self._inbound_queue.empty():
                raw_batch: list[AbstractIncomingMessage] = []

                # 1. Wait for first delivery (idle waiting)
                try:
                    first_msg = await asyncio.wait_for(
                        self._inbound_queue.get(),
                        timeout=0.2,
                    )
                    raw_batch.append(first_msg)
                except TimeoutError:
                    if not self._is_consuming and self._inbound_queue.empty():
                        break
                    continue

                # 2. Accumulate up to batch_size - 1 additional deliveries until timeout
                start_time = time.monotonic()
                while len(raw_batch) < self.batch_size:
                    remaining_time = self.batch_timeout_s - (time.monotonic() - start_time)
                    if remaining_time <= 0:
                        break
                    try:
                        next_msg = await asyncio.wait_for(
                            self._inbound_queue.get(),
                            timeout=remaining_time,
                        )
                        raw_batch.append(next_msg)
                    except TimeoutError:
                        break

                # 3. Parse envelopes and filter malformed deliveries
                valid_items: list[BatchItem] = []
                for raw_msg in raw_batch:
                    try:
                        envelope = JobEnvelope.from_message(raw_msg)
                        valid_items.append(BatchItem(envelope=envelope, raw_message=raw_msg))
                    except Exception as parse_err:
                        logger.error(
                            "Failed to parse JobEnvelope in batch consumer (%s): %s",
                            self.queue_name,
                            parse_err,
                        )
                        # Route unparseable message to dead letter and ack immediately
                        if self._publisher is not None:
                            try:
                                await self._publisher.publish_to_dead_letter(
                                    envelope=JobEnvelope(
                                        job_id="malformed",
                                        idempotency_key=f"malformed-{time.time()}",
                                        job_type="unknown",
                                        organization_id="00000000-0000-0000-0000-000000000000",
                                    ),
                                    failure_reason=f"EnvelopeParseError: {parse_err}",
                                    origin_routing_key=self.queue_name,
                                    origin_exchange=raw_msg.exchange or "",
                                )
                            except Exception as dlq_err:
                                logger.error("Failed to dead-letter malformed message: %s", dlq_err)
                        await raw_msg.ack()

                # 4. Dispatch valid batch
                if valid_items:
                    try:
                        await self.pre_batch_hook(valid_items)
                        await self.process_batch(valid_items)
                    except Exception as batch_err:
                        logger.error(
                            "Unexpected failure in batch processor for queue '%s': %s",
                            self.queue_name,
                            batch_err,
                        )
        finally:
            self._drain_complete.set()

    async def pre_batch_hook(self, items: list[BatchItem]) -> None:
        """Optional hook executed once per batch before individual jobs run.

        Subclasses may override this to perform amortized bulk DB lookups or warm
        provider connections (design.md §7.4).
        """

    async def process_batch(self, items: list[BatchItem]) -> None:
        """Process a micro-batch of jobs concurrently with strict prompt isolation (R3.6, R3.7).

        Executes `process_job` independently for each item in the batch via `asyncio.gather`.
        """
        await asyncio.gather(
            *(self._handle_single_item(item) for item in items),
            return_exceptions=True,
        )

    async def _handle_single_item(self, item: BatchItem) -> None:
        """Process an individual job delivery with tracing, manual ack, and error isolation."""
        envelope = item.envelope
        message = item.raw_message
        origin_exchange = message.exchange or ""
        origin_routing_key = message.routing_key or self.queue_name

        headers_dict = dict(message.headers) if message.headers else {}
        parent_ctx = extract_trace_context(headers_dict)

        track_ctx = (
            self.shutdown_coordinator.track_job() if self.shutdown_coordinator else nullcontext()
        )

        with (
            bind_log_context(
                trace_id=envelope.trace_id,
                message_id=envelope.message_id,
                thread_id=envelope.thread_id,
                job_id=envelope.job_id,
                organization_id=envelope.organization_id,
            ),
            trace_span(
                "broker.batch_consume",
                parent_context=parent_ctx,
                attributes={
                    "messaging.system": "rabbitmq",
                    "messaging.destination": self.queue_name,
                    "messaging.job_id": envelope.job_id,
                    "messaging.job_type": envelope.job_type,
                    "organization.id": envelope.organization_id,
                    "messaging.attempt": envelope.attempt,
                    "messaging.batch_size": self.batch_size,
                },
            ),
            track_ctx,
        ):
            try:
                # 3.5 Re-deliver recovery: if job is in RETRY_PENDING,
                # transition to GENERATING (R19.5, R18.2)
                await handle_job_recovery(envelope=envelope, job_store=self.job_store)

                # 3.6 Acquire lease on claim (R19.8, design.md §9)
                if self.job_store is not None and is_valid_uuid(envelope.job_id):
                    try:
                        await self.job_store.acquire_lease(
                            organization_id=envelope.organization_id,
                            job_id=UUID(envelope.job_id),
                            lease_timeout_s=self.lease_timeout_s,
                        )
                    except Exception as lease_err:
                        logger.warning(
                            "Failed to acquire lease for job %s: %s", envelope.job_id, lease_err
                        )

                # Independent job processing — strictly separate prompt/inference (R3.7)
                await self.process_job(envelope, message)

                # Manual ACK only after side effects commit (R3.3)
                await message.ack()
                logger.debug(
                    "Job %s successfully processed and ACKed on queue '%s'",
                    envelope.job_id,
                    self.queue_name,
                )
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
                    )
                    await message.ack()
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
                    await message.ack()

    async def stop(self) -> None:
        """Cancel subscription, drain in-flight batches, and close channels gracefully."""
        if not self._is_consuming:
            return

        # 1. Stop accepting new deliveries from AMQP broker
        if self._queue and self._consumer_tag:
            try:
                await self._queue.cancel(self._consumer_tag)
            except Exception as err:
                logger.warning("Error cancelling consumer tag %s: %s", self._consumer_tag, err)

        self._is_consuming = False

        # 2. Wait for background batch loop to drain queued messages
        if self._batch_loop_task and not self._batch_loop_task.done():
            try:
                await asyncio.wait_for(self._drain_complete.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("Batch worker loop drain timed out during shutdown")
                self._batch_loop_task.cancel()

        # 3. Clean up underlying channel and connection
        if self._channel and not self._channel.is_closed:
            await self._channel.close()

        if not self._external_conn and self._connection and not self._connection.is_closed:
            await self._connection.close()

        logger.info("Batch consumer stopped on queue '%s'", self.queue_name)

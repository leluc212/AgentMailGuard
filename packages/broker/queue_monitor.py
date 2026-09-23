"""Background queue depth monitor using passive AMQP declaration (R7.5, R20.5, R21.4).

Periodically samples message count and consumer count for all known queues via
``aio_pika.Channel.declare_queue(queue_name, passive=True)``, updating Prometheus
gauges ``queue_depth`` and ``queue_consumers`` without consuming messages or
modifying broker state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractRobustConnection,
)

from packages.core.settings import BrokerSettings, CategoryRoutingSettings
from packages.domain.taxonomy import get_default_registry
from packages.observability.metrics import PipelineMetrics, get_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator

logger = logging.getLogger(__name__)


def get_monitored_queues(
    broker_settings: BrokerSettings | None = None,
    routing_settings: CategoryRoutingSettings | None = None,
) -> list[str]:
    """Discover all queue names that should be monitored for depth metrics.

    Returns core pipeline queues, retry tier queues, the dead-letter queue,
    and all category×priority lane queues derived from the taxonomy registry.

    Parameters
    ----------
    broker_settings : BrokerSettings | None
        Broker configuration. Defaults to env-based settings.
    routing_settings : CategoryRoutingSettings | None
        Routing configuration for priority lanes. Defaults to env-based settings.

    Returns
    -------
    list[str]
        Sorted list of unique queue names to monitor.
    """
    b_cfg = broker_settings or BrokerSettings()
    rt_cfg = routing_settings or CategoryRoutingSettings()

    queues: list[str] = []

    # Core pipeline stage queues
    queues.extend([
        b_cfg.queue_mail_sync,
        b_cfg.queue_normalize,
        b_cfg.queue_triage,
        b_cfg.queue_dispatch,
        b_cfg.queue_knowledge,
        b_cfg.queue_dead_letter,
    ])

    # Retry tier queues
    queues.extend([
        "email.retry.30s",
        "email.retry.5m",
        "email.retry.30m",
    ])

    # Category × priority lane queues
    registry = get_default_registry()
    all_categories = registry.all_categories()
    priority_lanes = rt_cfg.priority_lanes or ["normal", "priority"]

    for category in all_categories:
        for lane in priority_lanes:
            queues.append(f"email.{category}.{lane}")

    return sorted(set(queues))


class QueueMonitor:
    """Async background poller that samples RabbitMQ queue depths via passive declaration.

    Uses ``declare_queue(queue_name, passive=True)`` to read ``message_count``
    and ``consumer_count`` from the broker without consuming messages or altering
    queue state. Updates Prometheus ``queue_depth`` and ``queue_consumers`` gauges.

    In AMQP 0-9-1, passively declaring a non-existent queue raises
    ``ChannelNotFoundEntity`` and closes the channel. This class gracefully
    catches the error, sets depth to 0, and re-opens the channel for subsequent
    queues.

    Parameters
    ----------
    connection : AbstractRobustConnection | None
        Shared AMQP connection. If None, a new connection is established on start.
    broker_settings : BrokerSettings | None
        Broker configuration for connection URL and queue names.
    metrics : PipelineMetrics | None
        Target metrics instance. Defaults to global singleton.
    queues : list[str] | None
        Explicit list of queue names to monitor. If None, auto-discovered via
        ``get_monitored_queues()``.
    shutdown_coordinator : GracefulShutdownCoordinator | None
        Registers drain callback for graceful shutdown.
    """

    def __init__(
        self,
        connection: AbstractRobustConnection | None = None,
        broker_settings: BrokerSettings | None = None,
        metrics: PipelineMetrics | None = None,
        queues: list[str] | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
    ) -> None:
        self.broker_settings = broker_settings or BrokerSettings()
        self.metrics = metrics or get_metrics()
        self.queue_names = queues or get_monitored_queues(broker_settings=self.broker_settings)
        self.shutdown_coordinator = shutdown_coordinator

        self._connection = connection
        self._external_conn = connection is not None
        self._channel: AbstractChannel | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._running = False

        if self.shutdown_coordinator is not None:
            self.shutdown_coordinator.register_drain_callback(self.stop)

    async def _ensure_channel(self) -> AbstractChannel:
        """Return an open channel, reconnecting if needed."""
        if self._connection is None or self._connection.is_closed:
            self._connection = await aio_pika.connect_robust(self.broker_settings.url)
        if self._channel is None or self._channel.is_closed:
            self._channel = await self._connection.channel()
        return self._channel

    async def sample_queue_depths(self) -> dict[str, int]:
        """Sample message count and consumer count for all monitored queues.

        Returns
        -------
        dict[str, int]
            Mapping of queue_name → message_count.
        """
        results: dict[str, int] = {}

        for queue_name in self.queue_names:
            try:
                channel = await self._ensure_channel()
                q = await channel.declare_queue(queue_name, passive=True)
                message_count = q.declaration_result.message_count or 0
                consumer_count = q.declaration_result.consumer_count or 0

                self.metrics.queue_depth.labels(queue=queue_name).set(message_count)
                self.metrics.queue_consumers.labels(queue=queue_name).set(consumer_count)
                results[queue_name] = message_count

            except Exception as exc:
                # ChannelNotFoundEntity (or similar) means queue does not exist yet.
                # Log at debug level and set depth to 0 — queue will appear when topology
                # is declared or when the first message is routed.
                logger.debug(
                    "Queue '%s' not found during depth sampling (%s); setting depth=0",
                    queue_name,
                    type(exc).__name__,
                )
                self.metrics.queue_depth.labels(queue=queue_name).set(0)
                self.metrics.queue_consumers.labels(queue=queue_name).set(0)
                results[queue_name] = 0

                # Channel is closed after ChannelNotFoundEntity — force re-open
                self._channel = None

        return results

    async def start(self, interval_seconds: float = 10.0) -> None:
        """Start background polling loop.

        Parameters
        ----------
        interval_seconds : float
            Seconds between queue depth samples. Default 10s.
        """
        if self._running:
            logger.warning("QueueMonitor already running")
            return

        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(interval_seconds),
            name="queue-monitor-poll",
        )
        logger.info(
            "QueueMonitor started: sampling %d queues every %.1fs",
            len(self.queue_names),
            interval_seconds,
        )

    async def _poll_loop(self, interval: float) -> None:
        """Internal polling loop executing sample_queue_depths periodically."""
        try:
            while self._running:
                try:
                    await self.sample_queue_depths()
                except Exception:
                    logger.exception("Unexpected error during queue depth sampling")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("QueueMonitor poll loop cancelled")

    async def stop(self) -> None:
        """Stop background polling and clean up resources."""
        self._running = False

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task

        if self._channel and not self._channel.is_closed:
            await self._channel.close()

        if not self._external_conn and self._connection and not self._connection.is_closed:
            await self._connection.close()

        logger.info("QueueMonitor stopped")

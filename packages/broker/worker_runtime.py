"""Process runtime shared by every broker-consuming worker service (R3.2, R20.7, R20.8).

One place owns the resources a worker process needs and their lifecycle:

    settings -> logging/tracing -> health registry + shutdown coordinator
             -> PostgreSQL pool (readiness: database)
             -> RabbitMQ robust connection (readiness: broker)
                  -> topology declaration (R3.2) and a publisher channel that raises on
                     unroutable publishes
             -> build(resources) -> start functions -> /healthz /readyz /metrics server
    SIGTERM  -> drain (consumers stop consuming) -> wait for in-flight jobs -> cleanup
                (consumers close their channels, then publisher channel, connection, pool)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import aio_pika
import asyncpg
from aio_pika.abc import AbstractChannel, AbstractRobustConnection

from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.observability.health import HealthRegistry, ReadinessCheck
from packages.observability.logging import setup_logging
from packages.observability.metrics import PipelineMetrics, get_metrics
from packages.observability.server import ObservabilityServer
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import init_tracer

logger = logging.getLogger(__name__)

StartFn = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class WorkerResources:
    """Shared, already-connected resources handed to a service's build function."""

    settings: AppSettings
    db_pool: asyncpg.Pool[Any]
    connection: AbstractRobustConnection
    publisher: MessagePublisher
    health: HealthRegistry
    shutdown: GracefulShutdownCoordinator
    metrics: PipelineMetrics


BuildFn = Callable[[WorkerResources], Awaitable[Sequence[StartFn]]]


async def declare_topology(connection: AbstractRobustConnection, settings: AppSettings) -> None:
    """Declare the full broker topology on a short-lived channel (R3.2, idempotent)."""
    channel = await connection.channel()
    try:
        await setup_topology(
            channel,
            broker_settings=settings.broker,
            retry_settings=settings.retry,
            routing_settings=settings.routing,
        )
    finally:
        if not channel.is_closed:
            await channel.close()


def database_check(pool: asyncpg.Pool[Any]) -> ReadinessCheck:
    """Readiness probe: the pool can run a trivial query."""

    async def check() -> tuple[bool, str]:
        try:
            async with pool.acquire() as conn:
                value = await conn.fetchval("SELECT 1")
            return (value == 1, "Database connection healthy")
        except Exception as err:
            return (False, f"Database check failed: {err}")

    return check


def broker_check(connection: AbstractRobustConnection) -> ReadinessCheck:
    """Readiness probe: the robust broker connection is open."""

    async def check() -> tuple[bool, str]:
        if connection.is_closed:
            return (False, "Broker connection closed")
        return (True, "Broker connection healthy")

    return check


class WorkerRuntime:
    """Owns a worker process's resources, startup order, health server and shutdown."""

    def __init__(
        self,
        *,
        service_name: str,
        settings: AppSettings,
        port: int,
        build: BuildFn,
        host: str = "0.0.0.0",
        install_signal_handlers: bool = True,
        configure_telemetry: bool = True,
    ) -> None:
        self._service_name = service_name
        self._settings = settings
        self._port = port
        self._host = host
        self._build = build
        self._install_signal_handlers = install_signal_handlers
        self._configure_telemetry = configure_telemetry
        self._resources: WorkerResources | None = None
        self._publish_channel: AbstractChannel | None = None
        self._server: ObservabilityServer | None = None

    @property
    def resources(self) -> WorkerResources:
        if self._resources is None:
            raise RuntimeError("WorkerRuntime.start() has not completed")
        return self._resources

    async def start(self) -> WorkerResources:
        """Connect, declare topology, build and start components, then serve health."""
        settings = self._settings
        if self._configure_telemetry:
            setup_logging(
                level=settings.telemetry.log_level,
                json_format=settings.telemetry.log_format == "json",
                service_name=self._service_name,
            )
            init_tracer(
                self._service_name.replace("_", "-"),
                otlp_endpoint=settings.telemetry.otlp_endpoint,
            )
        logger.info("Starting %s", self._service_name)

        health = HealthRegistry(service_name=self._service_name)
        shutdown = GracefulShutdownCoordinator(
            drain_timeout_s=settings.telemetry.drain_timeout_s,
            health_registry=health,
        )
        if self._install_signal_handlers:
            shutdown.attach_signal_handlers()

        db_pool = await create_pool_from_settings(settings.database)
        connection: AbstractRobustConnection | None = None
        try:
            connection = await aio_pika.connect_robust(settings.broker.url)
            health.register_readiness_check("database", database_check(db_pool))
            health.register_readiness_check("broker", broker_check(connection))

            await declare_topology(connection, settings)
            self._publish_channel = await connection.channel(on_return_raises=True)
            publisher = MessagePublisher(
                broker_settings=settings.broker,
                connection=connection,
                channel=self._publish_channel,
                retry_settings=settings.retry,
            )
            resources = WorkerResources(
                settings=settings,
                db_pool=db_pool,
                connection=connection,
                publisher=publisher,
                health=health,
                shutdown=shutdown,
                metrics=get_metrics(),
            )
            start_fns = await self._build(resources)
            # Registered after build(): consumers registered their own close() first, so
            # shared resources are released only after every consumer channel has closed.
            shutdown.register_cleanup_callback(self._release_resources)
            self._resources = resources
            for start_fn in start_fns:
                await start_fn()
        except BaseException:
            await self._abort_start(db_pool, connection)
            raise

        self._server = ObservabilityServer(host=self._host, port=self._port, health_registry=health)
        await self._server.start()
        logger.info("%s started; health server on port %d", self._service_name, self._port)
        return resources

    async def wait(self) -> None:
        """Block until the shutdown sequence completes, then stop the health server."""
        await self.resources.shutdown.wait_until_complete()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        logger.info("%s stopped cleanly", self._service_name)

    async def stop(self, reason: str = "MANUAL") -> None:
        """Trigger the graceful shutdown sequence and wait for it."""
        await self.resources.shutdown.trigger_shutdown(reason)
        await self.wait()

    async def run(self) -> None:
        """Process entry: start, then serve until SIGTERM/SIGINT drains and stops us."""
        await self.start()
        try:
            await self.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self.stop("MANUAL")

    async def _release_resources(self) -> None:
        resources = self.resources
        closers: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        if self._publish_channel is not None and not self._publish_channel.is_closed:
            closers.append(("publisher channel", self._publish_channel.close))
        closers.append(("broker connection", resources.connection.close))
        closers.append(("database pool", resources.db_pool.close))
        for name, closer in closers:
            try:
                await closer()
            except Exception as err:
                logger.warning("Error closing %s: %s", name, err)

    async def _abort_start(
        self, db_pool: asyncpg.Pool[Any], connection: AbstractRobustConnection | None
    ) -> None:
        logger.error("%s failed to start; releasing resources", self._service_name)
        if connection is not None and not connection.is_closed:
            try:
                await connection.close()
            except Exception as err:
                logger.warning("Error closing broker connection: %s", err)
        try:
            await db_pool.close()
        except Exception as err:
            logger.warning("Error closing database pool: %s", err)

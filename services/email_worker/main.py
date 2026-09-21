"""Email Normalization Worker Service entrypoint (Phase 1).

Runs the EmailNormalizationConsumer consuming from 'email.normalize', offloading
attachments/HTML to MinIO, persisting canonical messages in PostgreSQL, and
exposing /healthz, /readyz, and /metrics for Docker and Prometheus health checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import uvicorn
from fastapi import FastAPI

from packages.broker.publisher import MessagePublisher
from packages.core.settings import AppSettings
from packages.core.storage import get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread import PostgresThreadStore
from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.logging import setup_logging
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import init_tracer
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister

logger = logging.getLogger("email_worker.service")


def create_worker_health_app(health_registry: HealthRegistry) -> FastAPI:
    """Create a minimal FastAPI app serving health and metrics."""
    app = FastAPI(title="Email Worker Health & Metrics", docs_url=None, redoc_url=None)
    app.include_router(create_health_router(health_registry=health_registry))
    return app


async def run_worker() -> None:
    """Initialize resources, start consumer and health server, and coordinate shutdown."""
    settings = AppSettings()
    setup_logging(
        level=settings.telemetry.log_level,
        json_format=settings.telemetry.log_format == "json",
    )
    init_tracer("email-worker", otlp_endpoint=settings.telemetry.otlp_endpoint)

    logger.info("Starting Email Normalization Worker Service")

    # 1. Health registry & graceful shutdown coordinator
    health_reg = HealthRegistry(service_name="email_worker")
    shutdown_coordinator = GracefulShutdownCoordinator(
        drain_timeout_s=15.0,
        health_registry=health_reg,
    )
    shutdown_coordinator.attach_signal_handlers()

    # 2. Connect to Database
    db_pool = await create_pool_from_settings(settings.database)

    async def check_db() -> tuple[bool, str]:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1")
                return (val == 1, "Database connection healthy")
        except Exception as err:
            return (False, f"Database check failed: {err}")

    health_reg.register_readiness_check("database", check_db)

    # 3. Connect to Object Storage
    storage_client = get_storage_client(settings.object_storage)

    # 4. Message Stores & Normalizer
    message_store = PostgresMessageStore(db_pool)
    thread_store = PostgresThreadStore(db_pool)
    job_store = PostgresJobStore(db_pool)
    persister = EmailPersister(message_store=message_store, thread_store=thread_store)
    normalizer = EmailNormalizer()

    # 5. Connect Message Publisher for downstream triage dispatch
    publisher = MessagePublisher(broker_settings=settings.broker)

    # 6. Build consumer
    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage_client,
        broker_settings=settings.broker,
        publisher=publisher,
        job_store=job_store,
        shutdown_coordinator=shutdown_coordinator,
    )

    # Register cleanup callbacks
    async def cleanup_resources() -> None:
        logger.info("Cleaning up worker resources...")
        try:
            await publisher.close()
        except Exception as err:
            logger.warning("Error closing publisher: %s", err)
        try:
            await db_pool.close()
        except Exception as err:
            logger.warning("Error closing database pool: %s", err)

    shutdown_coordinator.register_cleanup_callback(cleanup_resources)

    # 7. Start consumer
    await consumer.start()
    logger.info("Email normalization consumer started successfully")

    # 8. Start HTTP health server
    health_port = int(os.getenv("PORT", "8002"))
    health_app = create_worker_health_app(health_reg)
    config = uvicorn.Config(
        app=health_app,
        host="0.0.0.0",
        port=health_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Run uvicorn server in a task
    server_task = asyncio.create_task(server.serve())

    # Wait for shutdown signal
    try:
        await shutdown_coordinator.wait_until_complete()
    except (asyncio.CancelledError, KeyboardInterrupt):
        await shutdown_coordinator.trigger_shutdown("MANUAL")

    server.should_exit = True
    await server_task
    logger.info("Email Normalization Worker Service stopped cleanly")


def main() -> None:
    """Main process entrypoint."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()

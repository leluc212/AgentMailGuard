"""Knowledge Ingestion Worker Service entrypoint (Phase 3).

Consumes document ingestion requests from RabbitMQ queue 'knowledge.ingest',
orchestrating parsing, chunking, deduplicated embedding, and atomic versioned persistence.
Exposes /healthz, /readyz, and /metrics for Docker and Prometheus health checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import uvicorn
from fastapi import FastAPI

from packages.core.settings import AppSettings
from packages.core.storage import get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.db.knowledge import PostgresKnowledgeStore
from packages.knowledge.embedder import get_embedder
from packages.knowledge.pipeline import KnowledgeIngestionPipeline
from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.logging import setup_logging
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import init_tracer
from services.knowledge_worker.consumer import KnowledgeIngestConsumer

logger = logging.getLogger("knowledge_worker.service")


def create_worker_health_app(health_registry: HealthRegistry) -> FastAPI:
    """Create a minimal FastAPI app serving health and metrics."""
    app = FastAPI(title="Knowledge Worker Health & Metrics", docs_url=None, redoc_url=None)
    app.include_router(create_health_router(health_registry=health_registry))
    return app


async def run_worker() -> None:
    """Initialize resources, start consumer and health server, and coordinate shutdown."""
    settings = AppSettings()
    setup_logging(
        level=settings.telemetry.log_level,
        json_format=settings.telemetry.log_format == "json",
    )
    init_tracer("knowledge-worker", otlp_endpoint=settings.telemetry.otlp_endpoint)

    logger.info("Starting Knowledge Ingestion Worker Service")

    # 1. Health registry & graceful shutdown coordinator
    health_reg = HealthRegistry(service_name="knowledge_worker")
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

    # 3. Connect to Object Storage & Embedder
    storage_client = get_storage_client(settings.object_storage)
    embedder = get_embedder(settings.embedding)

    # 4. Stores and Pipeline
    knowledge_store = PostgresKnowledgeStore(db_pool)
    job_store = PostgresJobStore(db_pool)
    pipeline = KnowledgeIngestionPipeline(
        store=knowledge_store,
        embedder=embedder,
        storage=storage_client,
        bucket_name=settings.object_storage.bucket_knowledge,
    )

    # 5. Build consumer
    consumer = KnowledgeIngestConsumer(
        pipeline=pipeline,
        broker_settings=settings.broker,
        shutdown_coordinator=shutdown_coordinator,
        job_store=job_store,
    )

    # Register cleanup callbacks
    async def cleanup_resources() -> None:
        logger.info("Cleaning up knowledge worker resources...")
        try:
            await db_pool.close()
        except Exception as err:
            logger.warning("Error closing database pool: %s", err)

    shutdown_coordinator.register_cleanup_callback(cleanup_resources)

    # 6. Start consumer
    await consumer.start()
    logger.info("Knowledge ingestion consumer started successfully")

    # 7. Start HTTP health server
    health_port = int(os.getenv("PORT", "8003"))
    health_app = create_worker_health_app(health_reg)
    config = uvicorn.Config(
        app=health_app,
        host="0.0.0.0",
        port=health_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())

    # Wait for shutdown signal
    try:
        await shutdown_coordinator.wait_until_complete()
    except (asyncio.CancelledError, KeyboardInterrupt):
        await shutdown_coordinator.trigger_shutdown("MANUAL")

    server.should_exit = True
    await server_task
    logger.info("Knowledge Ingestion Worker Service stopped cleanly")


def main() -> None:
    """Main process entrypoint."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()

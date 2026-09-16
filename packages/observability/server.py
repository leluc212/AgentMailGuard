"""Standalone HTTP server exposing /healthz, /readyz, and /metrics (R20.7, R21.4).

Background workers (email-worker, triage-worker, ai-worker, etc.) can launch this server
alongside their AMQP consumer to satisfy Docker Compose and Prometheus scrape contracts.
"""

import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from packages.observability.health import HealthRegistry, create_health_router

logger = logging.getLogger(__name__)


class ObservabilityServer:
    """Async background HTTP server hosting health and Prometheus endpoints."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        health_registry: HealthRegistry | None = None,
        metrics_registry: CollectorRegistry | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.health_registry = health_registry or HealthRegistry()
        self.metrics_registry = metrics_registry

        self.app = FastAPI(title="Observability Server", docs_url=None, redoc_url=None)
        health_router = create_health_router(
            health_registry=self.health_registry,
            metrics_registry=self.metrics_registry,
        )
        self.app.include_router(health_router)

        self._config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config=self._config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the HTTP server in an async background task."""
        if self._task is not None and not self._task.done():
            logger.warning("Observability server already running on %s:%d", self.host, self.port)
            return

        self._task = asyncio.create_task(self._server.serve())
        # Brief pause to allow the socket to bind
        for _ in range(50):
            if self._server.started:
                break
            await asyncio.sleep(0.01)

        logger.info("Observability server started on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Signal the HTTP server to exit and await shutdown."""
        if self._task is None:
            return

        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            if not self._task.done():
                self._task.cancel()
        finally:
            self._task = None
            logger.info("Observability server on http://%s:%d stopped", self.host, self.port)


async def start_observability_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    health_registry: HealthRegistry | None = None,
    metrics_registry: CollectorRegistry | None = None,
) -> ObservabilityServer:
    """Convenience helper to create and start an ObservabilityServer."""
    server = ObservabilityServer(
        host=host,
        port=port,
        health_registry=health_registry,
        metrics_registry=metrics_registry,
    )
    await server.start()
    return server

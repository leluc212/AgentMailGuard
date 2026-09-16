"""Unit tests for health/readiness endpoints and graceful shutdown coordinator (R20.7, R20.8)."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.shutdown import GracefulShutdownCoordinator


@pytest.mark.asyncio
async def test_health_registry_liveness_and_readiness() -> None:
    """Verify liveness returns 200 and readiness evaluates dependency checks."""
    h = HealthRegistry("api-service")

    # Liveness always 200
    code, live = h.check_liveness()
    assert code == 200
    assert live["status"] == "ok"
    assert live["service"] == "api-service"

    # Default readiness with no checks is 200 OK
    code, ready = await h.check_readiness()
    assert code == 200
    assert ready["status"] == "ok"

    # Register passing check
    async def db_check() -> tuple[bool, str]:
        return True, "database pool connected (5 idle)"

    h.register_readiness_check("postgres", db_check)
    code, ready = await h.check_readiness()
    assert code == 200
    assert ready["checks"]["postgres"] == "database pool connected (5 idle)"

    # Register failing check
    async def broker_check() -> tuple[bool, str]:
        return False, "rabbitmq channel closed"

    h.register_readiness_check("rabbitmq", broker_check)
    code, ready = await h.check_readiness()
    assert code == 503
    assert ready["status"] == "unready"
    assert ready["checks"]["rabbitmq"] == "rabbitmq channel closed"


@pytest.mark.asyncio
async def test_health_registry_drain_mode() -> None:
    """Verify entering drain mode forces readiness to 503 while liveness remains 200."""
    h = HealthRegistry("worker-1")
    assert h.is_draining is False

    h.set_draining(True)
    assert h.is_draining is True

    code, live = h.check_liveness()
    assert code == 200

    code, ready = await h.check_readiness()
    assert code == 503
    assert ready["status"] == "draining"


@pytest.mark.asyncio
async def test_fastapi_health_router_endpoints() -> None:
    """Verify HTTP client interactions with /healthz, /readyz, and /metrics via test router."""
    h = HealthRegistry("web-test")
    app = FastAPI()
    app.include_router(create_health_router(health_registry=h))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # /healthz
        res = await client.get("/healthz")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        # /readyz
        res = await client.get("/readyz")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        # /metrics
        res = await client.get("/metrics")
        assert res.status_code == 200
        assert "text/plain" in res.headers["content-type"]
        assert len(res.text) > 0


@pytest.mark.asyncio
async def test_graceful_shutdown_tracking_and_drain() -> None:
    """Verify GracefulShutdownCoordinator tracks in-flight jobs and coordinates callbacks."""
    h = HealthRegistry("shutdown-test")
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0, health_registry=h)

    drain_called = False
    cleanup_called = False

    async def on_drain() -> None:
        nonlocal drain_called
        drain_called = True

    async def on_cleanup() -> None:
        nonlocal cleanup_called
        cleanup_called = True

    coord.register_drain_callback(on_drain)
    coord.register_cleanup_callback(on_cleanup)

    # Simulate an active job running in the background
    job_completed = False

    async def background_job() -> None:
        nonlocal job_completed
        with coord.track_job():
            assert coord.active_jobs_count == 1
            await asyncio.sleep(0.05)
            job_completed = True

    job_task = asyncio.create_task(background_job())

    # Give job task a moment to start and increment active counter
    await asyncio.sleep(0.01)
    assert coord.active_jobs_count == 1

    # Trigger shutdown while job is still running
    await coord.trigger_shutdown("TEST_SIGTERM")

    assert job_completed is True
    assert drain_called is True
    assert cleanup_called is True
    assert h.is_draining is True
    assert coord.active_jobs_count == 0
    await job_task

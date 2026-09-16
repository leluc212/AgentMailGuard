"""Health and readiness probing framework and FastAPI router (R20.7).

Provides standard /healthz (liveness), /readyz (dependency readiness), and /metrics endpoints
supporting dependency verification and graceful draining states.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response, status
from prometheus_client import CollectorRegistry

from packages.observability.metrics import generate_metrics_payload

logger = logging.getLogger(__name__)

# Type for an asynchronous readiness check: returns (is_healthy, status_message)
ReadinessCheck = Callable[[], Awaitable[tuple[bool, str]]]


class HealthRegistry:
    """Registry maintaining service liveness, readiness probes, and drain state."""

    def __init__(self, service_name: str = "rag-email") -> None:
        self.service_name = service_name
        self._readiness_checks: dict[str, ReadinessCheck] = {}
        self._is_draining: bool = False

    def set_draining(self, draining: bool = True) -> None:
        """Mark the service as draining during graceful shutdown."""
        self._is_draining = draining
        if draining:
            logger.info(
                "Service '%s' entered DRAINING mode; readiness will report 503",
                self.service_name,
            )

    @property
    def is_draining(self) -> bool:
        """Return True if service is currently draining."""
        return self._is_draining

    def register_readiness_check(self, name: str, check_fn: ReadinessCheck) -> None:
        """Register an asynchronous dependency readiness check."""
        self._readiness_checks[name] = check_fn

    def check_liveness(self) -> tuple[int, dict[str, Any]]:
        """Evaluate liveness probe (/healthz). Returns 200 as long as process is running."""
        return (
            status.HTTP_200_OK,
            {
                "status": "ok",
                "service": self.service_name,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def check_readiness(self) -> tuple[int, dict[str, Any]]:
        """Evaluate all dependency readiness checks (/readyz).

        Returns 200 OK if all dependencies are healthy and service is not draining.
        Returns 503 SERVICE_UNAVAILABLE if any check fails or service is draining.
        """
        if self._is_draining:
            return (
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {
                    "status": "draining",
                    "service": self.service_name,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "checks": {"drain": "Service is draining active jobs for shutdown"},
                },
            )

        if not self._readiness_checks:
            return (
                status.HTTP_200_OK,
                {
                    "status": "ok",
                    "service": self.service_name,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "checks": {},
                },
            )

        check_names = list(self._readiness_checks.keys())
        tasks = [self._readiness_checks[name]() for name in check_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_ok = True
        checks_detail: dict[str, str] = {}

        for name, res in zip(check_names, results, strict=False):
            if isinstance(res, BaseException):
                all_ok = False
                checks_detail[name] = f"error: {res}"
            else:
                ok, msg = res
                if not ok:
                    all_ok = False
                checks_detail[name] = msg

        status_code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        status_str = "ok" if all_ok else "unready"

        return (
            status_code,
            {
                "status": status_str,
                "service": self.service_name,
                "timestamp": datetime.now(UTC).isoformat(),
                "checks": checks_detail,
            },
        )


def create_health_router(
    health_registry: HealthRegistry | None = None,
    metrics_registry: CollectorRegistry | None = None,
) -> APIRouter:
    """Create a FastAPI APIRouter serving /healthz, /readyz, and /metrics.

    Parameters
    ----------
    health_registry : HealthRegistry | None
        Health registry tracking liveness and readiness checks.
    metrics_registry : CollectorRegistry | None
        Target Prometheus metrics registry.

    Returns
    -------
    APIRouter
        Router ready to be mounted in FastAPI or standalone Starlette apps.
    """
    router = APIRouter(tags=["observability"])
    reg = health_registry or HealthRegistry()

    @router.get("/healthz", status_code=status.HTTP_200_OK)
    async def healthz(response: Response) -> dict[str, Any]:
        code, body = reg.check_liveness()
        response.status_code = code
        return body

    @router.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        code, body = await reg.check_readiness()
        response.status_code = code
        return body

    @router.get("/metrics")
    async def metrics() -> Response:
        payload, content_type = generate_metrics_payload(metrics_registry)
        return Response(content=payload, media_type=content_type)

    return router

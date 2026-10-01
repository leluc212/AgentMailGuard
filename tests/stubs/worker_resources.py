"""Fake WorkerResources for composition-root unit tests (no broker, no database)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from prometheus_client import CollectorRegistry

from packages.broker.publisher import MessagePublisher
from packages.broker.worker_runtime import WorkerResources
from packages.core.settings import AppSettings
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator


def fake_worker_resources(settings: AppSettings) -> WorkerResources:
    """Resources whose pool/connection/publisher are inert mocks; stores built on them do no I/O."""
    return WorkerResources(
        settings=settings,
        db_pool=cast(Any, MagicMock(name="db_pool")),
        connection=cast(Any, MagicMock(name="connection")),
        publisher=cast(MessagePublisher, MagicMock(spec=MessagePublisher)),
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )

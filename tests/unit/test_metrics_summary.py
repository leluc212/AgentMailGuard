"""Metrics summary endpoint (task 9.8, R25.9, R21.8, R23.6)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from services.api.main import create_app
from services.api.routers.metrics_summary import PrometheusUnavailableError, queries


class FakePrometheus:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.asked: list[str] = []

    async def query(self, promql: str) -> list[tuple[dict[str, str], float]]:
        self.asked.append(promql)
        if self.fail:
            raise PrometheusUnavailableError("connection refused")
        if "triage_funnel_outcomes_total" in promql:
            return [({"outcome": "early_exit"}, 4.0), ({"outcome": "ai_generated"}, 6.0)]
        if "emails_received_total" in promql:
            return [({}, 10.0)]
        if "histogram_quantile(0.5" in promql:
            return [({}, 2100.0)]
        if "histogram_quantile(0.95" in promql:
            return [({}, float("nan"))]  # an empty histogram
        if "queue_depth" in promql:
            return [({"queue": "email.triage"}, 3.0)]
        if "estimated_ai_cost_total" in promql:
            return [({"model_profile": "live"}, 0.012)]
        return []


async def _get(prometheus: FakePrometheus, org: object, window: str = "15m") -> object:
    app = create_app(lifespan_enabled=False)
    app.state.prometheus_client = prometheus
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(
            f"/v1/metrics/summary?window={window}", headers={"X-Organization-ID": str(org)}
        )


async def test_the_panels_and_their_scopes() -> None:
    org = uuid4()
    response = await _get(FakePrometheus(), org)

    assert response.status_code == status.HTTP_200_OK  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert body["funnel"] == {
        "scope": "organization",
        "values": {"early_exit": 4.0, "ai_generated": 6.0},
    }
    assert body["received"]["values"] == {"total": 10.0}
    assert body["latency_ms"] == {"scope": "stack", "values": {"p50": 2100.0, "p95": None}}
    assert body["queue_depth"]["values"] == {"email.triage": 3.0}
    assert body["cost_usd"] == {"scope": "stack", "values": {"live": 0.012}}


def test_organization_panels_filter_by_the_tenant() -> None:
    org = uuid4()
    promql = queries(org, "1h")

    assert f'organization="{org}"' in promql["funnel"]
    assert f'organization="{org}"' in promql["received"]
    assert "organization" not in promql["queues"]  # stack-wide: the metric has no tenant label


@pytest.mark.parametrize("window", ["15", "15w", "8d", "0m", "1h; drop"])
async def test_a_bad_window_is_422(window: str) -> None:
    prometheus = FakePrometheus()
    response = await _get(prometheus, uuid4(), window)

    assert response.status_code == 422  # type: ignore[attr-defined]
    assert prometheus.asked == []


async def test_prometheus_down_is_503() -> None:
    response = await _get(FakePrometheus(fail=True), uuid4())

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE  # type: ignore[attr-defined]
    assert response.json()["code"] == "PROMETHEUS_UNAVAILABLE"  # type: ignore[attr-defined]


async def test_the_tenant_header_is_required() -> None:
    app = create_app(lifespan_enabled=False)
    app.state.prometheus_client = FakePrometheus()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/v1/metrics/summary")

    assert response.status_code == status.HTTP_400_BAD_REQUEST

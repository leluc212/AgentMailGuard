"""Metrics summary for the demo console (task 9.8, R25.9, R21.8, ADR-0018 decision 9).

``GET /v1/metrics/summary?window=15m`` reads the R21 metrics back from Prometheus and returns the
early-exit funnel, p50/p95 end-to-end latency, queue depths and the cost increase over the window.
Each panel names its scope: ``organization`` where the metric carries the ``organization`` label
(filtered by the request's tenant, R23.6), ``stack`` where it does not (all tenants together).
"""

from __future__ import annotations

import logging
import math
import re
from typing import Annotated, Protocol
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from services.api.dependencies import get_organization_id

logger = logging.getLogger(__name__)

metrics_summary_router = APIRouter(prefix="/metrics", tags=["metrics"])

WINDOW_PATTERN = re.compile(r"^(\d{1,4})(s|m|h|d)$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_WINDOW_S = 7 * 86400


class PrometheusUnavailableError(RuntimeError):
    """Prometheus did not answer, or answered with an error."""


class PromQuery(Protocol):
    async def query(self, promql: str) -> list[tuple[dict[str, str], float]]: ...


class PrometheusClient:
    """Instant queries against Prometheus' HTTP API (``/api/v1/query``)."""

    def __init__(self, base_url: str, *, timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def query(self, promql: str) -> list[tuple[dict[str, str], float]]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/query", params={"query": promql}
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise PrometheusUnavailableError(str(err)) from err
        if body.get("status") != "success":
            raise PrometheusUnavailableError(str(body.get("error") or body))
        return [
            (dict(item.get("metric") or {}), float(item["value"][1]))
            for item in body.get("data", {}).get("result", [])
        ]


def parse_window(window: str) -> str:
    """A Prometheus range like ``15m``; at most 7 days.

    Raises:
        HTTPException: 422 for anything else.
    """
    match = WINDOW_PATTERN.fullmatch(window)
    if match is None or not 0 < int(match.group(1)) * _UNIT_S[match.group(2)] <= MAX_WINDOW_S:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": f"window must look like 30s, 15m, 6h or 1d (at most 7d), not {window!r}",
                "code": "INVALID_WINDOW",
            },
        )
    return window


def _number(value: float) -> float | None:
    """Prometheus answers NaN for an empty histogram; JSON has no NaN."""
    return None if math.isnan(value) or math.isinf(value) else value


class Panel(BaseModel):
    scope: str = Field(description="`organization` (filtered by the tenant) or `stack` (all).")
    values: dict[str, float | None] = Field(default_factory=dict)


class MetricsSummary(BaseModel):
    window: str
    organization_id: UUID
    funnel: Panel = Field(description="Triage funnel outcomes over the window (R6.10, NFR14).")
    received: Panel = Field(description="Emails the connector received over the window.")
    latency_ms: Panel = Field(description="p50 and p95 end-to-end latency over the window.")
    queue_depth: Panel = Field(description="Current depth of each queue.")
    cost_usd: Panel = Field(description="Estimated AI cost added over the window, by profile.")


def queries(org_id: UUID, window: str) -> dict[str, str]:
    """The PromQL of each panel (the tenant id is a validated UUID, safe in a label matcher)."""
    org = f'organization="{org_id}"'
    return {
        "funnel": f"sum by (outcome) (increase(triage_funnel_outcomes_total{{{org}}}[{window}]))",
        "received": f"sum(increase(emails_received_total{{{org}}}[{window}]))",
        "p50": (
            "histogram_quantile(0.5, sum by (le) "
            f"(rate(end_to_end_latency_ms_bucket[{window}])))"
        ),
        "p95": (
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(end_to_end_latency_ms_bucket[{window}])))"
        ),
        "queues": "max by (queue) (queue_depth)",
        "cost": f"sum by (model_profile) (increase(estimated_ai_cost_total[{window}]))",
    }


def _prometheus(request: Request) -> PromQuery:
    client: PromQuery | None = getattr(request.app.state, "prometheus_client", None)
    if client is not None:
        return client
    cfg = request.app.state.settings.observability
    return PrometheusClient(cfg.prometheus_url, timeout_s=cfg.query_timeout_s)


@metrics_summary_router.get(
    "/summary",
    summary="Metrics summary for the demo console",
    response_model=MetricsSummary,
)
async def get_metrics_summary(
    request: Request,
    org_id: Annotated[UUID, Depends(get_organization_id)],
    window: Annotated[
        str, Query(description="Prometheus range: 30s, 15m, 6h, 1d (max 7d).")
    ] = "15m",
) -> MetricsSummary:
    """Read the panels from Prometheus.

    Raises:
        HTTPException: 422 for a bad window; 503 when Prometheus does not answer.
    """
    window = parse_window(window)
    prom = _prometheus(request)
    q = queries(org_id, window)
    try:
        funnel = await prom.query(q["funnel"])
        received = await prom.query(q["received"])
        p50 = await prom.query(q["p50"])
        p95 = await prom.query(q["p95"])
        depths = await prom.query(q["queues"])
        cost = await prom.query(q["cost"])
    except PrometheusUnavailableError as err:
        logger.warning("Prometheus query failed: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Prometheus is not available.", "code": "PROMETHEUS_UNAVAILABLE"},
        ) from err

    def first(rows: list[tuple[dict[str, str], float]]) -> float | None:
        return _number(rows[0][1]) if rows else None

    def by(rows: list[tuple[dict[str, str], float]], label: str) -> dict[str, float | None]:
        return {labels.get(label, "unknown"): _number(value) for labels, value in rows}

    return MetricsSummary(
        window=window,
        organization_id=org_id,
        funnel=Panel(scope="organization", values=by(funnel, "outcome")),
        received=Panel(scope="organization", values={"total": first(received) or 0.0}),
        latency_ms=Panel(scope="stack", values={"p50": first(p50), "p95": first(p95)}),
        queue_depth=Panel(scope="stack", values=by(depths, "queue")),
        cost_usd=Panel(scope="stack", values=by(cost, "model_profile")),
    )


__all__ = ["PrometheusClient", "metrics_summary_router", "queries"]

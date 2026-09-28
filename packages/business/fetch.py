"""Bounded business-data fetch: deadline, degradation and telemetry (R13.7, R21.3).

The provider call runs under ``BUSINESS_DATA__TIMEOUT_MS``. On timeout or any provider error
the customer status and every planned fact become ``UNAVAILABLE`` and the context is marked
degraded, so the draft is still written and a timeout never reads as "no such order"
(design.md §5.4). An empty plan makes no provider call and returns ``None``.

Telemetry per job: one ``business.fetch`` span around the provider call, one
``business_lookups_total{entity, status}`` increment per fact, one
``business_lookup_latency_ms`` observation, and exactly one ``business_fetch`` log line
(also for an empty plan). Correlation ids come from the bound log context. The sender
address and provider error text are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry.trace import StatusCode

from packages.domain.business import BusinessContext, FetchPlan, unavailable_context
from packages.observability.metrics import get_metrics
from packages.observability.tracing import trace_span

if TYPE_CHECKING:
    from packages.business.protocol import BusinessDataProvider
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)

BUSINESS_FETCH_LOG_EVENT = "business_fetch"
"""Message of the one structured log line written per job by the business step (R21.3)."""

BUSINESS_FETCH_SPAN = "business.fetch"


def fact_statuses(context: BusinessContext | None) -> list[dict[str, Any]]:
    """One `BusinessFact.to_payload()` row per fact: entity, reference, status, reason."""
    if context is None:
        return []
    return [fact.to_payload() for fact in context.facts]


def business_payload(plan: FetchPlan, context: BusinessContext | None) -> dict[str, Any]:
    """CONTEXT_READY payload fields for replay (design.md §5.4): plan, statuses, degradation."""
    return {
        "business_plan": plan.to_payload(),
        "customer_status": context.customer_status.value if context is not None else None,
        "business_fact_statuses": fact_statuses(context),
        "business_data_degraded": context.degraded if context is not None else False,
    }


async def fetch_business_context(
    provider: BusinessDataProvider,
    *,
    organization_id: UUID,
    sender_email: str,
    plan: FetchPlan,
    timeout_ms: int,
    metrics: PipelineMetrics | None = None,
) -> BusinessContext | None:
    """Run one bounded provider call for a non-empty plan; degrade instead of failing."""
    if plan.is_empty:
        _log_fetch(organization_id, plan, None, outcome="not_planned", latency_ms=0.0)
        return None

    started = time.perf_counter()
    outcome = "ok"
    attributes: dict[str, Any] = {
        "organization_id": str(organization_id),
        "planned_refs": len(plan.refs),
        "snapshot": ",".join(sorted(entity.value for entity in plan.snapshot)),
        "timeout_ms": timeout_ms,
    }
    with trace_span(BUSINESS_FETCH_SPAN, attributes=attributes) as span:
        try:
            context = await asyncio.wait_for(
                provider.get_business_context(organization_id, sender_email, plan),
                timeout=timeout_ms / 1000,
            )
        except TimeoutError:
            outcome = "timeout"
            logger.warning(
                "Business data fetch timed out after %d ms for tenant %s",
                timeout_ms,
                organization_id,
            )
            context = unavailable_context(plan, datetime.now(UTC))
        except Exception as err:
            outcome = "error"
            logger.warning(
                "Business data fetch failed for tenant %s: %s",
                organization_id,
                type(err).__name__,
            )
            context = unavailable_context(plan, datetime.now(UTC))
        latency_ms = (time.perf_counter() - started) * 1000.0
        span.set_attribute("outcome", outcome)
        span.set_attribute("customer_status", context.customer_status.value)
        span.set_attribute("business_data_degraded", context.degraded)
        if outcome != "ok":
            span.set_status(StatusCode.ERROR, outcome)

    _record_metrics(metrics, context, latency_ms)
    _log_fetch(organization_id, plan, context, outcome=outcome, latency_ms=latency_ms)
    return context


def _record_metrics(
    metrics: PipelineMetrics | None, context: BusinessContext, latency_ms: float
) -> None:
    m = metrics or get_metrics()
    try:
        m.business_lookup_latency_ms.observe(latency_ms)
        for fact in context.facts:
            m.business_lookups_total.labels(
                entity=fact.entity.value, status=fact.status.value
            ).inc()
    except Exception:
        logger.warning("Failed to record business lookup metrics", exc_info=True)


def _log_fetch(
    organization_id: UUID,
    plan: FetchPlan,
    context: BusinessContext | None,
    *,
    outcome: str,
    latency_ms: float,
) -> None:
    with contextlib.suppress(Exception):  # logging must never fail the job
        logger.info(
            BUSINESS_FETCH_LOG_EVENT,
            extra={
                "organization_id": str(organization_id),
                "fields": {
                    "planned": not plan.is_empty,
                    "refs": [ref.reference for ref in plan.refs],
                    "snapshot": sorted(entity.value for entity in plan.snapshot),
                    "customer_status": (
                        context.customer_status.value if context is not None else None
                    ),
                    "facts": fact_statuses(context),
                    "business_data_degraded": (context.degraded if context is not None else False),
                    "outcome": outcome,
                    "latency_ms": round(latency_ms, 2),
                },
            },
        )

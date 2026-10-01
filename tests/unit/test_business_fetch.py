"""Bounded business-data fetch: deadline, degradation, span, metrics, log line.

Requirements: R13.7 (timeout + recorded degradation), R21.3 (structured log line),
R13.6 (UNAVAILABLE never becomes NOT_FOUND). Design: design.md §5.4, ADR-0008.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span

import packages.business.fetch as fetch_module
from packages.business.fetch import (
    BUSINESS_FETCH_LOG_EVENT,
    business_payload,
    fetch_business_context,
)
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
)
from packages.observability.context import bind_log_context
from packages.observability.logging import StructuredJSONFormatter
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from packages.observability.tracing import init_tracer
from packages.observability.tracing import trace_span as real_trace_span

ORDER_REF = EntityRef(entity=EntityType.ORDER, reference="ORD-82915")
TICKET_REF = EntityRef(entity=EntityType.TICKET, reference="TICK-4402")
PLAN = FetchPlan(refs=(ORDER_REF,))
AS_OF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
SENDER = "alice.smith@clientcorp.com"
LOGGER = "packages.business.fetch"


def _found() -> BusinessContext:
    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"),),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-82915",
                status=FactStatus.FOUND,
                attributes=(("status", "shipped"),),
            ),
        ),
    )


class SpyProvider:
    """Records calls; optionally sleeps or raises (a real async provider, no mock library)."""

    def __init__(
        self,
        result: BusinessContext | None = None,
        *,
        delay_s: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.delay_s = delay_s
        self.error = error
        self.calls: list[tuple[UUID, str, FetchPlan]] = []

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        self.calls.append((organization_id, sender_email, plan))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _lookups(metrics: PipelineMetrics, entity: str, status: str) -> float:
    value = metrics.registry.get_sample_value(
        "business_lookups_total", {"entity": entity, "status": status}
    )
    return value or 0.0


def _latency_count(metrics: PipelineMetrics) -> float:
    return metrics.registry.get_sample_value("business_lookup_latency_ms_count") or 0.0


def _fetch_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == BUSINESS_FETCH_LOG_EVENT]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Wrap the module's trace_span so each finished span's name and attributes are kept."""
    init_tracer("business-fetch-test")
    recorded: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def _recording(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        with real_trace_span(name, attributes=attributes) as span:
            yield span
        assert isinstance(span, ReadableSpan)
        recorded.append((name, dict(span.attributes or {})))

    monkeypatch.setattr(fetch_module, "trace_span", _recording)
    return recorded


async def test_empty_plan_makes_no_provider_call_and_writes_one_log_line(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = SpyProvider(_found())
    metrics = create_pipeline_metrics()

    result = await fetch_business_context(
        provider,
        organization_id=uuid4(),
        sender_email=SENDER,
        plan=FetchPlan(),
        timeout_ms=500,
        metrics=metrics,
    )

    assert result is None
    assert provider.calls == []
    assert spans == []
    assert _latency_count(metrics) == 0.0
    lines = _fetch_lines(caplog)
    assert len(lines) == 1
    fields = lines[0].__dict__["fields"]
    assert fields["planned"] is False
    assert fields["outcome"] == "not_planned"


async def test_found_context_is_returned_with_span_metrics_and_correlated_log(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    context = _found()
    provider = SpyProvider(context)
    metrics = create_pipeline_metrics()
    org_id = uuid4()

    with bind_log_context(trace_id="trace-5-4", job_id="job-5-4", organization_id=str(org_id)):
        result = await fetch_business_context(
            provider,
            organization_id=org_id,
            sender_email=SENDER,
            plan=PLAN,
            timeout_ms=500,
            metrics=metrics,
        )
        lines = _fetch_lines(caplog)
        assert len(lines) == 1
        rendered_line = json.loads(StructuredJSONFormatter().format(lines[0]))

    assert result is context
    assert provider.calls == [(org_id, SENDER, PLAN)]
    assert _lookups(metrics, "order", "FOUND") == 1.0
    assert _latency_count(metrics) == 1.0

    assert [name for name, _ in spans] == ["business.fetch"]
    attributes = spans[0][1]
    assert attributes["outcome"] == "ok"
    assert attributes["customer_status"] == "FOUND"
    assert attributes["business_data_degraded"] is False
    assert attributes["planned_refs"] == 1

    assert rendered_line["trace_id"] == "trace-5-4"
    assert rendered_line["job_id"] == "job-5-4"
    assert rendered_line["organization_id"] == str(org_id)
    assert rendered_line["fields"]["facts"] == [
        {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
    ]
    assert rendered_line["fields"]["business_data_degraded"] is False
    assert SENDER not in json.dumps(rendered_line)


async def test_timeout_degrades_every_planned_fact_to_unavailable(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = SpyProvider(_found(), delay_s=1.0)
    metrics = create_pipeline_metrics()
    plan = FetchPlan(refs=(ORDER_REF, TICKET_REF))

    result = await fetch_business_context(
        provider,
        organization_id=uuid4(),
        sender_email=SENDER,
        plan=plan,
        timeout_ms=20,
        metrics=metrics,
    )

    assert result is not None
    assert result.customer_status == CustomerStatus.UNAVAILABLE
    assert result.degraded is True
    assert {(f.reference, f.status) for f in result.facts} == {
        ("ORD-82915", FactStatus.UNAVAILABLE),
        ("TICK-4402", FactStatus.UNAVAILABLE),
    }
    assert _lookups(metrics, "order", "UNAVAILABLE") == 1.0
    assert _lookups(metrics, "ticket", "UNAVAILABLE") == 1.0
    # A timeout must never read as "we have no such order" (R13.6).
    assert _lookups(metrics, "order", "NOT_FOUND") == 0.0
    assert spans[0][1]["outcome"] == "timeout"
    fields = _fetch_lines(caplog)[0].__dict__["fields"]
    assert fields["outcome"] == "timeout"
    assert fields["business_data_degraded"] is True


async def test_provider_error_degrades_and_keeps_error_text_out_of_logs(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = SpyProvider(error=RuntimeError(f"connection reset while reading {SENDER}"))

    result = await fetch_business_context(
        provider,
        organization_id=uuid4(),
        sender_email=SENDER,
        plan=PLAN,
        timeout_ms=500,
        metrics=create_pipeline_metrics(),
    )

    assert result is not None
    assert result.customer_status == CustomerStatus.UNAVAILABLE
    assert result.degraded is True
    assert spans[0][1]["outcome"] == "error"
    all_text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in all_text
    assert SENDER not in all_text


def test_business_payload_records_plan_statuses_and_degraded_flag() -> None:
    payload = business_payload(PLAN, _found())
    assert payload == {
        "business_plan": PLAN.to_payload(),
        "customer_status": "FOUND",
        "business_fact_statuses": [
            {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
        ],
        "business_data_degraded": False,
    }
    json.dumps(payload)  # JSON-safe for the processing_event jsonb column

    assert business_payload(FetchPlan(), None) == {
        "business_plan": FetchPlan().to_payload(),
        "customer_status": None,
        "business_fact_statuses": [],
        "business_data_degraded": False,
    }

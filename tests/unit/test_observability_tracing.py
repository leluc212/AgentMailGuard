"""Unit tests for OpenTelemetry tracer initialization, spans, and carrier propagation.

Fulfills requirements: R21.1, R21.2.
"""

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span, StatusCode

from packages.observability.tracing import (
    extract_trace_context,
    get_current_trace_id,
    get_tracer,
    init_tracer,
    inject_trace_context,
    trace_span,
)


def test_init_and_get_tracer() -> None:
    """Verify tracer initialization and retrieval for services."""
    tracer = init_tracer(service_name="test-service")
    assert tracer is not None

    same_tracer = get_tracer("test-service")
    assert same_tracer is not None


def test_trace_span_attributes_and_active_id() -> None:
    """Verify trace_span creates valid spans, attaches attributes, and exposes hex trace_id."""
    init_tracer("trace-test")

    with trace_span("triage.classify", attributes={"stage": "rules", "confidence": 0.98}) as span:
        trace_id = get_current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32
        assert span.is_recording() is True


def test_trace_span_records_exception() -> None:
    """Verify exceptions raised inside trace_span set status to ERROR."""
    init_tracer("err-test")
    recorded_span: Span | None = None

    with (
        pytest.raises(RuntimeError, match="Processing crashed"),
        trace_span("failing.step") as span,
    ):
        recorded_span = span
        raise RuntimeError("Processing crashed")

    assert isinstance(recorded_span, ReadableSpan)
    assert recorded_span.status.status_code == StatusCode.ERROR
    assert "Processing crashed" in str(recorded_span.status.description)


def test_amqp_carrier_injection_and_extraction() -> None:
    """Verify W3C traceparent injection into AMQP carrier and extraction across hops (R21.1)."""
    init_tracer("carrier-test")

    with trace_span("root.publish") as span:
        expected_trace_id = f"{span.get_span_context().trace_id:032x}"

        # Inject into mock AMQP headers
        raw_headers = {"custom-header": "test-val"}
        headers = inject_trace_context(raw_headers)

        assert "traceparent" in headers
        assert headers["trace_id"] == expected_trace_id
        assert headers["custom-header"] == "test-val"

    # Now simulate consumer on the other side of the queue
    extracted_ctx = extract_trace_context(headers)

    with trace_span("broker.consume", parent_context=extracted_ctx) as child_span:
        child_trace_id = f"{child_span.get_span_context().trace_id:032x}"
        # Verified: child span inherits the exact same distributed trace ID across the queue hop
        assert child_trace_id == expected_trace_id

"""OpenTelemetry tracing initialization, span helpers, and AMQP carrier propagation (R21.1, R21.2).

Provides distributed context propagation across AMQP queue hops so that a single inbound email
maintains a unified trace across all processing workers per specs/design.md §10.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

# Global W3C TraceContext propagator
_PROPAGATOR = TraceContextTextMapPropagator()


def init_tracer(service_name: str = "rag-email", otlp_endpoint: str | None = None) -> Tracer:
    """Initialize OpenTelemetry TracerProvider and set global tracer.

    Parameters
    ----------
    service_name : str
        Name of the reporting service (e.g. 'api', 'email_worker', 'triage_worker').
    otlp_endpoint : str | None
        Optional OTLP exporter gRPC or HTTP endpoint.

    Returns
    -------
    Tracer
        Configured OpenTelemetry tracer.
    """
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(
                "Configured OTLP span exporter for service '%s' at %s",
                service_name,
                otlp_endpoint,
            )
        except Exception as err:
            logger.warning("Could not initialize OTLP span exporter: %s", err)

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


def get_tracer(service_name: str = "rag-email") -> Tracer:
    """Get the active OpenTelemetry tracer for the specified service name."""
    return trace.get_tracer(service_name)


def inject_trace_context(carrier: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inject active OpenTelemetry trace context into an AMQP carrier dictionary.

    Parameters
    ----------
    carrier : dict[str, Any] | None
        Target header dictionary. If None, creates a new dictionary.

    Returns
    -------
    dict[str, Any]
        Header dictionary enriched with W3C `traceparent` and hex correlation IDs.
    """
    headers = carrier.copy() if carrier is not None else {}
    _PROPAGATOR.inject(headers)

    # Attach hex trace_id and span_id explicitly for non-W3C readers
    current_span = trace.get_current_span()
    span_ctx = current_span.get_span_context()
    if span_ctx.is_valid:
        headers["trace_id"] = f"{span_ctx.trace_id:032x}"
        headers["span_id"] = f"{span_ctx.span_id:016x}"

    return headers


def extract_trace_context(carrier: dict[str, Any]) -> context.Context:
    """Extract OpenTelemetry trace context from an incoming carrier dictionary (AMQP headers).

    Parameters
    ----------
    carrier : dict[str, Any]
        Incoming message headers or carrier dict.

    Returns
    -------
    context.Context
        Extracted OpenTelemetry context containing the active parent span context.
    """
    clean_carrier = {str(k): str(v) for k, v in carrier.items() if v is not None}
    return _PROPAGATOR.extract(carrier=clean_carrier)


def get_current_trace_id() -> str | None:
    """Return the active 32-character hex trace ID, or None if no valid span is active."""
    span = trace.get_current_span()
    span_ctx = span.get_span_context()
    if span_ctx.is_valid:
        return f"{span_ctx.trace_id:032x}"
    return None


@contextmanager
def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    tracer_name: str = "rag-email",
    parent_context: context.Context | None = None,
) -> Iterator[Span]:
    """Context manager executing a block within an active OpenTelemetry span.

    Automatically handles exception recording, status setting, and attribute injection.

    Parameters
    ----------
    name : str
        Span name (e.g. 'email.lifecycle', 'broker.consume', 'triage.classify').
    attributes : dict[str, Any] | None
        Optional span attributes (e.g. model, tier, confidence, decided_by).
    tracer_name : str
        Tracer namespace identifier.
    parent_context : context.Context | None
        Optional parent context (extracted from queue message).

    Yields
    ------
    Span
        The newly started span.
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name, context=parent_context) as span:
        if attributes:
            for k, v in attributes.items():
                if v is not None:
                    # OpenTelemetry attributes support bool, int, float, str, or sequence thereof
                    val = v if isinstance(v, (bool, int, float, str)) else str(v)
                    span.set_attribute(k, val)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise

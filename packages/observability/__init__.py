"""Enterprise observability suite.

Covers structured logging, OpenTelemetry tracing, Prometheus metrics, and health probes.
"""

from packages.observability.context import (
    CORRELATION_KEYS,
    bind_log_context,
    clear_correlation_context,
    get_correlation_context,
    reset_correlation_context,
    set_correlation_context,
)
from packages.observability.health import (
    HealthRegistry,
    ReadinessCheck,
    create_health_router,
)
from packages.observability.logging import (
    CorrelationFilter,
    StructuredJSONFormatter,
    setup_logging,
)
from packages.observability.metrics import (
    PipelineMetrics,
    create_pipeline_metrics,
    generate_metrics_payload,
    get_metrics,
    record_ai_cost,
)
from packages.observability.server import (
    ObservabilityServer,
    start_observability_server,
)
from packages.observability.shutdown import (
    GracefulShutdownCoordinator,
)
from packages.observability.tracing import (
    extract_trace_context,
    get_current_trace_id,
    get_tracer,
    init_tracer,
    inject_trace_context,
    trace_span,
)

__all__ = [
    "CORRELATION_KEYS",
    "CorrelationFilter",
    "GracefulShutdownCoordinator",
    "HealthRegistry",
    "ObservabilityServer",
    "PipelineMetrics",
    "ReadinessCheck",
    "StructuredJSONFormatter",
    "bind_log_context",
    "clear_correlation_context",
    "create_health_router",
    "create_pipeline_metrics",
    "extract_trace_context",
    "generate_metrics_payload",
    "get_correlation_context",
    "get_current_trace_id",
    "get_metrics",
    "get_tracer",
    "init_tracer",
    "inject_trace_context",
    "record_ai_cost",
    "reset_correlation_context",
    "set_correlation_context",
    "setup_logging",
    "start_observability_server",
    "trace_span",
]

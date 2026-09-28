"""Prometheus metrics registry and token cost accounting (R21.4–R21.6).

Exposes all standardized pipeline counters, latency histograms with p50/p95/p99 buckets,
and monotonic AI cost calculation per specs/design.md §10.
"""

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from packages.core.pricing import estimate_inference_cost

if TYPE_CHECKING:
    from packages.core.settings import ModelPricing

logger = logging.getLogger(__name__)

# Standard latency buckets (in milliseconds)
CLASSIFICATION_BUCKETS = (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0)
RETRIEVAL_BUCKETS = (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0)
RERANK_BUCKETS = (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)
# 500 is the default BUSINESS_DATA__TIMEOUT_MS, so a p95 against the deadline reads a real edge.
BUSINESS_LOOKUP_BUCKETS = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)
# 1000 and 5000 are the NFR8 edges (LLM generation 1-5 s), so p95 checks read a real bucket.
GENERATION_BUCKETS = (100.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 5000.0, 8000.0, 15000.0)
END_TO_END_BUCKETS = (500.0, 1000.0, 2000.0, 4000.0, 6000.0, 8000.0, 10000.0, 20000.0)
QUEUE_WAIT_BUCKETS = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10000.0,
    30000.0,
    60000.0,
)
CALLS_PER_JOB_BUCKETS = (1.0, 2.0, 3.0, 4.0, 5.0)
CONTEXT_TOKEN_BUCKETS = (
    256.0,
    512.0,
    1024.0,
    2048.0,
    4096.0,
    8192.0,
    16384.0,
    32768.0,
    65536.0,
    131072.0,
)
PAYLOAD_SIZE_BUCKETS = (
    1024.0,
    10240.0,
    51200.0,
    256000.0,
    1048576.0,
    5242880.0,
    26214400.0,
)


@dataclass
class PipelineMetrics:
    """Container holding all Prometheus metric instruments for the pipeline."""

    registry: CollectorRegistry

    # --- Counters (R21.4) ---
    emails_received_total: Counter
    emails_classified_total: Counter
    emails_early_exit_total: Counter
    emails_templated_total: Counter
    emails_generated_total: Counter
    triage_funnel_outcomes_total: Counter
    failed_jobs_total: Counter
    retry_jobs_total: Counter
    input_tokens_total: Counter
    output_tokens_total: Counter
    embedding_tokens_total: Counter
    estimated_ai_cost_total: Counter
    generated_draft_cost_total: Counter
    llm_calls_total: Counter
    retrieval_underfilled_total: Counter
    retrieval_degraded_total: Counter
    rerank_fallback_total: Counter
    subscription_renewals_total: Counter
    raw_payloads_archived_total: Counter
    reaped_leases_total: Counter
    tokens_saved_total: Counter
    model_escalations_total: Counter
    draft_repairs_total: Counter
    draft_validation_failures_total: Counter
    citations_verified_total: Counter
    citation_mismatches_total: Counter
    business_lookups_total: Counter

    # --- Histograms (R21.4, R21.5) ---
    classification_latency_ms: Histogram
    retrieval_latency_ms: Histogram
    rerank_latency_ms: Histogram
    generation_latency_ms: Histogram
    end_to_end_latency_ms: Histogram
    queue_wait_ms: Histogram
    llm_calls_per_job: Histogram
    llm_context_tokens: Histogram
    raw_payload_size_bytes: Histogram
    business_lookup_latency_ms: Histogram

    # --- Gauges (R21.4) ---
    queue_depth: Gauge
    queue_consumers: Gauge
    retrieval_hit_rate: Gauge
    retrieval_top_k: Gauge


def create_pipeline_metrics(registry: CollectorRegistry | None = None) -> PipelineMetrics:
    """Initialize and register all pipeline metrics on the given CollectorRegistry.

    Parameters
    ----------
    registry : CollectorRegistry | None
        Target registry. If None, a new isolated CollectorRegistry is created.

    Returns
    -------
    PipelineMetrics
        Container with all registered instruments.
    """
    reg = registry if registry is not None else CollectorRegistry(auto_describe=True)

    return PipelineMetrics(
        registry=reg,
        # Counters
        emails_received_total=Counter(
            "emails_received_total",
            "Total inbound emails received by mail connector",
            ["organization", "mailbox"],
            registry=reg,
        ),
        emails_classified_total=Counter(
            "emails_classified_total",
            "Total emails processed by triage stage",
            ["organization", "category", "priority", "decided_by"],
            registry=reg,
        ),
        emails_early_exit_total=Counter(
            "emails_early_exit_total",
            "Total emails routed to early exit (no reply required, zero AI)",
            ["organization", "category", "reason"],
            registry=reg,
        ),
        emails_templated_total=Counter(
            "emails_templated_total",
            "Total emails resolved via deterministic template (zero LLM)",
            ["organization", "template_id"],
            registry=reg,
        ),
        emails_generated_total=Counter(
            "emails_generated_total",
            "Total draft responses generated by AI worker",
            ["organization", "category", "model_tier"],
            registry=reg,
        ),
        triage_funnel_outcomes_total=Counter(
            "triage_funnel_outcomes_total",
            "Triage funnel outcome counters (R6.10, R6.15, NFR14)",
            ["organization", "category", "outcome", "rag_mode"],
            registry=reg,
        ),
        failed_jobs_total=Counter(
            "failed_jobs_total",
            "Total jobs routed to terminal dead-letter queue",
            ["queue", "job_type", "error_type"],
            registry=reg,
        ),
        retry_jobs_total=Counter(
            "retry_jobs_total",
            "Total jobs scheduled on retry ladder",
            ["queue", "tier"],
            registry=reg,
        ),
        input_tokens_total=Counter(
            "input_tokens_total",
            "Total LLM prompt input tokens consumed",
            ["model", "tier"],
            registry=reg,
        ),
        output_tokens_total=Counter(
            "output_tokens_total",
            "Total LLM completion output tokens generated",
            ["model", "tier"],
            registry=reg,
        ),
        embedding_tokens_total=Counter(
            "embedding_tokens_total",
            "Total tokens passed through embedding models",
            ["model"],
            registry=reg,
        ),
        estimated_ai_cost_total=Counter(
            "estimated_ai_cost_total",
            "Monotonic cumulative estimated AI cost in USD",
            ["model", "tier"],
            registry=reg,
        ),
        generated_draft_cost_total=Counter(
            "generated_draft_cost_total",
            "Estimated USD cost of persisted AI drafts, by category (R21.6)",
            ["category", "model_tier"],
            registry=reg,
        ),
        llm_calls_total=Counter(
            "llm_calls_total",
            "Total discrete LLM inference invocations",
            ["kind", "model"],
            registry=reg,
        ),
        retrieval_underfilled_total=Counter(
            "retrieval_underfilled_total",
            "Count of filtered HNSW queries returning fewer than top-N candidates",
            ["tenant"],
            registry=reg,
        ),
        retrieval_degraded_total=Counter(
            "retrieval_degraded_total",
            "Total times hybrid retrieval degraded to a single branch due to failure or timeout",
            ["tenant", "failed_branch"],
            registry=reg,
        ),
        rerank_fallback_total=Counter(
            "rerank_fallback_total",
            "Total times cross-encoder reranking fell back to RRF order (R11.5)",
            ["tenant", "reason"],
            registry=reg,
        ),
        subscription_renewals_total=Counter(
            "subscription_renewals_total",
            "Total subscription renewals attempted",
            ["provider", "status"],
            registry=reg,
        ),
        raw_payloads_archived_total=Counter(
            "raw_payloads_archived_total",
            "Total raw email payloads archived in object storage",
            ["provider", "status"],
            registry=reg,
        ),
        reaped_leases_total=Counter(
            "reaped_leases_total",
            "Total stuck jobs reclaimed after lease expiration (R19.8)",
            ["action", "state"],
            registry=reg,
        ),
        tokens_saved_total=Counter(
            "tokens_saved_total",
            "Total prompt tokens saved via conversation summarization (R8.7, H3)",
            ["organization"],
            registry=reg,
        ),
        model_escalations_total=Counter(
            "model_escalations_total",
            "Total model tier escalations triggered by complexity router (R15.3, R15.6)",
            ["reason", "tier"],
            registry=reg,
        ),
        draft_repairs_total=Counter(
            "draft_repairs_total",
            "Total schema repair retry outcomes for generated drafts (R16.3)",
            ["status"],
            registry=reg,
        ),
        draft_validation_failures_total=Counter(
            "draft_validation_failures_total",
            "Total draft schema validation failures by pipeline stage (R16.2, R16.3)",
            ["stage"],
            registry=reg,
        ),
        citations_verified_total=Counter(
            "citations_verified_total",
            "Generated drafts whose citations were checked against the supplied context (R16.5)",
            ["category"],
            registry=reg,
        ),
        citation_mismatches_total=Counter(
            "citation_mismatches_total",
            "Generated drafts citing at least one chunk absent from the context (R16.5)",
            ["category"],
            registry=reg,
        ),
        business_lookups_total=Counter(
            "business_lookups_total",
            "Business facts produced per lookup, by entity and status (R13.6, R13.7)",
            ["entity", "status"],
            registry=reg,
        ),
        # Histograms (latency and calls per job)
        classification_latency_ms=Histogram(
            "classification_latency_ms",
            "Triage classification latency in milliseconds",
            ["stage"],
            buckets=CLASSIFICATION_BUCKETS,
            registry=reg,
        ),
        retrieval_latency_ms=Histogram(
            "retrieval_latency_ms",
            "Knowledge retrieval latency in milliseconds",
            ["mode"],
            buckets=RETRIEVAL_BUCKETS,
            registry=reg,
        ),
        rerank_latency_ms=Histogram(
            "rerank_latency_ms",
            "Cross-encoder reranking latency in milliseconds",
            buckets=RERANK_BUCKETS,
            registry=reg,
        ),
        generation_latency_ms=Histogram(
            "generation_latency_ms",
            "LLM draft generation latency in milliseconds",
            ["model", "tier"],
            buckets=GENERATION_BUCKETS,
            registry=reg,
        ),
        end_to_end_latency_ms=Histogram(
            "end_to_end_latency_ms",
            "Total end-to-end processing latency from sync to draft",
            buckets=END_TO_END_BUCKETS,
            registry=reg,
        ),
        queue_wait_ms=Histogram(
            "queue_wait_ms",
            "Time spent in queue before worker consumption",
            ["queue"],
            buckets=QUEUE_WAIT_BUCKETS,
            registry=reg,
        ),
        llm_calls_per_job=Histogram(
            "llm_calls_per_job",
            "Distribution of LLM calls made for a single job execution",
            ["kind"],
            buckets=CALLS_PER_JOB_BUCKETS,
            registry=reg,
        ),
        llm_context_tokens=Histogram(
            "llm_context_tokens",
            "Final assembled context size per inference request, in tokens (R11.7)",
            ["kind", "tier"],
            buckets=CONTEXT_TOKEN_BUCKETS,
            registry=reg,
        ),
        raw_payload_size_bytes=Histogram(
            "raw_payload_size_bytes",
            "Size of archived raw email payloads in bytes",
            ["provider"],
            buckets=PAYLOAD_SIZE_BUCKETS,
            registry=reg,
        ),
        business_lookup_latency_ms=Histogram(
            "business_lookup_latency_ms",
            "Duration of one bounded business-data provider call in milliseconds (R13.7)",
            buckets=BUSINESS_LOOKUP_BUCKETS,
            registry=reg,
        ),
        # Gauges
        queue_depth=Gauge(
            "queue_depth",
            "Current pending message depth in queue",
            ["queue"],
            registry=reg,
        ),
        queue_consumers=Gauge(
            "queue_consumers",
            "Current number of active consumers on queue",
            ["queue"],
            registry=reg,
        ),
        retrieval_hit_rate=Gauge(
            "retrieval_hit_rate",
            "Rolling knowledge retrieval hit rate",
            ["organization"],
            registry=reg,
        ),
        retrieval_top_k=Gauge(
            "retrieval_top_k",
            "Number of context chunks injected into prompt",
            ["organization"],
            registry=reg,
        ),
    )


# Singleton instance for default application telemetry
_DEFAULT_METRICS: PipelineMetrics | None = None


def get_metrics() -> PipelineMetrics:
    """Return the global default PipelineMetrics instance."""
    global _DEFAULT_METRICS
    if _DEFAULT_METRICS is None:
        _DEFAULT_METRICS = create_pipeline_metrics()
    return _DEFAULT_METRICS


def record_ai_cost(
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    price_table: Mapping[str, "ModelPricing"],
    metrics: PipelineMetrics | None = None,
) -> float:
    """Compute and record token counts and estimated AI cost (R21.6, design.md §10).

    Uses monotonic counter accumulation: cost is never recorded as a gauge so it can
    be cleanly summed over arbitrary evaluation windows (SC9).

    Parameters
    ----------
    model : str
        Model name (e.g. 'gpt-4o-mini', 'gpt-4o').
    tier : str
        Routing tier ('fast', 'strong', 'fallback').
    input_tokens : int
        Number of prompt tokens consumed.
    output_tokens : int
        Number of completion tokens generated.
    price_table : dict[str, ModelPricing]
        Configured per-model token pricing table.
    metrics : PipelineMetrics | None
        Target metrics instance (defaults to global singleton).

    Returns
    -------
    float
        Calculated cost in USD for this inference.
    """
    m = metrics or get_metrics()

    # Record token counts
    m.input_tokens_total.labels(model=model, tier=tier).inc(input_tokens)
    m.output_tokens_total.labels(model=model, tier=tier).inc(output_tokens)

    cost = estimate_inference_cost(model, input_tokens, output_tokens, price_table)
    if cost is None:
        logger.warning("No pricing configured for model '%s'; recorded tokens at 0 cost", model)
        return 0.0

    m.estimated_ai_cost_total.labels(model=model, tier=tier).inc(cost)
    return cost


def generate_metrics_payload(registry: CollectorRegistry | None = None) -> tuple[bytes, str]:
    """Generate the latest Prometheus exposition format text payload and content type header.

    Parameters
    ----------
    registry : CollectorRegistry | None
        Registry to scrape. If None, scrapes global metrics registry.

    Returns
    -------
    tuple[bytes, str]
        (raw_prometheus_bytes, content_type_header_string)
    """
    target_reg = registry or get_metrics().registry
    return generate_latest(target_reg), CONTENT_TYPE_LATEST


def record_retrieval_latency(
    metrics: PipelineMetrics | None,
    latency_ms: float,
    mode: str = "hybrid",
) -> None:
    """Record hybrid retrieval duration in milliseconds to Prometheus histogram (R11.6, R21.4).

    Parameters
    ----------
    metrics : PipelineMetrics | None
        Target pipeline metrics instance. If None, records to global metrics.
    latency_ms : float
        Retrieval latency in milliseconds.
    mode : str
        Retrieval mode: 'hybrid', 'degraded', or 'failed'.
    """
    m = metrics or get_metrics()
    with contextlib.suppress(Exception):
        m.retrieval_latency_ms.labels(mode=mode).observe(latency_ms)


def record_rerank_latency(
    metrics: PipelineMetrics | None,
    latency_ms: float,
) -> None:
    """Record semantic reranking duration in milliseconds to Prometheus histogram (R11.6, R21.4).

    Parameters
    ----------
    metrics : PipelineMetrics | None
        Target pipeline metrics instance. If None, records to global metrics.
    latency_ms : float
        Reranking latency in milliseconds.
    """
    m = metrics or get_metrics()
    with contextlib.suppress(Exception):
        m.rerank_latency_ms.observe(latency_ms)

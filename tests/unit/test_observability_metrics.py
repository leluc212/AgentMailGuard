"""Unit tests for Prometheus metrics registry and AI token cost accounting (R21.4–R21.6)."""

from packages.core.settings import ModelPricing
from packages.observability.metrics import (
    CLASSIFICATION_BUCKETS,
    GENERATION_BUCKETS,
    create_pipeline_metrics,
    generate_metrics_payload,
    record_ai_cost,
)


def test_metrics_registry_initialization_all_r21_metrics() -> None:
    """Verify all metrics specified in R21.4 are registered with proper labels and types."""
    m = create_pipeline_metrics()

    # Counters
    assert m.emails_received_total is not None
    assert m.emails_classified_total is not None
    assert m.emails_early_exit_total is not None
    assert m.emails_templated_total is not None
    assert m.emails_generated_total is not None
    assert m.triage_funnel_outcomes_total is not None
    assert m.failed_jobs_total is not None
    assert m.retry_jobs_total is not None
    assert m.input_tokens_total is not None
    assert m.output_tokens_total is not None
    assert m.embedding_tokens_total is not None
    assert m.estimated_ai_cost_total is not None
    assert m.llm_calls_total is not None
    assert m.retrieval_underfilled_total is not None
    assert m.retrieval_degraded_total is not None
    assert m.rerank_fallback_total is not None
    assert m.subscription_renewals_total is not None
    assert m.raw_payloads_archived_total is not None
    assert m.reaped_leases_total is not None
    assert m.tokens_saved_total is not None
    assert m.model_escalations_total is not None
    assert m.draft_repairs_total is not None
    assert m.draft_validation_failures_total is not None

    # Histograms
    assert m.classification_latency_ms is not None
    assert m.retrieval_latency_ms is not None
    assert m.rerank_latency_ms is not None
    assert m.generation_latency_ms is not None
    assert m.end_to_end_latency_ms is not None
    assert m.queue_wait_ms is not None
    assert m.llm_calls_per_job is not None
    assert m.raw_payload_size_bytes is not None

    # Gauges
    assert m.queue_depth is not None
    assert m.retrieval_hit_rate is not None
    assert m.retrieval_top_k is not None


def test_monotonic_ai_cost_calculation_per_price_table() -> None:
    """Verify monotonic AI cost calculation matches formula and records to counter (R21.6)."""
    m = create_pipeline_metrics()

    price_table = {
        "gpt-4o-mini": ModelPricing(input_per_m=0.15, output_per_m=0.60),
        "gpt-4o": ModelPricing(input_per_m=5.00, output_per_m=15.00),
    }

    # 10,000 input tokens, 2,000 output tokens on gpt-4o
    # Expected cost: (10,000 / 1,000,000 * 5.00) + (2,000 / 1,000,000 * 15.00)
    #             = 0.05 + 0.03 = 0.08
    cost = record_ai_cost(
        model="gpt-4o",
        tier="strong",
        input_tokens=10_000,
        output_tokens=2_000,
        price_table=price_table,
        metrics=m,
    )

    assert abs(cost - 0.08) < 1e-6

    # Verify Prometheus exposition contains the cumulative cost
    payload, content_type = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert "estimated_ai_cost_total" in payload_str
    assert "input_tokens_total" in payload_str
    assert "output_tokens_total" in payload_str
    assert "text/plain" in content_type


def test_latency_histogram_buckets() -> None:
    """Verify latency histograms have suitable buckets for p50/p95/p99 (R21.5)."""
    m = create_pipeline_metrics()

    # Record sample latencies
    m.classification_latency_ms.labels(stage="rules").observe(15.0)
    m.classification_latency_ms.labels(stage="rules").observe(45.0)
    m.generation_latency_ms.labels(model="gpt-4o", tier="strong").observe(1250.0)

    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert "classification_latency_ms_bucket" in payload_str
    assert "generation_latency_ms_bucket" in payload_str
    assert str(CLASSIFICATION_BUCKETS[0]) in payload_str
    assert str(GENERATION_BUCKETS[0]) in payload_str


def test_llm_calls_per_job_labels() -> None:
    """Verify llm_calls_per_job histogram supports 'kind' label (R14.10)."""
    m = create_pipeline_metrics()

    assert m.llm_calls_per_job is not None

    m.llm_calls_per_job.labels(kind="generate").observe(1)
    m.llm_calls_per_job.labels(kind="triage").observe(1)
    m.llm_calls_per_job.labels(kind="summarize").observe(1)
    m.llm_calls_per_job.labels(kind="repair").observe(1)
    m.llm_calls_per_job.labels(kind="total").observe(4)

    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert 'llm_calls_per_job_bucket{kind="generate"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="triage"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="summarize"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="repair"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="total"' in payload_str


def test_model_escalations_total_counter() -> None:
    """Verify model_escalations_total records escalations with reason and tier (R15.3, R15.6)."""
    m = create_pipeline_metrics()

    assert m.model_escalations_total is not None
    m.model_escalations_total.labels(
        reason="low_classification_confidence", tier="high_capability"
    ).inc()

    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert (
        'model_escalations_total{reason="low_classification_confidence",tier="high_capability"} 1.0'
        in payload_str
    )


def test_draft_repairs_and_validation_failure_metrics() -> None:
    """Verify draft repair and validation failure counters are registered (R16.2, R16.3)."""
    m = create_pipeline_metrics()

    assert m.draft_repairs_total is not None
    assert m.draft_validation_failures_total is not None

    m.draft_repairs_total.labels(status="succeeded").inc()
    m.draft_repairs_total.labels(status="failed").inc()
    m.draft_validation_failures_total.labels(stage="initial").inc()
    m.draft_validation_failures_total.labels(stage="repair").inc()

    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert 'draft_repairs_total{status="succeeded"} 1.0' in payload_str
    assert 'draft_repairs_total{status="failed"} 1.0' in payload_str
    assert 'draft_validation_failures_total{stage="initial"} 1.0' in payload_str
    assert 'draft_validation_failures_total{stage="repair"} 1.0' in payload_str


def test_citation_verification_metrics() -> None:
    """Verify the citation grounding counters are registered and emit (R16.5, R21.4)."""
    m = create_pipeline_metrics()

    assert m.citations_verified_total is not None
    assert m.citation_mismatches_total is not None

    m.citations_verified_total.labels(category="support").inc()
    m.citations_verified_total.labels(category="billing").inc()
    m.citation_mismatches_total.labels(category="support").inc()

    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert 'citations_verified_total{category="support"} 1.0' in payload_str
    assert 'citations_verified_total{category="billing"} 1.0' in payload_str
    assert 'citation_mismatches_total{category="support"} 1.0' in payload_str

"""Unit tests for retrieval and rerank latency metrics (R11.6, R21.4, R21.5, NFR5, NFR6).

Verifies:
- retrieval_latency_ms and rerank_latency_ms are recorded separately (R11.6).
- Latency metrics are exposed as Prometheus histograms with buckets for p50/p95/p99 (R21.4, R21.5).
- Histograms encompass hybrid retrieval (NFR5: 50–250ms) and reranking (NFR6: 20–200ms) targets.
- HybridRetriever observes retrieval_latency_ms across hybrid, degraded, and failed modes.
- RerankService observes rerank_latency_ms across success, timeout, and error fallback modes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from packages.observability.metrics import (
    RERANK_BUCKETS,
    RETRIEVAL_BUCKETS,
    create_pipeline_metrics,
    generate_metrics_payload,
    record_rerank_latency,
    record_retrieval_latency,
)
from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.rerank import RerankerUnavailableError, RerankService
from packages.retrieval.retriever import HybridRetriever, RetrievalError


class MockSearchBackend:
    """Mock search backend with configurable responses and failures."""

    def __init__(
        self,
        lexical_candidates: list[Candidate] | None = None,
        vector_candidates: list[Candidate] | None = None,
        lexical_exc: Exception | None = None,
        vector_exc: Exception | None = None,
        lexical_delay: float = 0.0,
        vector_delay: float = 0.0,
    ) -> None:
        self.lexical_candidates = lexical_candidates or []
        self.vector_candidates = vector_candidates or []
        self.lexical_exc = lexical_exc
        self.vector_exc = vector_exc
        self.lexical_delay = lexical_delay
        self.vector_delay = vector_delay

    async def lexical(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        if self.lexical_delay > 0:
            await asyncio.sleep(self.lexical_delay)
        if self.lexical_exc:
            raise self.lexical_exc
        return self.lexical_candidates[:top_n]

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        if self.vector_delay > 0:
            await asyncio.sleep(self.vector_delay)
        if self.vector_exc:
            raise self.vector_exc
        return self.vector_candidates[:top_n]


class MockReranker:
    """Mock reranker with configurable behavior for testing latency and timeouts."""

    def __init__(
        self,
        delay: float = 0.0,
        exc: Exception | None = None,
    ) -> None:
        self.delay = delay
        self.exc = exc

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        top_k: int | None = None,
    ) -> list[Candidate]:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        k = top_k or len(candidates)
        return list(reversed(candidates))[:k]


def _make_candidate(doc_id: str, chunk_id: str, score: float = 1.0) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=f"Content for {chunk_id}",
        fused_score=score,
        metadata={"title": f"Doc {doc_id}"},
    )


def _get_histogram_count(histogram: Any, **labels: str) -> float:
    """Retrieve cumulative sample count from a Prometheus histogram."""
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count") and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return float(sample.value)
    return 0.0


def _get_histogram_sum(histogram: Any, **labels: str) -> float:
    """Retrieve cumulative sample sum from a Prometheus histogram."""
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name.endswith("_sum") and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return float(sample.value)
    return 0.0


# ==============================================================================
# 1. Structural and Bucket Verification (R11.6, R21.4, R21.5, NFR5, NFR6)
# ==============================================================================


def test_retrieval_and_rerank_latency_registered_as_distinct_histograms() -> None:
    """Verify latency histograms are registered separately with SLO buckets."""
    metrics = create_pipeline_metrics()

    # Must be separate Histogram instances (R11.6)
    assert metrics.retrieval_latency_ms is not None
    assert metrics.rerank_latency_ms is not None
    assert metrics.retrieval_latency_ms is not metrics.rerank_latency_ms

    # NFR5: Hybrid retrieval target is 50–250 ms
    assert 50.0 in RETRIEVAL_BUCKETS
    assert 100.0 in RETRIEVAL_BUCKETS
    assert 250.0 in RETRIEVAL_BUCKETS
    assert 500.0 in RETRIEVAL_BUCKETS

    # NFR6: Reranking target is 20–200 ms
    assert 25.0 in RERANK_BUCKETS
    assert 50.0 in RERANK_BUCKETS
    assert 100.0 in RERANK_BUCKETS
    assert 250.0 in RERANK_BUCKETS

    # Verify Prometheus exposition separates them
    metrics.retrieval_latency_ms.labels(mode="hybrid").observe(75.0)
    metrics.rerank_latency_ms.observe(35.0)

    payload, content_type = generate_metrics_payload(metrics.registry)
    payload_str = payload.decode("utf-8")

    assert "retrieval_latency_ms_bucket" in payload_str
    assert 'retrieval_latency_ms_bucket{le="100.0",mode="hybrid"} 1.0' in payload_str
    assert "rerank_latency_ms_bucket" in payload_str
    assert 'rerank_latency_ms_bucket{le="50.0"} 1.0' in payload_str


def test_record_latency_helper_functions() -> None:
    """Verify record_retrieval_latency and record_rerank_latency safe helpers."""
    metrics = create_pipeline_metrics()

    # Record retrieval latencies
    record_retrieval_latency(metrics, 120.0, mode="hybrid")
    record_retrieval_latency(metrics, 80.0, mode="degraded")
    record_retrieval_latency(metrics, 2500.0, mode="failed")

    # Record rerank latencies
    record_rerank_latency(metrics, 42.0)
    record_rerank_latency(metrics, 180.0)

    # Verify metric counts
    assert _get_histogram_sum(metrics.retrieval_latency_ms, mode="hybrid") == 120.0
    assert _get_histogram_sum(metrics.retrieval_latency_ms, mode="degraded") == 80.0
    assert _get_histogram_sum(metrics.retrieval_latency_ms, mode="failed") == 2500.0
    assert _get_histogram_sum(metrics.rerank_latency_ms) == 222.0

    # Ensure None metrics uses global get_metrics without raising
    record_retrieval_latency(None, 50.0, mode="hybrid")
    record_rerank_latency(None, 30.0)


# ==============================================================================
# 2. HybridRetriever Latency Metric Verification (R11.6, R21.4, NFR5)
# ==============================================================================


@pytest.mark.asyncio
async def test_hybrid_retriever_observes_retrieval_latency_ms_hybrid_mode() -> None:
    """Verify HybridRetriever observes retrieval_latency_ms{mode='hybrid'} and not rerank."""
    metrics = create_pipeline_metrics()
    c1 = _make_candidate("d1", "c1", 0.9)
    c2 = _make_candidate("d2", "c2", 0.8)

    backend = MockSearchBackend(lexical_candidates=[c1], vector_candidates=[c2])
    retriever = HybridRetriever(backend=backend, metrics=metrics)

    query = RetrievalQuery(
        semantic_text="billing issue",
        filters={"organization_id": "org_test"},
        query_vector=[0.1] * 1536,
    )

    result = await retriever.retrieve(query)

    assert not result.retrieval_degraded
    assert len(result.candidates) > 0

    # Check retrieval_latency_ms was observed
    sample_count = _get_histogram_count(metrics.retrieval_latency_ms, mode="hybrid")
    assert sample_count == 1.0
    assert _get_histogram_sum(metrics.retrieval_latency_ms, mode="hybrid") > 0.0

    # Verify rerank_latency_ms was NOT observed (strict separation R11.6)
    assert _get_histogram_count(metrics.rerank_latency_ms) == 0.0


@pytest.mark.asyncio
async def test_hybrid_retriever_observes_retrieval_latency_ms_degraded_mode() -> None:
    """Verify HybridRetriever observes retrieval_latency_ms{mode='degraded'} on branch failure."""
    metrics = create_pipeline_metrics()
    c1 = _make_candidate("d1", "c1", 0.9)

    # Lexical fails, vector succeeds -> degraded
    backend = MockSearchBackend(
        lexical_exc=RuntimeError("Lexical timeout"),
        vector_candidates=[c1],
    )
    retriever = HybridRetriever(backend=backend, metrics=metrics)

    query = RetrievalQuery(
        semantic_text="billing issue",
        filters={"organization_id": "org_test"},
        query_vector=[0.1] * 1536,
    )

    result = await retriever.retrieve(query)

    assert result.retrieval_degraded
    assert result.surviving_branch == "vector"

    # retrieval_latency_ms observed with mode="degraded"
    assert _get_histogram_count(metrics.retrieval_latency_ms, mode="degraded") == 1.0
    assert _get_histogram_count(metrics.retrieval_latency_ms, mode="hybrid") == 0.0


@pytest.mark.asyncio
async def test_hybrid_retriever_observes_retrieval_latency_ms_failed_mode() -> None:
    """Verify HybridRetriever observes retrieval_latency_ms{mode='failed'} when both fail."""
    metrics = create_pipeline_metrics()

    # Both fail with raise_on_both_failed=False
    backend_no_raise = MockSearchBackend(
        lexical_exc=RuntimeError("Lexical error"),
        vector_exc=RuntimeError("Vector error"),
    )
    retriever_no_raise = HybridRetriever(
        backend=backend_no_raise,
        metrics=metrics,
        raise_on_both_failed=False,
    )

    query = RetrievalQuery(
        semantic_text="billing issue",
        filters={"organization_id": "org_test"},
        query_vector=[0.1] * 1536,
    )

    result = await retriever_no_raise.retrieve(query)
    assert result.retrieval_degraded
    assert result.candidates == []
    assert _get_histogram_count(metrics.retrieval_latency_ms, mode="failed") == 1.0

    # Both fail with raise_on_both_failed=True
    backend_raise = MockSearchBackend(
        lexical_exc=RuntimeError("Lexical error"),
        vector_exc=RuntimeError("Vector error"),
    )
    retriever_raise = HybridRetriever(
        backend=backend_raise,
        metrics=metrics,
        raise_on_both_failed=True,
    )

    with pytest.raises(RetrievalError):
        await retriever_raise.retrieve(query)

    # Failed count incremented again
    assert _get_histogram_count(metrics.retrieval_latency_ms, mode="failed") == 2.0


# ==============================================================================
# 3. RerankService Latency Metric Verification (R11.6, R21.4, NFR6)
# ==============================================================================


@pytest.mark.asyncio
async def test_rerank_service_observes_rerank_latency_ms_success() -> None:
    """Verify RerankService observes rerank_latency_ms on successful reranking."""
    metrics = create_pipeline_metrics()
    reranker = MockReranker(delay=0.01)
    service = RerankService(reranker=reranker, metrics=metrics)

    candidates = [
        _make_candidate("d1", "c1", 0.5),
        _make_candidate("d2", "c2", 0.8),
    ]

    result = await service.rerank(
        query="test query",
        candidates=candidates,
        organization_id="org_test",
        category="billing",
    )

    assert result.rerank_applied
    assert not result.fallback_recorded
    assert result.latency_ms > 0.0

    # Verify rerank_latency_ms histogram updated
    assert _get_histogram_count(metrics.rerank_latency_ms) == 1.0
    assert _get_histogram_sum(metrics.rerank_latency_ms) > 0.0

    # Verify retrieval_latency_ms was NOT modified
    assert _get_histogram_count(metrics.retrieval_latency_ms, mode="hybrid") == 0.0


@pytest.mark.asyncio
async def test_rerank_service_observes_rerank_latency_ms_timeout() -> None:
    """Verify RerankService observes rerank_latency_ms on timeout fallback (R11.5, NFR6)."""
    metrics = create_pipeline_metrics()
    reranker = MockReranker(delay=0.1)  # 100ms
    service = RerankService(
        reranker=reranker,
        metrics=metrics,
        timeout_seconds=0.02,
    )

    candidates = [
        _make_candidate("d1", "c1", 0.5),
        _make_candidate("d2", "c2", 0.8),
    ]

    result = await service.rerank(
        query="test query",
        candidates=candidates,
        organization_id="org_timeout",
    )

    assert not result.rerank_applied
    assert result.fallback_recorded
    assert "Timeout after" in (result.fallback_reason or "")
    assert result.latency_ms >= 15.0  # At least near the 20ms timeout

    # Rerank latency MUST still be recorded in histogram on timeout (R11.6, R21.4)
    assert _get_histogram_count(metrics.rerank_latency_ms) == 1.0
    assert _get_histogram_sum(metrics.rerank_latency_ms) >= 15.0

    # Fallback counter incremented
    assert (
        metrics.rerank_fallback_total.labels(tenant="org_timeout", reason="timeout")._value.get()
        == 1.0
    )


@pytest.mark.asyncio
async def test_rerank_service_observes_rerank_latency_ms_exception() -> None:
    """Verify RerankService observes rerank_latency_ms when an error triggers fallback (R11.5)."""
    metrics = create_pipeline_metrics()
    reranker = MockReranker(exc=RerankerUnavailableError("Model server down"))
    service = RerankService(reranker=reranker, metrics=metrics)

    candidates = [
        _make_candidate("d1", "c1", 0.5),
        _make_candidate("d2", "c2", 0.8),
    ]

    result = await service.rerank(
        query="test query",
        candidates=candidates,
        organization_id="org_err",
    )

    assert not result.rerank_applied
    assert result.fallback_recorded
    assert "Model server down" in (result.fallback_reason or "")

    # Rerank latency recorded
    assert _get_histogram_count(metrics.rerank_latency_ms) == 1.0
    assert (
        metrics.rerank_fallback_total.labels(tenant="org_err", reason="unavailable")._value.get()
        == 1.0
    )


# ==============================================================================
# 4. End-to-End Pipeline Multi-Sample Quantile Computability (R21.5)
# ==============================================================================


def test_retrieval_and_rerank_quantiles_computable() -> None:
    """Verify multiple observations produce monotonic buckets enabling p50/p95/p99 queries."""
    metrics = create_pipeline_metrics()

    # Simulate 10 retrieval observations spanning typical to tail latencies
    retrieval_latencies = [45.0, 52.0, 60.0, 75.0, 95.0, 110.0, 140.0, 180.0, 220.0, 480.0]
    for lat in retrieval_latencies:
        record_retrieval_latency(metrics, lat, mode="hybrid")

    # Simulate 10 rerank observations
    rerank_latencies = [22.0, 28.0, 35.0, 40.0, 48.0, 55.0, 65.0, 80.0, 120.0, 195.0]
    for lat in rerank_latencies:
        record_rerank_latency(metrics, lat)

    payload, _ = generate_metrics_payload(metrics.registry)
    payload_str = payload.decode("utf-8")

    # Verify counts and sums
    assert 'retrieval_latency_ms_count{mode="hybrid"} 10.0' in payload_str
    expected_sum = f"{sum(retrieval_latencies):.1f}"
    assert f'retrieval_latency_ms_sum{{mode="hybrid"}} {expected_sum}' in payload_str
    assert "rerank_latency_ms_count 10.0" in payload_str
    assert f"rerank_latency_ms_sum {sum(rerank_latencies):.1f}" in payload_str

    # Verify cumulative bucket monotonicity for Prometheus histogram_quantile()
    prev_bucket_val = 0.0
    for b in RETRIEVAL_BUCKETS:
        expected_cumulative = sum(1 for x in retrieval_latencies if x <= b)
        bucket_line = (
            f'retrieval_latency_ms_bucket{{le="{b}",mode="hybrid"}} {float(expected_cumulative)}'
        )
        assert bucket_line in payload_str
        assert expected_cumulative >= prev_bucket_val
        prev_bucket_val = expected_cumulative

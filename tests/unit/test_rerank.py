"""Unit tests for cross-encoder reranker, policy gating, and RRF fallback.

Requirements:
- R11.1: Support optional cross-encoder / semantic reranker over fused candidate set.
- R11.2: Allow reranking to be disabled by configuration, per organization and per category.
- R11.3: Pass configurable top-K to generation (defaulting to 4–6 chunks).
- R11.5: IF reranker is unavailable, THEN fall back to RRF order, record fallback, and continue.
- R11.6: Record rerank_latency_ms separately from retrieval_latency_ms.
- R21.4: Observability metrics (rerank_fallback_total, rerank_latency_ms).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload
from packages.retrieval.models import Candidate
from packages.retrieval.rerank import (
    CrossEncoderReranker,
    RerankerUnavailableError,
    RerankPolicy,
    RerankService,
    StubReranker,
)


def _make_candidate(
    chunk_id: str,
    doc_id: str = "doc-1",
    content: str = "chunk text",
    metadata: dict[str, Any] | None = None,
    fused_score: float | None = 0.03,
    rerank_score: float | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        metadata=metadata or {},
        fused_score=fused_score,
        rerank_score=rerank_score,
    )


class TestRerankPolicy:
    """Test policy-based enabling and disabling of reranking (R11.2)."""

    def test_globally_disabled(self) -> None:
        policy = RerankPolicy(enabled=False)
        assert not policy.is_enabled(organization_id="org-1", category="billing")
        assert not policy.is_enabled()

    def test_disabled_by_organization(self) -> None:
        policy = RerankPolicy(
            enabled=True,
            disabled_organizations={"org-disabled"},
        )
        assert not policy.is_enabled(organization_id="org-disabled", category="billing")
        assert policy.is_enabled(organization_id="org-enabled", category="billing")

    def test_disabled_by_category(self) -> None:
        policy = RerankPolicy(
            enabled=True,
            disabled_categories={"spam", "automated_notification"},
        )
        assert not policy.is_enabled(organization_id="org-1", category="spam")
        assert not policy.is_enabled(organization_id="org-1", category="automated_notification")
        assert policy.is_enabled(organization_id="org-1", category="billing")

    def test_allowlists(self) -> None:
        policy = RerankPolicy(
            enabled=True,
            enabled_organizations={"org-vip"},
            enabled_categories={"technical_support"},
        )
        assert policy.is_enabled(organization_id="org-vip", category="technical_support")
        assert not policy.is_enabled(organization_id="org-other", category="technical_support")
        assert not policy.is_enabled(organization_id="org-vip", category="billing")


class TestStubReranker:
    """Test deterministic in-memory reranker double (R24.5)."""

    async def test_scoring_and_reordering(self) -> None:
        c1 = _make_candidate("c1", content="alpha", fused_score=0.03)
        c2 = _make_candidate("c2", content="beta", fused_score=0.02)
        c3 = _make_candidate("c3", content="gamma", fused_score=0.01)

        # c2 given highest semantic relevance score
        reranker = StubReranker(score_map={"c1": 0.50, "c2": 0.95, "c3": 0.10})
        reranked = await reranker.rerank("query", [c1, c2, c3])

        assert [c.chunk_id for c in reranked] == ["c2", "c1", "c3"]
        assert reranked[0].rerank_score == 0.95
        assert reranked[1].rerank_score == 0.50
        assert reranked[2].rerank_score == 0.10

    async def test_tie_breaking(self) -> None:
        """Verify tie breaking prioritizes higher fused_score, then chunk_id."""
        c1 = _make_candidate("c1", fused_score=0.04)
        c2 = _make_candidate("c2", fused_score=0.02)

        # Both have same rerank score 0.90
        reranker = StubReranker(score_map={"c1": 0.90, "c2": 0.90})
        reranked = await reranker.rerank("query", [c2, c1])

        # c1 has higher fused_score (0.04 > 0.02), so c1 comes first
        assert [c.chunk_id for c in reranked] == ["c1", "c2"]

    async def test_top_k_truncation(self) -> None:
        cands = [_make_candidate(f"c{i}") for i in range(10)]
        reranker = StubReranker()
        reranked = await reranker.rerank("query", cands, top_k=4)
        assert len(reranked) == 4

    async def test_empty_candidates(self) -> None:
        reranker = StubReranker()
        assert await reranker.rerank("query", []) == []


class TestRerankService:
    """Test RerankService policy enforcement, fallback to RRF, and metrics (R11.1–R11.6)."""

    async def test_rerank_disabled_by_policy_preserves_rrf_order(self) -> None:
        """Verify disabling rerank by policy preserves RRF order (R11.2)."""
        c1 = _make_candidate("c1", fused_score=0.03)
        c2 = _make_candidate("c2", fused_score=0.02)

        # Reranker would promote c2, but policy disables it
        reranker = StubReranker(score_map={"c1": 0.1, "c2": 0.9})
        policy = RerankPolicy(disabled_organizations={"org-1"})
        service = RerankService(reranker, policy=policy)

        result = await service.rerank(
            "query",
            [c1, c2],
            organization_id="org-1",
            category="billing",
        )

        assert not result.rerank_applied
        assert not result.fallback_recorded
        assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]
        assert result.candidates[0].rerank_score is None

    async def test_rerank_success_and_metric_emission(self) -> None:
        """Verify successful rerank sets scores and emits latency metric (R11.1, R11.6)."""
        metrics = create_pipeline_metrics()
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")

        reranker = StubReranker(score_map={"c1": 0.2, "c2": 0.8})
        service = RerankService(reranker, metrics=metrics)

        result = await service.rerank("query", [c1, c2], organization_id="org-vip")

        assert result.rerank_applied
        assert not result.fallback_recorded
        assert [c.chunk_id for c in result.candidates] == ["c2", "c1"]
        assert result.latency_ms > 0.0

        # Verify latency recorded in Prometheus metrics
        payload, _ = generate_metrics_payload(metrics.registry)
        assert "rerank_latency_ms_bucket" in payload.decode("utf-8")

    async def test_fallback_on_unavailability(self) -> None:
        """Verify RRF order fallback when reranker is unavailable (R11.5)."""
        metrics = create_pipeline_metrics()
        c1 = _make_candidate("c1", fused_score=0.03)
        c2 = _make_candidate("c2", fused_score=0.02)

        reranker = StubReranker(is_available=False)
        service = RerankService(reranker, metrics=metrics)

        result = await service.rerank("query", [c1, c2], organization_id="org-1")

        assert not result.rerank_applied
        assert result.fallback_recorded
        assert result.fallback_reason is not None
        assert "unavailable" in result.fallback_reason
        # Original RRF order preserved
        assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]

        # Verify fallback metric incremented
        payload, _ = generate_metrics_payload(metrics.registry)
        payload_str = payload.decode("utf-8")
        assert 'rerank_fallback_total{reason="unavailable",tenant="org-1"}' in payload_str

    async def test_fallback_on_timeout(self) -> None:
        """Verify RRF order fallback when reranking times out (R11.5)."""
        metrics = create_pipeline_metrics()
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")

        # Reranker delays 0.3s, exceeding 0.05s timeout
        reranker = StubReranker(delay_seconds=0.3)
        service = RerankService(reranker, timeout_seconds=0.05, metrics=metrics)

        result = await service.rerank("query", [c1, c2], organization_id="org-slow")

        assert not result.rerank_applied
        assert result.fallback_recorded
        assert "Timeout" in (result.fallback_reason or "")
        assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]

        payload, _ = generate_metrics_payload(metrics.registry)
        payload_str = payload.decode("utf-8")
        assert 'rerank_fallback_total{reason="timeout",tenant="org-slow"}' in payload_str

    async def test_fallback_on_runtime_exception(self) -> None:
        """Verify RRF order fallback on unexpected reranker error (R11.5)."""
        metrics = create_pipeline_metrics()
        c1 = _make_candidate("c1")

        reranker = StubReranker(should_raise=RuntimeError("CUDA out of memory"))
        service = RerankService(reranker, metrics=metrics)

        result = await service.rerank("query", [c1], organization_id="org-err")

        assert not result.rerank_applied
        assert result.fallback_recorded
        assert "CUDA out of memory" in (result.fallback_reason or "")
        assert [c.chunk_id for c in result.candidates] == ["c1"]

        payload, _ = generate_metrics_payload(metrics.registry)
        payload_str = payload.decode("utf-8")
        assert 'rerank_fallback_total{reason="error",tenant="org-err"}' in payload_str

    def test_constructor_validation(self) -> None:
        reranker = StubReranker()
        with pytest.raises(ValueError, match="timeout_seconds must be > 0"):
            RerankService(reranker, timeout_seconds=-1.0)
        with pytest.raises(ValueError, match="default_top_k must be > 0"):
            RerankService(reranker, default_top_k=0)


class TestCrossEncoderReranker:
    """Test CrossEncoderReranker wrapper behavior."""

    async def test_missing_dependency_raises_unavailable(self) -> None:
        """Verify missing sentence_transformers dependency raises RerankerUnavailableError."""
        reranker = CrossEncoderReranker()
        with (
            patch.dict("sys.modules", {"sentence_transformers": None}),
            pytest.raises(RerankerUnavailableError, match="Failed to load CrossEncoder"),
        ):
            await reranker.rerank("query", [_make_candidate("c1")])

    async def test_mocked_model_scoring(self) -> None:
        """Verify model prediction and candidate update with mocked CrossEncoder."""
        c1 = _make_candidate("c1", content="payment method")
        c2 = _make_candidate("c2", content="invoice date")

        reranker = CrossEncoderReranker()
        mock_model = MagicMock()
        mock_model.predict.return_value = [0.35, 0.92]
        reranker._model = mock_model

        reranked = await reranker.rerank("invoice query", [c1, c2])

        assert len(reranked) == 2
        assert reranked[0].chunk_id == "c2"
        assert reranked[0].rerank_score == 0.92
        assert reranked[1].chunk_id == "c1"
        assert reranked[1].rerank_score == 0.35

        mock_model.predict.assert_called_once_with([
            ("invoice query", "payment method"),
            ("invoice query", "invoice date"),
        ])

    async def test_empty_candidates_returns_empty(self) -> None:
        reranker = CrossEncoderReranker()
        assert await reranker.rerank("query", []) == []

    def test_cached_unavailable_status_raises_immediately(self) -> None:
        reranker = CrossEncoderReranker()
        reranker._available = False
        with pytest.raises(RerankerUnavailableError, match="is marked unavailable"):
            reranker._load_model()

    def test_successful_dynamic_model_loading(self) -> None:
        reranker = CrossEncoderReranker(model_name="test-model", device="cpu")
        mock_ce_class = MagicMock()
        mock_instance = MagicMock()
        mock_ce_class.return_value = mock_instance

        mock_st = MagicMock()
        mock_st.CrossEncoder = mock_ce_class

        with patch.dict("sys.modules", {"sentence_transformers": mock_st}):
            loaded = reranker._load_model()
            assert loaded is mock_instance
            assert reranker._available is True
            mock_ce_class.assert_called_once_with("test-model", device="cpu")


class TestRerankServiceEdgeCases:
    """Test boundary and edge cases for RerankService."""

    async def test_service_rerank_empty_candidates(self) -> None:
        service = RerankService(reranker=StubReranker())
        result = await service.rerank("query", [])
        assert result.candidates == []
        assert result.rerank_applied is False
        assert result.fallback_recorded is False

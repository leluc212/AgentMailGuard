"""Unit tests for concurrent branch execution and degradation in HybridRetriever.

Requirements:
- R10.5: Concurrent execution of lexical and vector search branches.
- R10.6: IF one branch fails or times out, THEN THE SYSTEM SHALL degrade to the
  surviving branch, record retrieval_degraded=true, and continue.
- R10.9: Enforce configurable retrieval timeout and do not block worker indefinitely.
- R10.8: Return candidate with lexical rank, vector rank, fused score, source metadata.
- R21.4: Observability metrics (retrieval_degraded_total, retrieval_latency_ms).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from packages.knowledge.embedder import EmbeddingError, FakeEmbedder
from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.retriever import HybridRetriever, RetrievalError


def _make_candidate(
    chunk_id: str,
    doc_id: str = "doc-1",
    content: str = "chunk text",
    metadata: dict[str, Any] | None = None,
    lexical_rank: int | None = None,
    vector_rank: int | None = None,
    lexical_score: float | None = None,
    vector_score: float | None = None,
) -> Candidate:
    """Helper to construct candidate objects for testing."""
    return Candidate(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        metadata=metadata or {},
        lexical_rank=lexical_rank,
        vector_rank=vector_rank,
        lexical_score=lexical_score,
        vector_score=vector_score,
    )


class SlowMockBackend:
    """Mock SearchBackend with controlled delay and error injection."""

    def __init__(
        self,
        lexical_delay: float = 0.0,
        vector_delay: float = 0.0,
        lexical_candidates: list[Candidate] | None = None,
        vector_candidates: list[Candidate] | None = None,
        lexical_exc: Exception | None = None,
        vector_exc: Exception | None = None,
    ) -> None:
        self.lexical_delay = lexical_delay
        self.vector_delay = vector_delay
        self.lexical_candidates = lexical_candidates or []
        self.vector_candidates = vector_candidates or []
        self.lexical_exc = lexical_exc
        self.vector_exc = vector_exc
        self.lexical_calls = 0
        self.vector_calls = 0

    async def lexical(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        self.lexical_calls += 1
        if self.lexical_delay > 0:
            await asyncio.sleep(self.lexical_delay)
        if self.lexical_exc:
            raise self.lexical_exc
        return self.lexical_candidates[:top_n]

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        self.vector_calls += 1
        if self.vector_delay > 0:
            await asyncio.sleep(self.vector_delay)
        if self.vector_exc:
            raise self.vector_exc
        return self.vector_candidates[:top_n]


@pytest.fixture
def query() -> RetrievalQuery:
    return RetrievalQuery(
        semantic_text="how to reset enterprise password",
        lexical_terms=["reset", "password"],
        identifiers=["KB-102"],
        filters={"organization_id": "00000000-0000-0000-0000-000000000001"},
        query_vector=[0.1] * 1536,
    )


class TestHybridRetrieverConcurrency:
    """Test concurrent branch execution (R10.5)."""

    async def test_concurrent_execution_timing(self, query: RetrievalQuery) -> None:
        """Verify lexical and vector run in parallel rather than serially (R10.5)."""
        backend = SlowMockBackend(
            lexical_delay=0.1,
            vector_delay=0.1,
            lexical_candidates=[_make_candidate("c_lex")],
            vector_candidates=[_make_candidate("c_vec")],
        )
        retriever = HybridRetriever(backend, timeout_seconds=1.0)

        t0 = time.perf_counter()
        result = await retriever.retrieve(query)
        elapsed = time.perf_counter() - t0

        # If serial, elapsed >= 0.2s. Concurrently, elapsed ~ 0.1s (< 0.18s).
        assert elapsed < 0.18, f"Execution took {elapsed:.3f}s, expected parallel (<0.18s)"
        assert not result.retrieval_degraded
        assert len(result.candidates) == 2
        assert backend.lexical_calls == 1
        assert backend.vector_calls == 1

    async def test_normal_hybrid_fusion(self, query: RetrievalQuery) -> None:
        """Verify normal two-branch execution fuses via RRF with both ranks (R10.3, R10.8)."""
        c1 = _make_candidate("c1", lexical_score=5.0)
        c2 = _make_candidate("c2", lexical_score=3.0)
        c3 = _make_candidate("c1", vector_score=0.9)  # overlaps with c1
        c4 = _make_candidate("c4", vector_score=0.8)

        backend = SlowMockBackend(
            lexical_candidates=[c1, c2],
            vector_candidates=[c3, c4],
        )
        retriever = HybridRetriever(backend, default_top_n=10)

        result = await retriever.retrieve(query)

        assert not result.retrieval_degraded
        assert result.surviving_branch is None
        assert result.lexical_error is None
        assert result.vector_error is None
        assert len(result.candidates) == 3
        # c1 appears in both, must rank #1
        assert result.candidates[0].chunk_id == "c1"
        assert result.candidates[0].lexical_rank == 1
        assert result.candidates[0].vector_rank == 1
        assert result.candidates[0].fused_score == pytest.approx(2.0 / 61, rel=1e-9)


class TestHybridRetrieverDegradation:
    """Test timeout and error degradation to surviving branch (R10.6, R10.9)."""

    async def test_lexical_timeout_degrades_to_vector(self, query: RetrievalQuery) -> None:
        """Verify timeout in lexical branch degrades to vector branch (R10.6, R10.9)."""
        c_vec = _make_candidate("v1", vector_score=0.95)
        backend = SlowMockBackend(
            lexical_delay=0.3,  # exceeds 0.05s timeout
            vector_delay=0.01,
            lexical_candidates=[_make_candidate("l1")],
            vector_candidates=[c_vec],
        )
        retriever = HybridRetriever(
            backend,
            timeout_seconds=0.05,
        )

        result = await retriever.retrieve(query)

        assert result.retrieval_degraded
        assert result.surviving_branch == "vector"
        assert result.lexical_error is not None
        assert "Timeout" in result.lexical_error
        assert result.vector_error is None
        assert len(result.candidates) == 1
        assert result.candidates[0].chunk_id == "v1"
        assert result.candidates[0].vector_rank == 1
        assert result.candidates[0].lexical_rank is None
        assert pytest.approx(1.0 / 61, rel=1e-9) == result.candidates[0].fused_score

    async def test_vector_timeout_degrades_to_lexical(self, query: RetrievalQuery) -> None:
        """Verify timeout in vector branch degrades to lexical branch (R10.6, R10.9)."""
        c_lex = _make_candidate("l1", lexical_score=4.2)
        backend = SlowMockBackend(
            lexical_delay=0.01,
            vector_delay=0.3,  # exceeds 0.05s timeout
            lexical_candidates=[c_lex],
            vector_candidates=[_make_candidate("v1")],
        )
        retriever = HybridRetriever(
            backend,
            timeout_seconds=0.05,
        )

        result = await retriever.retrieve(query)

        assert result.retrieval_degraded
        assert result.surviving_branch == "lexical"
        assert result.vector_error is not None
        assert "Timeout" in result.vector_error
        assert result.lexical_error is None
        assert len(result.candidates) == 1
        assert result.candidates[0].chunk_id == "l1"
        assert result.candidates[0].lexical_rank == 1
        assert result.candidates[0].vector_rank is None
        assert pytest.approx(1.0 / 61, rel=1e-9) == result.candidates[0].fused_score

    async def test_lexical_exception_degrades_to_vector(self, query: RetrievalQuery) -> None:
        """Verify exception in lexical branch degrades to vector branch without crashing (R10.6)."""
        backend = SlowMockBackend(
            lexical_exc=RuntimeError("FTS query failed"),
            vector_candidates=[_make_candidate("v1")],
        )
        retriever = HybridRetriever(backend)

        result = await retriever.retrieve(query)

        assert result.retrieval_degraded
        assert result.surviving_branch == "vector"
        assert result.lexical_error == "FTS query failed"
        assert len(result.candidates) == 1
        assert result.candidates[0].chunk_id == "v1"

    async def test_vector_exception_degrades_to_lexical(self, query: RetrievalQuery) -> None:
        """Verify exception in vector branch degrades to lexical branch without crashing (R10.6)."""
        backend = SlowMockBackend(
            vector_exc=ConnectionError("pgvector connection lost"),
            lexical_candidates=[_make_candidate("l1")],
        )
        retriever = HybridRetriever(backend)

        result = await retriever.retrieve(query)

        assert result.retrieval_degraded
        assert result.surviving_branch == "lexical"
        assert result.vector_error == "pgvector connection lost"
        assert len(result.candidates) == 1
        assert result.candidates[0].chunk_id == "l1"

    async def test_both_branches_fail_handled_gracefully(self, query: RetrievalQuery) -> None:
        """Verify dual branch failure returns empty result with degraded=True."""
        backend = SlowMockBackend(
            lexical_exc=RuntimeError("FTS down"),
            vector_exc=RuntimeError("ANN down"),
        )
        retriever = HybridRetriever(backend, raise_on_both_failed=False)

        result = await retriever.retrieve(query)

        assert result.retrieval_degraded
        assert result.surviving_branch is None
        assert len(result.candidates) == 0
        assert result.lexical_error == "FTS down"
        assert result.vector_error == "ANN down"

    async def test_both_branches_fail_raises_when_configured(self, query: RetrievalQuery) -> None:
        """Verify dual branch failure raises RetrievalError when raise_on_both_failed=True."""
        backend = SlowMockBackend(
            lexical_exc=RuntimeError("FTS down"),
            vector_exc=RuntimeError("ANN down"),
        )
        retriever = HybridRetriever(backend, raise_on_both_failed=True)

        with pytest.raises(RetrievalError, match="Both retrieval branches failed"):
            await retriever.retrieve(query)


class TestHybridRetrieverObservabilityAndParams:
    """Test metrics emission (R21.4) and parameter overrides."""

    async def test_metrics_emitted_on_degraded_and_latency(self, query: RetrievalQuery) -> None:
        """Verify Prometheus metrics for degradation and latency are recorded."""
        metrics = create_pipeline_metrics()

        # Test lexical degradation metric
        backend_lex_fail = SlowMockBackend(
            lexical_exc=RuntimeError("err"),
            vector_candidates=[_make_candidate("v1")],
        )
        retriever_lex = HybridRetriever(backend_lex_fail, metrics=metrics)
        await retriever_lex.retrieve(query)

        org_id = query.organization_id or "unknown"
        lex_degraded_val = metrics.retrieval_degraded_total.labels(
            tenant=org_id, failed_branch="lexical"
        )._value.get()
        assert lex_degraded_val == 1.0

        # Test vector degradation metric
        backend_vec_fail = SlowMockBackend(
            vector_exc=RuntimeError("err"),
            lexical_candidates=[_make_candidate("l1")],
        )
        retriever_vec = HybridRetriever(backend_vec_fail, metrics=metrics)
        await retriever_vec.retrieve(query)

        vec_degraded_val = metrics.retrieval_degraded_total.labels(
            tenant=org_id, failed_branch="vector"
        )._value.get()
        assert vec_degraded_val == 1.0

        # Test dual failure degradation metric
        backend_both_fail = SlowMockBackend(
            lexical_exc=RuntimeError("err"),
            vector_exc=RuntimeError("err"),
        )
        retriever_both = HybridRetriever(backend_both_fail, metrics=metrics)
        await retriever_both.retrieve(query)

        both_degraded_val = metrics.retrieval_degraded_total.labels(
            tenant=org_id, failed_branch="both"
        )._value.get()
        assert both_degraded_val == 1.0

        # Verify latency histogram observed
        payload, _ = generate_metrics_payload(metrics.registry)
        payload_str = payload.decode("utf-8")
        assert "retrieval_latency_ms_bucket" in payload_str
        assert 'retrieval_degraded_total{failed_branch="lexical",tenant="' in payload_str
        assert 'retrieval_degraded_total{failed_branch="vector",tenant="' in payload_str
        assert 'retrieval_degraded_total{failed_branch="both",tenant="' in payload_str

    async def test_per_query_top_n_and_limit_overrides(self, query: RetrievalQuery) -> None:
        """Verify top_n and limit overrides work on per-query basis."""
        lex_cands = [_make_candidate(f"l_{i}") for i in range(10)]
        vec_cands = [_make_candidate(f"v_{i}") for i in range(10)]
        backend = SlowMockBackend(
            lexical_candidates=lex_cands,
            vector_candidates=vec_cands,
        )
        retriever = HybridRetriever(backend, default_top_n=20)

        result = await retriever.retrieve(query, top_n=5, limit=3)
        assert len(result.lexical_candidates) == 5
        assert len(result.vector_candidates) == 5
        assert len(result.candidates) == 3

    def test_constructor_validation(self) -> None:
        """Verify illegal constructor arguments raise ValueError."""
        backend = FakeSearchBackend()

        with pytest.raises(ValueError, match="timeout_seconds must be > 0"):
            HybridRetriever(backend, timeout_seconds=0.0)

        with pytest.raises(ValueError, match="default_top_n must be > 0"):
            HybridRetriever(backend, default_top_n=-1)


class _RecordingVectorBackend(SlowMockBackend):
    """Records the query the vector branch receives."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.vector_queries: list[RetrievalQuery] = []

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        self.vector_queries.append(q)
        return await super().vector(q, top_n)


class _FixedVectorEmbedder(FakeEmbedder):
    """Returns a fixed vector, to model a misbehaving provider."""

    def __init__(self, vector: list[float], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vector = vector

    async def embed_query(self, query: str) -> list[float]:
        self.recorded_calls.append({"texts": [query]})
        return list(self._vector)


def _unembedded_query(text: str = "how do I reset my enterprise password") -> RetrievalQuery:
    return RetrievalQuery(
        semantic_text=text,
        lexical_terms=["reset", "password"],
        filters={"organization_id": "00000000-0000-0000-0000-000000000001"},
    )


class TestQueryEmbedding:
    """3.16: the vector branch embeds the query (R10.1) under the timeout (R10.9)."""

    async def test_vector_branch_embeds_the_semantic_text(self) -> None:
        backend = _RecordingVectorBackend(vector_candidates=[_make_candidate("c_vec")])
        embedder = FakeEmbedder()
        query = _unembedded_query()

        result = await HybridRetriever(backend, embedder=embedder).retrieve(query)

        assert embedder.recorded_calls[0]["texts"] == [query.semantic_text]
        searched = backend.vector_queries[0].query_vector
        assert searched == await embedder.embed_query(query.semantic_text)
        assert result.query_vector_dimension == embedder.dimension
        assert not result.retrieval_degraded
        assert [c.chunk_id for c in result.vector_candidates] == ["c_vec"]

    async def test_embedding_does_not_mutate_the_callers_query(self) -> None:
        query = _unembedded_query()
        await HybridRetriever(_RecordingVectorBackend(), embedder=FakeEmbedder()).retrieve(query)
        assert query.query_vector is None

    async def test_supplied_vector_is_not_re_embedded(self, query: RetrievalQuery) -> None:
        backend = _RecordingVectorBackend()
        embedder = FakeEmbedder()

        result = await HybridRetriever(backend, embedder=embedder).retrieve(query)

        assert embedder.recorded_calls == []
        assert backend.vector_queries[0].query_vector == query.query_vector
        assert result.query_vector_dimension == len(query.query_vector or [])

    async def test_slow_embedder_degrades_to_lexical_within_the_timeout(self) -> None:
        metrics = create_pipeline_metrics()
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        retriever = HybridRetriever(
            backend,
            embedder=FakeEmbedder(simulate_latency_ms=2000),
            timeout_seconds=0.05,
            metrics=metrics,
        )

        t0 = time.perf_counter()
        result = await retriever.retrieve(_unembedded_query())
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, f"retrieval blocked for {elapsed:.2f}s behind the embedder"
        assert result.retrieval_degraded and result.surviving_branch == "lexical"
        assert [c.chunk_id for c in result.candidates] == ["c_lex"]
        assert result.vector_error is not None and "Timeout" in result.vector_error
        assert result.query_vector_dimension is None
        assert backend.vector_queries == []
        payload, _ = generate_metrics_payload(metrics.registry)
        assert 'failed_branch="vector"' in payload.decode()

    async def test_embedder_error_degrades_to_lexical(self) -> None:
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        embedder = FakeEmbedder(error_to_raise=EmbeddingError("provider unavailable"))

        result = await HybridRetriever(backend, embedder=embedder).retrieve(_unembedded_query())

        assert result.retrieval_degraded and result.surviving_branch == "lexical"
        assert result.vector_error == "provider unavailable"
        assert backend.vector_queries == []

    @pytest.mark.parametrize("vector", [[0.0] * 1536, [0.1] * 8], ids=["zeros", "short"])
    async def test_unusable_query_vector_is_a_vector_branch_failure(
        self, vector: list[float]
    ) -> None:
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        embedder = _FixedVectorEmbedder(vector)

        result = await HybridRetriever(backend, embedder=embedder).retrieve(_unembedded_query())

        assert result.retrieval_degraded and result.surviving_branch == "lexical"
        assert result.vector_error is not None and "unusable query vector" in result.vector_error
        assert backend.vector_queries == []

    async def test_empty_semantic_text_skips_embedding_without_degrading(self) -> None:
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        embedder = FakeEmbedder()

        result = await HybridRetriever(backend, embedder=embedder).retrieve(
            _unembedded_query("   ")
        )

        assert embedder.recorded_calls == []
        assert not result.retrieval_degraded
        assert result.query_vector_dimension is None

    async def test_query_embedding_tokens_are_counted(self) -> None:
        metrics = create_pipeline_metrics()
        embedder = FakeEmbedder(model_name="embed-test", metrics=metrics)

        await HybridRetriever(_RecordingVectorBackend(), embedder=embedder).retrieve(
            _unembedded_query()
        )

        counted = metrics.embedding_tokens_total.labels(model="embed-test")._value.get()
        assert counted > 0

"""Unit tests for the retrieval debug endpoint (POST /v1/search/debug) (R23.3, R23.1, R23.6).

Verifies:
- Mandatory organization_id scoping on all requests (R23.6).
- Constructed query includes exact identifiers (INV-...), keywords, and filters (R12.3, R23.3).
- Both branch results return with ranks and scores (R10.8, R23.3).
- Fused results and cross-encoder rerank scores are returned (R10.3, R11.1, R23.3).
- Final selected chunks match configured top-k (R11.3, R23.3).
- Graceful degradation when a search branch fails (R10.6).
- Reranker fallback handling when reranker times out or fails (R11.5).
- Inclusion in OpenAPI 3.1 documentation (R23.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from packages.knowledge.embedder import FakeEmbedder
from packages.observability.metrics import create_pipeline_metrics
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import Candidate
from packages.retrieval.rerank import RerankService, StubReranker
from services.api.main import create_app
from services.api.openapi import generate_openapi_spec, validate_openapi_spec

TEST_ORG_ID = uuid4()
OTHER_ORG_ID = uuid4()


@pytest.fixture
def fake_backend() -> FakeSearchBackend:
    """Fixture providing an in-memory FakeSearchBackend with sample chunks."""
    backend = FakeSearchBackend()
    backend.add_chunk(
        chunk_id="chunk-inv-001",
        document_id="doc-billing-01",
        organization_id=TEST_ORG_ID,
        content="Invoice INV-2026-01829 payment procedure and net-30 terms.",
        category="billing",
        metadata={"title": "Invoice Guidelines"},
        embedding=[0.1] * 1536,
    )
    backend.add_chunk(
        chunk_id="chunk-sup-002",
        document_id="doc-support-01",
        organization_id=TEST_ORG_ID,
        content="General customer support password reset and account unlock procedure.",
        category="support",
        metadata={"title": "Account Support"},
        embedding=[0.2] * 1536,
    )
    backend.add_chunk(
        chunk_id="chunk-other-003",
        document_id="doc-other-01",
        organization_id=OTHER_ORG_ID,
        content="Other tenant invoice INV-2026-01829 confidential data.",
        category="billing",
        metadata={"title": "Other Tenant"},
        embedding=[0.1] * 1536,
    )
    return backend


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    """Deterministic in-memory embedder."""
    return FakeEmbedder()


@pytest.fixture
def api_app(fake_backend: FakeSearchBackend, fake_embedder: FakeEmbedder) -> FastAPI:
    """Create a hermetic FastAPI application with test doubles in app.state."""
    app = create_app(lifespan_enabled=False)
    metrics = create_pipeline_metrics()
    app.state.search_backend = fake_backend
    app.state.embedder = fake_embedder
    app.state.metrics = metrics
    app.state.rerank_service = RerankService(
        reranker=StubReranker(),
        metrics=metrics,
    )
    return app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Async HTTP test client bound to the hermetic API app."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


class TestRetrievalDebugEndpoint:
    """Suite validating POST /v1/search/debug contract and behavior."""

    @pytest.mark.asyncio
    async def test_debug_endpoint_requires_tenant_scoping(self, client: AsyncClient) -> None:
        """Verify 400 Bad Request when X-Organization-ID header is missing or malformed (R23.6)."""
        payload = {"query": "invoice status"}

        # Missing header
        res_missing = await client.post("/v1/search/debug", json=payload)
        assert res_missing.status_code == status.HTTP_400_BAD_REQUEST
        assert res_missing.json()["detail"]["code"] == "ORGANIZATION_ID_REQUIRED"

        # Malformed header
        res_invalid = await client.post(
            "/v1/search/debug",
            headers={"X-Organization-ID": "invalid-uuid"},
            json=payload,
        )
        assert res_invalid.status_code == status.HTTP_400_BAD_REQUEST
        assert res_invalid.json()["detail"]["code"] == "INVALID_ORGANIZATION_ID"

    @pytest.mark.asyncio
    async def test_debug_endpoint_constructed_query_identifiers(
        self,
        client: AsyncClient,
    ) -> None:
        """Verify exact identifier extraction and filter synthesis (R12.3, R23.3)."""
        headers = {"X-Organization-ID": str(TEST_ORG_ID)}
        payload = {
            "subject": "Inquiry regarding billing status",
            "body_text": "Please review payment for INV-2026-01829 immediately.",
            "intent": "billing_inquiry",
            "category": "billing",
        }

        response = await client.post("/v1/search/debug", headers=headers, json=payload)
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        constructed = data["constructed_query"]

        assert str(TEST_ORG_ID) == data["organization_id"]
        assert "INV-2026-01829" in constructed["identifiers"]
        assert "INV-2026-01829" in constructed["lexical_text"]
        assert constructed["filters"]["organization_id"] == str(TEST_ORG_ID)
        assert constructed["filters"]["category"] == "billing"
        assert constructed["query_vector_present"] is True
        assert constructed["query_vector_dimension"] == 1536

    @pytest.mark.asyncio
    async def test_debug_endpoint_returns_ranks_and_scores_both_branches(
        self,
        client: AsyncClient,
    ) -> None:
        """Verify endpoint returns both branch result lists with ranks and scores (R10.8, R23.3)."""
        headers = {"X-Organization-ID": str(TEST_ORG_ID)}
        payload = {
            "query": "invoice payment procedure INV-2026-01829",
            "top_n": 10,
            "top_k": 3,
            "apply_rerank": True,
        }

        response = await client.post("/v1/search/debug", headers=headers, json=payload)
        assert response.status_code == status.HTTP_200_OK

        data = response.json()

        # 1. Lexical branch results
        lexical = data["lexical_results"]
        assert len(lexical) > 0
        assert lexical[0]["chunk_id"] == "chunk-inv-001"
        assert lexical[0]["lexical_rank"] == 1
        assert lexical[0]["lexical_score"] is not None

        # 2. Vector branch results
        vector = data["vector_results"]
        assert len(vector) > 0
        assert vector[0]["vector_rank"] == 1
        assert vector[0]["vector_score"] is not None

        # 3. Fused results (RRF)
        fused = data["fused_results"]
        assert len(fused) > 0
        assert fused[0]["fused_score"] is not None
        assert fused[0]["chunk_id"] == "chunk-inv-001"

        # 4. Rerank results
        rerank = data["rerank_results"]
        assert len(rerank) > 0
        assert rerank[0]["rerank_score"] is not None

        # 5. Final selected chunks
        selected = data["selected_chunks"]
        assert len(selected) <= 3
        assert selected[0]["chunk_id"] == "chunk-inv-001"

        # 6. Explanation & latency diagnostics
        explanation = data["explanation"]
        assert explanation["retrieval_degraded"] is False
        assert explanation["surviving_branch"] is None
        assert explanation["rerank_applied"] is True
        assert explanation["rerank_fallback_recorded"] is False
        assert explanation["total_latency_ms"] > 0.0
        assert explanation["retrieval_latency_ms"] > 0.0

        # Verify multi-tenant isolation (OTHER_ORG_ID chunk is NOT returned)
        all_returned_chunk_ids = {c["chunk_id"] for c in fused}
        assert "chunk-other-003" not in all_returned_chunk_ids

    @pytest.mark.asyncio
    async def test_debug_endpoint_degraded_branch_handling(
        self,
        api_app: FastAPI,
        client: AsyncClient,
        fake_backend: FakeSearchBackend,
    ) -> None:
        """Verify endpoint degrades gracefully when lexical branch fails (R10.6, R23.3)."""
        fake_backend.simulate_lexical_error = RuntimeError("Database FTS extension timeout")

        headers = {"X-Organization-ID": str(TEST_ORG_ID)}
        payload = {"query": "customer support account", "top_k": 2}

        response = await client.post("/v1/search/debug", headers=headers, json=payload)
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        explanation = data["explanation"]

        assert explanation["retrieval_degraded"] is True
        assert explanation["surviving_branch"] == "vector"
        assert len(data["lexical_results"]) == 0
        assert len(data["vector_results"]) > 0
        assert len(data["selected_chunks"]) > 0

    @pytest.mark.asyncio
    async def test_debug_endpoint_reranker_fallback_handling(
        self,
        api_app: FastAPI,
        client: AsyncClient,
    ) -> None:
        """Verify endpoint handles reranker timeout/failure with fallback to RRF order (R11.5)."""

        class FailingReranker:
            async def rerank(
                self,
                query: str,
                candidates: Sequence[Candidate],
                top_k: int | None = None,
            ) -> list[Candidate]:
                raise TimeoutError("Cross-encoder timed out after 50ms")

        api_app.state.rerank_service = RerankService(
            reranker=FailingReranker(),
            timeout_seconds=0.01,
        )

        headers = {"X-Organization-ID": str(TEST_ORG_ID)}
        payload = {"query": "invoice billing terms", "apply_rerank": True}

        response = await client.post("/v1/search/debug", headers=headers, json=payload)
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        explanation = data["explanation"]

        assert explanation["rerank_applied"] is False
        assert explanation["rerank_fallback_recorded"] is True
        assert "timeout" in (explanation["rerank_fallback_reason"] or "").lower()
        assert len(data["selected_chunks"]) > 0

    @pytest.mark.asyncio
    async def test_debug_endpoint_explicit_overrides_and_disabled_rerank(
        self,
        client: AsyncClient,
    ) -> None:
        """Verify endpoint respects overrides and disabled rerank (R11.2, R23.3)."""
        headers = {"X-Organization-ID": str(TEST_ORG_ID)}
        custom_vec = [0.1] * 1536
        payload = {
            "query": "custom query",
            "identifiers": ["INV-CUSTOM-001"],
            "lexical_terms": ["customterm"],
            "query_vector": custom_vec,
            "apply_rerank": False,
            "top_k": 2,
        }

        response = await client.post("/v1/search/debug", headers=headers, json=payload)
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert "INV-CUSTOM-001" in data["constructed_query"]["identifiers"]
        assert "customterm" in data["constructed_query"]["lexical_terms"]
        assert data["constructed_query"]["query_vector_present"] is True
        assert data["explanation"]["rerank_applied"] is False
        assert len(data["selected_chunks"]) <= 2


class TestOpenAPIAndDocsIntegration:
    """Validate /v1/search/debug is documented in OpenAPI 3.1 specification (R23.1)."""

    def test_search_debug_in_openapi_spec(self, api_app: FastAPI) -> None:
        """Ensure /v1/search/debug route and schemas appear in OpenAPI 3.1 document."""
        spec = generate_openapi_spec(api_app)
        assert spec["openapi"] == "3.1.0"

        paths = spec["paths"]
        assert "/v1/search/debug" in paths
        debug_op = paths.get("/v1/search/debug") or paths.get("v1/search/debug")
        assert debug_op is not None
        assert "post" in debug_op

        # Validate request and response schema references
        post_op = debug_op["post"]
        assert "RetrievalDebugRequest" in str(post_op)
        assert "RetrievalDebugResponse" in str(post_op)

        # Validate entire spec structure
        valid, errors = validate_openapi_spec(spec)
        assert valid, f"OpenAPI validation errors: {errors}"

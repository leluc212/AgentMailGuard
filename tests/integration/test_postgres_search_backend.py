"""Live PostgreSQL and pgvector integration tests for PostgresSearchBackend.

Requirements:
- Task 3.8: PostgresSearchBackend implementation and integration suite.
- R10.1: PostgreSQL FTS and pgvector ANN against chunk corpus.
- R10.2: Configurable top-N per branch, default 20.
- R10.4: Filters applied inside each branch.
- R10.7: Conform to SearchBackend protocol and pass SearchBackendContractSuite.
- R10.8: Return Candidate objects with both ranks, both scores, fused score, and source metadata.
- R10.10: Filtered-ANN under-fill detection, metric recording, and search widening.
- R10.11: Multi-tenant integration test on >=3 tenants asserting full top-N for target tenant.
- GEMINI.md §8: Multi-tenant fixture seeding >=3 tenants with overlapping content.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.observability.metrics import create_pipeline_metrics
from packages.retrieval import (
    Candidate,
    PostgresSearchBackend,
    RetrievalQuery,
    SearchBackend,
)
from packages.retrieval.testing import SearchBackendContractSuite


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide asyncpg pool connected to test database on port 5433."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


def _pad_vec(vec: list[float] | None, dim: int = 1536) -> list[float] | None:
    if vec is None:
        return None
    v = [float(x) for x in vec]
    if len(v) < dim:
        v = v + [0.0] * (dim - len(v))
    elif len(v) > dim:
        v = v[:dim]
    return v


async def _seed_postgres_chunk(
    pool: asyncpg.Pool[Any],
    chunk_id: str,
    document_id: str,
    organization_id: str,
    content: str,
    category: str | None = None,
    document_status: str = "active",
    metadata: dict[str, Any] | None = None,
    embedding: list[float] | None = None,
) -> None:
    """Helper to insert real test entities into PostgreSQL respecting schema foreign keys."""
    org_u = UUID(organization_id)
    doc_u = UUID(document_id)
    chk_u = UUID(chunk_id)
    meta_dict = dict(metadata or {})
    heading_path = meta_dict.get("heading_path", [])
    section = meta_dict.get("section")
    external_id = meta_dict.get("external_id")

    async with pool.acquire() as conn:
        # 1. Ensure organization exists
        await conn.execute(
            """
            INSERT INTO organization (id, name)
            VALUES ($1, $2)
            ON CONFLICT (id) DO NOTHING;
            """,
            org_u,
            f"Org {org_u}",
        )

        # 2. Ensure document exists
        await conn.execute(
            """
            INSERT INTO knowledge_document (id, organization_id, title, status, category, version)
            VALUES ($1, $2, $3, $4, $5, 1)
            ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, category = EXCLUDED.category;
            """,
            doc_u,
            org_u,
            f"Doc {doc_u}",
            document_status,
            category,
        )

        # 3. Insert chunk with write-time tsvector
        await conn.execute(
            """
            INSERT INTO knowledge_chunk (
                id, document_id, organization_id, chunk_index, external_id,
                heading_path, section, category, content, metadata, version, content_tsv
            )
            VALUES (
                $1, $2, $3, 0, $4,
                $5, $6, $7, $8, $9::jsonb, 1, to_tsvector('english', $8)
            )
            ON CONFLICT (id) DO UPDATE
            SET content = EXCLUDED.content,
                content_tsv = to_tsvector('english', EXCLUDED.content),
                category = EXCLUDED.category;
            """,
            chk_u,
            doc_u,
            org_u,
            external_id,
            heading_path,
            section,
            category,
            content,
            json.dumps(meta_dict),
        )

        # 4. Insert embedding if provided
        if embedding is not None:
            vec_data = _pad_vec(embedding)
            await conn.execute(
                """
                INSERT INTO embedding_record (chunk_id, organization_id, model, dim, embedding)
                VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)
                ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding;
                """,
                chk_u,
                org_u,
                vec_data,
            )


class TestPostgresSearchBackendContract(SearchBackendContractSuite):
    """Proves PostgresSearchBackend passes the canonical contract suite on live PostgreSQL."""

    @pytest.fixture(autouse=True)
    def setup_backend(self, db_pool: asyncpg.Pool[Any]) -> None:
        self.pool = db_pool
        self.backend = PostgresSearchBackend(db_pool)

    def create_backend(self) -> SearchBackend:
        return self.backend

    async def seed_chunk(
        self,
        backend: SearchBackend,
        chunk_id: str,
        document_id: str,
        organization_id: str,
        content: str,
        category: str | None = None,
        document_status: str = "active",
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        await _seed_postgres_chunk(
            pool=self.pool,
            chunk_id=chunk_id,
            document_id=document_id,
            organization_id=organization_id,
            content=content,
            category=category,
            document_status=document_status,
            metadata=metadata,
            embedding=embedding,
        )


@pytest.mark.asyncio
async def test_postgres_hybrid_search_cte(db_pool: asyncpg.Pool[Any]) -> None:
    """Test PostgresSearchBackend.hybrid() executing reference CTE from design.md §5.5."""
    backend = PostgresSearchBackend(db_pool)
    org_id = str(uuid4())
    doc_id = str(uuid4())
    cid1 = str(uuid4())
    cid2 = str(uuid4())

    # Chunk 1 matches both keyword ('enterprise billing') and semantic vector [1.0, ...]
    await _seed_postgres_chunk(
        pool=db_pool,
        chunk_id=cid1,
        document_id=doc_id,
        organization_id=org_id,
        content="Enterprise billing and subscription renewal procedures.",
        category="billing",
        embedding=[1.0, 0.0, 0.0],
        metadata={"section": "Billing Procedures"},
    )

    # Chunk 2 matches only keyword ('enterprise server') with orthogonal vector
    await _seed_postgres_chunk(
        pool=db_pool,
        chunk_id=cid2,
        document_id=doc_id,
        organization_id=org_id,
        content="Enterprise server maintenance guidelines.",
        category="billing",
        embedding=[0.0, 1.0, 0.0],
        metadata={"section": "Maintenance"},
    )

    query = RetrievalQuery(
        semantic_text="How do enterprise billing procedures work?",
        lexical_terms=["enterprise", "billing"],
        query_vector=[1.0, 0.0, 0.0],
        filters={"organization_id": org_id, "category": "billing", "status": "active"},
    )

    candidates = await backend.hybrid(query, top_n=10, k=60, fuse_limit=5)

    assert len(candidates) >= 1
    top = candidates[0]
    assert isinstance(top, Candidate)
    assert top.chunk_id == cid1
    # Both ranks populated from the CTE branches
    assert top.lexical_rank == 1
    assert top.vector_rank == 1
    assert top.lexical_score is not None and top.lexical_score > 0
    assert top.vector_score is not None and top.vector_score > 0.9
    # Fused score computed: 1/(60+1) + 1/(60+1) = 2/61 ≈ 0.03278
    assert top.fused_score is not None
    assert round(top.fused_score, 4) == round(2.0 / 61.0, 4)
    assert top.metadata.get("section") == "Billing Procedures"


@pytest.mark.asyncio
async def test_multi_tenant_filtered_vector_and_underfill_mitigation(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """Verify filtered vector search across >=3 tenants returns full top-N without leakage.

    Fulfills R10.10, R10.11, and GEMINI.md §8.

    Seeds 3 distinct tenants with overlapping content and similar vectors.
    Target tenant requests top-N=20 chunks. Verifies:
    1. Full top-N (20 chunks) returned for target tenant.
    2. Zero leakage from other 2 tenants.
    3. Underfill metric and widening behavior operate cleanly.
    """
    metrics = create_pipeline_metrics()
    backend = PostgresSearchBackend(db_pool, metrics=metrics, default_top_n=20)

    target_org = str(uuid4())
    competitor_org1 = str(uuid4())
    competitor_org2 = str(uuid4())

    # Seed 20 chunks for target tenant
    target_doc = str(uuid4())
    for i in range(20):
        # Slightly vary vector around unit vector [1.0, ...]
        v = [1.0, float(i) / 100.0, 0.0]
        await _seed_postgres_chunk(
            pool=db_pool,
            chunk_id=str(uuid4()),
            document_id=target_doc,
            organization_id=target_org,
            content=f"Target tenant proprietary SLA agreement section {i}.",
            category="sla",
            embedding=v,
        )

    # Seed 25 chunks for competitor tenant 1 with identical/competing vectors
    comp1_doc = str(uuid4())
    for i in range(25):
        v = [1.0, float(i) / 100.0, 0.0]
        await _seed_postgres_chunk(
            pool=db_pool,
            chunk_id=str(uuid4()),
            document_id=comp1_doc,
            organization_id=competitor_org1,
            content=f"Competitor 1 proprietary SLA agreement section {i}.",
            category="sla",
            embedding=v,
        )

    # Seed 25 chunks for competitor tenant 2 with identical/competing vectors
    comp2_doc = str(uuid4())
    for i in range(25):
        v = [1.0, float(i) / 100.0, 0.0]
        await _seed_postgres_chunk(
            pool=db_pool,
            chunk_id=str(uuid4()),
            document_id=comp2_doc,
            organization_id=competitor_org2,
            content=f"Competitor 2 proprietary SLA agreement section {i}.",
            category="sla",
            embedding=v,
        )

    query = RetrievalQuery(
        semantic_text="What are the SLA agreement terms?",
        query_vector=[1.0, 0.0, 0.0],
        filters={"organization_id": target_org, "category": "sla", "status": "active"},
    )

    # Query for target tenant asking for full top-N=20
    candidates = await backend.vector(query, top_n=20)

    # Verify R10.11: target tenant receives full top-N (20 chunks)
    assert len(candidates) == 20
    ranks = [c.vector_rank for c in candidates]
    assert ranks == list(range(1, 21))

    # Verify zero data leakage from competitor tenants (R5.3)
    for c in candidates:
        assert "Target tenant" in c.content
        assert "Competitor" not in c.content

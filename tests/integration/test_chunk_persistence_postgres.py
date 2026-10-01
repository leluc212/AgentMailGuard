"""Integration tests for PostgreSQL knowledge chunk and vector embedding persistence.

Requirements:
- R9.7: Single-transaction persistence of chunk and write-time content_tsv generation.
- R5.6: Maintain GIN index over chunk content_tsv column.
- R5.7: Maintain HNSW index over embedding_record.embedding using vector_cosine_ops.
- R5.3: Mandatory organization_id predicate and multi-tenant isolation (>=3 tenants).
- CLAUDE.md §8: Ephemeral PostgreSQL container on port 5433 with pgvector.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.knowledge import PostgresKnowledgeStore
from packages.domain.knowledge import KnowledgeChunk, KnowledgeDocument


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide asyncpg pool connected to test database on port 5433."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def ensure_test_org(pool: asyncpg.Pool[Any], org_id: uuid.UUID) -> None:
    """Ensure parent organization exists in database."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO organization (id, name)
            VALUES ($1, $2)
            ON CONFLICT (id) DO NOTHING;
            """,
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


def _make_unit_vector(dim: int, active_idx: int) -> list[float]:
    """Create a unit vector with 1.0 at active_idx."""
    vec = [0.0] * dim
    vec[active_idx % dim] = 1.0
    return vec


def _make_dense_vector(dim: int, seed_val: float) -> list[float]:
    """Create an L2-normalized dense vector."""
    raw = [math.sin(seed_val + i * 0.1) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@pytest.mark.asyncio
async def test_postgres_chunk_and_embedding_persistence(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify single-transaction persistence of chunk and embedding in PostgreSQL (R9.7, R5.6)."""
    store = PostgresKnowledgeStore(db_pool)
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    dim = 1536

    await ensure_test_org(db_pool, org_id)

    try:
        # 1. Insert parent knowledge document
        doc = KnowledgeDocument(
            id=doc_id,
            organization_id=org_id,
            title="SLA and Escalation Policy",
            category="support",
            version=1,
            status="active",
        )
        await store.insert_document(doc)

        # 2. Persist chunk with embedding in atomic transaction
        chunk = KnowledgeChunk(
            id=chunk_id,
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=0,
            external_id="DOC-SLA-01",
            heading_path=["Support", "SLA"],
            section="Critical Outage Response",
            category="support",
            content="Enterprise SLA guarantees 1-hour priority response window.",
            token_count=18,
            content_checksum="sha256:abc123mock",
            metadata={"source": "zendesk", "author": "ops"},
            version=1,
        )
        emb_vec = _make_dense_vector(dim, 42.0)

        persisted_count = await store.persist_chunks_with_embeddings(
            chunks=[chunk],
            embeddings=[emb_vec],
            model="text-embedding-3-small",
            dimension=dim,
            organization_id=org_id,
        )
        assert persisted_count == 1

        # 3. Verify chunk retrieval and write-time content_tsv population (R9.7, R5.6)
        fetched_chunk = await store.get_chunk(org_id, chunk_id)
        assert fetched_chunk is not None
        assert fetched_chunk.external_id == "DOC-SLA-01"
        assert fetched_chunk.section == "Critical Outage Response"
        assert fetched_chunk.metadata == {"source": "zendesk", "author": "ops"}

        # Direct SQL inspection of content_tsv
        async with db_pool.acquire() as conn:
            tsv_row = await conn.fetchrow(
                "SELECT content_tsv::text FROM knowledge_chunk "
                "WHERE id = $1 AND organization_id = $2;",
                chunk_id,
                org_id,
            )
            assert tsv_row is not None
            tsv_text = tsv_row["content_tsv"]
            # Assert both section words and content words were parsed into tsvector
            assert "outag" in tsv_text  # stem of Outage
            assert "enterpris" in tsv_text  # stem of Enterprise

        # 4. Verify embedding retrieval (R5.7)
        pair = await store.get_chunk_with_embedding(org_id, chunk_id)
        assert pair is not None
        chunk_obj, emb_obj = pair
        assert chunk_obj.id == chunk_id
        assert len(emb_obj) == dim
        assert abs(emb_obj[0] - emb_vec[0]) < 1e-4

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1;", org_id)


@pytest.mark.asyncio
async def test_atomic_transaction_rollback_on_failure(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify transaction rolls back cleanly if embedding write fails (R9.7).

    Neither the chunk row nor the embedding row is committed.
    """
    store = PostgresKnowledgeStore(db_pool)
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()

    await ensure_test_org(db_pool, org_id)

    try:
        # Insert parent document
        doc = KnowledgeDocument(
            id=doc_id,
            organization_id=org_id,
            title="Temporary Rollback Document",
        )
        await store.insert_document(doc)

        chunk = KnowledgeChunk(
            id=chunk_id,
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=0,
            content="This chunk must NOT be persisted if embedding insertion fails.",
        )

        # Dimension mismatch causes client pre-validation failure
        with pytest.raises(ValueError, match="dimension mismatch"):
            await store.persist_chunks_with_embeddings(
                chunks=[chunk],
                embeddings=[[0.1, 0.2]],  # Invalid dim
                model="text-embedding-3-small",
                dimension=1536,
                organization_id=org_id,
            )

        # Assert no chunk row committed
        assert await store.get_chunk(org_id, chunk_id) is None

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM knowledge_chunk WHERE document_id = $1;",
                doc_id,
            )
            assert count == 0

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1;", org_id)


@pytest.mark.asyncio
async def test_fts_gin_lexical_search_ranking(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify write-time content_tsv is searchable via GIN index with weight ranking (R5.6)."""
    store = PostgresKnowledgeStore(db_pool)
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    dim = 1536

    await ensure_test_org(db_pool, org_id)

    try:
        doc = KnowledgeDocument(
            id=doc_id,
            organization_id=org_id,
            title="Technical Documentation",
            category="docs",
        )
        await store.insert_document(doc)

        # Chunk 1: 'database' in section (weight A)
        c1 = KnowledgeChunk(
            id=uuid.uuid4(),
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=0,
            section="Database Replication",
            content="Streaming standby nodes provide replication across availability zones.",
            category="docs",
        )
        # Chunk 2: 'database' in body content only (weight B)
        c2 = KnowledgeChunk(
            id=uuid.uuid4(),
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=1,
            section="Application Architecture",
            content="The service connects to a PostgreSQL database for operational storage.",
            category="docs",
        )

        v1 = _make_dense_vector(dim, 10.0)
        v2 = _make_dense_vector(dim, 20.0)

        await store.persist_chunks_with_embeddings(
            chunks=[c1, c2],
            embeddings=[v1, v2],
            model="text-embedding-3-small",
            dimension=dim,
            organization_id=org_id,
        )

        # Search for 'database'
        results = await store.search_chunks_lexical(org_id, query="database", limit=5)
        assert len(results) == 2
        # c1 has 'database' in section heading ('A' weight) so ts_rank is higher
        assert results[0].id == c1.id
        assert results[1].id == c2.id

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1;", org_id)


@pytest.mark.asyncio
async def test_hnsw_vector_similarity_search(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify HNSW cosine vector search using <=> operator (R5.7)."""
    store = PostgresKnowledgeStore(db_pool)
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    dim = 1536

    await ensure_test_org(db_pool, org_id)

    try:
        doc = KnowledgeDocument(
            id=doc_id,
            organization_id=org_id,
            title="Security Policy",
        )
        await store.insert_document(doc)

        c1 = KnowledgeChunk(
            id=uuid.uuid4(),
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=0,
            content="Vector search target 1",
        )
        c2 = KnowledgeChunk(
            id=uuid.uuid4(),
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=1,
            content="Vector search target 2",
        )

        # Distinct orthogonal vectors
        emb1 = _make_unit_vector(dim, 5)
        emb2 = _make_unit_vector(dim, 50)

        await store.persist_chunks_with_embeddings(
            chunks=[c1, c2],
            embeddings=[emb1, emb2],
            model="text-embedding-3-small",
            dimension=dim,
            organization_id=org_id,
        )

        # Query vector close to emb1
        q_vec = _make_unit_vector(dim, 5)
        search_results = await store.search_chunks_vector(org_id, query_vector=q_vec, limit=2)
        assert len(search_results) == 2
        top_chunk, top_dist = search_results[0]
        assert top_chunk.id == c1.id
        assert top_dist < 0.001  # cosine distance ~0

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1;", org_id)


@pytest.mark.asyncio
async def test_multi_tenant_isolation_three_tenants(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify tenant isolation across >=3 tenants with identical external IDs (R5.3)."""
    store = PostgresKnowledgeStore(db_pool)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    tenant_c = uuid.uuid4()
    all_tenants = [tenant_a, tenant_b, tenant_c]
    dim = 1536

    for t in all_tenants:
        await ensure_test_org(db_pool, t)

    try:
        # Each tenant gets a document and chunk with identical external_id
        chunk_ids: dict[uuid.UUID, uuid.UUID] = {}
        for idx, t in enumerate(all_tenants):
            doc_id = uuid.uuid4()
            doc = KnowledgeDocument(
                id=doc_id,
                organization_id=t,
                title=f"Tenant Document {t.hex[:4]}",
                category="policy",
            )
            await store.insert_document(doc)

            c_id = uuid.uuid4()
            chunk_ids[t] = c_id
            chunk = KnowledgeChunk(
                id=c_id,
                document_id=doc_id,
                organization_id=t,
                chunk_index=0,
                external_id="GLOBAL-POLICY-01",  # Same external ID across tenants
                section="Return Policy",
                content=f"Confidential return policy terms for tenant {t.hex[:4]}.",
                category="policy",
            )
            emb = _make_dense_vector(dim, float(idx * 10))
            await store.persist_chunks_with_embeddings(
                chunks=[chunk],
                embeddings=[emb],
                model="text-embedding-3-small",
                dimension=dim,
                organization_id=t,
            )

        # 1. Tenant A direct chunk query
        chunk_a = await store.get_chunk(tenant_a, chunk_ids[tenant_a])
        assert chunk_a is not None
        assert str(tenant_a.hex[:4]) in chunk_a.content

        # Attempt to access Tenant B's chunk using Tenant A's org_id
        cross_chunk = await store.get_chunk(tenant_a, chunk_ids[tenant_b])
        assert cross_chunk is None

        # 2. Tenant A lexical search
        lex_results = await store.search_chunks_lexical(
            tenant_a, query="return policy terms", limit=10
        )
        assert len(lex_results) == 1
        assert lex_results[0].id == chunk_ids[tenant_a]

        # 3. Tenant A vector search
        q_vec = _make_dense_vector(dim, 0.0)
        vec_results = await store.search_chunks_vector(tenant_a, query_vector=q_vec, limit=10)
        assert len(vec_results) == 1
        assert vec_results[0][0].id == chunk_ids[tenant_a]

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = ANY($1::uuid[]);", all_tenants)


@pytest.mark.asyncio
async def test_cascading_deletion(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify deleting chunks cascades to embedding_record in PostgreSQL."""
    store = PostgresKnowledgeStore(db_pool)
    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    dim = 1536

    await ensure_test_org(db_pool, org_id)

    try:
        doc = KnowledgeDocument(
            id=doc_id,
            organization_id=org_id,
            title="Cascade Test Doc",
        )
        await store.insert_document(doc)

        chunk = KnowledgeChunk(
            id=chunk_id,
            document_id=doc_id,
            organization_id=org_id,
            chunk_index=0,
            content="Temporary chunk for cascade test.",
        )
        emb = _make_dense_vector(dim, 99.0)

        await store.persist_chunks_with_embeddings(
            chunks=[chunk],
            embeddings=[emb],
            model="text-embedding-3-small",
            dimension=dim,
            organization_id=org_id,
        )

        # Delete chunks by document
        deleted = await store.delete_chunks_by_document(org_id, doc_id)
        assert deleted == 1

        # Verify chunk is gone
        assert await store.get_chunk(org_id, chunk_id) is None

        # Verify embedding_record was cascaded at database foreign key level
        async with db_pool.acquire() as conn:
            emb_count = await conn.fetchval(
                "SELECT COUNT(*) FROM embedding_record WHERE chunk_id = $1;",
                chunk_id,
            )
            assert emb_count == 0

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1;", org_id)

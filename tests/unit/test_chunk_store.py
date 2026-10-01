"""Unit tests for knowledge chunk and vector embedding persistence.

Requirements:
- R9.7: Populate chunk tsvector at write time in the same transaction as chunk row.
- R5.6: Maintain GIN index over chunk content_tsv column.
- R5.7: Maintain HNSW index over embedding_record.embedding.
- R5.3: Strict multi-tenant organization_id scoping on every operation.
"""

from __future__ import annotations

import math
from uuid import uuid4

import pytest

from packages.db.knowledge import InMemoryKnowledgeStore, KnowledgeStore
from packages.domain.knowledge import KnowledgeChunk, KnowledgeDocument


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


@pytest.fixture
def store() -> InMemoryKnowledgeStore:
    return InMemoryKnowledgeStore()


def test_knowledge_store_protocol_conformance(store: InMemoryKnowledgeStore) -> None:
    """Verify InMemoryKnowledgeStore satisfies KnowledgeStore protocol."""
    assert isinstance(store, KnowledgeStore)


@pytest.mark.asyncio
async def test_document_lifecycle(store: InMemoryKnowledgeStore) -> None:
    """Verify document insertion, retrieval, and status updates."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="Refund Policy",
        source_uri="s3://docs/refund.pdf",
        category="policy",
        version=1,
    )

    inserted = await store.insert_document(doc)
    assert inserted.id == doc_id
    assert inserted.status == "pending"

    # Retrieval
    fetched = await store.get_document(org_id, doc_id)
    assert fetched is not None
    assert fetched.title == "Refund Policy"

    # Status update
    await store.update_document_status(org_id, doc_id, status="active", version=2)
    updated = await store.get_document(org_id, doc_id)
    assert updated is not None
    assert updated.status == "active"
    assert updated.version == 2

    # Tenant isolation on document
    other_org = uuid4()
    assert await store.get_document(other_org, doc_id) is None


@pytest.mark.asyncio
async def test_persist_chunks_with_embeddings_success(store: InMemoryKnowledgeStore) -> None:
    """Verify successful atomic batch chunk and embedding persistence."""
    org_id = uuid4()
    doc_id = uuid4()
    dim = 1536

    c1 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=0,
        section="Refund Window",
        content="Acme offers a 30-day refund window on software.",
        category="policy",
        version=1,
    )
    c2 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=1,
        section="SLA Escalation",
        content="Enterprise SLA response time is 1 hour.",
        category="policy",
        version=1,
    )

    emb1 = _make_dense_vector(dim, 1.0)
    emb2 = _make_dense_vector(dim, 2.0)

    count = await store.persist_chunks_with_embeddings(
        chunks=[c1, c2],
        embeddings=[emb1, emb2],
        model="text-embedding-3-small",
        dimension=dim,
        organization_id=org_id,
    )
    assert count == 2

    # Verify get_chunk
    fetched_c1 = await store.get_chunk(org_id, c1.id)
    assert fetched_c1 is not None
    assert fetched_c1.section == "Refund Window"

    # Verify get_chunk_with_embedding
    pair = await store.get_chunk_with_embedding(org_id, c1.id)
    assert pair is not None
    chunk_res, emb_res = pair
    assert chunk_res.id == c1.id
    assert len(emb_res) == dim
    assert emb_res[:3] == emb1[:3]

    # Verify get_chunks_by_document
    doc_chunks = await store.get_chunks_by_document(org_id, doc_id)
    assert len(doc_chunks) == 2
    assert [c.chunk_index for c in doc_chunks] == [0, 1]


@pytest.mark.asyncio
async def test_persist_chunks_validation_atomicity(store: InMemoryKnowledgeStore) -> None:
    """Verify count and dimension mismatches reject batch atomically."""
    org_id = uuid4()
    doc_id = uuid4()
    dim = 1536

    c1 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=0,
        content="Chunk 1",
    )
    c2 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=1,
        content="Chunk 2",
    )

    # 1. Count mismatch
    with pytest.raises(ValueError, match="Chunks count"):
        await store.persist_chunks_with_embeddings(
            chunks=[c1, c2],
            embeddings=[_make_dense_vector(dim, 1.0)],
            model="text-embedding-3-small",
            dimension=dim,
            organization_id=org_id,
        )
    assert await store.get_chunk(org_id, c1.id) is None

    # 2. Dimension mismatch
    with pytest.raises(ValueError, match="dimension mismatch"):
        await store.persist_chunks_with_embeddings(
            chunks=[c1, c2],
            embeddings=[_make_dense_vector(dim, 1.0), [0.1, 0.2]],  # Invalid dim 2 != 1536
            model="text-embedding-3-small",
            dimension=dim,
            organization_id=org_id,
        )
    # Ensure neither c1 nor c2 was persisted
    assert await store.get_chunk(org_id, c1.id) is None
    assert await store.get_chunk(org_id, c2.id) is None


@pytest.mark.asyncio
async def test_delete_chunks_by_document(store: InMemoryKnowledgeStore) -> None:
    """Verify deleting chunks by document cascades and respects version."""
    org_id = uuid4()
    doc_id = uuid4()
    dim = 1536

    c_v1 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=0,
        content="Version 1 chunk",
        version=1,
    )
    c_v2 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=0,
        content="Version 2 chunk",
        version=2,
    )

    await store.persist_chunks_with_embeddings(
        chunks=[c_v1, c_v2],
        embeddings=[_make_dense_vector(dim, 1.0), _make_dense_vector(dim, 2.0)],
        model="text-embedding-3-small",
        dimension=dim,
        organization_id=org_id,
    )

    # Delete only version 1
    deleted_v1 = await store.delete_chunks_by_document(org_id, doc_id, version=1)
    assert deleted_v1 == 1

    remaining = await store.get_chunks_by_document(org_id, doc_id)
    assert len(remaining) == 1
    assert remaining[0].version == 2
    assert await store.get_chunk_with_embedding(org_id, c_v1.id) is None
    assert await store.get_chunk_with_embedding(org_id, c_v2.id) is not None

    # Delete remaining all versions
    deleted_all = await store.delete_chunks_by_document(org_id, doc_id)
    assert deleted_all == 1
    assert len(await store.get_chunks_by_document(org_id, doc_id)) == 0


@pytest.mark.asyncio
async def test_tenant_isolation(store: InMemoryKnowledgeStore) -> None:
    """Verify tenant isolation across all chunk and vector operations (R5.3)."""
    tenant_a = uuid4()
    tenant_b = uuid4()
    doc_id = uuid4()
    dim = 1536

    chunk_a = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=tenant_a,
        content="Confidential financial report for Tenant A.",
    )
    chunk_b = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=tenant_b,
        content="Confidential financial report for Tenant B.",
    )

    vec = _make_dense_vector(dim, 42.0)

    await store.persist_chunks_with_embeddings(
        chunks=[chunk_a],
        embeddings=[vec],
        model="text-embedding-3-small",
        dimension=dim,
        organization_id=tenant_a,
    )
    await store.persist_chunks_with_embeddings(
        chunks=[chunk_b],
        embeddings=[vec],
        model="text-embedding-3-small",
        dimension=dim,
        organization_id=tenant_b,
    )

    # Tenant A querying
    assert await store.get_chunk(tenant_a, chunk_a.id) is not None
    assert await store.get_chunk(tenant_a, chunk_b.id) is None
    assert len(await store.get_chunks_by_document(tenant_a, doc_id)) == 1

    # Tenant B querying
    assert await store.get_chunk(tenant_b, chunk_b.id) is not None
    assert await store.get_chunk(tenant_b, chunk_a.id) is None
    assert len(await store.get_chunks_by_document(tenant_b, doc_id)) == 1


@pytest.mark.asyncio
async def test_search_chunks_lexical_and_vector(store: InMemoryKnowledgeStore) -> None:
    """Verify lexical keyword search and vector cosine similarity search."""
    org_id = uuid4()
    doc_id = uuid4()
    dim = 1536

    c1 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=0,
        section="Authentication",
        content="To authenticate with the API, provide an X-Api-Key bearer token.",
        category="security",
    )
    c2 = KnowledgeChunk(
        id=uuid4(),
        document_id=doc_id,
        organization_id=org_id,
        chunk_index=1,
        section="Billing Invoices",
        content="Monthly invoices are dispatched on the 1st of every calendar month.",
        category="billing",
    )

    # Unit vectors for orthogonal directions
    emb1 = _make_unit_vector(dim, 10)
    emb2 = _make_unit_vector(dim, 20)

    await store.persist_chunks_with_embeddings(
        chunks=[c1, c2],
        embeddings=[emb1, emb2],
        model="text-embedding-3-small",
        dimension=dim,
        organization_id=org_id,
    )

    # 1. Lexical search
    lex_res = await store.search_chunks_lexical(org_id, query="bearer token", limit=5)
    assert len(lex_res) == 1
    assert lex_res[0].id == c1.id

    # Filtered lexical search
    lex_filtered = await store.search_chunks_lexical(
        org_id, query="invoices", category="billing", limit=5
    )
    assert len(lex_filtered) == 1
    assert lex_filtered[0].id == c2.id

    # 2. Vector search
    # Query vector close to emb1
    q_vec = _make_unit_vector(dim, 10)
    vec_res = await store.search_chunks_vector(org_id, query_vector=q_vec, limit=2)
    assert len(vec_res) == 2
    top_chunk, top_dist = vec_res[0]
    assert top_chunk.id == c1.id
    assert top_dist < 0.001  # identical direction -> cosine distance ~0

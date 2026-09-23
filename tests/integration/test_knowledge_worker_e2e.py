"""Live end-to-end integration tests for knowledge ingestion pipeline and versioning.

Requirements:
- R9.1: Ingestion pipeline execution (parse -> chunk -> enrich -> embed -> persist -> active).
- R9.8: Version N+1 re-ingestion with atomic flip (never unsearchable).
- R9.9: Content checksum deduplication skips re-embedding across versions.
- R9.10: Per-document status tracking with failure reason.
- R5.3: Strict multi-tenant isolation across >=3 tenants.
- GEMINI.md §8: Ephemeral PostgreSQL (port 5433) and MinIO (port 9010).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.core.storage import (
    MinioObjectStorageClient,
    ObjectKeyBuilder,
    get_storage_client,
)
from packages.db.connection import create_pool_from_settings
from packages.db.knowledge import PostgresKnowledgeStore
from packages.domain.knowledge import KnowledgeDocument
from packages.knowledge.chunker import ChunkerConfig, StructuralChunker
from packages.knowledge.embedder import FakeEmbedder
from packages.knowledge.pipeline import KnowledgeIngestionPipeline


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide asyncpg pool connected to test database on port 5433."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def storage_client() -> MinioObjectStorageClient:
    """Instantiate live MinIO storage client from settings."""
    client = get_storage_client()
    assert isinstance(client, MinioObjectStorageClient)
    return client


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


@pytest.mark.asyncio
async def test_live_minio_and_postgres_ingestion_lifecycle(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """Verify live MinIO fetch -> chunk -> embed -> Postgres persist -> version N+1 re-ingest."""
    await storage_client.bootstrap_buckets()
    bucket = storage_client.settings.bucket_knowledge

    org_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    await ensure_test_org(db_pool, org_id)

    store = PostgresKnowledgeStore(db_pool)
    embedder = FakeEmbedder(dimension=1536)
    chunker = StructuralChunker(ChunkerConfig(min_tokens=20, max_tokens=120, overlap_tokens=10))

    pipeline = KnowledgeIngestionPipeline(
        store=store,
        embedder=embedder,
        chunker=chunker,
        storage=storage_client,
        bucket_name=bucket,
    )

    # 1. Upload initial document to MinIO
    object_key = ObjectKeyBuilder.knowledge_doc(org_id, doc_id, version=1, filename="sla.md")
    v1_content = (
        b"# Enterprise Service Level Agreement\n\n"
        b"Our guaranteed platform uptime is ninety-nine point nine percent across all regions. "
        b"Scheduled maintenance windows are announced at least seventy-two hours in advance.\n\n"
        b"# Incident Severity and Response Times\n\n"
        b"Severity one critical incidents require initial response within fifteen minutes. "
        b"Severity two major incidents require response within one business hour by engineering.\n"
    )

    await storage_client.put_bytes(
        bucket=bucket,
        key=object_key,
        data=v1_content,
        content_type="text/markdown",
    )

    # 2. Insert pending document record in Postgres
    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="SLA Document",
        category="support",
        object_key=object_key,
        mime_type="text/markdown",
        status="pending",
    )
    await store.insert_document(doc)

    # 3. Execute ingestion pipeline (R9.1)
    res1 = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        filename="sla.md",
    )

    assert res1.status == "active"
    assert res1.version == 1
    assert res1.total_chunks >= 2
    assert res1.embedded_chunks == res1.total_chunks
    assert res1.carried_forward_chunks == 0

    # Verify status in PostgreSQL (R9.10)
    db_doc1 = await store.get_document(org_id, doc_id)
    assert db_doc1 is not None
    assert db_doc1.status == "active"
    assert db_doc1.version == 1

    # Verify chunks and embeddings in PostgreSQL (R9.6, R9.7)
    chunks_v1 = await store.get_chunks_by_document(org_id, doc_id, version=1)
    assert len(chunks_v1) == res1.total_chunks
    for c in chunks_v1:
        pair = await store.get_chunk_with_embedding(org_id, c.id)
        assert pair is not None
        assert len(pair[1]) == 1536

    # Verify lexical search works on persisted chunks (R5.6)
    search_results = await store.search_chunks_lexical(org_id, query="uptime")
    assert len(search_results) > 0
    assert any("uptime" in c.content.lower() for c in search_results)

    # 4. Re-ingestion (R9.8, R9.9): update section 2 in MinIO, keep section 1 unchanged
    embedder.clear_recorded_calls()
    v2_content = (
        b"# Enterprise Service Level Agreement\n\n"
        b"Our guaranteed platform uptime is ninety-nine point nine percent across all regions. "
        b"Scheduled maintenance windows are announced at least seventy-two hours in advance.\n\n"
        b"# Incident Severity and Response Times\n\n"
        b"Severity one critical incidents require initial response within five minutes. "
        b"Severity two major incidents require response within thirty minutes by engineering.\n"
    )

    object_key_v2 = ObjectKeyBuilder.knowledge_doc(org_id, doc_id, version=2, filename="sla.md")
    await storage_client.put_bytes(
        bucket=bucket,
        key=object_key_v2,
        data=v2_content,
        content_type="text/markdown",
    )

    # Update doc object_key to point to v2
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE knowledge_document SET object_key = $1 WHERE id = $2 AND organization_id = $3",
            object_key_v2,
            doc_id,
            org_id,
        )

    res2 = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        filename="sla.md",
    )

    assert res2.status == "active"
    assert res2.version == 2
    assert res2.total_chunks >= 2
    # Section 1 content was unchanged -> embedding carried forward (R9.9)
    assert res2.carried_forward_chunks >= 1
    assert res2.embedded_chunks < res2.total_chunks

    # Document version in DB is now 2 (R9.8)
    db_doc2 = await store.get_document(org_id, doc_id)
    assert db_doc2 is not None
    assert db_doc2.version == 2
    assert db_doc2.status == "active"

    # Old version 1 chunks were deleted atomically (R9.8)
    v1_remaining = await store.get_chunks_by_document(org_id, doc_id, version=1)
    assert len(v1_remaining) == 0

    # New version 2 chunks exist and are searchable
    v2_remaining = await store.get_chunks_by_document(org_id, doc_id, version=2)
    assert len(v2_remaining) == res2.total_chunks


@pytest.mark.asyncio
async def test_multi_tenant_isolation_three_tenants(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """GEMINI.md §8: Multi-tenant fixture seeds >=3 tenants with overlapping content."""
    store = PostgresKnowledgeStore(db_pool)
    embedder = FakeEmbedder(dimension=1536)
    chunker = StructuralChunker(ChunkerConfig(min_tokens=20, max_tokens=100))

    pipeline = KnowledgeIngestionPipeline(
        store=store,
        embedder=embedder,
        chunker=chunker,
    )

    org_ids = [uuid.uuid4() for _ in range(3)]
    for oid in org_ids:
        await ensure_test_org(db_pool, oid)

    # Ingest overlapping content into all 3 tenants
    shared_text = """# Confidential Company Guidelines

The company standard policy requires two-factor authentication for all remote access.
Security credentials must be rotated every ninety calendar days without exception.
"""

    doc_ids = []
    for oid in org_ids:
        did = uuid.uuid4()
        doc_ids.append(did)
        doc = KnowledgeDocument(
            id=did,
            organization_id=oid,
            title="Security Guidelines",
            category="security",
            status="pending",
        )
        await store.insert_document(doc)
        res = await pipeline.ingest_document(
            document_id=did,
            organization_id=oid,
            raw_bytes=shared_text.encode("utf-8"),
            filename="security.md",
        )
        assert res.status == "active"

    # Verify strict multi-tenant isolation (R5.3)
    for i, oid in enumerate(org_ids):
        # Tenant i can see their own document and chunks
        my_doc = await store.get_document(oid, doc_ids[i])
        assert my_doc is not None
        assert my_doc.organization_id == oid

        my_chunks = await store.get_chunks_by_document(oid, doc_ids[i])
        assert len(my_chunks) > 0
        for c in my_chunks:
            assert c.organization_id == oid

        # Tenant i cannot see documents of other tenants
        for j, other_did in enumerate(doc_ids):
            if i != j:
                other_doc = await store.get_document(oid, other_did)
                assert other_doc is None

                other_chunks = await store.get_chunks_by_document(oid, other_did)
                assert len(other_chunks) == 0

        # Lexical search for tenant i returns only tenant i chunks
        lex_results = await store.search_chunks_lexical(oid, query="authentication")
        assert len(lex_results) > 0
        for r in lex_results:
            assert r.organization_id == oid

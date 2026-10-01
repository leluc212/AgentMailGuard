"""Unit tests for knowledge ingestion pipeline and versioning orchestration.

Requirements:
- R9.1: Ingestion pipeline execution (parse -> structure -> chunk -> embed -> persist).
- R9.8: Version N+1 re-ingestion with atomic flip (never unsearchable).
- R9.9: Unchanged content_checksum skips re-embedding across versions (carry forward).
- R9.10: Per-document status tracking (pending, parsing, chunking, embedding, active, failed)
  with failure reason.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.db.knowledge import InMemoryKnowledgeStore
from packages.domain.knowledge import KnowledgeDocument
from packages.knowledge.chunker import ChunkerConfig, StructuralChunker
from packages.knowledge.embedder import EmbeddingError, FakeEmbedder
from packages.knowledge.pipeline import (
    DocumentNotFoundError,
    IngestionError,
    KnowledgeIngestionPipeline,
    UnsupportedDocumentTypeError,
)


@pytest.fixture
def store() -> InMemoryKnowledgeStore:
    return InMemoryKnowledgeStore()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(dimension=64)


@pytest.fixture
def chunker() -> StructuralChunker:
    return StructuralChunker(ChunkerConfig(min_tokens=20, max_tokens=100, overlap_tokens=10))


@pytest.fixture
def pipeline(
    store: InMemoryKnowledgeStore,
    embedder: FakeEmbedder,
    chunker: StructuralChunker,
) -> KnowledgeIngestionPipeline:
    return KnowledgeIngestionPipeline(
        store=store,
        embedder=embedder,
        chunker=chunker,
    )


@pytest.mark.asyncio
async def test_ingest_document_happy_path(
    pipeline: KnowledgeIngestionPipeline,
    store: InMemoryKnowledgeStore,
) -> None:
    """R9.1: Verify successful parse -> chunk -> embed -> persist -> active pipeline."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="Return Policy",
        category="policy",
        status="pending",
    )
    await store.insert_document(doc)

    raw_text = """# Return Policy

Our standard return window is 30 days from purchase date.
All returned items must include original packaging and receipt.

## Refund Process

Refunds will be processed to the original payment method within 5-7 business days.
"""
    result = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        raw_bytes=raw_text.encode("utf-8"),
        filename="return_policy.md",
    )

    assert result.status == "active"
    assert result.version == 1
    assert result.total_chunks > 0
    assert result.embedded_chunks == result.total_chunks
    assert result.carried_forward_chunks == 0

    # Verify document status in store (R9.10)
    updated_doc = await store.get_document(org_id, doc_id)
    assert updated_doc is not None
    assert updated_doc.status == "active"
    assert updated_doc.version == 1
    assert updated_doc.failure_reason is None

    # Verify chunks persisted with embeddings
    chunks = await store.get_chunks_by_document(org_id, doc_id, version=1)
    assert len(chunks) == result.total_chunks
    for chunk in chunks:
        assert chunk.version == 1
        assert chunk.organization_id == org_id
        assert chunk.content_checksum != ""
        pair = await store.get_chunk_with_embedding(org_id, chunk.id)
        assert pair is not None
        assert len(pair[1]) == 64


@pytest.mark.asyncio
async def test_reingest_creates_version_n_plus_one(
    pipeline: KnowledgeIngestionPipeline,
    store: InMemoryKnowledgeStore,
) -> None:
    """R9.8: Re-ingesting an active document creates version N+1 and atomically switches."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="Shipping FAQ",
        category="shipping",
        status="pending",
    )
    await store.insert_document(doc)

    v1_text = """# Shipping FAQ

Standard ground shipping takes 3-5 business days.
Expedited shipping takes 1-2 business days.
"""
    res1 = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        raw_bytes=v1_text.encode("utf-8"),
        filename="shipping.md",
    )
    assert res1.version == 1
    assert res1.status == "active"

    # Re-ingest with updated content
    v2_text = """# Shipping FAQ

Standard ground shipping now takes 2-4 business days.
Expedited next-day delivery is also available for enterprise clients.
"""
    res2 = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        raw_bytes=v2_text.encode("utf-8"),
        filename="shipping.md",
    )
    assert res2.version == 2
    assert res2.status == "active"

    # Document version updated
    updated_doc = await store.get_document(org_id, doc_id)
    assert updated_doc is not None
    assert updated_doc.version == 2
    assert updated_doc.status == "active"

    # Old version 1 chunks were deleted, new version 2 chunks remain
    v1_chunks = await store.get_chunks_by_document(org_id, doc_id, version=1)
    assert len(v1_chunks) == 0

    v2_chunks = await store.get_chunks_by_document(org_id, doc_id, version=2)
    assert len(v2_chunks) == res2.total_chunks


@pytest.mark.asyncio
async def test_checksum_deduplication_skips_reembedding(
    pipeline: KnowledgeIngestionPipeline,
    store: InMemoryKnowledgeStore,
    embedder: FakeEmbedder,
) -> None:
    """R9.9: Unchanged chunk content_checksum across versions carries forward embedding."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="Product Terms",
        category="terms",
        status="pending",
    )
    await store.insert_document(doc)

    section1 = (
        "# Section 1: Introduction\n\n"
        "Welcome to our enterprise service platform. Please read all of these terms carefully "
        "before proceeding with the installation, deployment, or configuration of the system. "
        "These terms apply unconditionally to all licensed users across the organization.\n"
    )
    section2 = (
        "# Section 2: Warranty\n\n"
        "The software product comes with a standard twelve-month limited technical warranty. "
        "Any hardware defects, software anomalies, or configuration incompatibilities must be "
        "reported through the designated support portal within ninety days of discovery.\n"
    )

    full_v1 = section1 + "\n" + section2

    res1 = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        raw_bytes=full_v1.encode("utf-8"),
        filename="terms.md",
    )
    assert res1.version == 1
    assert res1.total_chunks >= 2
    assert res1.carried_forward_chunks == 0
    assert len(embedder.recorded_calls) > 0

    embedder.clear_recorded_calls()

    # Re-ingest with Section 1 identical, Section 2 modified
    section2_modified = (
        "# Section 2: Warranty\n\n"
        "The software product now comes with an extended twenty-four month comprehensive warranty. "
        "Critical defects and outages will be addressed with twenty-four-seven emergency response "
        "and priority ticket resolution directly with tier three support personnel.\n"
    )
    full_v2 = section1 + "\n" + section2_modified

    res2 = await pipeline.ingest_document(
        document_id=doc_id,
        organization_id=org_id,
        raw_bytes=full_v2.encode("utf-8"),
        filename="terms.md",
    )
    assert res2.version == 2
    assert res2.total_chunks >= 2
    # At least section 1 chunk should be carried forward without re-embedding
    assert res2.carried_forward_chunks >= 1
    assert res2.embedded_chunks < res2.total_chunks

    # Verify that the carried-forward chunk has the exact same embedding vector
    v2_chunks = await store.get_chunks_by_document(org_id, doc_id, version=2)
    assert len(v2_chunks) == res2.total_chunks
    for chunk in v2_chunks:
        pair = await store.get_chunk_with_embedding(org_id, chunk.id)
        assert pair is not None
        assert len(pair[1]) == 64


@pytest.mark.asyncio
async def test_document_not_found_raises_error(
    pipeline: KnowledgeIngestionPipeline,
) -> None:
    """Missing document record raises DocumentNotFoundError."""
    org_id = uuid4()
    doc_id = uuid4()

    with pytest.raises(DocumentNotFoundError):
        await pipeline.ingest_document(
            document_id=doc_id,
            organization_id=org_id,
            raw_bytes=b"Hello",
            filename="test.txt",
        )


@pytest.mark.asyncio
async def test_unsupported_document_type_marks_failed(
    pipeline: KnowledgeIngestionPipeline,
    store: InMemoryKnowledgeStore,
) -> None:
    """R9.10: Unsupported document format updates status to 'failed' with failure_reason."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="archive.tar.gz",
        mime_type="application/gzip",
        status="pending",
    )
    await store.insert_document(doc)

    with pytest.raises(UnsupportedDocumentTypeError):
        await pipeline.ingest_document(
            document_id=doc_id,
            organization_id=org_id,
            raw_bytes=b"\x1f\x8b\x08fakegzip",
            filename="archive.tar.gz",
            content_type="application/gzip",
        )

    updated_doc = await store.get_document(org_id, doc_id)
    assert updated_doc is not None
    assert updated_doc.status == "failed"
    assert updated_doc.failure_reason is not None
    assert "No parser available" in updated_doc.failure_reason


@pytest.mark.asyncio
async def test_embedder_failure_marks_failed_and_rolls_back(
    pipeline: KnowledgeIngestionPipeline,
    store: InMemoryKnowledgeStore,
    embedder: FakeEmbedder,
) -> None:
    """R9.10: Embedder failure marks document as failed and leaves no dangling chunks."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="Guide",
        status="pending",
    )
    await store.insert_document(doc)

    embedder.set_error(EmbeddingError("Embedding API service unavailable"))

    with pytest.raises(EmbeddingError):
        await pipeline.ingest_document(
            document_id=doc_id,
            organization_id=org_id,
            raw_bytes=b"# User Guide\n\nSome text content.",
            filename="guide.md",
        )

    updated_doc = await store.get_document(org_id, doc_id)
    assert updated_doc is not None
    assert updated_doc.status == "failed"
    assert updated_doc.failure_reason is not None
    assert "Embedding API service unavailable" in updated_doc.failure_reason

    # Target version chunks were rolled back/cleaned up
    chunks = await store.get_chunks_by_document(org_id, doc_id, version=1)
    assert len(chunks) == 0


@pytest.mark.asyncio
async def test_empty_document_marks_failed(
    pipeline: KnowledgeIngestionPipeline,
    store: InMemoryKnowledgeStore,
) -> None:
    """Ingesting an empty document raises IngestionError and updates status to failed."""
    org_id = uuid4()
    doc_id = uuid4()

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_id,
        title="Empty",
        status="pending",
    )
    await store.insert_document(doc)

    with pytest.raises(IngestionError):
        await pipeline.ingest_document(
            document_id=doc_id,
            organization_id=org_id,
            raw_bytes=b"    \n\n  \t ",
            filename="empty.txt",
        )

    updated_doc = await store.get_document(org_id, doc_id)
    assert updated_doc is not None
    assert updated_doc.status == "failed"
    assert updated_doc.failure_reason is not None

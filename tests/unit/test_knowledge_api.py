"""Unit tests for knowledge upload API and status querying endpoints.

Requirements:
- R23.7: Knowledge upload flow showing per-document ingestion status.
- R23.2: Expose endpoints for knowledge documents.
- R5.8: Store uploaded source documents in MinIO object storage.
- R5.3, R23.6: Strict multi-tenant isolation with mandatory X-Organization-ID header.
- R9.10: Per-document status exposure (pending, parsing, chunking, embedding, active, failed).
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.core.storage import FakeObjectStorageClient, ObjectKeyBuilder
from packages.db.knowledge import InMemoryKnowledgeStore
from packages.domain.knowledge import KnowledgeDocument
from services.api.main import create_app


@pytest.fixture
def org_a() -> UUID:
    return uuid4()


@pytest.fixture
def org_b() -> UUID:
    return uuid4()


@pytest.fixture
def mock_publisher() -> MagicMock:
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    return publisher


@pytest.fixture
def test_app(mock_publisher: MagicMock) -> FastAPI:
    """Create test FastAPI app with in-memory knowledge store and fake storage client."""
    app = create_app(lifespan_enabled=False)
    app.state.knowledge_store = InMemoryKnowledgeStore()
    app.state.storage_client = FakeObjectStorageClient()
    app.state.publisher = mock_publisher
    return app


@pytest.fixture
async def client(test_app: FastAPI) -> Any:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_upload_document_happy_path(
    client: AsyncClient,
    test_app: FastAPI,
    org_a: UUID,
    mock_publisher: MagicMock,
) -> None:
    """R23.7, R5.8, R9.1: Multipart upload accepts document, writes to storage, and enqueues job."""
    file_bytes = b"# Refund Policy\n\nStandard 30 days return window."
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"title": "Refund Policy 2026", "category": "policy"},
        files={"file": ("refund_policy.md", io.BytesIO(file_bytes), "text/markdown")},
    )

    assert response.status_code == 202
    data = response.json()
    assert "document" in data
    assert "job_id" in data

    doc_data = data["document"]
    assert doc_data["title"] == "Refund Policy 2026"
    assert doc_data["category"] == "policy"
    assert doc_data["status"] == "pending"
    assert doc_data["version"] == 1
    assert doc_data["organization_id"] == str(org_a)
    assert doc_data["object_key"] is not None
    assert "refund_policy.md" in doc_data["object_key"]

    # Verify object storage write (R5.8)
    storage: FakeObjectStorageClient = test_app.state.storage_client
    stored_bytes = await storage.get_bytes("knowledge-docs", doc_data["object_key"])
    assert stored_bytes == file_bytes

    # Verify broker job enqueue (R3.1)
    mock_publisher.publish.assert_awaited_once()
    publish_args = mock_publisher.publish.await_args.kwargs
    assert publish_args["envelope"].job_type == "knowledge_ingest"
    assert publish_args["envelope"].payload["document_id"] == doc_data["id"]


@pytest.mark.asyncio
async def test_upload_unsupported_format_raises_400(
    client: AsyncClient,
    org_a: UUID,
) -> None:
    """R9.2: Uploading non-supported format (.exe) returns 400 Bad Request."""
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        files={"file": ("malicious.exe", io.BytesIO(b"\x4d\x5a\x90"), "application/x-msdownload")},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "UNSUPPORTED_DOCUMENT_FORMAT"


@pytest.mark.asyncio
async def test_upload_empty_file_raises_400(
    client: AsyncClient,
    org_a: UUID,
) -> None:
    """Empty files return 400 Bad Request."""
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        files={"file": ("empty.txt", io.BytesIO(b"   \n  "), "text/plain")},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "EMPTY_DOCUMENT"


@pytest.mark.asyncio
async def test_upload_reingest_increments_version(
    client: AsyncClient,
    test_app: FastAPI,
    org_a: UUID,
) -> None:
    """R9.8: Re-ingesting an existing document increments version to N+1."""
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    doc_id = uuid4()
    v1_doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_a,
        title="Terms of Service",
        category="legal",
        version=1,
        status="active",
    )
    await store.insert_document(v1_doc)

    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"document_id": str(doc_id)},
        files={"file": ("tos.md", io.BytesIO(b"# Updated Terms"), "text/markdown")},
    )
    assert response.status_code == 202
    data = response.json()
    assert data["document"]["id"] == str(doc_id)
    assert data["document"]["version"] == 2
    assert data["document"]["status"] == "pending"


@pytest.mark.asyncio
async def test_upload_reingest_nonexistent_doc_raises_404(
    client: AsyncClient,
    org_a: UUID,
) -> None:
    """Re-ingesting non-existent document ID returns 404 Not Found."""
    missing_id = uuid4()
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"document_id": str(missing_id)},
        files={"file": ("doc.txt", io.BytesIO(b"some content"), "text/plain")},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_list_documents_pagination_and_filtering(
    client: AsyncClient,
    test_app: FastAPI,
    org_a: UUID,
) -> None:
    """R23.7, R23.2: List knowledge documents with pagination and status/category filtering."""
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store

    doc1 = KnowledgeDocument(
        id=uuid4(),
        organization_id=org_a,
        title="Doc 1 Active Support",
        category="support",
        status="active",
    )
    doc2 = KnowledgeDocument(
        id=uuid4(),
        organization_id=org_a,
        title="Doc 2 Pending Billing",
        category="billing",
        status="pending",
    )
    doc3 = KnowledgeDocument(
        id=uuid4(),
        organization_id=org_a,
        title="Doc 3 Failed Support",
        category="support",
        status="failed",
        failure_reason="Corrupt PDF structure",
    )
    for d in [doc1, doc2, doc3]:
        await store.insert_document(d)

    # 1. List all for org_a
    r_all = await client.get(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_all.status_code == 200
    assert r_all.json()["total_count"] == 3
    assert len(r_all.json()["items"]) == 3

    # 2. Filter by status=active
    r_act = await client.get(
        "/v1/knowledge/documents?status=active",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_act.status_code == 200
    assert r_act.json()["total_count"] == 1
    assert r_act.json()["items"][0]["title"] == "Doc 1 Active Support"

    # 3. Filter by category=support
    r_sup = await client.get(
        "/v1/knowledge/documents?category=support",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_sup.status_code == 200
    assert r_sup.json()["total_count"] == 2

    # 4. Filter by status=failed to inspect failure_reason (R9.10)
    r_fail = await client.get(
        "/v1/knowledge/documents?status=failed",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_fail.status_code == 200
    assert r_fail.json()["items"][0]["failure_reason"] == "Corrupt PDF structure"


@pytest.mark.asyncio
async def test_get_document_details(
    client: AsyncClient,
    test_app: FastAPI,
    org_a: UUID,
) -> None:
    """R23.7, R9.10: Single document inspection returning full status."""
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    doc_id = uuid4()
    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_a,
        title="Security SLA",
        category="security",
        version=3,
        status="active",
    )
    await store.insert_document(doc)

    response = await client.get(
        f"/v1/knowledge/documents/{doc_id}",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(doc_id)
    assert data["status"] == "active"
    assert data["version"] == 3

    # Not found case
    r_missing = await client.get(
        f"/v1/knowledge/documents/{uuid4()}",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_missing.status_code == 404


@pytest.mark.asyncio
async def test_delete_document(
    client: AsyncClient,
    test_app: FastAPI,
    org_a: UUID,
) -> None:
    """Document deletion deletes DB records and storage objects."""
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    storage: FakeObjectStorageClient = test_app.state.storage_client

    doc_id = uuid4()
    object_key = f"knowledge/{org_a}/{doc_id}/v1/doc.md"
    await storage.put_bytes("knowledge-docs", object_key, b"# Data")

    doc = KnowledgeDocument(
        id=doc_id,
        organization_id=org_a,
        title="To Delete",
        object_key=object_key,
        status="active",
    )
    await store.insert_document(doc)

    # Delete
    r_del = await client.delete(
        f"/v1/knowledge/documents/{doc_id}",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_del.status_code == 204

    # Storage object deleted
    assert not await storage.object_exists("knowledge-docs", object_key)

    # Document in DB deleted
    r_get = await client.get(
        f"/v1/knowledge/documents/{doc_id}",
        headers={"X-Organization-ID": str(org_a)},
    )
    assert r_get.status_code == 404


@pytest.mark.asyncio
async def test_tenant_isolation(
    client: AsyncClient,
    test_app: FastAPI,
    org_a: UUID,
    org_b: UUID,
) -> None:
    """R5.3, R23.6: Strict tenant isolation prevents cross-tenant document visibility."""
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store

    doc_a = KnowledgeDocument(
        id=uuid4(),
        organization_id=org_a,
        title="Tenant A Confidential",
        status="active",
    )
    await store.insert_document(doc_a)

    # Tenant B listing cannot see Tenant A document
    r_list = await client.get(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_b)},
    )
    assert r_list.status_code == 200
    assert r_list.json()["total_count"] == 0

    # Tenant B direct access returns 404
    r_get = await client.get(
        f"/v1/knowledge/documents/{doc_a.id}",
        headers={"X-Organization-ID": str(org_b)},
    )
    assert r_get.status_code == 404

    # Tenant B delete returns 404
    r_del = await client.delete(
        f"/v1/knowledge/documents/{doc_a.id}",
        headers={"X-Organization-ID": str(org_b)},
    )
    assert r_del.status_code == 404


@pytest.mark.asyncio
async def test_missing_tenant_header_raises_400(
    client: AsyncClient,
) -> None:
    """R23.6: Missing X-Organization-ID header returns 400 Bad Request."""
    response = await client.get("/v1/knowledge/documents")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ORGANIZATION_ID_REQUIRED"


@pytest.mark.asyncio
async def test_upload_publish_failure_returns_503_and_marks_document_failed(
    client: AsyncClient, test_app: FastAPI, org_a: UUID, mock_publisher: MagicMock
) -> None:
    mock_publisher.publish.side_effect = ConnectionError("broker down")
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"title": "Policy"},
        files={"file": ("p.md", io.BytesIO(b"# Policy"), "text/markdown")},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "INGEST_ENQUEUE_FAILED"
    doc_id = UUID(body["detail"]["document_id"])
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    doc = await store.get_document(org_a, doc_id)
    assert doc is not None
    assert doc.status == "failed"
    assert doc.failure_reason is not None and "broker down" in doc.failure_reason


@pytest.mark.asyncio
async def test_reingest_publish_failure_keeps_previous_active_version(
    client: AsyncClient, test_app: FastAPI, org_a: UUID, mock_publisher: MagicMock
) -> None:
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    doc_id = uuid4()
    await store.insert_document(
        KnowledgeDocument(id=doc_id, organization_id=org_a, title="ToS", version=1, status="active")
    )
    mock_publisher.publish.side_effect = ConnectionError("broker down")
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"document_id": str(doc_id)},
        files={"file": ("tos.md", io.BytesIO(b"# New terms"), "text/markdown")},
    )
    assert response.status_code == 503
    doc = await store.get_document(org_a, doc_id)
    assert doc is not None and doc.status == "active" and doc.version == 1
    storage: FakeObjectStorageClient = test_app.state.storage_client
    orphan_key = ObjectKeyBuilder.knowledge_doc(
        organization_id=org_a, document_id=doc_id, version=2, filename="tos.md"
    )
    assert not await storage.object_exists("knowledge-docs", orphan_key)


@pytest.mark.asyncio
async def test_upload_without_publisher_returns_503(
    client: AsyncClient, test_app: FastAPI, org_a: UUID
) -> None:
    test_app.state.publisher = None
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        files={"file": ("p.md", io.BytesIO(b"# Policy"), "text/markdown")},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "INGEST_ENQUEUE_FAILED"

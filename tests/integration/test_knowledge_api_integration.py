"""Live integration tests for knowledge upload API and status querying endpoints.

Requirements:
- R23.7: Knowledge upload flow showing per-document ingestion status.
- R23.2: Knowledge document endpoints.
- R5.8: MinIO/S3 object storage persistence.
- R5.3, R23.6: Strict multi-tenant isolation across >=3 tenants.
- GEMINI.md §8: Ephemeral PostgreSQL (port 5433) and MinIO (port 9010).
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.core.settings import AppSettings
from packages.core.storage import (
    MinioObjectStorageClient,
    get_storage_client,
)
from packages.db.connection import create_pool_from_settings
from services.api.main import create_app


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide asyncpg connection pool to test PostgreSQL database."""
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


@pytest.fixture
def api_app(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> FastAPI:
    """Construct FastAPI application wired to live database pool and storage client."""
    app = create_app(lifespan_enabled=False)
    app.state.db_pool = db_pool
    app.state.storage_client = storage_client
    return app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _ensure_org(pool: asyncpg.Pool[Any], org_id: UUID) -> None:
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
async def test_live_knowledge_upload_lifecycle(
    client: AsyncClient,
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """Verify live multipart upload -> MinIO blob -> Postgres row -> list -> get -> delete."""
    await storage_client.bootstrap_buckets()
    org_id = uuid4()
    await _ensure_org(db_pool, org_id)

    file_content = (
        b"# Production Security Guidelines\n\n"
        b"1. Multi-factor authentication is strictly required.\n"
        b"2. Access tokens expire after eight hours.\n"
    )

    # 1. Upload via POST /v1/knowledge/documents (R23.7, R5.8)
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_id)},
        data={"title": "SecOps Guidelines", "category": "security"},
        files={"file": ("secops.md", io.BytesIO(file_content), "text/markdown")},
    )
    assert response.status_code == 202
    data = response.json()
    doc_id = data["document"]["id"]
    object_key = data["document"]["object_key"]

    assert data["document"]["title"] == "SecOps Guidelines"
    assert data["document"]["status"] == "pending"
    assert data["document"]["version"] == 1
    assert "secops.md" in object_key

    # 2. Verify object stored in live MinIO (R5.8)
    bucket = storage_client.settings.bucket_knowledge
    assert await storage_client.object_exists(bucket, object_key)
    downloaded = await storage_client.get_bytes(bucket, object_key)
    assert downloaded == file_content

    # 3. Verify record in live PostgreSQL
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_document WHERE id = $1 AND organization_id = $2",
            UUID(doc_id),
            org_id,
        )
        assert row is not None
        assert row["title"] == "SecOps Guidelines"
        assert row["status"] == "pending"
        assert row["version"] == 1

    # 4. Query list via GET /v1/knowledge/documents (R23.7, R23.2)
    list_res = await client.get(
        "/v1/knowledge/documents?category=security",
        headers={"X-Organization-ID": str(org_id)},
    )
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total_count"] >= 1
    assert any(d["id"] == doc_id for d in list_data["items"])

    # 5. Query details via GET /v1/knowledge/documents/{id}
    detail_res = await client.get(
        f"/v1/knowledge/documents/{doc_id}",
        headers={"X-Organization-ID": str(org_id)},
    )
    assert detail_res.status_code == 200
    assert detail_res.json()["id"] == doc_id
    assert detail_res.json()["status"] == "pending"

    # 6. Delete document via DELETE /v1/knowledge/documents/{id}
    del_res = await client.delete(
        f"/v1/knowledge/documents/{doc_id}",
        headers={"X-Organization-ID": str(org_id)},
    )
    assert del_res.status_code == 204

    # Verify deleted in MinIO and PostgreSQL
    assert not await storage_client.object_exists(bucket, object_key)
    async with db_pool.acquire() as conn:
        check_row = await conn.fetchrow(
            "SELECT * FROM knowledge_document WHERE id = $1 AND organization_id = $2",
            UUID(doc_id),
            org_id,
        )
        assert check_row is None


@pytest.mark.asyncio
async def test_live_knowledge_multi_tenant_isolation_three_tenants(
    client: AsyncClient,
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """GEMINI.md §8: Multi-tenant fixture seeds >=3 tenants and verifies strict isolation."""
    await storage_client.bootstrap_buckets()
    org_ids = [uuid4() for _ in range(3)]
    for oid in org_ids:
        await _ensure_org(db_pool, oid)

    doc_ids: list[str] = []

    # Upload distinct documents to all 3 tenants
    for i, oid in enumerate(org_ids):
        content = f"# Tenant {i} Confidential Document\n\nSpecific tenant {i} policy.".encode()
        res = await client.post(
            "/v1/knowledge/documents",
            headers={"X-Organization-ID": str(oid)},
            data={"title": f"Doc Tenant {i}", "category": "isolated"},
            files={"file": (f"doc_{i}.md", io.BytesIO(content), "text/markdown")},
        )
        assert res.status_code == 202
        doc_ids.append(res.json()["document"]["id"])

    # Verify tenant isolation (R5.3, R23.6)
    for i, oid in enumerate(org_ids):
        # Tenant i sees only their own documents
        list_res = await client.get(
            "/v1/knowledge/documents?category=isolated",
            headers={"X-Organization-ID": str(oid)},
        )
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == doc_ids[i]
        assert items[0]["organization_id"] == str(oid)

        # Tenant i direct access to other tenants' docs returns 404
        for j, other_doc_id in enumerate(doc_ids):
            if i != j:
                cross_res = await client.get(
                    f"/v1/knowledge/documents/{other_doc_id}",
                    headers={"X-Organization-ID": str(oid)},
                )
                assert cross_res.status_code == 404

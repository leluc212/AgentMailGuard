"""Integration tests for REST API against live container infrastructure.

Tests:
- Full application lifespan startup and shutdown with live PostgreSQL.
- Database connection pool registration and readiness reporting (/readyz).
- End-to-end request processing with tenant scoping and OpenTelemetry tracing.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from packages.core.settings import APISettings
from services.api.main import create_app


@pytest.fixture
async def live_api_client() -> AsyncIterator[AsyncClient]:
    """Create an AsyncClient with full application lifespan connected to live database."""
    settings = APISettings()
    # Configure connection to live PostgreSQL container on port 5433
    settings.database.host = "localhost"
    settings.database.port = 5433
    settings.database.name = "rag_email"
    settings.database.user = "postgres"
    settings.database.password = "postgres"

    app = create_app(settings=settings, lifespan_enabled=True)

    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client


class TestLiveAPIIntegration:
    """Verify API application against live PostgreSQL container."""

    async def test_live_readiness_with_database(self, live_api_client: AsyncClient) -> None:
        """Verify that /readyz queries live database and returns 200 healthy."""
        response = await live_api_client.get("/readyz")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "ok"
        assert "database" in data["checks"]
        assert data["checks"]["database"] == "Database connection healthy"

    async def test_live_tenant_request_lifecycle(self, live_api_client: AsyncClient) -> None:
        """Verify full tenant-scoped request cycle with trace context against live app."""
        org_id = uuid4()
        trace_hex = "0af7651916cd43dd8448eb211c80319c"
        traceparent = f"00-{trace_hex}-b7ad6b7169203331-01"

        response = await live_api_client.get(
            "/v1/ping",
            headers={
                "X-Organization-ID": str(org_id),
                "traceparent": traceparent,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "ok"
        assert data["organization_id"] == str(org_id)
        assert response.headers.get("X-Trace-ID") == trace_hex

    async def test_live_paginated_sample_items(self, live_api_client: AsyncClient) -> None:
        """Verify paginated response parsing against live application."""
        org_id = uuid4()
        response = await live_api_client.get(
            "/v1/sample-items?limit=25&offset=50",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 25
        assert data["total_count"] == 120
        assert data["limit"] == 25
        assert data["offset"] == 50
        assert data["has_more"] is True

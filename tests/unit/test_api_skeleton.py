"""Unit tests for REST API skeleton, OpenAPI generation, pagination, and tenant scoping.

Covers:
- R23.1: Versioned REST API (/v1/...) with generated OpenAPI 3.1 documentation.
- R23.6: Mandatory organization_id scoping on all requests and pagination on list endpoints.
- R20.7: Un-scoped health/readiness endpoints at root.
- R21.1-R21.3: Trace context propagation and correlation logging.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from packages.core.pagination import PageParams, paginate
from services.api.main import create_app
from services.api.openapi import generate_openapi_spec, validate_openapi_spec


@pytest.fixture
def api_app() -> FastAPI:
    """Create a hermetic FastAPI application instance with lifespan disabled."""
    return create_app(lifespan_enabled=False)


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Async HTTP test client bound to the hermetic API app."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


class TestOpenAPIAndDocs:
    """Validate OpenAPI 3.1 specification generation and documentation endpoints (R23.1)."""

    def test_generate_and_validate_openapi_spec(self, api_app: FastAPI) -> None:
        spec = generate_openapi_spec(api_app)
        assert spec["openapi"] == "3.1.0"
        assert spec["info"]["title"] == "Enterprise RAG Email API"
        assert spec["info"]["version"] == "0.1.0"

        # Validate schema structure
        valid, errors = validate_openapi_spec(spec)
        assert valid, f"OpenAPI validation errors: {errors}"

        # Verify key endpoints registered
        paths = spec["paths"]
        assert "/healthz" in paths
        assert "/readyz" in paths
        assert "/metrics" in paths
        assert "/v1/ping" in paths
        assert "/v1/sample-items" in paths

    async def test_openapi_json_endpoint(self, client: AsyncClient) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["openapi"] == "3.1.0"
        assert "/v1/ping" in data["paths"]

    async def test_docs_and_redoc_endpoints(self, client: AsyncClient) -> None:
        docs_resp = await client.get("/docs")
        assert docs_resp.status_code == status.HTTP_200_OK
        assert "text/html" in docs_resp.headers["content-type"]

        redoc_resp = await client.get("/redoc")
        assert redoc_resp.status_code == status.HTTP_200_OK
        assert "text/html" in redoc_resp.headers["content-type"]


class TestRootObservabilityEndpoints:
    """Verify that root health and telemetry endpoints are un-scoped (R20.7, R21.4)."""

    async def test_healthz_unscoped(self, client: AsyncClient) -> None:
        # Should succeed without any X-Organization-ID header
        response = await client.get("/healthz")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "ok"

    async def test_readyz_unscoped(self, client: AsyncClient) -> None:
        # Should succeed without any X-Organization-ID header
        response = await client.get("/readyz")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "ok"

    async def test_metrics_unscoped(self, client: AsyncClient) -> None:
        # Should succeed without any X-Organization-ID header
        response = await client.get("/metrics")
        assert response.status_code == status.HTTP_200_OK
        assert "text/plain" in response.headers["content-type"]


class TestTenantScoping:
    """Validate mandatory organization_id scoping on /v1 routes (R23.6, R5.3)."""

    async def test_missing_organization_id_fails(self, client: AsyncClient) -> None:
        response = await client.get("/v1/ping")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert body["code"] == "ORGANIZATION_ID_REQUIRED"
        assert "X-Organization-ID" in body["error"]

    async def test_invalid_uuid_fails(self, client: AsyncClient) -> None:
        response = await client.get(
            "/v1/ping",
            headers={"X-Organization-ID": "not-a-valid-uuid"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert body["code"] == "INVALID_ORGANIZATION_ID"

    async def test_valid_uuid_header_succeeds(self, client: AsyncClient) -> None:
        org_id = uuid4()
        response = await client.get(
            "/v1/ping",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "ok"
        assert body["organization_id"] == str(org_id)

    async def test_valid_uuid_query_param_succeeds(self, client: AsyncClient) -> None:
        org_id = uuid4()
        response = await client.get(f"/v1/ping?organization_id={org_id}")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "ok"
        assert body["organization_id"] == str(org_id)


class TestPaginationHelper:
    """Validate pagination models, query parsing, and response envelope (R23.6)."""

    def test_core_paginate_computation(self) -> None:
        params = PageParams(limit=10, offset=0)
        resp = paginate(items=["a", "b"], total_count=20, params=params)
        assert resp.total_count == 20
        assert resp.limit == 10
        assert resp.offset == 0
        assert resp.has_more is True

        # Last page
        last_params = PageParams(limit=10, offset=15)
        last_resp = paginate(items=["c", "d", "e", "f", "g"], total_count=20, params=last_params)
        assert last_resp.has_more is False

    async def test_api_sample_items_default_pagination(self, client: AsyncClient) -> None:
        org_id = uuid4()
        response = await client.get(
            "/v1/sample-items",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body["items"]) == 50
        assert body["total_count"] == 120
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert body["has_more"] is True
        assert body["items"][0] == f"tenant-{org_id}-item-0"

    async def test_api_sample_items_custom_pagination(self, client: AsyncClient) -> None:
        org_id = uuid4()
        response = await client.get(
            "/v1/sample-items?limit=10&offset=115",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body["items"]) == 5
        assert body["limit"] == 10
        assert body["offset"] == 115
        assert body["has_more"] is False

    async def test_api_pagination_bounds_validation(self, client: AsyncClient) -> None:
        org_id = uuid4()
        headers = {"X-Organization-ID": str(org_id)}

        # limit < 1
        r1 = await client.get("/v1/sample-items?limit=0", headers=headers)
        assert r1.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        # limit > 100
        r2 = await client.get("/v1/sample-items?limit=101", headers=headers)
        assert r2.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        # offset < 0
        r3 = await client.get("/v1/sample-items?offset=-1", headers=headers)
        assert r3.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestTraceContextAndErrors:
    """Validate W3C trace propagation and structured error formats (R21.1-R21.3)."""

    async def test_traceparent_header_propagation(self, client: AsyncClient) -> None:
        trace_id_hex = "4bf92f3577b34da6a3ce929d0e0e4736"
        traceparent = f"00-{trace_id_hex}-00f067aa0ba902b7-01"
        org_id = uuid4()

        response = await client.get(
            "/v1/ping",
            headers={
                "X-Organization-ID": str(org_id),
                "traceparent": traceparent,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.headers.get("X-Trace-ID") == trace_id_hex

    async def test_not_found_error_schema(self, client: AsyncClient) -> None:
        response = await client.get("/v1/nonexistent-endpoint")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        body = response.json()
        assert body["code"] == "HTTP_404"
        assert "error" in body

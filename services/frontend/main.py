"""Review UI service entrypoint (R23.4, R23.5, R23.7; design.md §5.8; ADR-0009).

Server-rendered FastAPI + Jinja2 + htmx pages. The service holds no database or broker
connection: every read and decision is a /v1 call carrying X-Organization-Id (R23.6). It has
no login, so docker-compose publishes it on 127.0.0.1 only (ADR-0009).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from packages.core.settings import FrontendServiceSettings
from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.logging import setup_logging
from services.frontend.api_client import ApiError, ReviewApiClient, build_http_client
from services.frontend.drafts import drafts_router
from services.frontend.knowledge import knowledge_router
from services.frontend.timeline import timeline_router
from services.frontend.web import api_error_handler

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(
    settings: FrontendServiceSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    configure_logging: bool = True,
) -> FastAPI:
    """Build the review UI; ``transport`` lets tests route /v1 calls in-process."""
    active = settings or FrontendServiceSettings()
    http = build_http_client(active.frontend, transport=transport)
    api = ReviewApiClient(http)
    health = HealthRegistry(service_name=active.service_name)

    async def check_api() -> tuple[bool, str]:
        try:
            response = await http.get("/healthz")
        except httpx.HTTPError as exc:
            return False, f"API unreachable: {exc}"
        return response.status_code == 200, f"API /healthz returned {response.status_code}"

    health.register_readiness_check("api", check_api)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if configure_logging:
            setup_logging(
                level=active.telemetry.log_level,
                json_format=active.telemetry.log_format == "json",
            )
        try:
            yield
        finally:
            await api.aclose()

    app = FastAPI(
        title="Email review UI",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = active
    app.state.api = api
    app.state.organization_id = active.frontend.organization_id
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_health_router(health))
    app.include_router(drafts_router)
    app.include_router(timeline_router)
    app.include_router(knowledge_router)
    app.add_exception_handler(ApiError, api_error_handler)
    return app


app = create_app()

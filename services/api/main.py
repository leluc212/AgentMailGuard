"""FastAPI application entrypoint and factory.

Implements REST API skeleton with /v1 prefix, OpenAPI 3.1 generation,
mandatory tenant scoping, and observability integration per R23.1, R23.6, and R21.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from packages.core.settings import APISettings
from packages.db.connection import create_pool_from_settings
from packages.observability.context import bind_log_context, get_correlation_context
from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.logging import setup_logging
from packages.observability.tracing import (
    extract_trace_context,
    get_current_trace_id,
    init_tracer,
    trace_span,
)
from services.api.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from services.api.routers.v1 import v1_router

logger = logging.getLogger("api.server")


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Middleware extracting W3C trace context and binding request correlation IDs."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        headers_dict = dict(request.headers)
        parent_ctx = extract_trace_context(headers_dict)

        span_name = f"HTTP {request.method} {request.url.path}"
        with trace_span(
            span_name,
            attributes={
                "http.method": request.method,
                "http.url": str(request.url),
                "http.target": request.url.path,
            },
            parent_context=parent_ctx,
        ):
            trace_id = get_current_trace_id() or headers_dict.get("x-request-id")
            with bind_log_context(trace_id=trace_id):
                response = await call_next(request)
                active_trace = trace_id or get_correlation_context().get("trace_id")
                if active_trace:
                    response.headers["X-Trace-ID"] = active_trace
                return response


def create_app(
    settings: APISettings | None = None,
    lifespan_enabled: bool = True,
    health_registry: HealthRegistry | None = None,
) -> FastAPI:
    """Construct and configure the FastAPI application."""
    active_settings = settings or APISettings()
    reg = health_registry or HealthRegistry(service_name=active_settings.service_name)

    @asynccontextmanager
    async def app_lifespan(app_instance: FastAPI) -> AsyncIterator[dict[str, Any]]:
        if not lifespan_enabled:
            app_instance.state.db_pool = None
            yield {}
            return

        # 1. Initialize logging & OpenTelemetry tracer
        setup_logging(
            level=active_settings.telemetry.log_level,
            json_format=active_settings.telemetry.log_format == "json",
        )
        init_tracer(
            active_settings.service_name,
            otlp_endpoint=active_settings.telemetry.otlp_endpoint,
        )
        logger.info("Initializing API application lifespan")

        # 2. Initialize Database Pool
        db_pool = None
        try:
            db_pool = await create_pool_from_settings(active_settings.database)
            app_instance.state.db_pool = db_pool

            async def check_db() -> tuple[bool, str]:
                try:
                    async with db_pool.acquire() as conn:
                        res = await conn.fetchval("SELECT 1")
                        return (res == 1, "Database connection healthy")
                except Exception as err:
                    return (False, f"Database check failed: {err}")

            reg.register_readiness_check("database", check_db)
            logger.info("Database connection pool established successfully")
        except Exception as exc:
            logger.warning("Database pool initialization deferred or failed: %s", exc)
            app_instance.state.db_pool = None

        yield {"db_pool": db_pool}

        # 3. Shutdown cleanup
        logger.info("Shutting down API application lifespan")
        if db_pool is not None:
            await db_pool.close()
            logger.info("Database connection pool closed")

    app = FastAPI(
        title=active_settings.title,
        version=active_settings.version,
        description=active_settings.description,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=app_lifespan,
    )

    # Middleware registration
    app.add_middleware(TraceContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Custom structured exception handlers
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Mount un-scoped health/readiness/metrics at root
    app.include_router(create_health_router(health_registry=reg))

    # Mount scoped /v1 API router
    app.include_router(v1_router)

    return app


# Default ASGI application instance for uvicorn
app = create_app()

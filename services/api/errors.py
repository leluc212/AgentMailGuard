"""Standard API error schemas and exception handlers.

Provides structured error response schemas according to R23.1 and R21.3,
ensuring consistent JSON error envelopes across all endpoints.
"""

import logging
from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from packages.observability.context import get_correlation_context
from packages.observability.tracing import get_current_trace_id

logger = logging.getLogger("api.errors")


class APIErrorResponse(BaseModel):
    """Standard error response model for all API errors."""

    error: str = Field(description="High-level error message or category.")
    code: str = Field(description="Machine-readable error code.")
    detail: Any | None = Field(
        default=None,
        description="Optional detailed error context, debug hints, or validation failures.",
    )
    request_id: str | None = Field(
        default=None,
        description="Correlation trace ID for request tracking.",
    )


def _get_request_id() -> str | None:
    """Extract current trace ID or correlation trace_id."""
    trace_id = get_current_trace_id()
    if trace_id:
        return trace_id
    ctx = get_correlation_context()
    return ctx.get("trace_id")


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle standard FastAPI/Starlette HTTPExceptions."""
    request_id = _get_request_id()
    if isinstance(exc, StarletteHTTPException):
        status_code = exc.status_code
        detail = exc.detail
        headers = getattr(exc, "headers", None)
    else:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = str(exc)
        headers = None

    if isinstance(detail, dict):
        error_msg = detail.get("error", detail.get("message", "HTTP Error"))
        code = detail.get("code", f"HTTP_{status_code}")
        detail_data = detail.get("detail", detail)
    else:
        error_msg = str(detail) if detail else "HTTP Error"
        code = f"HTTP_{status_code}"
        detail_data = None

    payload = APIErrorResponse(
        error=error_msg,
        code=code,
        detail=detail_data,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload.model_dump()),
        headers=headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Handle Pydantic request validation errors."""
    request_id = _get_request_id()
    raw_errors = exc.errors() if isinstance(exc, RequestValidationError) else str(exc)
    errors = jsonable_encoder(raw_errors)
    payload = APIErrorResponse(
        error="Request validation failed",
        code="VALIDATION_ERROR",
        detail=errors,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=jsonable_encoder(payload.model_dump()),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected server errors."""
    request_id = _get_request_id()
    logger.exception(
        "Unhandled server error processing request: %s %s",
        request.method,
        request.url,
    )
    payload = APIErrorResponse(
        error="Internal Server Error",
        code="INTERNAL_SERVER_ERROR",
        detail="An unexpected error occurred. Use request_id to correlate logs.",
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=payload.model_dump(),
    )

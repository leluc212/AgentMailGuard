"""Shared helpers for the review UI routes (R23.4, R23.6; ADR-0009)."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from services.frontend.api_client import ApiError, ReviewApiClient

MISSING_ORGANIZATION = (
    "FRONTEND__ORGANIZATION_ID is not set. Set it in .env to the organization whose drafts "
    "you review, then restart the frontend."
)


def require_organization(request: Request) -> None:
    """Refuse to call /v1 without a tenant: the API requires X-Organization-Id (R23.6)."""
    if request.app.state.organization_id is None:
        raise ApiError(503, "ORGANIZATION_NOT_CONFIGURED", MISSING_ORGANIZATION)


def api_client(request: Request) -> ReviewApiClient:
    client: ReviewApiClient = request.app.state.api
    return client


def templates(request: Request) -> Jinja2Templates:
    loaded: Jinja2Templates = request.app.state.templates
    return loaded


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


async def api_error_handler(request: Request, exc: Exception) -> Response:
    """htmx: announce the error in the live region and swap nothing; pages: an error page."""
    if not isinstance(exc, ApiError):
        raise exc
    loaded = templates(request)
    if is_htmx(request):
        return loaded.TemplateResponse(
            request,
            "_status.html",
            {"status_message": f"Error: {exc.message}"},
            headers={"HX-Reswap": "none"},
        )
    return loaded.TemplateResponse(
        request, "error.html", {"error": exc}, status_code=exc.status_code
    )

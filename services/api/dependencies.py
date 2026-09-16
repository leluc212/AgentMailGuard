"""FastAPI dependencies for tenant scoping and resource access.

Implements mandatory organization_id scoping per R23.6 and R5.3, binding
tenant context to async correlation logging (R21.3).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import Depends, Header, HTTPException, Query, Request, status

from packages.observability.context import bind_log_context


@dataclass(frozen=True)
class TenantContext:
    """Encapsulates authenticated tenant context for request processing."""

    organization_id: UUID


def get_organization_id(
    x_organization_id: Annotated[
        str | None,
        Header(
            alias="X-Organization-ID",
            description="Tenant organization UUID scoping all operations.",
        ),
    ] = None,
    organization_id: Annotated[
        str | None,
        Query(
            alias="organization_id",
            description="Tenant organization UUID scoping all operations (query fallback).",
        ),
    ] = None,
) -> UUID:
    """Extract and validate mandatory organization_id from header or query param.

    Raises:
        HTTPException: 400 Bad Request if missing or invalid UUID format.
    """
    raw_id = x_organization_id or organization_id
    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": (
                    "Missing mandatory organization_id scoping header "
                    "'X-Organization-ID' or query parameter"
                ),
                "code": "ORGANIZATION_ID_REQUIRED",
            },
        )

    try:
        parsed_uuid = UUID(raw_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": f"Invalid organization_id format: '{raw_id}'. Must be a valid UUID.",
                "code": "INVALID_ORGANIZATION_ID",
            },
        ) from None

    # Bind organization_id into async contextvars for structured logging & correlation
    bind_log_context(organization_id=str(parsed_uuid))

    return parsed_uuid


def get_tenant_context(
    org_id: Annotated[UUID, Depends(get_organization_id)],
) -> TenantContext:
    """Construct TenantContext from resolved organization_id."""
    return TenantContext(organization_id=org_id)


async def get_db_connection(request: Request) -> AsyncIterator[asyncpg.Connection | None]:
    """Provide an asyncpg database connection from app pool if available."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        async with pool.acquire() as conn:
            yield conn
    else:
        yield None

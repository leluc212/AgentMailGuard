"""Version 1 API router definition.

Enforces mandatory organization_id scoping across all /v1 endpoints (R23.6, R5.3)
and provides standard pagination support.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from packages.core.pagination import PaginatedResponse, paginate
from services.api.dependencies import get_organization_id
from services.api.pagination import PaginationParamsDep
from services.api.routers.jobs import jobs_router
from services.api.routers.mailboxes import mailbox_router
from services.api.routers.messages import message_router
from services.api.routers.threads import thread_router

v1_router = APIRouter(
    prefix="/v1",
    tags=["v1"],
    dependencies=[Depends(get_organization_id)],
)
v1_router.include_router(mailbox_router)
v1_router.include_router(thread_router)
v1_router.include_router(message_router)
v1_router.include_router(jobs_router)


class PingResponse(BaseModel):
    """Response schema for tenant ping endpoint."""

    status: str = Field(default="ok", description="Health status.")
    organization_id: UUID = Field(description="Resolved organization ID.")


@v1_router.get(
    "/ping",
    summary="Tenant Ping",
    description="Verify tenant API availability and validate organization scoping.",
    response_model=PingResponse,
)
async def ping(
    org_id: Annotated[UUID, Depends(get_organization_id)],
) -> PingResponse:
    """Return healthy status with verified tenant UUID."""
    return PingResponse(status="ok", organization_id=org_id)


@v1_router.get(
    "/sample-items",
    summary="Paginated Sample List",
    description="Demonstrates standardized pagination and tenant scoping per R23.6.",
    response_model=PaginatedResponse[str],
)
async def sample_items(
    org_id: Annotated[UUID, Depends(get_organization_id)],
    pagination: PaginationParamsDep,
) -> PaginatedResponse[str]:
    """Return a paginated list demonstrating PaginatedResponse and PageParams."""
    total_records = 120
    items = [
        f"tenant-{org_id}-item-{i}"
        for i in range(
            pagination.offset,
            min(pagination.offset + pagination.limit, total_records),
        )
    ]
    return paginate(items=items, total_count=total_records, params=pagination)

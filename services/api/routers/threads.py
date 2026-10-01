"""Email thread management and query endpoints (R23.2, R23.6, R5.3).

Provides:
- GET /v1/threads: paginated thread listing with optional mailbox and status filters.
- GET /v1/threads/{id}: thread detail view with all messages in chronological order.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from packages.core.pagination import PaginatedResponse, paginate
from packages.observability.context import bind_log_context
from services.api.dependencies import (
    MessageStoreDep,
    ThreadStoreDep,
    get_organization_id,
)
from services.api.pagination import PaginationParamsDep
from services.api.schemas.threads import (
    MessageSummaryInThread,
    ThreadDetailResponse,
    ThreadSummaryResponse,
)

logger = logging.getLogger("api.threads")

thread_router = APIRouter(prefix="/threads", tags=["threads"])


@thread_router.get(
    "",
    summary="List Email Threads",
    description=(
        "Fetch paginated list of email threads for the organization with optional "
        "mailbox and status filtering, ordered by last_message_at DESC (R23.2, R23.6)."
    ),
    response_model=PaginatedResponse[ThreadSummaryResponse],
)
async def list_threads(
    org_id: Annotated[UUID, Depends(get_organization_id)],
    thread_store: ThreadStoreDep,
    pagination: PaginationParamsDep,
    mailbox_id: Annotated[
        UUID | None,
        Query(description="Filter threads belonging to a specific mailbox UUID"),
    ] = None,
    status: Annotated[
        str | None,
        Query(description="Filter threads by operational status: 'open', 'closed', 'snoozed'"),
    ] = None,
) -> PaginatedResponse[ThreadSummaryResponse]:
    """Fetch paginated email threads strictly scoped to the requesting organization."""
    bind_log_context(organization_id=str(org_id))

    threads, total = await thread_store.list_threads(
        organization_id=org_id,
        limit=pagination.limit,
        offset=pagination.offset,
        mailbox_id=mailbox_id,
        status=status,
    )

    items = [
        ThreadSummaryResponse(
            id=UUID(str(t.id)),
            organization_id=UUID(str(t.organization_id)),
            mailbox_id=UUID(str(t.mailbox_id)),
            provider_thread_id=t.provider_thread_id,
            subject_normalized=t.subject_normalized,
            participants=list(t.participants),
            first_message_at=t.first_message_at,
            last_message_at=t.last_message_at,
            message_count=t.message_count,
            status=t.status,
        )
        for t in threads
    ]

    return paginate(items=items, total_count=total, params=pagination)


@thread_router.get(
    "/{id}",
    summary="Get Thread Details",
    description=(
        "Fetch detailed email thread metadata along with its complete list of messages "
        "ordered chronologically (received_at ASC) per R23.2 and R23.6."
    ),
    response_model=ThreadDetailResponse,
)
async def get_thread(
    id: Annotated[UUID, Path(description="Target thread unique identifier.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    thread_store: ThreadStoreDep,
    message_store: MessageStoreDep,
) -> ThreadDetailResponse:
    """Fetch thread metadata and chronological messages ensuring tenant scoping."""
    bind_log_context(organization_id=str(org_id))

    thread = await thread_store.get_thread(org_id, id)
    if thread is None or str(thread.organization_id) != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Thread '{id}' not found",
                "code": "THREAD_NOT_FOUND",
            },
        )

    # Fetch associated messages in thread ordered by received_at ASC
    messages = await message_store.get_messages_by_thread(org_id, id)

    msg_summaries = [
        MessageSummaryInThread(
            id=UUID(str(m.message_id)),
            sender_email=m.sender.email if m.sender else None,
            sender_name=m.sender.name if m.sender else None,
            subject=m.subject,
            snippet=m.snippet,
            received_at=m.received_at,
            direction=m.direction,
            has_attachments=len(m.attachments) > 0,
            normalization_failed=m.normalization_failed,
        )
        for m in messages
    ]

    return ThreadDetailResponse(
        id=UUID(str(thread.id)),
        organization_id=UUID(str(thread.organization_id)),
        mailbox_id=UUID(str(thread.mailbox_id)),
        provider_thread_id=thread.provider_thread_id,
        subject_normalized=thread.subject_normalized,
        participants=list(thread.participants),
        first_message_at=thread.first_message_at,
        last_message_at=thread.last_message_at,
        message_count=thread.message_count,
        status=thread.status,
        messages=msg_summaries,
    )

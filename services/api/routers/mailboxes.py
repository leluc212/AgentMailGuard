"""Mailbox management and manual re-sync endpoints (R2.11, R23.2, R23.6, R5.3).

Provides POST /v1/mailboxes/{id}/resync with optional time window filtering,
asynchronous job enqueuing to mail.ingest, and strict tenant isolation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status

from packages.broker.envelope import JobEnvelope
from packages.core.pagination import PaginatedResponse, paginate
from packages.core.settings import AppSettings
from packages.observability.context import bind_log_context
from packages.observability.tracing import get_current_trace_id, trace_span
from services.api.dependencies import (
    MailboxStoreDep,
    PublisherDep,
    get_organization_id,
)
from services.api.pagination import PaginationParamsDep
from services.api.schemas.mailboxes import (
    MailboxResponse,
    ResyncRequest,
    ResyncResponse,
    TimeWindow,
)

logger = logging.getLogger(__name__)

mailbox_router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


@mailbox_router.post(
    "/{id}/resync",
    summary="Manual Mailbox Re-Sync",
    description=(
        "Trigger an asynchronous re-synchronization for a mailbox with an optional "
        "time window (since, until) and full_resync override per R2.11 and R23.2."
    ),
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResyncResponse,
)
async def resync_mailbox(
    id: Annotated[UUID, Path(description="Mailbox unique identifier to re-sync.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    mailbox_store: MailboxStoreDep,
    publisher: PublisherDep,
    request: Annotated[
        ResyncRequest | None,
        Body(description="Optional time window and resync flags."),
    ] = None,
) -> ResyncResponse:
    """Validate mailbox existence and tenant ownership, then enqueue a sync job."""
    if request is None:
        request = ResyncRequest()
    bind_log_context(mailbox_id=str(id), organization_id=str(org_id))

    with trace_span(
        "api.mailbox.resync",
        attributes={
            "mailbox_id": str(id),
            "organization_id": str(org_id),
            "full_resync": request.full_resync,
            "force": request.force,
        },
    ):
        # 1. Fetch mailbox and verify tenant ownership (R5.3, R23.6)
        mailbox = await mailbox_store.get(id)
        if mailbox is None or str(mailbox.organization_id) != str(org_id):
            logger.warning("Mailbox %s not found for organization %s", id, org_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": f"Mailbox '{id}' not found",
                    "code": "MAILBOX_NOT_FOUND",
                },
            )

        # 2. Check operational status guard (R1.5, R2.10)
        if mailbox.status == "needs_reauth" and not request.force:
            logger.warning(
                "Mailbox %s is in 'needs_reauth' status; rejecting resync without force flag",
                id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": (
                        f"Mailbox '{id}' requires re-authentication (status: needs_reauth). "
                        "Resolve credentials or provide force=true to retry sync."
                    ),
                    "code": "MAILBOX_NEEDS_REAUTH",
                },
            )

        if mailbox.status == "paused" and not request.force:
            logger.warning("Mailbox %s is paused; rejecting resync without force flag", id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": f"Mailbox '{id}' is paused. Set force=true to proceed.",
                    "code": "MAILBOX_PAUSED",
                },
            )

        # If forced and was in needs_reauth, transition back to active
        if request.force and mailbox.status in ("needs_reauth", "paused"):
            await mailbox_store.update_status(id, "active")

        # 3. Construct synchronization job envelope (R3.1, R2.11)
        trace_id = get_current_trace_id() or uuid4().hex
        job_id = str(uuid4())
        now = datetime.now(UTC)

        payload: dict[str, object] = {
            "provider": mailbox.provider,
            "manual": True,
            "full_resync": request.full_resync,
        }

        time_window: TimeWindow | None = None
        if request.since is not None or request.until is not None:
            time_window = TimeWindow(since=request.since, until=request.until)
            if request.since is not None:
                payload["since"] = request.since.isoformat()
            if request.until is not None:
                payload["until"] = request.until.isoformat()

        job = JobEnvelope(
            job_id=job_id,
            idempotency_key=f"{org_id}:{id}:manual-resync:{job_id[:8]}",
            job_type="sync_mailbox",
            organization_id=str(org_id),
            mailbox_id=str(id),
            trace_id=trace_id,
            payload=payload,
        )

        # 4. Asynchronously enqueue to broker (design.md §5.1, R3.1)
        if publisher is not None:
            broker_settings = AppSettings().broker
            await publisher.publish(
                exchange_name=broker_settings.exchange_mail_ingest,
                routing_key=broker_settings.queue_mail_sync,
                envelope=job,
            )
            logger.info(
                "Enqueued manual re-sync job %s for mailbox %s (org=%s)",
                job_id,
                id,
                org_id,
            )
        else:
            logger.warning(
                "No publisher configured on app state; job %s created without broker dispatch",
                job_id,
            )

        return ResyncResponse(
            job_id=job_id,
            mailbox_id=id,
            status="enqueued",
            enqueued_at=now,
            time_window=time_window,
        )


@mailbox_router.get(
    "/{id}",
    summary="Get Mailbox Details",
    description="Fetch mailbox metadata and operational status with tenant scoping (R23.2).",
    response_model=MailboxResponse,
)
async def get_mailbox(
    id: Annotated[UUID, Path(description="Target mailbox unique identifier.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    mailbox_store: MailboxStoreDep,
) -> MailboxResponse:
    """Fetch mailbox details ensuring tenant scoping."""
    bind_log_context(mailbox_id=str(id), organization_id=str(org_id))

    mailbox = await mailbox_store.get(id)
    if mailbox is None or str(mailbox.organization_id) != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Mailbox '{id}' not found",
                "code": "MAILBOX_NOT_FOUND",
            },
        )

    return MailboxResponse(
        id=UUID(str(mailbox.id)),
        organization_id=UUID(str(mailbox.organization_id)),
        address=mailbox.address,
        display_name=mailbox.display_name,
        status=mailbox.status,
        provider=mailbox.provider,
    )


@mailbox_router.get(
    "",
    summary="List Mailboxes",
    description=(
        "Fetch paginated list of mailboxes for the organization with optional "
        "status and provider filtering (R23.2, R23.6)."
    ),
    response_model=PaginatedResponse[MailboxResponse],
)
async def list_mailboxes(
    org_id: Annotated[UUID, Depends(get_organization_id)],
    mailbox_store: MailboxStoreDep,
    pagination: PaginationParamsDep,
    status: Annotated[
        str | None,
        Query(description="Filter by operational status: 'active', 'paused', 'needs_reauth'"),
    ] = None,
    provider: Annotated[
        str | None,
        Query(description="Filter by mail provider: 'gmail', 'graph', 'imap'"),
    ] = None,
) -> PaginatedResponse[MailboxResponse]:
    """Fetch paginated mailboxes with organization scoping and optional filtering."""
    bind_log_context(organization_id=str(org_id))

    mailboxes, total = await mailbox_store.list_mailboxes(
        organization_id=org_id,
        limit=pagination.limit,
        offset=pagination.offset,
        status=status,
        provider=provider,
    )

    items = [
        MailboxResponse(
            id=UUID(str(m.id)),
            organization_id=UUID(str(m.organization_id)),
            address=m.address,
            display_name=m.display_name,
            status=m.status,
            provider=m.provider,
        )
        for m in mailboxes
    ]

    return paginate(items=items, total_count=total, params=pagination)

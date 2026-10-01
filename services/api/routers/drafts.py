"""Draft review endpoints: list, read, edit, approve and reject (R16.6, R16.7, R23.2, R23.6).

Approve commits the decision first, then publishes the dispatch job to ``email.dispatch``. A
repeated approve re-publishes while the job is DRAFTED, DISPATCHED or RETRY_PENDING (dispatch
is idempotent) and writes no second feedback row, so a lost publish cannot strand an
approved draft. Reject moves the job DRAFTED -> COMPLETED with no send (design.md §5.8).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from packages.broker.envelope import JobEnvelope
from packages.core.idempotency import derive_idempotency_key
from packages.core.pagination import InvalidCursorError, decode_cursor
from packages.db.review import (
    DecisionOutcome,
    DraftConflictError,
    DraftDetail,
    DraftView,
    ReviewInput,
)
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry, get_default_registry
from packages.observability.context import bind_log_context
from packages.observability.metrics import PipelineMetrics, get_metrics
from services.api.dependencies import PublisherDep, ReviewStoreDep, get_organization_id
from services.api.schemas.drafts import (
    BusinessDataResponse,
    BusinessFactResponse,
    CitedChunkResponse,
    DraftDecisionRequest,
    DraftDecisionResponse,
    DraftDetailResponse,
    DraftEditRequest,
    DraftListResponse,
    DraftSummaryResponse,
    FeedbackResponse,
    OriginalEmailResponse,
)

logger = logging.getLogger("api.drafts")

drafts_router = APIRouter(prefix="/drafts", tags=["drafts"])

REPUBLISH_JOB_STATES = frozenset(
    {JobState.DRAFTED.value, JobState.DISPATCHED.value, JobState.RETRY_PENDING.value}
)
"""Job states in which an approve (first or repeated) publishes the dispatch job."""

DraftIdPath = Annotated[UUID, Path(description="Draft unique identifier.")]
OrgId = Annotated[UUID, Depends(get_organization_id)]


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _opt_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or str(value) == "":
        return None
    return _uuid(value)


def _not_found(draft_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": f"Draft '{draft_id}' not found", "code": "DRAFT_NOT_FOUND"},
    )


def _conflict(err: DraftConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": err.message, "code": err.code, "current_status": err.status},
    )


def _metrics(request: Request) -> PipelineMetrics:
    metrics: PipelineMetrics | None = getattr(request.app.state, "metrics", None)
    return metrics or get_metrics()


def _taxonomy(request: Request) -> TaxonomyRegistry:
    """The registry create_app loaded from config/categories.yaml (tasks 6.4, 6.8)."""
    registry: TaxonomyRegistry | None = getattr(request.app.state, "taxonomy", None)
    return registry or get_default_registry()


def _summary_fields(view: DraftView) -> dict[str, Any]:
    draft = view.draft
    return {
        "id": draft.id,
        "organization_id": _uuid(draft.organization_id),
        "job_id": _opt_uuid(draft.job_id),
        "message_id": _uuid(draft.message_id),
        "thread_id": _uuid(draft.thread_id),
        "mailbox_id": view.mailbox_id,
        "status": draft.status,
        "category": view.category,
        "job_state": view.job_state,
        "subject": draft.subject,
        "original_subject": view.original_subject,
        "sender_email": view.sender_email,
        "confidence": draft.confidence,
        "citation_mismatch": draft.citation_mismatch,
        "model_tier": draft.model_tier,
        "created_at": draft.created_at,
    }


def _detail_response(detail: DraftDetail, taxonomy: TaxonomyRegistry) -> DraftDetailResponse:
    draft = detail.view.draft
    original = detail.original
    feedback = detail.feedback
    business = detail.business_data
    return DraftDetailResponse(
        **_summary_fields(detail.view),
        body=draft.body,
        action=draft.action,
        citations=list(draft.citations),
        provider_ref=draft.provider_ref,
        original=OriginalEmailResponse(
            message_id=_uuid(original.message_id),
            sender_email=original.sender.email,
            sender_name=original.sender.name,
            subject=original.subject,
            body_text=original.body_text,
            received_at=original.received_at,
            rfc822_message_id=original.rfc822_message_id,
        ),
        thread_summary=detail.thread_summary,
        cited_chunks=[
            CitedChunkResponse(
                citation_id=c.citation_id,
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                external_id=c.external_id,
                content=c.content,
                heading_path=list(c.heading_path),
            )
            for c in detail.cited_chunks
        ],
        business_data=(
            BusinessDataResponse(
                customer_status=business.customer_status,
                degraded=business.degraded,
                facts=[
                    BusinessFactResponse(
                        entity=f.entity, reference=f.reference, status=f.status, reason=f.reason
                    )
                    for f in business.facts
                ],
            )
            if business is not None
            else None
        ),
        feedback=(
            FeedbackResponse(
                id=feedback.id,
                decision=feedback.decision,
                edited_body=feedback.edited_body,
                edit_distance=feedback.edit_distance,
                rating=feedback.rating,
                reviewer=feedback.reviewer,
                review_ms=feedback.review_ms,
                comment=feedback.comment,
                created_at=feedback.created_at,
            )
            if feedback is not None
            else None
        ),
        dispatch_mode=(
            taxonomy.dispatch_mode_for(detail.view.category).value if detail.view.category else None
        ),
    )


def _review_input(body: DraftDecisionRequest | None) -> ReviewInput:
    if body is None:
        return ReviewInput()
    return ReviewInput(
        reviewer=body.reviewer, rating=body.rating, review_ms=body.review_ms, comment=body.comment
    )


def _decision_response(outcome: DecisionOutcome, *, published: bool) -> DraftDecisionResponse:
    draft = outcome.view.draft
    return DraftDecisionResponse(
        draft_id=draft.id,
        job_id=_opt_uuid(draft.job_id),
        draft_status=draft.status,
        job_state=outcome.job.state if outcome.job is not None else None,
        decision=outcome.feedback.decision,
        edit_distance=outcome.feedback.edit_distance,
        feedback_id=outcome.feedback.id,
        created=outcome.created,
        published=published,
    )


def _count_decision(request: Request, outcome: DecisionOutcome) -> None:
    """draft_decisions_total counts first decisions only (R16.7, R21.4, SC3)."""
    if outcome.created:
        _metrics(request).draft_decisions_total.labels(
            decision=outcome.feedback.decision, category=outcome.view.category or "unknown"
        ).inc()


async def _publish_dispatch(publisher: Any, org_id: UUID, outcome: DecisionOutcome) -> None:
    """Publish the dispatch job after the approval committed (design.md §5.8)."""
    view = outcome.view
    draft = view.draft
    job = outcome.job
    assert job is not None
    unavailable = {
        "error": (
            "The approval is recorded but the dispatch job was not queued; "
            "approve again once the broker is available."
        ),
        "draft_id": str(draft.id),
    }
    if publisher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**unavailable, "code": "PUBLISHER_UNAVAILABLE"},
        )
    routing_key = publisher.settings.queue_dispatch
    exchange_name = publisher.settings.exchange_for_queue(routing_key)
    if exchange_name is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**unavailable, "code": "PUBLISHER_UNAVAILABLE"},
        )
    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=derive_idempotency_key(
            organization_id=org_id,
            mailbox_id=view.mailbox_id,
            provider_message_id=view.original_provider_message_id,
            operation_type="dispatch",
        ),
        job_type="dispatch",
        organization_id=str(org_id),
        message_id=str(draft.message_id),
        thread_id=str(draft.thread_id),
        mailbox_id=str(view.mailbox_id),
        payload={"draft_id": str(draft.id), "trigger": "approve"},
    )
    try:
        await publisher.publish(
            exchange_name=exchange_name, routing_key=routing_key, envelope=envelope
        )
    except Exception as err:
        logger.error("Dispatch publish for draft %s failed: %s", draft.id, err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**unavailable, "code": "DISPATCH_PUBLISH_FAILED"},
        ) from err


@drafts_router.get(
    "",
    summary="List Drafts",
    description="Drafts newest first, filtered by status, category and mailbox (R16.6, R23.6).",
    response_model=DraftListResponse,
)
async def list_drafts(
    org_id: OrgId,
    store: ReviewStoreDep,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")] = 50,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page.")] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(draft|approved|rejected|dispatched)$"),
    ] = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    mailbox: Annotated[UUID | None, Query(description="Mailbox UUID.")] = None,
) -> DraftListResponse:
    bind_log_context(organization_id=str(org_id))
    after = None
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursorError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": str(err), "code": "INVALID_CURSOR"},
            ) from err
    page = await store.list_drafts(
        org_id,
        status=status_filter,
        category=category,
        mailbox_id=mailbox,
        after=after,
        limit=limit,
    )
    return DraftListResponse(
        items=[DraftSummaryResponse(**_summary_fields(v)) for v in page.items],
        next_cursor=page.next_cursor,
        limit=limit,
    )


@drafts_router.get(
    "/{draft_id}",
    summary="Get Draft",
    description="Draft with its email, thread summary, cited chunks and business facts (R16.6).",
    response_model=DraftDetailResponse,
)
async def get_draft(
    request: Request, draft_id: DraftIdPath, org_id: OrgId, store: ReviewStoreDep
) -> DraftDetailResponse:
    bind_log_context(organization_id=str(org_id))
    detail = await store.get_draft_detail(org_id, draft_id)
    if detail is None:
        raise _not_found(draft_id)
    return _detail_response(detail, _taxonomy(request))


@drafts_router.patch(
    "/{draft_id}",
    summary="Edit Draft",
    description="Replace the draft body while its status is draft (R16.6).",
    response_model=DraftSummaryResponse,
)
async def edit_draft(
    draft_id: DraftIdPath, org_id: OrgId, store: ReviewStoreDep, body: DraftEditRequest
) -> DraftSummaryResponse:
    bind_log_context(organization_id=str(org_id))
    try:
        view = await store.edit_draft(org_id, draft_id, body=body.body, subject=body.subject)
    except DraftConflictError as err:
        raise _conflict(err) from err
    if view is None:
        raise _not_found(draft_id)
    return DraftSummaryResponse(**_summary_fields(view))


@drafts_router.post(
    "/{draft_id}/approve",
    summary="Approve Draft",
    description=(
        "Record the approval and one feedback row, then publish the dispatch job to "
        "email.dispatch. Repeating it re-publishes until the job completes (R16.6, R16.7)."
    ),
    response_model=DraftDecisionResponse,
)
async def approve_draft(
    request: Request,
    draft_id: DraftIdPath,
    org_id: OrgId,
    store: ReviewStoreDep,
    publisher: PublisherDep,
    body: DraftDecisionRequest | None = None,
) -> DraftDecisionResponse:
    bind_log_context(organization_id=str(org_id))
    try:
        outcome = await store.approve_draft(org_id, draft_id, _review_input(body))
    except DraftConflictError as err:
        raise _conflict(err) from err
    if outcome is None:
        raise _not_found(draft_id)
    _count_decision(request, outcome)
    published = False
    if outcome.job is not None and outcome.job.state in REPUBLISH_JOB_STATES:
        await _publish_dispatch(publisher, org_id, outcome)
        published = True
    return _decision_response(outcome, published=published)


@drafts_router.post(
    "/{draft_id}/reject",
    summary="Reject Draft",
    description="Record the rejection and move the job DRAFTED -> COMPLETED; nothing is sent.",
    response_model=DraftDecisionResponse,
)
async def reject_draft(
    request: Request,
    draft_id: DraftIdPath,
    org_id: OrgId,
    store: ReviewStoreDep,
    body: DraftDecisionRequest | None = None,
) -> DraftDecisionResponse:
    bind_log_context(organization_id=str(org_id))
    try:
        outcome = await store.reject_draft(org_id, draft_id, _review_input(body))
    except DraftConflictError as err:
        raise _conflict(err) from err
    if outcome is None:
        raise _not_found(draft_id)
    _count_decision(request, outcome)
    return _decision_response(outcome, published=False)

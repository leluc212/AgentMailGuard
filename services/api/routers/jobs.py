"""Processing job detail, timeline, and operator replay endpoints (R18.6, R18.7, R23.2, R23.6)."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from packages.broker.envelope import JobEnvelope
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.observability.context import bind_log_context
from services.api.dependencies import (
    JobStoreDep,
    PublisherDep,
    get_organization_id,
)
from services.api.schemas.jobs import (
    JobDetailResponse,
    JobReplayRequest,
    JobReplayResponse,
    JobTimelineResponse,
    ProcessingEventResponse,
)

logger = logging.getLogger("api.jobs")

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])


@jobs_router.get(
    "/{id}",
    summary="Get Job Details",
    description=(
        "Fetch processing job metadata and current state with organization scoping (R23.2, R23.6)."
    ),
    response_model=JobDetailResponse,
)
async def get_job(
    id: Annotated[UUID, Path(description="Target processing job unique identifier.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    job_store: JobStoreDep,
) -> JobDetailResponse:
    """Fetch job details ensuring tenant scoping."""
    bind_log_context(organization_id=str(org_id))

    job = await job_store.get_job(org_id, id)
    if job is None or str(job.organization_id) != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Job '{id}' not found",
                "code": "JOB_NOT_FOUND",
            },
        )

    return JobDetailResponse(
        id=UUID(str(job.id)),
        organization_id=UUID(str(job.organization_id)),
        message_id=UUID(str(job.message_id)) if job.message_id else None,
        thread_id=UUID(str(job.thread_id)) if job.thread_id else None,
        job_type=job.job_type,
        state=job.state,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        idempotency_key=job.idempotency_key,
        queue_name=job.queue_name,
        priority=job.priority,
        lease_expires_at=job.lease_expires_at,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@jobs_router.get(
    "/{id}/timeline",
    summary="Get Job Processing Timeline",
    description="Fetch chronological event history for a specific processing job (R18.6, R23.2).",
    response_model=JobTimelineResponse,
)
async def get_job_timeline(
    id: Annotated[UUID, Path(description="Target processing job unique identifier.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    job_store: JobStoreDep,
) -> JobTimelineResponse:
    """Fetch chronological events for a job."""
    bind_log_context(organization_id=str(org_id))

    job = await job_store.get_job(org_id, id)
    if job is None or str(job.organization_id) != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Job '{id}' not found",
                "code": "JOB_NOT_FOUND",
            },
        )

    events = await job_store.list_events_for_job(org_id, id)

    event_responses = [
        ProcessingEventResponse(
            id=ev.id,
            job_id=UUID(str(ev.job_id)) if ev.job_id else None,
            message_id=UUID(str(ev.message_id)) if ev.message_id else None,
            organization_id=UUID(str(ev.organization_id)),
            event_type=ev.event_type,
            state_from=ev.state_from,
            state_to=ev.state_to,
            payload=ev.payload,
            trace_id=ev.trace_id,
            created_at=ev.created_at,
        )
        for ev in events
    ]

    return JobTimelineResponse(
        job_id=UUID(str(job.id)),
        organization_id=UUID(str(job.organization_id)),
        current_state=job.state,
        total_events=len(events),
        events=event_responses,
    )


@jobs_router.post(
    "/{id}/replay",
    summary="Replay Dead-Lettered Job",
    description=(
        "Replay a failed job in DEAD_LETTER state from its last good state "
        "via operator action (R18.7, R23.2). Atomically transitions state to RETRY_PENDING "
        "and republishes to the message broker."
    ),
    response_model=JobReplayResponse,
)
async def replay_job(
    request: Request,
    id: Annotated[UUID, Path(description="Target dead-lettered job unique identifier.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    job_store: JobStoreDep,
    publisher: PublisherDep,
    body: JobReplayRequest | None = None,
) -> JobReplayResponse:
    """Replay a DEAD_LETTER job back into RETRY_PENDING and redeliver."""
    bind_log_context(organization_id=str(org_id))
    req_body = body or JobReplayRequest()

    job = await job_store.get_job(org_id, id)
    if job is None or str(job.organization_id) != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Job '{id}' not found",
                "code": "JOB_NOT_FOUND",
            },
        )

    # Enforce R18.7: replay is only legal from DEAD_LETTER state
    if job.state != JobState.DEAD_LETTER.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    f"Job '{id}' in state '{job.state}' cannot be replayed. "
                    "Only jobs in 'DEAD_LETTER' state can be replayed by operators."
                ),
                "code": "JOB_NOT_REPLAYABLE",
                "current_state": job.state,
            },
        )

    previous_state = job.state
    replay_payload = {
        "operator_replay": True,
        "reason": req_body.reason,
        "reset_attempts": req_body.reset_attempts,
    }

    try:
        replayed_job, event = await job_store.replay_job(
            organization_id=org_id,
            job_id=id,
            payload=replay_payload,
            reset_attempts=req_body.reset_attempts,
        )
    except IllegalStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": str(exc),
                "code": "ILLEGAL_STATE_TRANSITION",
            },
        ) from exc

    # Attempt publishing to message broker if publisher attached to app state
    republished = False
    routing_key = replayed_job.queue_name or "email.triage"

    if publisher is not None:
        try:
            broker_cfg = getattr(
                publisher, "settings", getattr(publisher, "broker_settings", None)
            )
            exchange_name = (
                getattr(broker_cfg, "exchange_email_route", "email.events")
                if broker_cfg
                else "email.events"
            )
            envelope = JobEnvelope(
                job_id=str(replayed_job.id),
                idempotency_key=replayed_job.idempotency_key,
                job_type=replayed_job.job_type,
                organization_id=str(replayed_job.organization_id),
                message_id=str(replayed_job.message_id or ""),
                thread_id=str(replayed_job.thread_id or ""),
                attempt=replayed_job.attempt,
                payload={"replayed": True, "reason": req_body.reason},
            )
            await publisher.publish(
                exchange_name=exchange_name,
                routing_key=routing_key,
                envelope=envelope,
            )
            republished = True
            logger.info(
                "Replayed job %s published to exchange %s with key %s",
                replayed_job.id,
                exchange_name,
                routing_key,
            )
        except Exception as pub_err:
            logger.error("Failed to republish replayed job %s: %s", replayed_job.id, pub_err)

    return JobReplayResponse(
        job_id=UUID(str(replayed_job.id)),
        organization_id=UUID(str(replayed_job.organization_id)),
        previous_state=previous_state,
        new_state=replayed_job.state,
        attempt=replayed_job.attempt,
        republished=republished,
        routing_key=routing_key,
        replayed_at=event.created_at,
    )

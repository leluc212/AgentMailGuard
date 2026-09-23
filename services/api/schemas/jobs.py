"""Pydantic schemas for processing job and timeline endpoints (R18.6, R18.7, R23.2, R23.6)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ProcessingEventResponse(BaseModel):
    """Telemetry event recording state transitions and audit trails (R18.4, R18.6)."""

    id: int | None = Field(default=None, description="Monotonic event ID.")
    job_id: UUID | None = Field(default=None, description="Associated job UUID.")
    message_id: UUID | None = Field(default=None, description="Associated message UUID.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    event_type: str = Field(description="Telemetry event category.")
    state_from: str | None = Field(default=None, description="Origin job state.")
    state_to: str = Field(description="Destination job state.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Event metadata payload.")
    trace_id: str | None = Field(default=None, description="Distributed trace context ID.")
    created_at: datetime = Field(description="Event creation timestamp.")


class JobDetailResponse(BaseModel):
    """Detailed representation of an asynchronous processing job (R18.1, R23.2)."""

    id: UUID = Field(description="Job unique identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    message_id: UUID | None = Field(default=None, description="Target email message UUID.")
    thread_id: UUID | None = Field(default=None, description="Target conversation thread UUID.")
    job_type: str = Field(description="Pipeline job type.")
    state: str = Field(description="Current job lifecycle state.")
    attempt: int = Field(default=0, description="Delivery attempt counter.")
    max_attempts: int = Field(default=5, description="Maximum allowed attempts.")
    idempotency_key: str = Field(description="Deterministic idempotency token.")
    queue_name: str | None = Field(default=None, description="Target queue name.")
    priority: str | None = Field(default=None, description="Processing priority.")
    lease_expires_at: datetime | None = Field(
        default=None, description="Lease expiration timestamp."
    )
    last_error: str | None = Field(default=None, description="Most recent error message.")
    created_at: datetime = Field(description="Job creation timestamp.")
    updated_at: datetime = Field(description="Last update timestamp.")


class JobTimelineResponse(BaseModel):
    """Chronological event history for a job (R18.6, R23.2)."""

    job_id: UUID = Field(description="Target job identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    current_state: str = Field(description="Current job lifecycle state.")
    total_events: int = Field(description="Total number of events.")
    events: list[ProcessingEventResponse] = Field(
        default_factory=list, description="Chronological event history."
    )


class JobReplayRequest(BaseModel):
    """Request payload for replaying a DEAD_LETTER job (R18.7, R23.2)."""

    reset_attempts: bool = Field(
        default=True,
        description="Whether to reset attempt counter to 0 for a fresh retry budget.",
    )
    reason: str | None = Field(
        default=None,
        description="Operator justification or explanation for the replay.",
    )


class JobReplayResponse(BaseModel):
    """Response returned upon initiating operator replay of a job (R18.7, R23.2)."""

    job_id: UUID = Field(description="Replayed job identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    previous_state: str = Field(description="State before replay.")
    new_state: str = Field(description="New job state (RETRY_PENDING).")
    attempt: int = Field(description="Current attempt counter.")
    republished: bool = Field(
        description="Whether the job was republished to the message broker."
    )
    routing_key: str | None = Field(
        default=None, description="Target queue or routing key for redelivery."
    )
    replayed_at: datetime = Field(description="Timestamp when replay occurred.")

"""Pydantic schemas for mailbox endpoints (R2.11, R23.2)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TimeWindow(BaseModel):
    """Optional time window filter for mailbox re-synchronization."""

    since: datetime | None = Field(
        default=None,
        description="Inclusive start timestamp (ISO-8601 UTC) for synchronization.",
    )
    until: datetime | None = Field(
        default=None,
        description="Inclusive end timestamp (ISO-8601 UTC) for synchronization.",
    )

    @model_validator(mode="after")
    def validate_time_window(self) -> "TimeWindow":
        """Validate that since is not after until if both are provided."""
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ValueError("'since' timestamp must not be after 'until' timestamp")
        return self


class ResyncRequest(BaseModel):
    """Request payload for POST /v1/mailboxes/{id}/resync (R2.11)."""

    since: datetime | None = Field(
        default=None,
        description="Optional start timestamp filter for messages to synchronize.",
    )
    until: datetime | None = Field(
        default=None,
        description="Optional end timestamp filter for messages to synchronize.",
    )
    full_resync: bool = Field(
        default=False,
        description="Force full historical re-sync ignoring existing checkpoints.",
    )
    force: bool = Field(
        default=False,
        description="Force re-sync even if mailbox status is 'needs_reauth' or 'paused'.",
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> "ResyncRequest":
        """Validate that since is not after until."""
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ValueError("'since' timestamp must not be after 'until' timestamp")
        return self


class ResyncResponse(BaseModel):
    """Response payload for POST /v1/mailboxes/{id}/resync (R2.11, R23.2)."""

    job_id: str = Field(description="Unique job identifier for enqueued sync operation.")
    mailbox_id: UUID = Field(description="Mailbox UUID being synchronized.")
    status: str = Field(default="enqueued", description="Job status: 'enqueued'.")
    enqueued_at: datetime = Field(description="Timestamp when sync was enqueued.")
    time_window: TimeWindow | None = Field(
        default=None, description="Applied time window filter if specified."
    )


class MailboxResponse(BaseModel):
    """Response payload for mailbox detail queries (R23.2)."""

    id: UUID = Field(description="Mailbox unique identifier.")
    organization_id: UUID = Field(description="Tenant organization identifier.")
    address: str = Field(description="Email address associated with the mailbox.")
    display_name: str | None = Field(
        default=None, description="Human-readable mailbox display name."
    )
    status: str = Field(description="Operational status: 'active', 'paused', 'needs_reauth'.")
    provider: str = Field(description="Underlying mail provider identifier.")
    created_at: datetime | None = Field(default=None, description="Mailbox registration timestamp.")

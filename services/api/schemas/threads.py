"""Pydantic schemas for email thread endpoints (R23.2, R23.6)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class MessageSummaryInThread(BaseModel):
    """Concise representation of an email message within a thread view."""

    id: UUID = Field(description="Message unique identifier.")
    sender_email: str | None = Field(default=None, description="Sender email address.")
    sender_name: str | None = Field(default=None, description="Sender display name.")
    subject: str = Field(description="Message subject header.")
    snippet: str = Field(description="Short preview snippet of message body.")
    received_at: datetime = Field(description="Timestamp when message was received.")
    direction: str = Field(
        default="inbound", description="Message direction: 'inbound' or 'outbound'."
    )
    has_attachments: bool = Field(default=False, description="Whether message carries attachments.")
    normalization_failed: bool = Field(
        default=False, description="Whether message normalization failed."
    )


class ThreadSummaryResponse(BaseModel):
    """Summary representation of an email thread for list queries."""

    id: UUID = Field(description="Thread unique identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    mailbox_id: UUID = Field(description="Mailbox UUID owning this thread.")
    provider_thread_id: str | None = Field(
        default=None, description="Native provider thread ID if available."
    )
    subject_normalized: str = Field(description="Normalized canonical subject.")
    participants: list[str] = Field(
        default_factory=list, description="Unique email addresses participating in thread."
    )
    first_message_at: datetime | None = Field(
        default=None, description="Timestamp of the earliest message in thread."
    )
    last_message_at: datetime | None = Field(
        default=None, description="Timestamp of the most recent message in thread."
    )
    message_count: int = Field(default=0, description="Total count of messages in thread.")
    status: str = Field(default="open", description="Thread status: 'open', 'closed', 'snoozed'.")


class ThreadDetailResponse(ThreadSummaryResponse):
    """Detailed representation of an email thread including its chronological messages."""

    messages: list[MessageSummaryInThread] = Field(
        default_factory=list,
        description="Chronologically ordered messages belonging to this thread.",
    )

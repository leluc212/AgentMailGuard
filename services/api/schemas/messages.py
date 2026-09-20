"""Pydantic schemas for email message endpoints (R23.2, R23.6)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class EmailAddressResponse(BaseModel):
    """Structured email address representation."""

    name: str | None = Field(default=None, description="Display name of the party.")
    email: str = Field(description="Email address.")


class AttachmentSummaryResponse(BaseModel):
    """Metadata for an email attachment record."""

    id: UUID = Field(description="Attachment unique identifier.")
    filename: str | None = Field(default=None, description="Original attachment filename.")
    mime_type: str | None = Field(default=None, description="Content MIME type.")
    size_bytes: int | None = Field(default=None, description="Payload size in bytes.")
    object_key: str = Field(description="Object storage object key path.")
    download_url: str | None = Field(
        default=None, description="Temporary presigned URL for downloading attachment payload."
    )


class MessageDetailResponse(BaseModel):
    """Detailed representation of a persisted email message."""

    id: UUID = Field(description="Message unique identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    mailbox_id: UUID = Field(description="Mailbox UUID owning this message.")
    thread_id: UUID = Field(description="Associated thread identifier.")
    provider_message_id: str = Field(description="Native mail provider message ID.")
    rfc822_message_id: str | None = Field(
        default=None, description="RFC822 Message-ID header value."
    )
    in_reply_to: str | None = Field(default=None, description="In-Reply-To header reference.")
    references_ids: list[str] = Field(
        default_factory=list, description="References header message IDs."
    )
    direction: str = Field(
        default="inbound", description="Message direction: 'inbound' or 'outbound'."
    )
    sender: EmailAddressResponse | None = Field(default=None, description="Sender information.")
    recipients: list[EmailAddressResponse] = Field(
        default_factory=list, description="Primary 'To' recipient addresses."
    )
    cc: list[EmailAddressResponse] = Field(
        default_factory=list, description="Carbon copy 'Cc' recipient addresses."
    )
    subject: str = Field(default="", description="Full subject header.")
    subject_normalized: str = Field(default="", description="Normalized subject string.")
    body_text: str = Field(default="", description="Extracted plain text message body.")
    body_text_clean: str = Field(
        default="", description="Clean body text with quoted history and signatures stripped."
    )
    snippet: str = Field(default="", description="Short preview snippet.")
    raw_object_key: str | None = Field(
        default=None, description="Object storage key for the raw MIME payload."
    )
    html_object_key: str | None = Field(
        default=None, description="Object storage key for the rendered HTML payload."
    )
    received_at: datetime = Field(description="Timestamp when message was received.")
    normalization_failed: bool = Field(
        default=False, description="Flag indicating if MIME parsing failed."
    )
    attachments: list[AttachmentSummaryResponse] = Field(
        default_factory=list, description="List of attachment metadata records."
    )

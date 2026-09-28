"""Pydantic schemas for the draft review API (R16.6, R16.7, R23.2, R23.6; design.md §5.8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DraftSummaryResponse(BaseModel):
    """One row of the review queue."""

    id: UUID = Field(description="Draft identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    job_id: UUID | None = Field(description="Processing job carrying generation and dispatch.")
    message_id: UUID = Field(description="The inbound email the draft answers.")
    thread_id: UUID = Field(description="Conversation thread UUID.")
    mailbox_id: UUID = Field(description="Mailbox that received the email.")
    status: str = Field(description="draft | approved | rejected | dispatched.")
    category: str | None = Field(description="Latest triage category of the email.")
    job_state: str | None = Field(description="Current processing_job state.")
    subject: str | None = Field(description="Draft subject.")
    original_subject: str = Field(description="Subject of the inbound email.")
    sender_email: str = Field(description="Sender of the inbound email.")
    confidence: float | None = Field(description="Model confidence, if reported.")
    citation_mismatch: bool = Field(description="True when a citation was not in the context.")
    model_tier: str | None = Field(description="fast | strong model tier.")
    created_at: datetime = Field(description="Draft creation time.")


class DraftListResponse(BaseModel):
    """A keyset page of drafts, newest first (R23.6)."""

    items: list[DraftSummaryResponse] = Field(description="Drafts on this page.")
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` for the next page; null on the last page."
    )
    limit: int = Field(ge=1, description="Requested page size.")


class OriginalEmailResponse(BaseModel):
    """The inbound email under review."""

    message_id: UUID = Field(description="Email message UUID.")
    sender_email: str = Field(description="Sender address.")
    sender_name: str | None = Field(description="Sender display name.")
    subject: str = Field(description="Subject line.")
    body_text: str = Field(description="Plain-text body.")
    received_at: datetime = Field(description="Receipt time.")
    rfc822_message_id: str | None = Field(description="RFC 5322 Message-ID, without <>.")


class CitedChunkResponse(BaseModel):
    """A knowledge chunk the draft cites, with its text."""

    citation_id: str = Field(description="Citation id as written in the draft.")
    chunk_id: str = Field(description="knowledge_chunk UUID.")
    document_id: str | None = Field(description="knowledge_document UUID.")
    external_id: str | None = Field(description="Stable chunk id such as DOC-125-08.")
    content: str = Field(description="Chunk text.")
    heading_path: list[str] = Field(description="Section headings above the chunk.")


class BusinessFactResponse(BaseModel):
    """One [BUSINESS DATA] fact status used for the draft."""

    entity: str = Field(description="order | ticket | invoice.")
    reference: str | None = Field(description="Reference such as ORD-82915.")
    status: str = Field(description="FOUND | NOT_FOUND | NOT_LOOKED_UP | UNAVAILABLE.")
    reason: str | None = Field(description="Why a fact was not looked up.")


class BusinessDataResponse(BaseModel):
    """Customer status and fact statuses from the job's CONTEXT_READY event."""

    customer_status: str | None = Field(description="Customer resolution status.")
    degraded: bool = Field(description="True when the business lookup timed out or failed.")
    facts: list[BusinessFactResponse] = Field(description="Planned facts and their status.")


class FeedbackResponse(BaseModel):
    """The draft's feedback row (R16.7)."""

    id: UUID = Field(description="Feedback UUID.")
    decision: str = Field(description="accepted | edited | rejected.")
    edited_body: str | None = Field(description="Final body when it differs from the draft.")
    edit_distance: int | None = Field(description="Character edit distance from the draft.")
    rating: int | None = Field(description="Optional 1-5 rating.")
    reviewer: str | None = Field(description="Free-text reviewer label (no login, ADR-0009).")
    review_ms: int | None = Field(description="Time from opening the draft to deciding.")
    comment: str | None = Field(description="Optional reviewer comment.")
    created_at: datetime = Field(description="Decision time.")


class DraftDetailResponse(DraftSummaryResponse):
    """A draft with everything a reviewer needs (task 6.1)."""

    body: str = Field(description="Draft body.")
    action: str = Field(description="reply | forward | escalate.")
    citations: list[dict[str, Any]] = Field(description="Raw citation records of the draft.")
    provider_ref: str | None = Field(description="Provider id recorded at dispatch.")
    original: OriginalEmailResponse = Field(description="The inbound email.")
    thread_summary: str | None = Field(description="Rolling thread summary, if any.")
    cited_chunks: list[CitedChunkResponse] = Field(description="Cited chunks with text.")
    business_data: BusinessDataResponse | None = Field(description="Business fact statuses.")
    feedback: FeedbackResponse | None = Field(description="The decision, once made.")
    dispatch_mode: str | None = Field(
        default=None,
        description="create_draft | send_reply for the draft's category, read at request time.",
    )


class DraftEditRequest(BaseModel):
    """PATCH body: the reviewer's edited text."""

    body: str = Field(min_length=1, max_length=100_000, description="New draft body.")
    subject: str | None = Field(default=None, max_length=998, description="New subject line.")


class DraftDecisionRequest(BaseModel):
    """Optional fields of an approve or reject call."""

    reviewer: str | None = Field(default=None, max_length=200, description="Reviewer label.")
    rating: int | None = Field(default=None, ge=1, le=5, description="Optional 1-5 rating.")
    review_ms: int | None = Field(
        default=None, ge=0, le=86_400_000, description="Client-measured review time in ms."
    )
    comment: str | None = Field(default=None, max_length=2000, description="Optional comment.")


class DraftDecisionResponse(BaseModel):
    """Result of approve or reject."""

    draft_id: UUID = Field(description="Draft identifier.")
    job_id: UUID | None = Field(description="Processing job identifier.")
    draft_status: str = Field(description="Draft status after the call.")
    job_state: str | None = Field(description="Job state after the call.")
    decision: str = Field(description="accepted | edited | rejected (the first decision).")
    edit_distance: int | None = Field(description="Character edit distance from the draft.")
    feedback_id: UUID = Field(description="The draft's one feedback row.")
    created: bool = Field(description="False when this repeated an earlier decision.")
    published: bool = Field(description="True when a dispatch job was published by this call.")

"""Email message detail and attachment query endpoints (R23.2, R23.6, R5.3).

Provides:
- GET /v1/messages/{id}: message detail view with recipient lists, attachments metadata,
  clean text, and optional presigned download URLs.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status

from packages.observability.context import bind_log_context
from services.api.dependencies import (
    MessageStoreDep,
    StorageClientDep,
    get_organization_id,
)
from services.api.schemas.messages import (
    AttachmentSummaryResponse,
    EmailAddressResponse,
    MessageDetailResponse,
)

logger = logging.getLogger("api.messages")

message_router = APIRouter(prefix="/messages", tags=["messages"])


@message_router.get(
    "/{id}",
    summary="Get Message Details",
    description=(
        "Fetch detailed email message metadata, recipient lists, clean body text, "
        "and attachment references with organization scoping (R23.2, R23.6)."
    ),
    response_model=MessageDetailResponse,
)
async def get_message(
    id: Annotated[UUID, Path(description="Target email message unique identifier.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    message_store: MessageStoreDep,
    storage_client: StorageClientDep = None,
) -> MessageDetailResponse:
    """Fetch message details ensuring tenant scoping."""
    bind_log_context(organization_id=str(org_id))

    msg = await message_store.get_message(org_id, id)
    if msg is None or str(msg.organization_id) != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Message '{id}' not found",
                "code": "MESSAGE_NOT_FOUND",
            },
        )

    # Fetch attachments metadata
    att_records = await message_store.get_attachments(org_id, id)
    attachment_summaries: list[AttachmentSummaryResponse] = []

    for att in att_records:
        download_url: str | None = None
        if storage_client is not None and hasattr(storage_client, "get_presigned_url"):
            try:
                # Resolve bucket and key
                download_url = await storage_client.get_presigned_url(
                    bucket="attachments",
                    key=att.object_key,
                    expires_seconds=3600,
                )
            except Exception as s_err:
                logger.warning("Failed generating presigned URL for %s: %s", att.object_key, s_err)

        attachment_summaries.append(
            AttachmentSummaryResponse(
                id=UUID(str(att.id)),
                filename=att.filename,
                mime_type=att.mime_type,
                size_bytes=att.size_bytes,
                object_key=att.object_key,
                download_url=download_url,
            )
        )

    sender_resp = (
        EmailAddressResponse(name=msg.sender.name, email=msg.sender.email) if msg.sender else None
    )
    recipients_resp = [EmailAddressResponse(name=r.name, email=r.email) for r in msg.recipients]
    cc_resp = [EmailAddressResponse(name=c.name, email=c.email) for c in msg.cc]

    return MessageDetailResponse(
        id=UUID(str(msg.message_id)),
        organization_id=UUID(str(msg.organization_id)),
        mailbox_id=UUID(str(msg.mailbox_id)),
        thread_id=UUID(str(msg.thread_id)),
        provider_message_id=msg.provider_message_id,
        rfc822_message_id=msg.rfc822_message_id,
        in_reply_to=msg.in_reply_to,
        references_ids=list(msg.references_ids),
        direction=msg.direction,
        sender=sender_resp,
        recipients=recipients_resp,
        cc=cc_resp,
        subject=msg.subject,
        subject_normalized=msg.subject_normalized,
        body_text=msg.body_text,
        body_text_clean=msg.body_text_clean,
        snippet=msg.snippet,
        raw_object_key=msg.raw_object_key,
        html_object_key=msg.html_object_key,
        received_at=msg.received_at,
        normalization_failed=msg.normalization_failed,
        attachments=attachment_summaries,
    )

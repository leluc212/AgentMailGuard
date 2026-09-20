"""Email normalization orchestrator.

Requirements:
- R4.1: Parse MIME and produce canonical schema:
        message_id, thread_id, mailbox_id, sender, recipients, cc, subject,
        body_text, body_text_clean, received_at, attachments, provider.
- R4.2: Extract plain text, converting HTML bodies when no text part exists.
- R4.3: Separate quoted reply history into body_text and body_text_clean.
- R4.4: Detect and strip signature blocks, recording signature_stripped flag.
- R4.7, R5.8: Extract attachment metadata and offload binary payloads to MinIO.
- R4.9: Mark normalization_failed=True on unrecoverable MIME parsing errors.
- design.md §5.2: Output conforms to Normalized message JSON contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from packages.core.storage import ObjectKeyBuilder, StorageProtocol
from packages.domain.entities import EmailAddress, NormalizedMessage
from services.email_worker.attachments import (
    ExtractedAttachment,
    extract_attachments_from_message,
    offload_attachments,
)
from services.email_worker.history import clean_email_body
from services.email_worker.parser import (
    extract_email_headers,
    parse_mime_bytes,
    select_message_body,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizationContext:
    """Execution context and foreign keys for email normalization."""

    organization_id: UUID | str
    mailbox_id: UUID | str
    message_id: UUID | str
    provider: str
    provider_message_id: str
    thread_id: UUID | str | None = None
    raw_object_key: str | None = None


@dataclass(frozen=True)
class NormalizationResult:
    """Normalization output containing the canonical entity, attachments, and raw HTML."""

    message: NormalizedMessage
    raw_html: str | None = None
    extracted_attachments: list[ExtractedAttachment] = field(default_factory=list)


class EmailNormalizer:
    """Pure normalizer converting RFC 822 MIME byte payloads into canonical domain entities."""

    def normalize(
        self,
        raw_mime: bytes,
        context: NormalizationContext,
    ) -> NormalizationResult:
        """Parse raw MIME bytes into a canonical NormalizedMessage entity."""
        thread_id = context.thread_id or context.message_id

        try:
            if not raw_mime or not raw_mime.strip():
                raise ValueError("Empty MIME payload")

            mime_msg = parse_mime_bytes(raw_mime)
            if not mime_msg.items():
                raise ValueError("Unparseable MIME payload: no email headers found")

            headers = extract_email_headers(mime_msg)
            body_text_raw, html_body, _ = select_message_body(mime_msg)
            body_text, body_text_clean, sig_stripped = clean_email_body(body_text_raw)

            # Generate short text snippet from clean body
            snippet = " ".join(body_text_clean[:200].split())

            # Extract attachments
            extracted_atts = extract_attachments_from_message(
                message=mime_msg,
                organization_id=context.organization_id,
                message_id=context.message_id,
            )
            attachment_refs = [att.ref for att in extracted_atts]

            normalized = NormalizedMessage(
                message_id=context.message_id,
                thread_id=thread_id,
                mailbox_id=context.mailbox_id,
                organization_id=context.organization_id,
                provider=context.provider,
                provider_message_id=context.provider_message_id,
                sender=headers.sender,
                received_at=headers.received_at,
                rfc822_message_id=headers.rfc822_message_id,
                in_reply_to=headers.in_reply_to,
                references_ids=headers.references_ids,
                recipients=headers.recipients,
                cc=headers.cc,
                subject=headers.subject,
                subject_normalized=headers.subject_normalized,
                body_text=body_text,
                body_text_clean=body_text_clean,
                snippet=snippet,
                raw_object_key=context.raw_object_key,
                html_object_key=None,
                direction="inbound",
                attachments=attachment_refs,
                normalization_failed=False,
                signature_stripped=sig_stripped,
            )

            return NormalizationResult(
                message=normalized,
                raw_html=html_body,
                extracted_attachments=extracted_atts,
            )

        except Exception as exc:
            logger.error("MIME normalization failed for message %s: %s", context.message_id, exc)
            fallback_text = (
                raw_mime.decode("utf-8", errors="replace").replace("\x00", "")[:1000]
                if raw_mime
                else ""
            )

            failed_message = NormalizedMessage(
                message_id=context.message_id,
                thread_id=thread_id,
                mailbox_id=context.mailbox_id,
                organization_id=context.organization_id,
                provider=context.provider,
                provider_message_id=context.provider_message_id,
                sender=EmailAddress(email=""),
                received_at=datetime.now(UTC),
                rfc822_message_id=None,
                in_reply_to=None,
                references_ids=[],
                recipients=[],
                cc=[],
                subject="",
                subject_normalized="",
                body_text=fallback_text,
                body_text_clean=fallback_text,
                snippet="[normalization failed]",
                raw_object_key=context.raw_object_key,
                html_object_key=None,
                direction="inbound",
                attachments=[],
                normalization_failed=True,
                signature_stripped=False,
            )

            return NormalizationResult(
                message=failed_message,
                raw_html=None,
                extracted_attachments=[],
            )

    async def normalize_and_offload(
        self,
        raw_mime: bytes,
        context: NormalizationContext,
        storage_client: StorageProtocol,
        attachments_bucket: str = "attachments",
        html_bucket: str = "html",
    ) -> NormalizationResult:
        """Normalize MIME message and asynchronously offload attachments/HTML to storage."""
        result = self.normalize(raw_mime=raw_mime, context=context)

        # Offload attachments to MinIO if present (R4.7, R5.8)
        if result.extracted_attachments:
            await offload_attachments(
                attachments=result.extracted_attachments,
                storage_client=storage_client,
                bucket=attachments_bucket,
            )

        # Offload raw HTML body to MinIO if present
        if result.raw_html:
            html_key = ObjectKeyBuilder.html_body(
                organization_id=context.organization_id,
                mailbox_id=context.mailbox_id,
                message_id=context.message_id,
            )
            await storage_client.put_bytes(
                bucket=html_bucket,
                key=html_key,
                data=result.raw_html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                metadata={"message_id": str(context.message_id)},
            )
            result.message.html_object_key = html_key

        return result

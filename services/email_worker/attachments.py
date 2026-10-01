"""Attachment extraction and object storage offloading.

Requirements:
- R4.7: Extract attachment metadata (filename, MIME type, size, checksum)
        and store binary content in object storage, never in a relational column.
- R5.8: Key attachments deterministically by {org_id}/{message_id}/{attachment_id}/{filename}.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from email.message import Message
from uuid import UUID, uuid4

from packages.core.storage import ObjectKeyBuilder, StorageProtocol
from packages.domain.entities import AttachmentRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedAttachment:
    """Extracted attachment holding both metadata and binary payload."""

    ref: AttachmentRef
    payload: bytes
    attachment_id: UUID
    content_id: str | None = None


def extract_attachments_from_message(
    message: Message,
    organization_id: UUID | str,
    message_id: UUID | str,
) -> list[ExtractedAttachment]:
    """Extract all attachment parts from an email MIME message.

    Identifies parts with Content-Disposition: attachment, or inline parts
    with filenames or Content-IDs. Computes SHA-256 and builds deterministic
    object keys via ObjectKeyBuilder.attachment (R4.7, R5.8).
    """
    extracted: list[ExtractedAttachment] = []
    attachment_counter = 0
    ignored_subpart_ids: set[int] = set()

    for part in message.walk():
        if id(part) in ignored_subpart_ids:
            continue

        is_rfc822 = part.get_content_type() == "message/rfc822"

        # Multipart containers (except message/rfc822 attachments) are not payload attachments
        if part.is_multipart() and not is_rfc822:
            continue

        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_id = part.get("Content-ID")
        if content_id:
            content_id = content_id.strip("<> \t\r\n")

        # Check if part is an attachment
        is_attachment = disposition == "attachment"
        is_inline_media = disposition == "inline" and (
            filename is not None or content_id is not None
        )
        has_named_file = filename is not None and disposition is None

        if not (is_attachment or is_inline_media or has_named_file):
            continue

        attachment_counter += 1

        payload: bytes
        if is_rfc822:
            payload_obj = part.get_payload()
            if isinstance(payload_obj, list) and payload_obj:
                inner = payload_obj[0]
                if hasattr(inner, "as_bytes"):
                    raw_inner_bytes = inner.as_bytes()
                    payload = (
                        raw_inner_bytes
                        if isinstance(raw_inner_bytes, bytes)
                        else str(raw_inner_bytes).encode("utf-8")
                    )
                elif isinstance(inner, bytes):
                    payload = inner
                elif isinstance(inner, str):
                    payload = inner.encode("utf-8")
                else:
                    payload = str(inner).encode("utf-8")

                # Ignore nested parts of this attached message so they are not treated as top-level
                if hasattr(inner, "walk"):
                    for subpart in inner.walk():
                        ignored_subpart_ids.add(id(subpart))
            elif hasattr(part, "as_bytes"):
                raw_part_bytes = part.as_bytes()
                payload = (
                    raw_part_bytes
                    if isinstance(raw_part_bytes, bytes)
                    else str(raw_part_bytes).encode("utf-8")
                )
            else:
                payload = b""
        else:
            raw_payload = part.get_payload(decode=True)
            if isinstance(raw_payload, bytes):
                payload = raw_payload
            elif isinstance(raw_payload, str):
                payload = raw_payload.encode("utf-8", errors="replace")
            else:
                payload = b""

        # Clean or generate fallback filename
        clean_name = filename.strip() if filename and filename.strip() else ""
        if not clean_name:
            subtype = part.get_content_subtype()
            ext = f".{subtype}" if subtype and subtype != "octet-stream" else ".bin"
            clean_name = f"attachment_{attachment_counter}{ext}"

        size_bytes = len(payload)
        sha256 = hashlib.sha256(payload).hexdigest()
        att_id = uuid4()
        mime_type = part.get_content_type() or "application/octet-stream"

        object_key = ObjectKeyBuilder.attachment(
            organization_id=organization_id,
            message_id=message_id,
            attachment_id=att_id,
            filename=clean_name,
        )

        ref = AttachmentRef(
            filename=clean_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            object_key=object_key,
            checksum=sha256,
        )

        extracted.append(
            ExtractedAttachment(
                ref=ref,
                payload=payload,
                attachment_id=att_id,
                content_id=content_id,
            )
        )

    return extracted


async def offload_attachments(
    attachments: list[ExtractedAttachment],
    storage_client: StorageProtocol,
    bucket: str = "attachments",
) -> list[AttachmentRef]:
    """Offload binary payloads to object storage and return metadata references (R4.7, R5.8)."""
    refs: list[AttachmentRef] = []
    for att in attachments:
        metadata: dict[str, str] = {
            "filename": att.ref.filename,
            "sha256": att.ref.checksum or "",
            "mime_type": att.ref.mime_type,
        }
        if att.content_id:
            metadata["content_id"] = att.content_id

        await storage_client.put_bytes(
            bucket=bucket,
            key=att.ref.object_key,
            data=att.payload,
            content_type=att.ref.mime_type,
            metadata=metadata,
        )
        refs.append(att.ref)
        logger.debug(
            "Offloaded attachment %s (%d bytes) to %s/%s",
            att.ref.filename,
            att.ref.size_bytes,
            bucket,
            att.ref.object_key,
        )

    return refs

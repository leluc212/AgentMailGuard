"""Raw payload archival and replay management (R4.10, R5.8).

Persists inbound/outbound provider raw payloads (RFC 822 MIME bytes or JSON envelopes)
in object storage before normalization, keyed with SHA-256 digests and tenant metadata
for durable audit and later replay.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import AppSettings
from packages.core.storage import ObjectKeyBuilder, StorageProtocol

if TYPE_CHECKING:
    from packages.broker.envelope import JobEnvelope

logger = logging.getLogger(__name__)


class ChecksumMismatchError(Exception):
    """Raised when retrieved object content fails SHA-256 integrity verification."""


@dataclass(frozen=True)
class ArchivedPayloadRef:
    """Metadata reference to an archived raw message payload in object storage (R4.10, R5.8)."""

    object_key: str
    bucket: str
    sha256: str
    size_bytes: int
    content_type: str = "message/rfc822"
    metadata: dict[str, str] = field(default_factory=dict)


def compute_payload_digest(payload: bytes | str) -> tuple[bytes, str]:
    """Normalize payload to bytes and compute hexadecimal SHA-256 digest."""
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


class RawPayloadArchiver:
    """Service archiving raw provider payloads to MinIO/S3 object storage for replay."""

    def __init__(
        self,
        storage_client: StorageProtocol,
        settings: AppSettings | None = None,
    ) -> None:
        self.storage_client = storage_client
        self.settings = settings or AppSettings()

    async def archive(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_message_id: str,
        raw_payload: bytes | str,
        provider: str,
        provider_thread_id: str | None = None,
        content_type: str = "message/rfc822",
        received_at: datetime | None = None,
    ) -> ArchivedPayloadRef:
        """Store raw provider payload in object storage with metadata headers (R4.10, R5.8).

        Parameters
        ----------
        organization_id : UUID | str
            Tenant organization scoping key.
        mailbox_id : UUID | str
            Mailbox unique identifier.
        provider_message_id : str
            Unique message identifier assigned by upstream provider.
        raw_payload : bytes | str
            Unparsed MIME payload or JSON payload.
        provider : str
            Neutral mail provider identifier.
        provider_thread_id : str | None
            Optional provider conversation thread ID.
        content_type : str
            MIME content type (defaults to 'message/rfc822').
        received_at : datetime | None
            Timestamp when provider received the message.

        Returns
        -------
        ArchivedPayloadRef
            Immutable reference to archived object with SHA-256 and key.
        """
        raw_bytes, sha256_digest = compute_payload_digest(raw_payload)
        size_bytes = len(raw_bytes)
        bucket = self.settings.object_storage.bucket_raw_mime

        object_key = ObjectKeyBuilder.raw_mime(
            organization_id=organization_id,
            mailbox_id=mailbox_id,
            message_id=provider_message_id,
        )

        metadata: dict[str, str] = {
            "organization_id": str(organization_id),
            "mailbox_id": str(mailbox_id),
            "provider_message_id": provider_message_id,
            "sha256": sha256_digest,
            "provider": provider,
            "size_bytes": str(size_bytes),
            "archived_at": datetime.now(UTC).isoformat(),
        }
        if provider_thread_id:
            metadata["provider_thread_id"] = provider_thread_id
        if received_at:
            metadata["received_at"] = received_at.isoformat()

        await self.storage_client.put_bytes(
            bucket=bucket,
            key=object_key,
            data=raw_bytes,
            content_type=content_type,
            metadata=metadata,
        )

        logger.info(
            "Archived raw payload for message %s (org=%s, mbx=%s, size=%d bytes, sha256=%s)",
            provider_message_id,
            organization_id,
            mailbox_id,
            size_bytes,
            sha256_digest[:8],
        )

        return ArchivedPayloadRef(
            object_key=object_key,
            bucket=bucket,
            sha256=sha256_digest,
            size_bytes=size_bytes,
            content_type=content_type,
            metadata=metadata,
        )

    async def retrieve(
        self,
        object_key: str,
        bucket: str | None = None,
        expected_checksum: str | None = None,
    ) -> bytes:
        """Fetch raw payload bytes from object storage with optional integrity verification.

        Parameters
        ----------
        object_key : str
            Object key in storage.
        bucket : str | None
            Target bucket (defaults to bucket_raw_mime).
        expected_checksum : str | None
            Optional expected SHA-256 digest to verify integrity.

        Raises
        ------
        ChecksumMismatchError
            If expected_checksum does not match retrieved bytes SHA-256.
        ObjectNotFoundError
            If object key does not exist.
        """
        target_bucket = bucket or self.settings.object_storage.bucket_raw_mime
        data = await self.storage_client.get_bytes(bucket=target_bucket, key=object_key)

        if expected_checksum is not None:
            actual_digest = hashlib.sha256(data).hexdigest()
            if actual_digest != expected_checksum:
                raise ChecksumMismatchError(
                    f"Integrity check failed for {object_key}: expected {expected_checksum}, "
                    f"got {actual_digest}"
                )

        return data

    def create_replay_envelope(
        self,
        ref: ArchivedPayloadRef,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_message_id: str,
        provider: str,
        provider_thread_id: str | None = None,
        history_id: str | None = None,
        internal_date: datetime | None = None,
        trace_id: str | None = None,
    ) -> JobEnvelope:
        """Construct a persistent JobEnvelope ready to replay to the normalization queue (R4.10).

        Returns
        -------
        JobEnvelope
            Pre-configured envelope for 'email.normalize' queue.
        """
        from packages.broker.envelope import JobEnvelope

        idem_key = derive_idempotency_key(
            organization_id=organization_id,
            mailbox_id=mailbox_id,
            provider_message_id=provider_message_id,
            operation_type="normalize",
        )

        payload: dict[str, Any] = {
            "raw_object_key": ref.object_key,
            "raw_bucket": ref.bucket,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "provider": provider,
            "provider_message_id": provider_message_id,
            "provider_thread_id": provider_thread_id,
            "history_id": history_id,
            "internal_date": internal_date.isoformat() if internal_date else None,
            "replay": True,
        }

        return JobEnvelope(
            idempotency_key=idem_key,
            organization_id=str(organization_id),
            mailbox_id=str(mailbox_id),
            message_id=provider_message_id,
            thread_id=provider_thread_id or "",
            job_type="normalize_email",
            trace_id=trace_id or "",
            payload=payload,
        )

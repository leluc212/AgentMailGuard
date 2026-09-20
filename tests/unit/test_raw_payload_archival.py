"""Unit tests for raw payload archival and replay management (R4.10, R5.8).

Covers:
- Storing binary RFC 822 MIME bytes and string payloads in object storage.
- Deterministic SHA-256 checksumming and standardized key conventions.
- S3 metadata headers for tenant scoping, size, and timestamp.
- Integrity verification during payload retrieval.
- Replay envelope construction for email.normalize queue.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.archive import (
    ArchivedPayloadRef,
    ChecksumMismatchError,
    RawPayloadArchiver,
    compute_payload_digest,
)
from packages.core.idempotency import derive_idempotency_key
from packages.core.storage import FakeObjectStorageClient, ObjectNotFoundError

SAMPLE_RFC822 = b"""From: alice@example.com
To: bob@example.com
Subject: Test Email
Date: Mon, 15 Sep 2026 10:00:00 +0000
Message-ID: <msg-12345@example.com>
Content-Type: text/plain; charset=utf-8

Hello Bob, this is a raw MIME test message for archival and replay.
"""


@pytest.fixture
def storage_client() -> FakeObjectStorageClient:
    return FakeObjectStorageClient()


@pytest.fixture
def archiver(storage_client: FakeObjectStorageClient) -> RawPayloadArchiver:
    return RawPayloadArchiver(storage_client=storage_client)


class TestComputePayloadDigest:
    """Test SHA-256 calculation and byte normalization."""

    def test_digest_bytes(self) -> None:
        raw_bytes, digest = compute_payload_digest(SAMPLE_RFC822)
        assert raw_bytes == SAMPLE_RFC822
        assert digest == hashlib.sha256(SAMPLE_RFC822).hexdigest()

    def test_digest_string(self) -> None:
        payload_str = "JSON payload representation or plain text email"
        raw_bytes, digest = compute_payload_digest(payload_str)
        assert raw_bytes == payload_str.encode("utf-8")
        assert digest == hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


class TestRawPayloadArchiver:
    """Test suite for RawPayloadArchiver."""

    @pytest.mark.asyncio
    async def test_archive_binary_mime_payload(
        self,
        archiver: RawPayloadArchiver,
        storage_client: FakeObjectStorageClient,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        msg_id = "gmail-msg-001"
        now = datetime.now(UTC)

        ref = await archiver.archive(
            organization_id=org_id,
            mailbox_id=mbx_id,
            provider_message_id=msg_id,
            raw_payload=SAMPLE_RFC822,
            provider="dummy_provider",
            provider_thread_id="gmail-thread-001",
            received_at=now,
        )

        # 1. Assert reference attributes
        expected_key = f"raw/{org_id}/{mbx_id}/{msg_id}.eml"
        expected_sha = hashlib.sha256(SAMPLE_RFC822).hexdigest()

        assert isinstance(ref, ArchivedPayloadRef)
        assert ref.object_key == expected_key
        assert ref.bucket == "raw-mime"
        assert ref.sha256 == expected_sha
        assert ref.size_bytes == len(SAMPLE_RFC822)
        assert ref.content_type == "message/rfc822"

        # 2. Verify stored in FakeObjectStorageClient
        assert await storage_client.object_exists(ref.bucket, ref.object_key)
        stored_bytes = await storage_client.get_bytes(ref.bucket, ref.object_key)
        assert stored_bytes == SAMPLE_RFC822

        # 3. Verify metadata
        meta = await storage_client.get_object_metadata(ref.bucket, ref.object_key)
        assert meta["content_type"] == "message/rfc822"
        headers = meta["metadata"]
        assert headers["organization_id"] == str(org_id)
        assert headers["mailbox_id"] == str(mbx_id)
        assert headers["provider_message_id"] == msg_id
        assert headers["provider_thread_id"] == "gmail-thread-001"
        assert headers["sha256"] == expected_sha
        assert headers["size_bytes"] == str(len(SAMPLE_RFC822))
        assert "archived_at" in headers

    @pytest.mark.asyncio
    async def test_archive_string_payload(
        self,
        archiver: RawPayloadArchiver,
        storage_client: FakeObjectStorageClient,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        msg_id = "graph-msg-002"
        json_payload = '{"id": "graph-msg-002", "subject": "Graph Test"}'

        ref = await archiver.archive(
            organization_id=org_id,
            mailbox_id=mbx_id,
            provider_message_id=msg_id,
            raw_payload=json_payload,
            provider="dummy_provider",
            content_type="application/json",
        )

        assert ref.content_type == "application/json"
        assert ref.size_bytes == len(json_payload.encode("utf-8"))
        assert ref.sha256 == hashlib.sha256(json_payload.encode("utf-8")).hexdigest()

        retrieved = await archiver.retrieve(ref.object_key)
        assert retrieved.decode("utf-8") == json_payload

    @pytest.mark.asyncio
    async def test_retrieve_with_integrity_verification(
        self,
        archiver: RawPayloadArchiver,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        msg_id = "msg-verify"

        ref = await archiver.archive(
            organization_id=org_id,
            mailbox_id=mbx_id,
            provider_message_id=msg_id,
            raw_payload=SAMPLE_RFC822,
            provider="dummy_provider",
        )

        # Succeeded verification
        data = await archiver.retrieve(ref.object_key, expected_checksum=ref.sha256)
        assert data == SAMPLE_RFC822

        # Failed verification raises ChecksumMismatchError
        corrupt_sha = "0" * 64
        with pytest.raises(ChecksumMismatchError) as exc_info:
            await archiver.retrieve(ref.object_key, expected_checksum=corrupt_sha)
        assert "Integrity check failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_retrieve_missing_object_raises_not_found(
        self,
        archiver: RawPayloadArchiver,
    ) -> None:
        with pytest.raises(ObjectNotFoundError):
            await archiver.retrieve("raw/nonexistent/key.eml")

    def test_create_replay_envelope(
        self,
        archiver: RawPayloadArchiver,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        msg_id = "replay-msg-001"
        sha = hashlib.sha256(SAMPLE_RFC822).hexdigest()
        now = datetime.now(UTC)

        ref = ArchivedPayloadRef(
            object_key=f"raw/{org_id}/{mbx_id}/{msg_id}.eml",
            bucket="raw-mime",
            sha256=sha,
            size_bytes=len(SAMPLE_RFC822),
            content_type="message/rfc822",
        )

        envelope = archiver.create_replay_envelope(
            ref=ref,
            organization_id=org_id,
            mailbox_id=mbx_id,
            provider_message_id=msg_id,
            provider="dummy_provider",
            provider_thread_id="thread-replay-001",
            history_id="12345",
            internal_date=now,
            trace_id="test-trace-replay-999",
        )

        expected_idem = derive_idempotency_key(
            organization_id=org_id,
            mailbox_id=mbx_id,
            provider_message_id=msg_id,
            operation_type="normalize",
        )

        assert envelope.job_type == "normalize_email"
        assert envelope.organization_id == str(org_id)
        assert envelope.mailbox_id == str(mbx_id)
        assert envelope.message_id == msg_id
        assert envelope.thread_id == "thread-replay-001"
        assert envelope.idempotency_key == expected_idem
        assert envelope.trace_id == "test-trace-replay-999"

        payload = envelope.payload
        assert payload["raw_object_key"] == ref.object_key
        assert payload["raw_bucket"] == "raw-mime"
        assert payload["sha256"] == sha
        assert payload["size_bytes"] == len(SAMPLE_RFC822)
        assert payload["provider"] == "dummy_provider"
        assert payload["replay"] is True
        assert payload["history_id"] == "12345"
        assert payload["internal_date"] == now.isoformat()

        # Verify AMQP message format is persistent
        amqp_msg = envelope.to_message()
        assert amqp_msg.delivery_mode.value == 2  # DeliveryMode.PERSISTENT

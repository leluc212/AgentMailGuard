"""Live integration tests for raw payload archival in MinIO (R4.10, R5.8).

Runs against the live MinIO container on localhost:9000.
Verifies:
- Multi-tenant raw MIME archival across >=3 tenants with strict key scoping.
- Byte-for-byte fidelity and SHA-256 integrity verification.
- Metadata persistence in object storage (tenant, message ID, sha256).
- Replay JobEnvelope reconstitution from live stored payload references.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.broker.envelope import JobEnvelope
from packages.core.archive import RawPayloadArchiver
from packages.core.storage import MinioObjectStorageClient, get_storage_client

SAMPLE_MIME_1 = b"""From: tenant1.user@company.com
To: support@platform.com
Subject: Issue with billing
Date: Wed, 16 Sep 2026 09:30:00 +0000
Message-ID: <t1-msg-101@company.com>
Content-Type: text/plain; charset=utf-8

Please check invoice #45882.
"""

SAMPLE_MIME_2 = b"""From: tenant2.ops@enterprise.org
To: alerts@platform.com
Subject: Certificate renewal notice
Date: Wed, 16 Sep 2026 10:15:00 +0000
Message-ID: <t2-msg-202@enterprise.org>
Content-Type: text/plain; charset=utf-8

Our SSL certificate expires in 14 days.
"""

SAMPLE_MIME_3 = b"""From: tenant3.dev@startup.io
To: dev@platform.com
Subject: API webhook payload format
Date: Wed, 16 Sep 2026 11:00:00 +0000
Message-ID: <t3-msg-303@startup.io>
Content-Type: text/plain; charset=utf-8

We noticed a new field in the webhook payload.
"""


@pytest.fixture
async def storage_client() -> AsyncGenerator[MinioObjectStorageClient, None]:
    client = get_storage_client()
    assert isinstance(client, MinioObjectStorageClient)
    await client.bootstrap_buckets()
    yield client


@pytest.mark.asyncio
async def test_minio_raw_payload_archival_multi_tenant(
    storage_client: MinioObjectStorageClient,
) -> None:
    """Verify raw MIME archival and retrieval across >=3 tenants in MinIO (R4.10, R5.8)."""
    archiver = RawPayloadArchiver(storage_client=storage_client)

    # 3 Tenants (R5.3 / testing strategy mandate)
    org1, org2, org3 = uuid4(), uuid4(), uuid4()
    mbx1, mbx2, mbx3 = uuid4(), uuid4(), uuid4()
    msg1, msg2, msg3 = "msg-live-101", "msg-live-202", "msg-live-303"

    uploaded_keys: list[tuple[str, str]] = []

    try:
        # 1. Archive payloads for Tenant 1, 2, and 3
        now = datetime.now(UTC)
        ref1 = await archiver.archive(
            organization_id=org1,
            mailbox_id=mbx1,
            provider_message_id=msg1,
            raw_payload=SAMPLE_MIME_1,
            provider="dummy_provider",
            provider_thread_id="thread-live-101",
            received_at=now,
        )
        uploaded_keys.append((ref1.bucket, ref1.object_key))

        ref2 = await archiver.archive(
            organization_id=org2,
            mailbox_id=mbx2,
            provider_message_id=msg2,
            raw_payload=SAMPLE_MIME_2,
            provider="dummy_provider",
            provider_thread_id="thread-live-202",
            received_at=now,
        )
        uploaded_keys.append((ref2.bucket, ref2.object_key))

        ref3 = await archiver.archive(
            organization_id=org3,
            mailbox_id=mbx3,
            provider_message_id=msg3,
            raw_payload=SAMPLE_MIME_3,
            provider="dummy_provider",
            provider_thread_id="thread-live-303",
            received_at=now,
        )
        uploaded_keys.append((ref3.bucket, ref3.object_key))

        # 2. Verify deterministic tenant-scoped keys
        assert ref1.object_key == f"raw/{org1}/{mbx1}/{msg1}.eml"
        assert ref2.object_key == f"raw/{org2}/{mbx2}/{msg2}.eml"
        assert ref3.object_key == f"raw/{org3}/{mbx3}/{msg3}.eml"

        # 3. Verify SHA-256 digests
        assert ref1.sha256 == hashlib.sha256(SAMPLE_MIME_1).hexdigest()
        assert ref2.sha256 == hashlib.sha256(SAMPLE_MIME_2).hexdigest()
        assert ref3.sha256 == hashlib.sha256(SAMPLE_MIME_3).hexdigest()

        # 4. Verify MinIO metadata persistence
        meta1 = await storage_client.get_object_metadata(ref1.bucket, ref1.object_key)
        assert meta1["metadata"]["x-amz-meta-organization_id"] == str(org1)
        assert meta1["metadata"]["x-amz-meta-sha256"] == ref1.sha256
        assert meta1["metadata"]["x-amz-meta-provider_message_id"] == msg1

        # 5. Retrieve from MinIO and verify byte-for-byte fidelity with checksum check
        retrieved1 = await archiver.retrieve(ref1.object_key, expected_checksum=ref1.sha256)
        retrieved2 = await archiver.retrieve(ref2.object_key, expected_checksum=ref2.sha256)
        retrieved3 = await archiver.retrieve(ref3.object_key, expected_checksum=ref3.sha256)

        assert retrieved1 == SAMPLE_MIME_1
        assert retrieved2 == SAMPLE_MIME_2
        assert retrieved3 == SAMPLE_MIME_3

        # 6. Verify replay envelope reconstitution (R4.10)
        replay_envelope = archiver.create_replay_envelope(
            ref=ref1,
            organization_id=org1,
            mailbox_id=mbx1,
            provider_message_id=msg1,
            provider="dummy_provider",
            provider_thread_id="thread-live-101",
            internal_date=now,
        )

        assert replay_envelope.job_type == "normalize_email"
        assert replay_envelope.payload["raw_object_key"] == ref1.object_key
        assert replay_envelope.payload["sha256"] == ref1.sha256
        assert replay_envelope.payload["replay"] is True

        # Test AMQP round-trip serialization
        amqp_msg = replay_envelope.to_message()
        restored_job = JobEnvelope.model_validate_json(amqp_msg.body)
        assert restored_job.idempotency_key == replay_envelope.idempotency_key
        assert restored_job.payload["raw_object_key"] == ref1.object_key

    finally:
        # Cleanup uploaded test objects
        for bucket, key in uploaded_keys:
            with contextlib.suppress(Exception):
                await storage_client.delete_object(bucket, key)

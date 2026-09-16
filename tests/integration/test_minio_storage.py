"""Integration tests for MinIO/S3 object storage client against live container (R5.8)."""

from uuid import uuid4

import httpx
import pytest

from packages.core.settings import AppSettings
from packages.core.storage import (
    MinioObjectStorageClient,
    ObjectKeyBuilder,
    ObjectNotFoundError,
    get_storage_client,
)


@pytest.fixture
def storage_client() -> MinioObjectStorageClient:
    """Instantiate live MinIO storage client from settings."""
    client = get_storage_client()
    assert isinstance(client, MinioObjectStorageClient)
    return client


async def test_minio_bootstrap_buckets(storage_client: MinioObjectStorageClient) -> None:
    """Verify bucket bootstrap creates all 3 configured buckets idempotently (R5.8)."""
    # 1. Initial bootstrap
    await storage_client.bootstrap_buckets()
    settings = AppSettings().object_storage
    for b in [settings.bucket_raw_mime, settings.bucket_attachments, settings.bucket_knowledge]:
        assert b in storage_client.configured_buckets

    # 2. Second bootstrap is idempotent and does not raise
    bootstrapped_again = await storage_client.bootstrap_buckets()
    assert isinstance(bootstrapped_again, list)


async def test_minio_raw_mime_lifecycle(storage_client: MinioObjectStorageClient) -> None:
    """Verify raw MIME payload upload, retrieval, and deletion in raw-mime bucket."""
    org_id = uuid4()
    mailbox_id = uuid4()
    msg_id = uuid4()

    key = ObjectKeyBuilder.raw_mime(org_id, mailbox_id, msg_id)
    bucket = storage_client.settings.bucket_raw_mime
    raw_payload = (
        b"From: sender@example.com\r\nTo: recipient@example.com\r\nSubject: Test\r\n\r\nBody."
    )

    try:
        # 1. Upload
        uploaded_key = await storage_client.put_bytes(
            bucket=bucket,
            key=key,
            data=raw_payload,
            content_type="message/rfc822",
        )
        assert uploaded_key == key
        assert await storage_client.object_exists(bucket, key)

        # 2. Download and verify
        downloaded = await storage_client.get_bytes(bucket, key)
        assert downloaded == raw_payload

        # 3. Metadata check
        meta = await storage_client.get_object_metadata(bucket, key)
        assert meta["size"] == len(raw_payload)
        assert meta["content_type"] == "message/rfc822"

    finally:
        # 4. Cleanup
        await storage_client.delete_object(bucket, key)
        assert not await storage_client.object_exists(bucket, key)


async def test_minio_attachment_and_presigned_url(storage_client: MinioObjectStorageClient) -> None:
    """Verify attachment upload, presigned URL generation, and direct HTTP fetch."""
    org_id = uuid4()
    msg_id = uuid4()
    att_id = uuid4()

    key = ObjectKeyBuilder.attachment(org_id, msg_id, att_id, "invoice.pdf")
    bucket = storage_client.settings.bucket_attachments
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"

    try:
        # 1. Upload attachment
        await storage_client.put_bytes(
            bucket=bucket,
            key=key,
            data=pdf_bytes,
            content_type="application/pdf",
        )

        # 2. Generate presigned URL
        url = await storage_client.get_presigned_url(bucket, key, expires_seconds=300)
        assert "http" in url
        assert key in url

        # 3. Download via direct HTTP GET
        async with httpx.AsyncClient() as http_client:
            res = await http_client.get(url)
            assert res.status_code == 200
            assert res.content == pdf_bytes

    finally:
        await storage_client.delete_object(bucket, key)
        assert not await storage_client.object_exists(bucket, key)


async def test_minio_knowledge_document_lifecycle(storage_client: MinioObjectStorageClient) -> None:
    """Verify source knowledge document storage in knowledge-docs bucket."""
    org_id = uuid4()
    doc_id = uuid4()

    key = ObjectKeyBuilder.knowledge_doc(org_id, doc_id, version=1, filename="support_faqs.md")
    bucket = storage_client.settings.bucket_knowledge
    doc_text = b"# Customer Support FAQs\n\nQ: How do I return an item?\nA: Contact support."

    try:
        await storage_client.put_bytes(
            bucket=bucket,
            key=key,
            data=doc_text,
            content_type="text/markdown",
        )
        assert await storage_client.object_exists(bucket, key)

        retrieved = await storage_client.get_bytes(bucket, key)
        assert retrieved == doc_text

    finally:
        await storage_client.delete_object(bucket, key)


async def test_minio_missing_object_error(storage_client: MinioObjectStorageClient) -> None:
    """Verify ObjectNotFoundError is raised when accessing missing objects."""
    bucket = storage_client.settings.bucket_raw_mime
    missing_key = f"non-existent/{uuid4()}.eml"

    assert not await storage_client.object_exists(bucket, missing_key)

    with pytest.raises(ObjectNotFoundError, match="not found"):
        await storage_client.get_bytes(bucket, missing_key)

    with pytest.raises(ObjectNotFoundError, match="not found"):
        await storage_client.get_object_metadata(bucket, missing_key)

"""Unit tests for object storage key conventions and in-memory fake client (R5.8)."""

from uuid import uuid4

import pytest

from packages.core.storage import (
    FakeObjectStorageClient,
    ObjectKeyBuilder,
    ObjectNotFoundError,
    get_storage_client,
)


def test_object_key_builder_raw_mime() -> None:
    """Verify raw MIME key convention."""
    org_id = uuid4()
    mailbox_id = uuid4()
    msg_id = uuid4()

    key = ObjectKeyBuilder.raw_mime(org_id, mailbox_id, msg_id)
    assert key == f"raw/{org_id}/{mailbox_id}/{msg_id}.eml"


def test_object_key_builder_html_body() -> None:
    """Verify HTML body key convention."""
    org_id = uuid4()
    mailbox_id = uuid4()
    msg_id = uuid4()

    key = ObjectKeyBuilder.html_body(org_id, mailbox_id, msg_id)
    assert key == f"html/{org_id}/{mailbox_id}/{msg_id}.html"


def test_object_key_builder_attachment() -> None:
    """Verify attachment key convention and path traversal sanitization."""
    org_id = uuid4()
    msg_id = uuid4()
    att_id = uuid4()

    # Normal filename
    key = ObjectKeyBuilder.attachment(org_id, msg_id, att_id, "report.pdf")
    assert key == f"attachments/{org_id}/{msg_id}/{att_id}/report.pdf"

    # Directory traversal sanitization
    unsafe_key = ObjectKeyBuilder.attachment(org_id, msg_id, att_id, "../../etc/shadow")
    assert ".." not in unsafe_key
    assert unsafe_key == f"attachments/{org_id}/{msg_id}/{att_id}/shadow"

    # Empty filename fallback
    empty_key = ObjectKeyBuilder.attachment(org_id, msg_id, att_id, "")
    assert empty_key == f"attachments/{org_id}/{msg_id}/{att_id}/file"


def test_object_key_builder_knowledge_doc() -> None:
    """Verify knowledge document key convention."""
    org_id = uuid4()
    doc_id = uuid4()

    key = ObjectKeyBuilder.knowledge_doc(org_id, doc_id, version=2, filename="handbook.md")
    assert key == f"knowledge/{org_id}/{doc_id}/v2/handbook.md"

    # Traversal sanitization
    unsafe = ObjectKeyBuilder.knowledge_doc(org_id, doc_id, version=1, filename="../secret.txt")
    assert unsafe == f"knowledge/{org_id}/{doc_id}/v1/secret.txt"


async def test_fake_object_storage_lifecycle() -> None:
    """Verify in-memory fake client CRUD operations, metadata, and error handling."""
    client = get_storage_client(fake=True)
    assert isinstance(client, FakeObjectStorageClient)

    # 1. Bootstrap
    bootstrapped = await client.bootstrap_buckets()
    assert isinstance(bootstrapped, list)

    bucket = "attachments"
    key = "test/org/att-123.pdf"
    content = b"%PDF-1.4 test document content"

    # 2. Object does not exist initially
    assert not await client.object_exists(bucket, key)
    with pytest.raises(ObjectNotFoundError, match="not found"):
        await client.get_bytes(bucket, key)

    # 3. Put bytes
    saved_key = await client.put_bytes(
        bucket=bucket,
        key=key,
        data=content,
        content_type="application/pdf",
        metadata={"author": "alice"},
    )
    assert saved_key == key
    assert await client.object_exists(bucket, key)

    # 4. Get bytes
    retrieved = await client.get_bytes(bucket, key)
    assert retrieved == content

    # 5. Metadata
    meta = await client.get_object_metadata(bucket, key)
    assert meta["size"] == len(content)
    assert meta["content_type"] == "application/pdf"
    assert meta["metadata"] == {"author": "alice"}

    # 6. Presigned URL
    url = await client.get_presigned_url(bucket, key)
    assert key in url
    assert bucket in url

    # 7. Delete
    await client.delete_object(bucket, key)
    assert not await client.object_exists(bucket, key)

    # 8. Presigned URL on missing object raises ObjectNotFoundError
    with pytest.raises(ObjectNotFoundError):
        await client.get_presigned_url(bucket, key)

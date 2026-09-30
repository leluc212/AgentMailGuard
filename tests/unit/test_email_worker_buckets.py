"""The email worker offloads HTML bodies and attachments to the configured buckets (R4.1, R5.8).

The bucket names used to be hard-coded in ``normalize_and_offload`` ("html" was never created, so
the first message with an HTML part failed with NoSuchBucket). The consumer now passes the names
from ObjectStorageSettings, so a deployment that renames a bucket keeps working.
"""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import ObjectStorageSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.message import InMemoryMessageStore
from packages.db.thread import InMemoryThreadStore
from packages.domain.entities import NormalizedMessage
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister


def _html_mail_with_attachment() -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "support@example.com"
    msg["Subject"] = "Invoice question"
    msg.set_content("Please see the attached invoice.")
    msg.add_alternative("<p>Please see the <b>attached</b> invoice.</p>", subtype="html")
    msg.add_attachment(
        b"%PDF-1.4 invoice", maintype="application", subtype="pdf", filename="invoice.pdf"
    )
    return msg.as_bytes()


async def _process(
    storage_settings: ObjectStorageSettings | None,
) -> tuple[FakeObjectStorageClient, NormalizedMessage]:
    """Run one job through a consumer whose storage client uses ``storage_settings``."""
    storage = FakeObjectStorageClient(storage_settings)
    raw_bucket = storage.settings.bucket_raw_mime
    org_id, mailbox_id = uuid4(), uuid4()
    raw_key = f"raw/{org_id}/{mailbox_id}/invoice.eml"
    await storage.put_bytes(raw_bucket, raw_key, _html_mail_with_attachment())
    messages = InMemoryMessageStore()
    publisher = MagicMock(spec=MessagePublisher)
    publisher.publish = AsyncMock()
    consumer = EmailNormalizationConsumer(
        normalizer=EmailNormalizer(),
        persister=EmailPersister(message_store=messages, thread_store=InMemoryThreadStore()),
        storage_client=storage,
        publisher=publisher,
        object_storage_settings=storage_settings,
    )
    envelope = JobEnvelope(
        idempotency_key=f"idem-{uuid4()}",
        organization_id=str(org_id),
        mailbox_id=str(mailbox_id),
        message_id="prov-msg-html-001",
        thread_id="",
        job_type="normalize_email",
        payload={
            "raw_object_key": raw_key,
            "raw_bucket": raw_bucket,
            "provider": "fake",
            "provider_message_id": "prov-msg-html-001",
        },
    )

    await consumer.process_job(envelope, MagicMock())
    stored = await messages.get_message_by_provider_id(org_id, mailbox_id, "prov-msg-html-001")
    assert stored is not None
    return storage, stored


async def test_html_body_and_attachment_go_to_the_configured_buckets() -> None:
    settings = ObjectStorageSettings(bucket_attachments="att-test", bucket_html="html-test")

    storage, _ = await _process(settings)

    assert len(storage.buckets["html-test"]) == 1
    assert len(storage.buckets["att-test"]) == 1
    # The fake creates a bucket on first write, so a write to a default name would show here.
    assert "attachments" not in storage.buckets and "html" not in storage.buckets


async def test_default_settings_keep_the_default_bucket_names() -> None:
    storage, _ = await _process(None)

    assert len(storage.buckets["html"]) == 1
    assert len(storage.buckets["attachments"]) == 1


async def test_persisted_message_points_at_the_html_object() -> None:
    settings = ObjectStorageSettings(bucket_html="html-test")

    storage, stored = await _process(settings)

    (html_key,) = storage.buckets["html-test"]
    assert html_key.startswith("html/") and html_key.endswith(".html")
    assert stored.html_object_key == html_key

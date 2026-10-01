"""Unit tests for normalization failure handling and dead-lettering (R4.9, R3.5)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from packages.broker.consumer import FatalError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.db.message import InMemoryMessageStore
from packages.db.thread import InMemoryThreadStore
from packages.domain.entities import NormalizedMessage
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer, NormalizationContext
from services.email_worker.persister import EmailPersister


class InMemoryStorage:
    """In-memory mock storage client for unit testing."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}

    async def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.blobs[(bucket, key)] = data

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        if (bucket, key) not in self.blobs:
            raise KeyError(f"Key '{key}' not found in bucket '{bucket}'")
        return self.blobs[(bucket, key)]


@pytest.mark.asyncio
async def test_normalizer_corrupted_mime_produces_failed_message() -> None:
    normalizer = EmailNormalizer()
    corrupted_bytes = b"\xff\xfe\x00\x00\x1b\x2cMalformed MIME content that crashes parser\x00\xff"

    org_id = uuid4()
    mbx_id = uuid4()
    msg_id = uuid4()
    context = NormalizationContext(
        organization_id=org_id,
        mailbox_id=mbx_id,
        message_id=msg_id,
        provider="gmail",
        provider_message_id="corrupt-msg-001",
        raw_object_key="raw/corrupt.eml",
    )

    result = normalizer.normalize(raw_mime=corrupted_bytes, context=context)
    msg = result.message

    # R4.9: normalization_failed set to True and raw_object_key retained
    assert msg.normalization_failed is True
    assert msg.raw_object_key == "raw/corrupt.eml"
    assert msg.snippet == "[normalization failed]"
    assert "Malformed MIME" in msg.body_text


@pytest.mark.asyncio
async def test_persister_suppresses_dispatch_on_normalization_failed() -> None:
    msg_store = InMemoryMessageStore()
    thd_store = InMemoryThreadStore()
    persister = EmailPersister(message_store=msg_store, thread_store=thd_store)

    failed_msg = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="gmail",
        provider_message_id="fail-prov-001",
        sender=MagicMock(email=""),
        received_at=MagicMock(),
        subject="[Unparseable]",
        subject_normalized="[Unparseable]",
        body_text="corrupted content",
        body_text_clean="corrupted content",
        snippet="[normalization failed]",
        raw_object_key="raw/fail-prov-001.eml",
        html_object_key=None,
        direction="inbound",
        attachments=[],
        normalization_failed=True,
        signature_stripped=False,
    )

    result = await persister.persist(failed_msg)

    # R4.9: Message is saved into store (NEVER discarded)
    assert result.success is True
    assert result.should_dispatch is False  # Suppressed downstream dispatch

    saved = await msg_store.get_message(failed_msg.organization_id, failed_msg.message_id)
    assert saved is not None
    assert saved.normalization_failed is True
    assert saved.raw_object_key == "raw/fail-prov-001.eml"


@pytest.mark.asyncio
async def test_consumer_persists_and_raises_fatal_error_on_normalization_failure() -> None:
    normalizer = EmailNormalizer()
    msg_store = InMemoryMessageStore()
    thd_store = InMemoryThreadStore()
    persister = EmailPersister(message_store=msg_store, thread_store=thd_store)
    storage = InMemoryStorage()

    org_id = uuid4()
    mbx_id = uuid4()
    raw_key = "raw-emails/corrupt.eml"

    # Store intentionally corrupted bytes
    await storage.put_bytes("raw-emails", raw_key, b"\x00\xff\xfe\x00corrupted payload\xff")

    publisher = MagicMock(spec=MessagePublisher)
    publisher.publish = AsyncMock()

    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage,  # type: ignore[arg-type]
        publisher=publisher,
        triage_exchange="email.triage",
        triage_routing_key="email.triage",
    )

    envelope = JobEnvelope(
        trace_id="test-trace-123",
        idempotency_key="idemp-123",
        organization_id=str(org_id),
        mailbox_id=str(mbx_id),
        message_id="prov-msg-corrupt-001",
        thread_id="",
        job_type="normalize_email",
        payload={
            "raw_object_key": raw_key,
            "raw_bucket": "raw-emails",
            "provider": "gmail",
            "provider_message_id": "prov-msg-corrupt-001",
        },
    )

    # Mock normalizer.normalize_and_offload to return normalization_failed=True
    # to simulate unrecoverable MIME parser failure
    raw_incoming = MagicMock()

    with pytest.raises(FatalError) as exc_info:
        await consumer.process_job(envelope, raw_incoming)

    assert "MIME normalization failed" in str(exc_info.value)

    # Verify R4.9: Message was NOT discarded; it was persisted in database
    saved = await msg_store.get_message_by_provider_id(
        organization_id=org_id,
        mailbox_id=mbx_id,
        provider_message_id="prov-msg-corrupt-001",
    )
    assert saved is not None
    assert saved.raw_object_key == raw_key

    # Verify R4.9: Triage job was NOT published
    assert publisher.publish.call_count == 0


@pytest.mark.asyncio
async def test_consumer_success_path_publishes_triage_job() -> None:
    normalizer = EmailNormalizer()
    msg_store = InMemoryMessageStore()
    thd_store = InMemoryThreadStore()
    persister = EmailPersister(message_store=msg_store, thread_store=thd_store)
    storage = InMemoryStorage()

    org_id = uuid4()
    mbx_id = uuid4()
    raw_key = "raw-emails/valid.eml"

    valid_mime = (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: Meeting Update\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"The meeting is confirmed for 2 PM.\r\n"
    )
    await storage.put_bytes("raw-emails", raw_key, valid_mime)

    publisher = MagicMock(spec=MessagePublisher)
    publisher.publish = AsyncMock()

    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage,  # type: ignore[arg-type]
        publisher=publisher,
        triage_exchange="email.triage",
        triage_routing_key="email.triage",
    )

    envelope = JobEnvelope(
        trace_id="test-trace-valid",
        idempotency_key="idemp-valid",
        organization_id=str(org_id),
        mailbox_id=str(mbx_id),
        message_id="prov-msg-valid-001",
        thread_id="",
        job_type="normalize_email",
        payload={
            "raw_object_key": raw_key,
            "raw_bucket": "raw-emails",
            "provider": "gmail",
            "provider_message_id": "prov-msg-valid-001",
        },
    )

    raw_incoming = MagicMock()
    await consumer.process_job(envelope, raw_incoming)

    # Verify message is persisted with normalization_failed=False
    saved = await msg_store.get_message_by_provider_id(
        organization_id=org_id,
        mailbox_id=mbx_id,
        provider_message_id="prov-msg-valid-001",
    )
    assert saved is not None
    assert saved.normalization_failed is False
    assert saved.subject == "Meeting Update"

    # Verify triage job is published
    assert publisher.publish.call_count == 1
    call_args = publisher.publish.call_args[1]
    assert call_args["exchange_name"] == "email.triage"
    assert call_args["routing_key"] == "email.triage"
    dispatched_env: JobEnvelope = call_args["envelope"]
    assert dispatched_env.job_type == "triage_email"
    assert dispatched_env.payload["provider_message_id"] == "prov-msg-valid-001"


@pytest.mark.asyncio
async def test_consumer_missing_raw_key_raises_fatal_error() -> None:
    normalizer = EmailNormalizer()
    msg_store = InMemoryMessageStore()
    thd_store = InMemoryThreadStore()
    persister = EmailPersister(message_store=msg_store, thread_store=thd_store)
    storage = InMemoryStorage()

    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage,  # type: ignore[arg-type]
    )

    envelope = JobEnvelope(
        trace_id="test-trace-missing",
        idempotency_key="idemp-missing",
        organization_id=str(uuid4()),
        mailbox_id=str(uuid4()),
        message_id="prov-msg-missing",
        thread_id="",
        job_type="normalize_email",
        payload={},  # Missing raw_object_key
    )

    raw_incoming = MagicMock()
    with pytest.raises(FatalError) as exc_info:
        await consumer.process_job(envelope, raw_incoming)

    assert "missing required 'raw_object_key'" in str(exc_info.value)

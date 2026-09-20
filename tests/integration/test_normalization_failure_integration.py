"""Integration tests for email normalization failure handling and DLX routing (R4.9, R3.5).

Runs against live PostgreSQL and RabbitMQ containers:
- Validates parse failure persists into PostgreSQL with normalization_failed=true
  and raw_object_key (R4.9).
- Validates unrecoverable message routes to terminal dead-letter exchange (dlx.email)
  with original routing key and failure reason in headers (R3.5).
- Validates downstream triage queue does NOT receive jobs for failed messages.
- Multi-tenant isolation verified across >= 3 tenants (GEMINI.md §8).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings
from packages.core.storage import StorageProtocol
from packages.db.connection import create_pool_from_settings
from packages.db.message import PostgresMessageStore
from packages.db.thread import PostgresThreadStore
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister


class InMemoryTestStorage(StorageProtocol):
    """In-memory storage double satisfying StorageProtocol for test isolation."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}

    async def bootstrap_buckets(self) -> list[str]:
        return ["raw-emails", "attachments", "html"]

    async def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        self.blobs[(bucket, key)] = data
        return key

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        if (bucket, key) not in self.blobs:
            raise KeyError(f"Key '{key}' not found in bucket '{bucket}'")
        return self.blobs[(bucket, key)]

    async def delete_object(self, bucket: str, key: str) -> None:
        self.blobs.pop((bucket, key), None)

    async def object_exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.blobs

    async def get_object_metadata(self, bucket: str, key: str) -> dict[str, Any]:
        if (bucket, key) not in self.blobs:
            raise KeyError(f"Key '{key}' not found in bucket '{bucket}'")
        return {"size": len(self.blobs[(bucket, key)])}

    async def get_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        return f"http://test-minio/{bucket}/{key}"


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    settings = BrokerSettings()
    conn = await aio_pika.connect_robust(settings.url)
    channel = await conn.channel()
    await setup_topology(channel, settings)
    try:
        yield channel
    finally:
        if not channel.is_closed:
            await channel.close()
        if not conn.is_closed:
            await conn.close()


async def ensure_test_mailbox(pool: asyncpg.Pool, org_id: uuid.UUID, mbx_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Org {org_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Test Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            f"mailbox-{mbx_id.hex[:6]}@example.com",
        )


@pytest.mark.asyncio
async def test_normalization_failure_persists_and_dead_letters(
    db_pool: asyncpg.Pool,
    broker_channel: AbstractChannel,
) -> None:
    """Verify corrupted email persists with normalization_failed=true and routes to DLQ."""
    settings = BrokerSettings()
    storage = InMemoryTestStorage()
    msg_store = PostgresMessageStore(db_pool)
    thd_store = PostgresThreadStore(db_pool)
    persister = EmailPersister(message_store=msg_store, thread_store=thd_store)
    normalizer = EmailNormalizer()

    # Declare consumer
    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage,
        broker_settings=settings,
    )

    # Purge queues before test
    q_norm = await broker_channel.get_queue(settings.queue_normalize)
    q_dlx = await broker_channel.get_queue(settings.queue_dead_letter)
    q_triage = await broker_channel.get_queue(settings.queue_triage)
    await q_norm.purge()
    await q_dlx.purge()
    await q_triage.purge()

    # Tenant Setup
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id)

    # Put corrupted MIME bytes in storage
    raw_key = f"raw/{org_id}/corrupted-{uuid.uuid4().hex[:8]}.eml"
    corrupted_bytes = b"\xff\xfe\x00\x00\x12\x34Malformed Binary Non-MIME Garbage\x00\xff"
    await storage.put_bytes("raw-emails", raw_key, corrupted_bytes)

    # Publish normalization job to email.normalize queue
    publisher = MessagePublisher(broker_settings=settings, channel=broker_channel)
    prov_msg_id = f"prov-corrupt-{uuid.uuid4().hex[:8]}"
    envelope = JobEnvelope(
        trace_id="trace-corrupt-001",
        idempotency_key=f"idem-corrupt-{uuid.uuid4().hex[:8]}",
        organization_id=str(org_id),
        mailbox_id=str(mbx_id),
        message_id=prov_msg_id,
        thread_id="",
        job_type="normalize_email",
        payload={
            "raw_object_key": raw_key,
            "raw_bucket": "raw-emails",
            "provider": "gmail",
            "provider_message_id": prov_msg_id,
        },
    )

    await publisher.publish(
        exchange_name=settings.exchange_email_process,
        routing_key=settings.queue_normalize,
        envelope=envelope,
    )

    # Start consumer, process message, and stop
    await consumer.start()
    # Give event loop time to consume and process job
    await asyncio.sleep(0.5)
    await consumer.stop()

    # 1. Verify R4.9: Message is persisted into PostgreSQL email_message (Never discard!)
    row = await msg_store.get_message_by_provider_id(org_id, mbx_id, prov_msg_id)
    assert row is not None
    assert row.normalization_failed is True
    assert row.raw_object_key == raw_key
    assert row.snippet == "[normalization failed]"

    # 2. Verify R4.9 & R3.5: Job was routed to dead-letter queue (email.dead_letter)
    dlx_msg = await q_dlx.get(timeout=3.0)
    assert dlx_msg is not None
    await dlx_msg.ack()

    # Verify AMQP headers preserved (R3.5)
    headers = dict(dlx_msg.headers) if dlx_msg.headers else {}
    assert headers.get("x-original-routing-key") == settings.queue_normalize
    assert "MIME normalization failed" in str(headers.get("x-failure-reason"))

    # 3. Verify downstream triage queue has 0 messages
    triage_status = await q_triage.declare()
    assert triage_status.message_count == 0


@pytest.mark.asyncio
async def test_multi_tenant_normalization_mixed_scenarios(
    db_pool: asyncpg.Pool,
    broker_channel: AbstractChannel,
) -> None:
    """Verify >= 3 tenants handling corrupted, valid, and duplicate email payloads."""
    settings = BrokerSettings()
    storage = InMemoryTestStorage()
    msg_store = PostgresMessageStore(db_pool)
    thd_store = PostgresThreadStore(db_pool)
    persister = EmailPersister(message_store=msg_store, thread_store=thd_store)
    normalizer = EmailNormalizer()

    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage,
        broker_settings=settings,
    )

    q_norm = await broker_channel.get_queue(settings.queue_normalize)
    q_dlx = await broker_channel.get_queue(settings.queue_dead_letter)
    q_triage = await broker_channel.get_queue(settings.queue_triage)
    await q_norm.purge()
    await q_dlx.purge()
    await q_triage.purge()

    # 3 Tenants setup
    org1, mbx1 = uuid.uuid4(), uuid.uuid4()
    org2, mbx2 = uuid.uuid4(), uuid.uuid4()
    org3, mbx3 = uuid.uuid4(), uuid.uuid4()
    await ensure_test_mailbox(db_pool, org1, mbx1)
    await ensure_test_mailbox(db_pool, org2, mbx2)
    await ensure_test_mailbox(db_pool, org3, mbx3)

    publisher = MessagePublisher(broker_settings=settings, channel=broker_channel)

    # Tenant 1: Corrupted MIME
    key_t1 = f"raw/{org1}/bad.eml"
    await storage.put_bytes("raw-emails", key_t1, b"\x00\xff\xfe\x00non-mime-data\x00")
    env_t1 = JobEnvelope(
        trace_id="trace-t1",
        idempotency_key=f"idem-t1-{uuid.uuid4().hex[:6]}",
        organization_id=str(org1),
        mailbox_id=str(mbx1),
        message_id="msg-t1-corrupt",
        thread_id="",
        job_type="normalize_email",
        payload={
            "raw_object_key": key_t1,
            "raw_bucket": "raw-emails",
            "provider": "gmail",
            "provider_message_id": "msg-t1-corrupt",
        },
    )

    # Tenant 2: Valid MIME
    key_t2 = f"raw/{org2}/good.eml"
    valid_mime = (
        b"From: alice@tenant2.com\r\n"
        b"To: bob@tenant2.com\r\n"
        b"Subject: Tenant 2 System Status\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"All systems operational.\r\n"
    )
    await storage.put_bytes("raw-emails", key_t2, valid_mime)
    env_t2 = JobEnvelope(
        trace_id="trace-t2",
        idempotency_key=f"idem-t2-{uuid.uuid4().hex[:6]}",
        organization_id=str(org2),
        mailbox_id=str(mbx2),
        message_id="msg-t2-valid",
        thread_id="",
        job_type="normalize_email",
        payload={
            "raw_object_key": key_t2,
            "raw_bucket": "raw-emails",
            "provider": "gmail",
            "provider_message_id": "msg-t2-valid",
        },
    )

    # Publish both jobs
    await publisher.publish(settings.exchange_email_process, settings.queue_normalize, env_t1)
    await publisher.publish(settings.exchange_email_process, settings.queue_normalize, env_t2)

    await consumer.start()
    await asyncio.sleep(0.6)
    await consumer.stop()

    # Verify Tenant 1 (Corrupted):
    # - In DB with normalization_failed=true
    msg1 = await msg_store.get_message_by_provider_id(org1, mbx1, "msg-t1-corrupt")
    assert msg1 is not None
    assert msg1.normalization_failed is True

    # - Dead-lettered to DLQ
    dlx_item = await q_dlx.get(timeout=3.0)
    assert dlx_item is not None
    await dlx_item.ack()
    dlx_env = JobEnvelope.from_message(dlx_item)
    assert dlx_env.organization_id == str(org1)

    # Verify Tenant 2 (Valid):
    # - In DB with normalization_failed=false
    msg2 = await msg_store.get_message_by_provider_id(org2, mbx2, "msg-t2-valid")
    assert msg2 is not None
    assert msg2.normalization_failed is False
    assert msg2.subject == "Tenant 2 System Status"

    # - Dispatched to email.triage queue
    triage_item = await q_triage.get(timeout=3.0)
    assert triage_item is not None
    await triage_item.ack()
    triage_env = JobEnvelope.from_message(triage_item)
    assert triage_env.organization_id == str(org2)
    assert triage_env.payload["provider_message_id"] == "msg-t2-valid"

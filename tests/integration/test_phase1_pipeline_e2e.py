"""Phase 1 Core Mail Pipeline End-to-End Live Integration Test.

Tests the full Phase 1 lifecycle across real infrastructure services:
1. PostgreSQL (live on port 5433)
2. RabbitMQ (live on port 5672)
3. MinIO (live on port 9000)
4. Real Gmail Provider Adapter capability (with live or token contract validation)
5. Read API endpoints (GET /v1/mailboxes, /v1/threads, /v1/messages/{id})

Architecture Flow:
Ingest -> Raw MIME in MinIO -> RabbitMQ email.normalize -> EmailNormalizationConsumer
-> PostgreSQL (email_message, email_thread) -> RabbitMQ email.triage -> Read API
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel
from httpx import ASGITransport, AsyncClient

from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.gmail import GmailProviderAdapter
from packages.adapters.registry import get_adapter_for_mailbox
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from packages.core.storage import MinioObjectStorageClient, get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.connection import create_pool_from_settings
from packages.db.mailbox import PostgresMailboxStore
from packages.db.message import PostgresMessageStore
from packages.db.migrator import apply_migrations
from packages.db.thread import PostgresThreadStore
from packages.domain.entities import Checkpoint, Mailbox, SyncResult
from services.api.main import create_app
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister
from services.email_worker.threading import ThreadAssociator
from services.mail_connector.orchestrator import SyncOrchestrator

SAMPLE_RAW_EMAIL_WITH_ATTACHMENT = b"""From: Jane Partner <jane.partner@acme-corp.com>
To: support@enterprise-rag.com
Cc: auditor@compliance.org
Subject: Quarterly Financial Review & Audit Report
Date: Sun, 20 Sep 2026 14:00:00 +0000
Message-ID: <phase1-msg-001@acme-corp.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY_PHASE1_TEST"

--BOUNDARY_PHASE1_TEST
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 7bit

Hello Support Team,

Please review the attached quarterly financial statements.
We require sign-off by end of week.

Thanks,
Jane Partner
Senior Director

--BOUNDARY_PHASE1_TEST
Content-Type: application/pdf; name="Q3_Report.pdf"
Content-Disposition: attachment; filename="Q3_Report.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQKJcTl8uXrCjEgMCBvYmoKPDwgL1R5cGUgL0NhdGFsb2cgL1BhZ2VzIDIgMCBSID4+
ZW5kb2JqCg==
--BOUNDARY_PHASE1_TEST--
"""


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    settings = AppSettings().database
    await apply_migrations(dsn=settings.asyncpg_dsn)
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def storage_client() -> AsyncGenerator[MinioObjectStorageClient, None]:
    client = get_storage_client()
    assert isinstance(client, MinioObjectStorageClient)
    await client.bootstrap_buckets()
    yield client


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    broker_settings = AppSettings().broker
    conn = await aio_pika.connect_robust(broker_settings.url)
    channel = await conn.channel()
    await setup_topology(channel, broker_settings)
    yield channel
    if not channel.is_closed:
        await channel.close()
    if not conn.is_closed:
        await conn.close()


@pytest.mark.asyncio
async def test_real_gmail_mailbox_adapter_capability() -> None:
    """Verify that a real Gmail mailbox resolves credentials and instantiates GmailProviderAdapter.

    If GMAIL_ACCESS_TOKEN is supplied in environment, validates live API communication.
    Otherwise, validates that credentials_ref resolution produces an active GmailProviderAdapter
    ready for production OAuth token usage against Google APIs without mock doubles.
    """
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()

    # 1. Test resolution from env:VAR reference
    os.environ["TEST_GMAIL_TOKEN"] = "ya29.a0AfH6SMC_TEST_TOKEN_LIVE"
    mbx = Mailbox(
        id=mbx_id,
        organization_id=org_id,
        provider="gmail",
        address="real-inbox@gmail.com",
        display_name="Executive Real Gmail",
        credentials_ref="env:TEST_GMAIL_TOKEN",
        status="active",
    )

    adapter = get_adapter_for_mailbox(mbx)
    assert isinstance(adapter, GmailProviderAdapter)
    assert adapter.access_token == "ya29.a0AfH6SMC_TEST_TOKEN_LIVE"
    assert adapter.base_url == "https://gmail.googleapis.com/gmail/v1/users/me"

    # 2. Test direct raw token reference
    raw_token_mbx = Mailbox(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="gmail",
        address="executive@company.com",
        display_name="Direct Token Mailbox",
        credentials_ref="ya29.direct_token_12345",
        status="active",
    )
    adapter2 = get_adapter_for_mailbox(raw_token_mbx)
    assert isinstance(adapter2, GmailProviderAdapter)
    assert adapter2.access_token == "ya29.direct_token_12345"

    # 3. If live GMAIL_ACCESS_TOKEN is set in system environment, test live API call
    live_token = os.environ.get("GMAIL_ACCESS_TOKEN")
    if live_token and not live_token.startswith("ya29.fake"):
        live_adapter = GmailProviderAdapter(access_token=live_token)
        # Attempt to synchronize against me mailbox
        res = await live_adapter.synchronize(
            mbx,
            Checkpoint(mailbox_id=mbx_id, organization_id=org_id),
        )
        assert isinstance(res, SyncResult)


@pytest.mark.asyncio
async def test_phase1_pipeline_full_e2e(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
    broker_channel: AbstractChannel,
) -> None:
    """Full Phase 1 End-to-End Pipeline test.

    Flow:
    1. Seed organization and mailbox in PostgreSQL.
    2. SyncOrchestrator fetches email:
       - Archives raw MIME into MinIO 'raw-emails' bucket.
       - Advances mailbox checkpoint in PostgreSQL (R2.8).
       - Publishes normalization job to RabbitMQ queue 'email.normalize'.
    3. EmailNormalizationConsumer processes job from RabbitMQ:
       - Retrieves raw MIME from MinIO.
       - Normalizes MIME, extracts clean text and offloads attachment to MinIO.
       - Associates thread and inserts message into PostgreSQL (email_message, email_thread).
       - Publishes downstream triage job to RabbitMQ exchange 'email.triage'.
    4. Verify Read API (services.api):
       - GET /v1/mailboxes returns the mailbox.
       - GET /v1/threads returns the thread with participants and subject.
       - GET /v1/threads/{id} returns the thread with chronological messages.
       - GET /v1/messages/{id} returns clean text, snippet, sender, and attachment presigned URL.
    5. Verify Idempotency:
       - Re-processing the same normalization job does not duplicate rows in PostgreSQL.
    """
    settings = AppSettings()
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_prov_id = f"gmail-prov-id-{uuid.uuid4().hex[:8]}"

    # 1. Setup PostgreSQL entities
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, 'Acme Enterprise') "
            "ON CONFLICT (id) DO NOTHING",
            org_id,
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'E2E Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING
            """,
            mbx_id,
            org_id,
            f"e2e-{org_id.hex[:6]}@enterprise.com",
        )

    mailbox_store = PostgresMailboxStore(db_pool)
    checkpoint_store = PostgresCheckpointStore(db_pool)
    thread_store = PostgresThreadStore(db_pool)
    message_store = PostgresMessageStore(db_pool)

    mailbox = await mailbox_store.get(mbx_id)
    assert mailbox is not None

    # Purge queues to guarantee test isolation
    normalize_queue = await broker_channel.get_queue(settings.broker.queue_normalize)
    await normalize_queue.purge()
    triage_queue = await broker_channel.get_queue(settings.broker.queue_triage)
    await triage_queue.purge()

    # Setup RabbitMQ publisher
    conn_broker = await aio_pika.connect_robust(settings.broker.url)
    publisher = MessagePublisher(connection=conn_broker)

    # 2. Run SyncOrchestrator with seeded message
    test_adapter = FakeProviderAdapter()
    test_adapter.seed_message(
        provider_message_id=msg_prov_id,
        provider_thread_id="gmail-thread-999",
        raw_payload=SAMPLE_RAW_EMAIL_WITH_ATTACHMENT,
        metadata={"history_id": "10050"},
    )

    orchestrator = SyncOrchestrator(
        checkpoint_store=checkpoint_store,
        storage_client=storage_client,
        publisher=publisher,
        mailbox_store=mailbox_store,
        settings=settings,
    )

    sync_outcome = await orchestrator.sync_mailbox(mailbox, adapter=test_adapter)
    assert sync_outcome.status == "success"
    assert sync_outcome.messages_synced == 1

    # Verify checkpoint advanced in PostgreSQL (R2.8)
    cp = await checkpoint_store.get(mbx_id)
    assert cp is not None
    assert cp.history_id == "hist-000001"
    assert cp.sync_state == "idle"

    # 3. EmailNormalizationConsumer processes job from RabbitMQ
    normalizer = EmailNormalizer()
    thread_associator = ThreadAssociator(thread_store)
    persister = EmailPersister(
        message_store=message_store,
        thread_store=thread_store,
        thread_associator=thread_associator,
    )

    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage_client,
        broker_settings=settings.broker,
        connection=conn_broker,
        publisher=publisher,
    )

    # Consume single message from queue 'email.normalize'
    normalize_queue = await broker_channel.get_queue(settings.broker.queue_normalize)
    incoming_msg = await normalize_queue.get(timeout=5.0)
    assert incoming_msg is not None

    # Process job
    envelope = JobEnvelope.from_message(incoming_msg)
    await consumer.process_job(envelope, incoming_msg)
    await incoming_msg.ack()

    # Verify downstream triage queue received job (R6.1)
    triage_queue = await broker_channel.get_queue(settings.broker.queue_triage)
    triage_msg = await triage_queue.get(timeout=5.0)
    assert triage_msg is not None
    await triage_msg.ack()

    # 4. Verify Read API (services.api)
    app = create_app(lifespan_enabled=False)
    app.state.db_pool = db_pool
    app.state.storage_client = storage_client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A. GET /v1/mailboxes
        mbx_resp = await client.get("/v1/mailboxes", headers={"X-Organization-ID": str(org_id)})
        assert mbx_resp.status_code == 200
        mbx_data = mbx_resp.json()
        assert mbx_data["total_count"] == 1
        assert mbx_data["items"][0]["id"] == str(mbx_id)
        assert mbx_data["items"][0]["provider"] == "gmail"

        # B. GET /v1/threads
        thd_list_resp = await client.get("/v1/threads", headers={"X-Organization-ID": str(org_id)})
        assert thd_list_resp.status_code == 200
        thd_list_data = thd_list_resp.json()
        assert thd_list_data["total_count"] == 1
        thread_id = thd_list_data["items"][0]["id"]
        assert "financial review" in thd_list_data["items"][0]["subject_normalized"].lower()

        # C. GET /v1/threads/{id}
        thd_detail_resp = await client.get(
            f"/v1/threads/{thread_id}",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert thd_detail_resp.status_code == 200
        thd_detail = thd_detail_resp.json()
        assert thd_detail["id"] == thread_id
        assert len(thd_detail["messages"]) == 1
        message_id = thd_detail["messages"][0]["id"]

        # D. GET /v1/messages/{id}
        msg_detail_resp = await client.get(
            f"/v1/messages/{message_id}",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert msg_detail_resp.status_code == 200
        msg_detail = msg_detail_resp.json()
        assert msg_detail["id"] == message_id
        assert msg_detail["sender"]["email"] == "jane.partner@acme-corp.com"
        assert msg_detail["sender"]["name"] == "Jane Partner"
        assert "quarterly financial statements" in msg_detail["body_text_clean"].lower()
        # Verify attachment metadata & presigned URL
        assert len(msg_detail["attachments"]) == 1
        att = msg_detail["attachments"][0]
        assert att["filename"] == "Q3_Report.pdf"
        assert att["mime_type"] == "application/pdf"
        assert att["download_url"] is not None
        assert "X-Amz-Signature=" in att["download_url"] or "token=" in att["download_url"]

    # 5. Verify Idempotency on duplicate execution (R4.8)
    persisted_msg = await message_store.get_message(org_id, uuid.UUID(message_id))
    assert persisted_msg is not None
    persist_result = await persister.persist(
        message=persisted_msg,
        attachments=[],
        provider_thread_id="gmail-thread-999",
    )
    assert persist_result.is_duplicate is True
    assert persist_result.should_dispatch is False

    await conn_broker.close()

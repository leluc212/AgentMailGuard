"""Multi-tenant integration tests for mail data read API against live PostgreSQL.

Requirements: R23.2, R23.6, R5.3.
- Multi-tenant fixtures seed >= 3 tenants with overlapping content (GEMINI.md §8).
- Organization scoping strictly enforced: cross-tenant access returns 404.
- Pagination limits and offsets hold against PostgreSQL tables.
- Chronological ordering of messages in threads verified.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.core.settings import AppSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.connection import create_pool_from_settings
from packages.db.message import PostgresMessageStore
from packages.db.thread import PostgresThreadStore
from packages.domain.entities import (
    AttachmentRef,
    EmailAddress,
    EmailThread,
    NormalizedMessage,
)
from services.api.main import create_app


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def api_app(db_pool: asyncpg.Pool) -> FastAPI:
    app = create_app(lifespan_enabled=False)
    app.state.db_pool = db_pool
    app.state.storage_client = FakeObjectStorageClient()
    return app


async def create_tenant_mailbox(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    address: str,
    provider: str = "gmail",
    status: str = "active",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Org {org_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status;
            """,
            mbx_id,
            org_id,
            provider,
            address,
            f"Mailbox {address}",
            status,
        )


@pytest.mark.asyncio
async def test_mail_read_api_multi_tenant_isolation(
    api_app: FastAPI, db_pool: asyncpg.Pool
) -> None:
    """Verify >= 3 tenants query their mailboxes, threads, and messages in total isolation."""
    # 1. Setup 3 distinct tenants with overlapping provider IDs and names (GEMINI.md §8)
    org1, mbx1 = uuid.uuid4(), uuid.uuid4()
    org2, mbx2 = uuid.uuid4(), uuid.uuid4()
    org3, mbx3 = uuid.uuid4(), uuid.uuid4()

    await create_tenant_mailbox(db_pool, org1, mbx1, f"shared-{org1.hex[:4]}@tenant1.com", "gmail")
    await create_tenant_mailbox(db_pool, org2, mbx2, f"shared-{org2.hex[:4]}@tenant2.com", "graph")
    await create_tenant_mailbox(db_pool, org3, mbx3, f"shared-{org3.hex[:4]}@tenant3.com", "imap")

    thd_store = PostgresThreadStore(db_pool)
    msg_store = PostgresMessageStore(db_pool)

    now = datetime.now(UTC)

    # Seed threads for each tenant with identical normalized subjects
    thd1_id = uuid.uuid4()
    thd2_id = uuid.uuid4()
    thd3_id = uuid.uuid4()

    t1 = EmailThread(
        id=thd1_id,
        organization_id=org1,
        mailbox_id=mbx1,
        provider_thread_id="thread-shared-001",
        subject_normalized="contract review",
        participants=["user1@tenant1.com"],
        last_message_at=now - timedelta(hours=1),
        message_count=1,
    )
    t2 = EmailThread(
        id=thd2_id,
        organization_id=org2,
        mailbox_id=mbx2,
        provider_thread_id="thread-shared-001",
        subject_normalized="contract review",
        participants=["user2@tenant2.com"],
        last_message_at=now,
        message_count=2,
    )
    t3 = EmailThread(
        id=thd3_id,
        organization_id=org3,
        mailbox_id=mbx3,
        provider_thread_id="thread-shared-001",
        subject_normalized="contract review",
        participants=["user3@tenant3.com"],
        last_message_at=now + timedelta(hours=1),
        message_count=1,
    )

    await thd_store.create_thread(t1)
    await thd_store.create_thread(t2)
    await thd_store.create_thread(t3)

    # Seed messages
    msg1_id = uuid.uuid4()
    msg2_id = uuid.uuid4()
    att1 = AttachmentRef(
        filename="contract_t1.pdf",
        mime_type="application/pdf",
        size_bytes=50000,
        object_key=f"attachments/{org1}/c1.pdf",
    )

    m1 = NormalizedMessage(
        message_id=msg1_id,
        thread_id=thd1_id,
        mailbox_id=mbx1,
        organization_id=org1,
        provider="gmail",
        provider_message_id="msg-shared-prov-id",
        sender=EmailAddress(name="Alice", email="user1@tenant1.com"),
        received_at=now - timedelta(hours=1),
        subject="Contract Review",
        subject_normalized="contract review",
        body_text="Tenant 1 contract text.",
        body_text_clean="Tenant 1 contract text.",
        snippet="Tenant 1 contract text.",
        attachments=[att1],
    )
    m2 = NormalizedMessage(
        message_id=msg2_id,
        thread_id=thd2_id,
        mailbox_id=mbx2,
        organization_id=org2,
        provider="graph",
        provider_message_id="msg-shared-prov-id",
        sender=EmailAddress(name="Bob", email="user2@tenant2.com"),
        received_at=now,
        subject="Contract Review",
        subject_normalized="contract review",
        body_text="Tenant 2 contract text.",
        body_text_clean="Tenant 2 contract text.",
        snippet="Tenant 2 contract text.",
    )

    await msg_store.insert_message(m1)
    await msg_store.insert_message(m2)

    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # -------------------------------------------------------------------
        # 1. GET /v1/mailboxes
        # -------------------------------------------------------------------
        resp1 = await client.get("/v1/mailboxes", headers={"X-Organization-ID": str(org1)})
        assert resp1.status_code == 200
        mbx_items_1 = resp1.json()["items"]
        assert len(mbx_items_1) == 1
        assert mbx_items_1[0]["id"] == str(mbx1)
        assert mbx_items_1[0]["provider"] == "gmail"

        resp2 = await client.get("/v1/mailboxes", headers={"X-Organization-ID": str(org2)})
        assert resp2.status_code == 200
        mbx_items_2 = resp2.json()["items"]
        assert len(mbx_items_2) == 1
        assert mbx_items_2[0]["id"] == str(mbx2)
        assert mbx_items_2[0]["provider"] == "graph"

        # -------------------------------------------------------------------
        # 2. GET /v1/threads (List)
        # -------------------------------------------------------------------
        thd_resp_1 = await client.get("/v1/threads", headers={"X-Organization-ID": str(org1)})
        assert thd_resp_1.status_code == 200
        assert thd_resp_1.json()["total_count"] == 1
        assert thd_resp_1.json()["items"][0]["id"] == str(thd1_id)

        thd_resp_3 = await client.get("/v1/threads", headers={"X-Organization-ID": str(org3)})
        assert thd_resp_3.status_code == 200
        assert thd_resp_3.json()["total_count"] == 1
        assert thd_resp_3.json()["items"][0]["id"] == str(thd3_id)

        # -------------------------------------------------------------------
        # 3. GET /v1/threads/{id} (Detail + messages)
        # -------------------------------------------------------------------
        # Tenant 1 can view its own thread
        thd_detail_1 = await client.get(
            f"/v1/threads/{thd1_id}",
            headers={"X-Organization-ID": str(org1)},
        )
        assert thd_detail_1.status_code == 200
        assert thd_detail_1.json()["id"] == str(thd1_id)
        assert len(thd_detail_1.json()["messages"]) == 1
        assert thd_detail_1.json()["messages"][0]["id"] == str(msg1_id)

        # Tenant 2 cannot view Tenant 1's thread (404)
        thd_cross = await client.get(
            f"/v1/threads/{thd1_id}",
            headers={"X-Organization-ID": str(org2)},
        )
        assert thd_cross.status_code == 404

        # -------------------------------------------------------------------
        # 4. GET /v1/messages/{id} (Detail + attachments)
        # -------------------------------------------------------------------
        # Tenant 1 can view its own message
        msg_detail_1 = await client.get(
            f"/v1/messages/{msg1_id}",
            headers={"X-Organization-ID": str(org1)},
        )
        assert msg_detail_1.status_code == 200
        data1 = msg_detail_1.json()
        assert data1["id"] == str(msg1_id)
        assert data1["sender"]["email"] == "user1@tenant1.com"
        assert len(data1["attachments"]) == 1
        assert data1["attachments"][0]["filename"] == "contract_t1.pdf"

        # Tenant 3 cannot view Tenant 1's message (404)
        msg_cross = await client.get(
            f"/v1/messages/{msg1_id}",
            headers={"X-Organization-ID": str(org3)},
        )
        assert msg_cross.status_code == 404


@pytest.mark.asyncio
async def test_mail_read_api_pagination_and_filters(
    api_app: FastAPI, db_pool: asyncpg.Pool
) -> None:
    """Verify pagination, filtering and ordering on mailboxes and threads against PostgreSQL."""
    org_id = uuid.uuid4()
    mbx1 = uuid.uuid4()
    mbx2 = uuid.uuid4()

    # Seed 2 mailboxes: one active/gmail, one paused/imap
    await create_tenant_mailbox(
        db_pool, org_id, mbx1, f"mbx1-{org_id.hex[:4]}@test.com", "gmail", status="active"
    )
    await create_tenant_mailbox(
        db_pool, org_id, mbx2, f"mbx2-{org_id.hex[:4]}@test.com", "imap", status="paused"
    )

    thd_store = PostgresThreadStore(db_pool)
    msg_store = PostgresMessageStore(db_pool)
    now = datetime.now(UTC)

    # Seed 3 threads with differing last_message_at and statuses
    t_old = EmailThread(
        id=uuid.uuid4(),
        organization_id=org_id,
        mailbox_id=mbx1,
        provider_thread_id="thd-old",
        subject_normalized="old thread",
        participants=["old@test.com"],
        last_message_at=now - timedelta(days=2),
        message_count=1,
        status="closed",
    )
    t_mid = EmailThread(
        id=uuid.uuid4(),
        organization_id=org_id,
        mailbox_id=mbx1,
        provider_thread_id="thd-mid",
        subject_normalized="mid thread",
        participants=["mid@test.com"],
        last_message_at=now - timedelta(days=1),
        message_count=2,
        status="active",
    )
    t_new = EmailThread(
        id=uuid.uuid4(),
        organization_id=org_id,
        mailbox_id=mbx2,
        provider_thread_id="thd-new",
        subject_normalized="new thread",
        participants=["new@test.com"],
        last_message_at=now,
        message_count=1,
        status="active",
    )
    await thd_store.create_thread(t_old)
    await thd_store.create_thread(t_mid)
    await thd_store.create_thread(t_new)

    # Seed 2 messages for t_mid to test chronological message ordering
    m_earlier = NormalizedMessage(
        message_id=uuid.uuid4(),
        thread_id=t_mid.id,
        mailbox_id=mbx1,
        organization_id=org_id,
        provider="gmail",
        provider_message_id="mid-msg-1",
        sender=EmailAddress(name="Mid 1", email="mid1@test.com"),
        received_at=now - timedelta(days=1, minutes=10),
        subject="Mid Subject",
        subject_normalized="mid thread",
        body_text="First message",
        body_text_clean="First message",
        snippet="First message",
    )
    m_later = NormalizedMessage(
        message_id=uuid.uuid4(),
        thread_id=t_mid.id,
        mailbox_id=mbx1,
        organization_id=org_id,
        provider="gmail",
        provider_message_id="mid-msg-2",
        sender=EmailAddress(name="Mid 2", email="mid2@test.com"),
        received_at=now - timedelta(days=1),
        subject="Mid Subject",
        subject_normalized="mid thread",
        body_text="Second message",
        body_text_clean="Second message",
        snippet="Second message",
    )
    # Insert in reverse order to ensure DB query orders by received_at ASC
    await msg_store.insert_message(m_later)
    await msg_store.insert_message(m_earlier)

    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Filter mailboxes by provider=gmail
        mbx_f = await client.get(
            "/v1/mailboxes?provider=gmail",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert mbx_f.status_code == 200
        assert mbx_f.json()["total_count"] == 1
        assert mbx_f.json()["items"][0]["id"] == str(mbx1)

        # Filter mailboxes by status=paused
        mbx_p = await client.get(
            "/v1/mailboxes?status=paused",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert mbx_p.status_code == 200
        assert mbx_p.json()["total_count"] == 1
        assert mbx_p.json()["items"][0]["id"] == str(mbx2)

        # Threads ordering: newest first (t_new, t_mid, t_old)
        thd_list = await client.get(
            "/v1/threads?limit=2&offset=0",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert thd_list.status_code == 200
        data = thd_list.json()
        assert data["total_count"] == 3
        assert len(data["items"]) == 2
        assert data["items"][0]["id"] == str(t_new.id)
        assert data["items"][1]["id"] == str(t_mid.id)

        # Filter threads by mailbox_id=mbx1
        thd_mbx = await client.get(
            f"/v1/threads?mailbox_id={mbx1}",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert thd_mbx.status_code == 200
        assert thd_mbx.json()["total_count"] == 2

        # Thread detail message ordering (received_at ASC)
        thd_detail = await client.get(
            f"/v1/threads/{t_mid.id}",
            headers={"X-Organization-ID": str(org_id)},
        )
        assert thd_detail.status_code == 200
        msgs = thd_detail.json()["messages"]
        assert len(msgs) == 2
        assert msgs[0]["id"] == str(m_earlier.message_id)
        assert msgs[1]["id"] == str(m_later.message_id)

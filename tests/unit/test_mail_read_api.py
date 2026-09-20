"""Pure unit tests for mail data read endpoints and provider credentials (R23.2, R23.6, R5.3)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.adapters.gmail import GmailProviderAdapter
from packages.adapters.registry import (
    get_adapter_for_mailbox,
    resolve_provider_credentials,
)
from packages.core.storage import FakeObjectStorageClient
from packages.db.mailbox import InMemoryMailboxStore
from packages.db.message import InMemoryMessageStore
from packages.db.thread import InMemoryThreadStore
from packages.domain.entities import (
    AttachmentRef,
    EmailAddress,
    EmailThread,
    Mailbox,
    NormalizedMessage,
)
from services.api.main import create_app


@pytest.fixture
def org_a() -> UUID:
    return uuid4()


@pytest.fixture
def org_b() -> UUID:
    return uuid4()


@pytest.fixture
def test_app() -> FastAPI:
    """Create test FastAPI application with in-memory stores attached to app.state."""
    app = create_app(lifespan_enabled=False)
    app.state.mailbox_store = InMemoryMailboxStore()
    app.state.thread_store = InMemoryThreadStore()
    app.state.message_store = InMemoryMessageStore()
    app.state.storage_client = FakeObjectStorageClient()
    return app


@pytest.mark.asyncio
async def test_list_mailboxes_paginated_and_filtered(
    test_app: FastAPI, org_a: UUID, org_b: UUID
) -> None:
    """Verify GET /v1/mailboxes tenant isolation, pagination, and status filtering.

    Requirements: R23.2, R23.6.
    """
    mbx_store: InMemoryMailboxStore = test_app.state.mailbox_store

    # Seed 3 mailboxes in org_a, 2 in org_b
    mbx_a1 = Mailbox(
        id=uuid4(),
        organization_id=org_a,
        provider="gmail",
        address="support@orga.com",
        display_name="Org A Support",
        status="active",
    )
    mbx_a2 = Mailbox(
        id=uuid4(),
        organization_id=org_a,
        provider="graph",
        address="billing@orga.com",
        display_name="Org A Billing",
        status="paused",
    )
    mbx_a3 = Mailbox(
        id=uuid4(),
        organization_id=org_a,
        provider="gmail",
        address="sales@orga.com",
        display_name="Org A Sales",
        status="active",
    )
    mbx_b1 = Mailbox(
        id=uuid4(),
        organization_id=org_b,
        provider="gmail",
        address="info@orgb.com",
        status="active",
    )
    mbx_store.add(mbx_a1)
    mbx_store.add(mbx_a2)
    mbx_store.add(mbx_a3)
    mbx_store.add(mbx_b1)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Query org_a list
        resp = await client.get("/v1/mailboxes", headers={"X-Organization-ID": str(org_a)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 3
        assert len(data["items"]) == 3
        assert all(item["organization_id"] == str(org_a) for item in data["items"])

        # 2. Query with pagination limit=2, offset=1
        resp_page = await client.get(
            "/v1/mailboxes?limit=2&offset=1",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp_page.status_code == 200
        page_data = resp_page.json()
        assert page_data["total_count"] == 3
        assert len(page_data["items"]) == 2
        assert page_data["has_more"] is False

        # 3. Filter by provider=gmail
        resp_filt = await client.get(
            "/v1/mailboxes?provider=gmail",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp_filt.status_code == 200
        filt_data = resp_filt.json()
        assert filt_data["total_count"] == 2
        assert all(item["provider"] == "gmail" for item in filt_data["items"])

        # 4. Filter by status=paused
        resp_paused = await client.get(
            "/v1/mailboxes?status=paused",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp_paused.status_code == 200
        paused_data = resp_paused.json()
        assert paused_data["total_count"] == 1
        assert paused_data["items"][0]["address"] == "billing@orga.com"


@pytest.mark.asyncio
async def test_list_threads_paginated_and_ordered(
    test_app: FastAPI, org_a: UUID, org_b: UUID
) -> None:
    """Verify GET /v1/threads tenant scoping, pagination, and recency ordering (R23.2, R23.6)."""
    thd_store: InMemoryThreadStore = test_app.state.thread_store
    mbx_id1 = uuid4()
    mbx_id2 = uuid4()

    now = datetime.now(UTC)
    t1 = EmailThread(
        id=uuid4(),
        organization_id=org_a,
        mailbox_id=mbx_id1,
        subject_normalized="billing issue",
        participants=["alice@test.com", "billing@orga.com"],
        last_message_at=now - timedelta(hours=2),
        message_count=2,
    )
    t2 = EmailThread(
        id=uuid4(),
        organization_id=org_a,
        mailbox_id=mbx_id1,
        subject_normalized="urgent server down",
        participants=["bob@test.com", "support@orga.com"],
        last_message_at=now,
        message_count=5,
    )
    t3 = EmailThread(
        id=uuid4(),
        organization_id=org_b,
        mailbox_id=mbx_id2,
        subject_normalized="org b thread",
        last_message_at=now,
        message_count=1,
    )

    await thd_store.create_thread(t1)
    await thd_store.create_thread(t2)
    await thd_store.create_thread(t3)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch org_a threads - must be ordered by last_message_at DESC
        resp = await client.get("/v1/threads", headers={"X-Organization-ID": str(org_a)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 2
        assert len(data["items"]) == 2
        # t2 is newer than t1, so t2 must be first
        assert data["items"][0]["id"] == str(t2.id)
        assert data["items"][1]["id"] == str(t1.id)

        # 2. Pagination
        resp_p = await client.get(
            "/v1/threads?limit=1&offset=0",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp_p.status_code == 200
        assert resp_p.json()["has_more"] is True
        assert len(resp_p.json()["items"]) == 1


@pytest.mark.asyncio
async def test_get_thread_detail_with_chronological_messages(
    test_app: FastAPI, org_a: UUID, org_b: UUID
) -> None:
    """Verify GET /v1/threads/{id} returns ordered messages and enforces tenant boundary."""
    thd_store: InMemoryThreadStore = test_app.state.thread_store
    msg_store: InMemoryMessageStore = test_app.state.message_store

    thd_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)

    thread = EmailThread(
        id=thd_id,
        organization_id=org_a,
        mailbox_id=mbx_id,
        subject_normalized="project update",
        participants=["lead@orga.com", "dev@orga.com"],
        last_message_at=now,
        message_count=2,
    )
    await thd_store.create_thread(thread)

    msg1 = NormalizedMessage(
        message_id=uuid4(),
        thread_id=thd_id,
        mailbox_id=mbx_id,
        organization_id=org_a,
        provider="gmail",
        provider_message_id="msg-1",
        sender=EmailAddress(name="Dev", email="dev@orga.com"),
        received_at=now - timedelta(minutes=30),
        subject="Project Update",
        subject_normalized="project update",
        body_text="First draft ready.",
        body_text_clean="First draft ready.",
        snippet="First draft ready.",
    )
    msg2 = NormalizedMessage(
        message_id=uuid4(),
        thread_id=thd_id,
        mailbox_id=mbx_id,
        organization_id=org_a,
        provider="gmail",
        provider_message_id="msg-2",
        sender=EmailAddress(name="Lead", email="lead@orga.com"),
        received_at=now,
        subject="Re: Project Update",
        subject_normalized="project update",
        body_text="Looks great, thank you.",
        body_text_clean="Looks great, thank you.",
        snippet="Looks great, thank you.",
    )
    await msg_store.insert_message(msg1)
    await msg_store.insert_message(msg2)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Success for tenant A
        resp = await client.get(
            f"/v1/threads/{thd_id}",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(thd_id)
        assert data["subject_normalized"] == "project update"
        assert len(data["messages"]) == 2
        # Ordered by received_at ASC: msg1 then msg2
        assert data["messages"][0]["id"] == str(msg1.message_id)
        assert data["messages"][1]["id"] == str(msg2.message_id)

        # 2. Cross-tenant attempt returns 404
        resp_b = await client.get(
            f"/v1/threads/{thd_id}",
            headers={"X-Organization-ID": str(org_b)},
        )
        assert resp_b.status_code == 404

        # 3. Nonexistent thread returns 404
        resp_404 = await client.get(
            f"/v1/threads/{uuid4()}",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_get_message_detail_with_attachments(
    test_app: FastAPI, org_a: UUID, org_b: UUID
) -> None:
    """Verify GET /v1/messages/{id} returns message and attachment details with presigned URLs."""
    msg_store: InMemoryMessageStore = test_app.state.message_store
    storage: FakeObjectStorageClient = test_app.state.storage_client

    msg_id = uuid4()
    thd_id = uuid4()
    mbx_id = uuid4()

    att1 = AttachmentRef(
        filename="invoice.pdf",
        mime_type="application/pdf",
        size_bytes=1048576,
        object_key=f"attachments/{org_a}/inv.pdf",
        checksum="sha256:abc123",
    )
    await storage.put_bytes("attachments", att1.object_key, b"%PDF-1.4...")

    msg = NormalizedMessage(
        message_id=msg_id,
        thread_id=thd_id,
        mailbox_id=mbx_id,
        organization_id=org_a,
        provider="gmail",
        provider_message_id="prov-msg-999",
        rfc822_message_id="<999@test.com>",
        sender=EmailAddress(name="Vendor", email="vendor@company.com"),
        recipients=[EmailAddress(name="Accounts", email="accounts@orga.com")],
        cc=[EmailAddress(name="Audit", email="audit@orga.com")],
        received_at=datetime.now(UTC),
        subject="Invoice #INV-2026-001",
        subject_normalized="invoice #inv-2026-001",
        body_text="Please find attached invoice.",
        body_text_clean="Please find attached invoice.",
        snippet="Please find attached invoice.",
        raw_object_key=f"raw/{org_a}/msg.eml",
        attachments=[att1],
    )
    await msg_store.insert_message(msg)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Success for tenant A
        resp = await client.get(
            f"/v1/messages/{msg_id}",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(msg_id)
        assert data["provider_message_id"] == "prov-msg-999"
        assert data["sender"]["email"] == "vendor@company.com"
        assert data["recipients"][0]["email"] == "accounts@orga.com"
        assert data["cc"][0]["email"] == "audit@orga.com"
        assert len(data["attachments"]) == 1
        assert data["attachments"][0]["filename"] == "invoice.pdf"
        assert data["attachments"][0]["download_url"] is not None
        assert "inv.pdf" in data["attachments"][0]["download_url"]

        # 2. Cross-tenant request returns 404
        resp_b = await client.get(
            f"/v1/messages/{msg_id}",
            headers={"X-Organization-ID": str(org_b)},
        )
        assert resp_b.status_code == 404


def test_resolve_provider_credentials_and_adapter_instantiation() -> None:
    """Verify credentials resolution for real Gmail mailbox usage."""
    # 1. env: resolution
    os.environ["TEST_ENV_GMAIL_TOKEN"] = "token-from-env-var-123"
    try:
        kwargs = resolve_provider_credentials("env:TEST_ENV_GMAIL_TOKEN", "gmail")
        assert kwargs.get("access_token") == "token-from-env-var-123"
    finally:
        del os.environ["TEST_ENV_GMAIL_TOKEN"]

    # 2. ya29. direct token string resolution
    direct_token = "ya29.a0ARrdaM8sampleOAuthAccessTokenForTesting"
    kwargs_direct = resolve_provider_credentials(direct_token, "gmail")
    assert kwargs_direct.get("access_token") == direct_token

    # 3. Environment fallback
    os.environ["GMAIL_ACCESS_TOKEN"] = "token-fallback-456"
    try:
        kwargs_fb = resolve_provider_credentials(None, "gmail")
        assert kwargs_fb.get("access_token") == "token-fallback-456"
    finally:
        del os.environ["GMAIL_ACCESS_TOKEN"]

    # 4. get_adapter_for_mailbox passes access_token to GmailProviderAdapter
    mbx = Mailbox(
        id=uuid4(),
        organization_id=uuid4(),
        provider="gmail",
        address="test@gmail.com",
        credentials_ref=direct_token,
    )
    adapter = get_adapter_for_mailbox(mbx)
    assert isinstance(adapter, GmailProviderAdapter)
    assert adapter.access_token == direct_token

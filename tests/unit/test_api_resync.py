"""Unit tests for mailbox management and manual re-sync endpoints (R2.11, R23.2, R23.6, R5.3).

Covers:
- POST /v1/mailboxes/{id}/resync returning 202 Accepted and publishing JobEnvelope to mail.ingest.
- Optional time window filter (since, until) and validation.
- Operational status checks (needs_reauth / paused) returning 409 unless force=True.
- Strict multi-tenant isolation (cross-tenant requests return 404).
- GET /v1/mailboxes/{id} returning 200 OK with MailboxResponse.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.core.settings import AppSettings
from packages.db.mailbox import InMemoryMailboxStore
from packages.domain.entities import Mailbox
from services.api.main import create_app


class FakeMessagePublisher:
    """In-memory publisher test double capturing published envelopes."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append(
            {
                "exchange_name": exchange_name,
                "routing_key": routing_key,
                "envelope": envelope,
                "headers": headers,
            }
        )


@pytest.fixture
def org_id_a() -> UUID:
    return uuid4()


@pytest.fixture
def org_id_b() -> UUID:
    return uuid4()


@pytest.fixture
def mailbox_a(org_id_a: UUID) -> Mailbox:
    return Mailbox(
        id=uuid4(),
        organization_id=org_id_a,
        provider="dummy_provider",
        address="user.a@example.com",
        display_name="User A",
        status="active",
        credentials_ref="creds/org_a/mbx_a",
    )


@pytest.fixture
def mailbox_reauth(org_id_a: UUID) -> Mailbox:
    return Mailbox(
        id=uuid4(),
        organization_id=org_id_a,
        provider="dummy_provider",
        address="reauth@example.com",
        display_name="Reauth User",
        status="needs_reauth",
        credentials_ref="creds/org_a/mbx_reauth",
    )


@pytest.fixture
def mailbox_paused(org_id_a: UUID) -> Mailbox:
    return Mailbox(
        id=uuid4(),
        organization_id=org_id_a,
        provider="dummy_provider",
        address="paused@example.com",
        display_name="Paused User",
        status="paused",
        credentials_ref="creds/org_a/mbx_paused",
    )


@pytest.fixture
def mailbox_b(org_id_b: UUID) -> Mailbox:
    return Mailbox(
        id=uuid4(),
        organization_id=org_id_b,
        provider="dummy_provider",
        address="user.b@example.com",
        display_name="User B",
        status="active",
        credentials_ref="creds/org_b/mbx_b",
    )


@pytest.fixture
def mailbox_store(
    mailbox_a: Mailbox,
    mailbox_reauth: Mailbox,
    mailbox_paused: Mailbox,
    mailbox_b: Mailbox,
) -> InMemoryMailboxStore:
    store = InMemoryMailboxStore([mailbox_a, mailbox_reauth, mailbox_paused, mailbox_b])
    return store


@pytest.fixture
def publisher() -> FakeMessagePublisher:
    return FakeMessagePublisher()


@pytest.fixture
def api_app(mailbox_store: InMemoryMailboxStore, publisher: FakeMessagePublisher) -> FastAPI:
    app = create_app(lifespan_enabled=False)
    app.state.mailbox_store = mailbox_store
    app.state.publisher = publisher
    return app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


class TestManualResyncEndpoint:
    """Test suite for POST /v1/mailboxes/{id}/resync."""

    async def test_resync_success_default(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_a: Mailbox,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 202 Accepted and JobEnvelope enqueued with default parameters."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_a.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert data["mailbox_id"] == str(mailbox_a.id)
        assert data["status"] == "enqueued"
        assert "job_id" in data
        assert data["time_window"] is None

        # Verify published message to broker
        assert len(publisher.published) == 1
        published = publisher.published[0]
        broker_settings = AppSettings().broker
        assert published["exchange_name"] == broker_settings.exchange_mail_ingest
        assert published["routing_key"] == broker_settings.queue_mail_sync

        envelope: JobEnvelope = published["envelope"]
        assert envelope.job_id == data["job_id"]
        assert envelope.job_type == "sync_mailbox"
        assert envelope.organization_id == str(org_id_a)
        assert envelope.mailbox_id == str(mailbox_a.id)
        assert envelope.payload["full_resync"] is False
        assert envelope.payload["manual"] is True
        assert envelope.payload["provider"] == mailbox_a.provider

    async def test_resync_with_time_window(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_a: Mailbox,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 202 Accepted when valid since/until time window is provided."""
        now = datetime.now(UTC)
        since = now - timedelta(days=7)
        until = now

        response = await client.post(
            f"/v1/mailboxes/{mailbox_a.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={
                "since": since.isoformat(),
                "until": until.isoformat(),
                "full_resync": True,
            },
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert data["time_window"] is not None
        assert data["time_window"]["since"] is not None
        assert data["time_window"]["until"] is not None

        assert len(publisher.published) == 1
        envelope = publisher.published[0]["envelope"]
        assert envelope.payload["full_resync"] is True
        assert envelope.payload["since"] == since.isoformat()
        assert envelope.payload["until"] == until.isoformat()

    async def test_resync_invalid_time_window_422(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_a: Mailbox,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 422 Unprocessable Entity when since > until."""
        now = datetime.now(UTC)
        since = now
        until = now - timedelta(days=1)  # until is before since

        response = await client.post(
            f"/v1/mailboxes/{mailbox_a.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={
                "since": since.isoformat(),
                "until": until.isoformat(),
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert len(publisher.published) == 0

    async def test_resync_mailbox_not_found(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 404 Not Found when mailbox UUID does not exist."""
        random_id = uuid4()
        response = await client.post(
            f"/v1/mailboxes/{random_id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        body = response.json()
        assert body["code"] == "MAILBOX_NOT_FOUND"
        assert len(publisher.published) == 0

    async def test_resync_cross_tenant_isolation_404(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_b: Mailbox,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify tenant A cannot re-sync tenant B's mailbox and receives 404 (R5.3, R23.6)."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_b.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},  # Tenant A
            json={},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        body = response.json()
        assert body["code"] == "MAILBOX_NOT_FOUND"
        assert len(publisher.published) == 0

    async def test_resync_missing_tenant_header_400(
        self,
        client: AsyncClient,
        mailbox_a: Mailbox,
    ) -> None:
        """Verify 400 Bad Request when organization_id is omitted."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_a.id}/resync",
            json={},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert body["code"] == "ORGANIZATION_ID_REQUIRED"

    async def test_resync_needs_reauth_rejected_without_force(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_reauth: Mailbox,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 409 Conflict when mailbox is in needs_reauth status without force=True."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_reauth.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={"force": False},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        body = response.json()
        assert body["code"] == "MAILBOX_NEEDS_REAUTH"
        assert len(publisher.published) == 0

    async def test_resync_needs_reauth_allowed_with_force(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_reauth: Mailbox,
        mailbox_store: InMemoryMailboxStore,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 202 Accepted when mailbox is in needs_reauth status and force=True."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_reauth.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={"force": True},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        assert len(publisher.published) == 1

        # Mailbox operational status should be reset to active
        updated = await mailbox_store.get(mailbox_reauth.id)
        assert updated is not None
        assert updated.status == "active"

    async def test_resync_paused_rejected_without_force(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_paused: Mailbox,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 409 Conflict when mailbox is paused without force=True."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_paused.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={"force": False},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        body = response.json()
        assert body["code"] == "MAILBOX_PAUSED"
        assert len(publisher.published) == 0

    async def test_resync_paused_allowed_with_force(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_paused: Mailbox,
        mailbox_store: InMemoryMailboxStore,
        publisher: FakeMessagePublisher,
    ) -> None:
        """Verify 202 Accepted when mailbox is paused and force=True."""
        response = await client.post(
            f"/v1/mailboxes/{mailbox_paused.id}/resync",
            headers={"X-Organization-ID": str(org_id_a)},
            json={"force": True},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        assert len(publisher.published) == 1

        updated = await mailbox_store.get(mailbox_paused.id)
        assert updated is not None
        assert updated.status == "active"


class TestGetMailboxEndpoint:
    """Test suite for GET /v1/mailboxes/{id}."""

    async def test_get_mailbox_success(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_a: Mailbox,
    ) -> None:
        """Verify 200 OK returning MailboxResponse for matching tenant."""
        response = await client.get(
            f"/v1/mailboxes/{mailbox_a.id}",
            headers={"X-Organization-ID": str(org_id_a)},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == str(mailbox_a.id)
        assert data["organization_id"] == str(org_id_a)
        assert data["address"] == mailbox_a.address
        assert data["display_name"] == mailbox_a.display_name
        assert data["status"] == "active"
        assert data["provider"] == mailbox_a.provider

    async def test_get_mailbox_cross_tenant_404(
        self,
        client: AsyncClient,
        org_id_a: UUID,
        mailbox_b: Mailbox,
    ) -> None:
        """Verify 404 Not Found when attempting to fetch another tenant's mailbox (R5.3)."""
        response = await client.get(
            f"/v1/mailboxes/{mailbox_b.id}",
            headers={"X-Organization-ID": str(org_id_a)},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        body = response.json()
        assert body["code"] == "MAILBOX_NOT_FOUND"

    async def test_get_mailbox_nonexistent_404(
        self,
        client: AsyncClient,
        org_id_a: UUID,
    ) -> None:
        """Verify 404 Not Found for non-existent mailbox."""
        response = await client.get(
            f"/v1/mailboxes/{uuid4()}",
            headers={"X-Organization-ID": str(org_id_a)},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        body = response.json()
        assert body["code"] == "MAILBOX_NOT_FOUND"

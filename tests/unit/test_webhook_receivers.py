"""Unit tests for provider webhook receivers and handshakes.

Requirements:
- R2.1: Provider webhook endpoints accepting change notifications and completing handshakes.
- R2.2: Acknowledge within 5s with NO synchronous provider fetch inside the request.
- R2.3: Treat notification as signal only without trusting payload as authoritative message state.
- R3.1: Enqueue sync_mailbox job to RabbitMQ mail.sync queue.
"""

import base64
import json
from typing import Any

import httpx
import pytest

from packages.adapters.webhooks import get_job_publisher
from packages.broker.envelope import JobEnvelope
from services.api.main import create_app


class MockMessagePublisher:
    """In-memory mock publisher recording published job envelopes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((exchange_name, routing_key, envelope))


@pytest.fixture
def mock_publisher() -> MockMessagePublisher:
    return MockMessagePublisher()


@pytest.fixture
def test_client(mock_publisher: MockMessagePublisher) -> httpx.AsyncClient:
    app = create_app(lifespan_enabled=False)
    app.state.publisher = mock_publisher
    app.dependency_overrides[get_job_publisher] = lambda: mock_publisher

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Microsoft Graph Validation Handshake & Ingestion (R2.1, R2.2, R2.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_validation_handshake_get(test_client: httpx.AsyncClient) -> None:
    """Verify Graph validation handshake on GET returns validationToken in text/plain (R2.1)."""
    resp = await test_client.get(
        "/v1/webhooks/graph",
        params={"validationToken": "token-graph-validation-12345"},
    )
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert resp.text == "token-graph-validation-12345"


@pytest.mark.asyncio
async def test_graph_validation_handshake_post(test_client: httpx.AsyncClient) -> None:
    """Verify Graph validation handshake on POST returns validationToken in text/plain (R2.1)."""
    resp = await test_client.post(
        "/v1/webhooks/graph",
        params={"validationToken": "post-token-exchange-abc"},
    )
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert resp.text == "post-token-exchange-abc"


@pytest.mark.asyncio
async def test_graph_notification_ingestion_enqueues_sync_job(
    test_client: httpx.AsyncClient, mock_publisher: MockMessagePublisher
) -> None:
    """Verify Graph notification enqueues sync_mailbox job without provider fetch (R2.2, R2.3)."""
    payload = {
        "value": [
            {
                "subscriptionId": "sub-graph-01",
                "changeType": "created",
                "resource": "Users/user@domain.com/Messages/msg-graph-01",
                "resourceData": {"id": "msg-graph-01"},
                "clientState": "secret-mbx-graph-01",
            }
        ]
    }

    resp = await test_client.post("/v1/webhooks/graph", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["enqueued_count"] == 1

    # Verify RabbitMQ publish destination and envelope contract
    assert len(mock_publisher.published) == 1
    exchange, routing_key, envelope = mock_publisher.published[0]
    assert exchange == "mail.ingest"
    assert routing_key == "mail.sync.requested"

    assert isinstance(envelope, JobEnvelope)
    assert envelope.job_type == "sync_mailbox"
    assert envelope.mailbox_id == "mbx-graph-01"
    assert envelope.payload["provider"] == "graph"
    assert envelope.payload["subscription_id"] == "sub-graph-01"
    assert envelope.payload["change_type"] == "created"
    # Ensure signal only: no message body or full content inside envelope
    assert "body" not in envelope.payload


@pytest.mark.asyncio
async def test_graph_notification_with_path_mailbox_id(
    test_client: httpx.AsyncClient, mock_publisher: MockMessagePublisher
) -> None:
    """Verify Graph notification with path parameter resolves mailbox explicitly."""
    payload = {
        "value": [
            {
                "subscriptionId": "sub-graph-02",
                "changeType": "updated",
                "resource": "Users/user@domain.com/Messages/msg-02",
                "resourceData": {"id": "msg-02"},
            }
        ]
    }

    resp = await test_client.post("/v1/webhooks/graph/mbx-explicit-123", json=payload)
    assert resp.status_code == 202

    assert len(mock_publisher.published) == 1
    _, _, envelope = mock_publisher.published[0]
    assert envelope.mailbox_id == "mbx-explicit-123"
    assert envelope.job_type == "sync_mailbox"


# ---------------------------------------------------------------------------
# Gmail / Google Cloud Pub/Sub Handshake & Ingestion (R2.1, R2.2, R2.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_validation_probe_get(test_client: httpx.AsyncClient) -> None:
    """Verify Gmail verification probe on GET returns 200 OK (R2.1)."""
    resp = await test_client.get("/v1/webhooks/gmail")
    assert resp.status_code == 200
    assert resp.text == "OK"


@pytest.mark.asyncio
async def test_gmail_validation_challenge_query(test_client: httpx.AsyncClient) -> None:
    """Verify challenge query echo for Pub/Sub verification (R2.1)."""
    resp = await test_client.get(
        "/v1/webhooks/gmail",
        params={"challenge": "hub-challenge-token-999"},
    )
    assert resp.status_code == 200
    assert resp.text == "hub-challenge-token-999"


@pytest.mark.asyncio
async def test_gmail_push_notification_enqueues_sync_job(
    test_client: httpx.AsyncClient, mock_publisher: MockMessagePublisher
) -> None:
    """Verify Gmail Pub/Sub push notification enqueues sync_mailbox job (R2.2, R2.3)."""
    inner_data = json.dumps({"emailAddress": "ops@example.com", "historyId": "87654321"}).encode()
    b64_data = base64.urlsafe_b64encode(inner_data).decode("ascii")

    envelope_payload = {
        "message": {
            "data": b64_data,
            "messageId": "pubsub-msg-101",
            "publishTime": "2026-09-17T01:00:00Z",
        },
        "subscription": "projects/my-org/subscriptions/gmail-push-watch",
    }

    resp = await test_client.post("/v1/webhooks/gmail", json=envelope_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "acknowledged"

    # Verify RabbitMQ publish
    assert len(mock_publisher.published) == 1
    exchange, routing_key, envelope = mock_publisher.published[0]
    assert exchange == "mail.ingest"
    assert routing_key == "mail.sync.requested"

    assert envelope.job_type == "sync_mailbox"
    assert envelope.mailbox_id == "ops@example.com"
    assert envelope.payload["provider"] == "gmail"
    assert envelope.payload["history_id"] == "87654321"
    assert envelope.payload["email_address"] == "ops@example.com"
    # Ensure signal only: no message body or full content inside envelope
    assert "body" not in envelope.payload


@pytest.mark.asyncio
async def test_gmail_push_notification_with_path_mailbox_id(
    test_client: httpx.AsyncClient, mock_publisher: MockMessagePublisher
) -> None:
    """Verify Gmail push notification preserves explicit mailbox path parameter."""
    inner_data = json.dumps({"emailAddress": "info@example.com", "historyId": "12345"}).encode()
    b64_data = base64.urlsafe_b64encode(inner_data).decode("ascii")
    payload = {"message": {"data": b64_data}}

    resp = await test_client.post("/v1/webhooks/gmail/mbx-gmail-fixed-id", json=payload)
    assert resp.status_code == 200

    assert len(mock_publisher.published) == 1
    _, _, envelope = mock_publisher.published[0]
    assert envelope.mailbox_id == "mbx-gmail-fixed-id"
    assert envelope.payload["history_id"] == "12345"


# ---------------------------------------------------------------------------
# Error Handling & Edge Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_payload_heartbeat(test_client: httpx.AsyncClient) -> None:
    """Verify empty or ping payload gracefully acknowledges with 200."""
    resp_graph = await test_client.post("/v1/webhooks/graph", json={})
    assert resp_graph.status_code == 200

    resp_gmail = await test_client.post("/v1/webhooks/gmail", json={})
    assert resp_gmail.status_code == 200


@pytest.mark.asyncio
async def test_malformed_payload_rejected(test_client: httpx.AsyncClient) -> None:
    """Verify malformed payloads return 400 Bad Request."""
    resp_graph = await test_client.post(
        "/v1/webhooks/graph",
        content=b"Invalid JSON data",
        headers={"Content-Type": "application/json"},
    )
    assert resp_graph.status_code == 400

    resp_gmail = await test_client.post(
        "/v1/webhooks/gmail",
        content=b"Invalid JSON data",
        headers={"Content-Type": "application/json"},
    )
    assert resp_gmail.status_code == 400

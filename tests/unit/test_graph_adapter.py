"""Unit tests for GraphProviderAdapter.

Requirements:
- R1.2: GraphProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.6: Delta query following @odata.nextLink to terminal @odata.deltaLink.
- R2.7: Invalid delta token detection triggering bounded full resync.
- design.md §5.1: MailProviderAdapter protocol conformance.
"""

import json

import httpx
import pytest

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    RateLimited,
    Transient,
)
from packages.adapters.graph import (
    GraphProviderAdapter,
    MicrosoftGraphProviderAdapter,
    parse_graph_notification,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter, get_adapter_for_mailbox
from packages.adapters.testing import MailProviderAdapterContractSuite
from packages.domain.entities import Checkpoint, Mailbox

# ---------------------------------------------------------------------------
# Step 1: Change notification parsing & error translation tests
# ---------------------------------------------------------------------------


def test_parse_graph_notification_dict() -> None:
    """Verify parsing valid Microsoft Graph webhook change notification payload."""
    payload = {
        "value": [
            {
                "subscriptionId": "sub-graph-001",
                "subscriptionExpirationDateTime": "2026-09-20T18:23:45.935Z",
                "changeType": "created",
                "resource": "Users/user@example.com/Messages/AAMkADh01",
                "resourceData": {
                    "@odata.type": "#Microsoft.Graph.Message",
                    "@odata.id": "Users/user@example.com/Messages/AAMkADh01",
                    "id": "AAMkADh01",
                },
                "clientState": "secretState123",
            }
        ]
    }
    notifications = parse_graph_notification(payload)
    assert len(notifications) == 1
    notif = notifications[0]
    assert notif.subscription_id == "sub-graph-001"
    assert notif.change_type == "created"
    assert notif.resource == "Users/user@example.com/Messages/AAMkADh01"
    assert notif.resource_id == "AAMkADh01"
    assert notif.client_state == "secretState123"
    assert notif.subscription_expiration_datetime == "2026-09-20T18:23:45.935Z"


def test_parse_graph_notification_bytes_and_str() -> None:
    """Verify parsing string and byte payloads."""
    payload_dict = {
        "value": [
            {
                "subscriptionId": "sub-graph-002",
                "changeType": "updated",
                "resource": "me/messages/msg-002",
                "resourceData": {"id": "msg-002"},
            }
        ]
    }
    raw_str = json.dumps(payload_dict)
    raw_bytes = raw_str.encode("utf-8")

    from_str = parse_graph_notification(raw_str)
    assert len(from_str) == 1
    assert from_str[0].subscription_id == "sub-graph-002"

    from_bytes = parse_graph_notification(raw_bytes)
    assert len(from_bytes) == 1
    assert from_bytes[0].resource_id == "msg-002"


def test_parse_graph_notification_single_item() -> None:
    """Verify parsing single notification dictionary (non-array format)."""
    payload = {
        "subscriptionId": "sub-single",
        "changeType": "created",
        "resource": "me/messages/msg-single",
        "resourceData": {"id": "msg-single"},
        "clientState": "state-xyz",
    }
    result = parse_graph_notification(payload)
    assert len(result) == 1
    assert result[0].subscription_id == "sub-single"
    assert result[0].resource_id == "msg-single"


def test_parse_graph_notification_invalid() -> None:
    """Verify invalid payloads raise ValueError."""
    with pytest.raises(ValueError):
        parse_graph_notification({"unknown": "field"})

    with pytest.raises(ValueError):
        parse_graph_notification(12345)  # type: ignore


@pytest.mark.asyncio
async def test_error_translation_rate_limited() -> None:
    """Verify 429 response maps to RateLimited with parsed Retry-After (R1.6)."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "45"},
            json={"error": {"code": "activityLimitReached", "message": "Rate limit exceeded"}},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GraphProviderAdapter(client=client)

    with pytest.raises(RateLimited) as exc_info:
        await adapter._request("GET", "https://graph.microsoft.com/v1.0/me")
    assert exc_info.value.retry_after == 45.0
    assert exc_info.value.provider == "graph"


@pytest.mark.asyncio
async def test_error_translation_auth_expired() -> None:
    """Verify 401 and 403 map to AuthExpired."""

    def mock_handler_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "InvalidAuthenticationToken", "message": "Token expired"}},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler_401))
    adapter = GraphProviderAdapter(client=client)

    with pytest.raises(AuthExpired) as exc_info:
        await adapter._request("GET", "https://graph.microsoft.com/v1.0/me")
    assert exc_info.value.provider == "graph"


@pytest.mark.asyncio
async def test_error_translation_not_found() -> None:
    """Verify 404 maps to NotFound."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "ResourceNotFound", "message": "Item not found"}},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GraphProviderAdapter(client=client)

    with pytest.raises(NotFound) as exc_info:
        await adapter._request("GET", "https://graph.microsoft.com/v1.0/me/messages/nonexistent")
    assert exc_info.value.provider == "graph"


@pytest.mark.asyncio
async def test_error_translation_transient_and_permanent() -> None:
    """Verify 503 and network errors map to Transient, and 400 maps to Permanent."""

    def mock_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "Service Unavailable"}, request=request)

    client_503 = httpx.AsyncClient(transport=httpx.MockTransport(mock_503))
    adapter_503 = GraphProviderAdapter(client=client_503)
    with pytest.raises(Transient):
        await adapter_503._request("GET", "https://graph.microsoft.com/v1.0/me")

    def mock_400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "BadRequest", "message": "Malformed request"}},
            request=request,
        )

    client_400 = httpx.AsyncClient(transport=httpx.MockTransport(mock_400))
    adapter_400 = GraphProviderAdapter(client=client_400)
    with pytest.raises(Permanent):
        await adapter_400._request("GET", "https://graph.microsoft.com/v1.0/me")


# ---------------------------------------------------------------------------
# Step 2: Contract test suite & Registry resolution
# ---------------------------------------------------------------------------


def create_mock_graph_transport() -> httpx.MockTransport:
    """Create an httpx MockTransport simulating Microsoft Graph API endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        # Subscriptions
        if path == "/v1.0/subscriptions" and method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "graph-sub-contract-001",
                    "resource": "/me/mailFolders('Inbox')/messages",
                    "expirationDateTime": "2026-09-20T18:00:00.000Z",
                    "clientState": "secret-state",
                },
                request=request,
            )
        if path.startswith("/v1.0/subscriptions/") and method == "PATCH":
            return httpx.Response(
                200,
                json={
                    "id": path.split("/")[-1],
                    "resource": "/me/mailFolders('Inbox')/messages",
                    "expirationDateTime": "2026-09-25T18:00:00.000Z",
                },
                request=request,
            )

        # Delta sync
        if path == "/v1.0/me/mailFolders/Inbox/messages/delta":
            return httpx.Response(
                200,
                json={
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages/delta?$deltatoken=initial-token",
                    "value": [{"id": "graph-msg-sync-1"}],
                },
                request=request,
            )

        # Raw message content ($value)
        if path.endswith("/$value"):
            return httpx.Response(
                200,
                content=(
                    b"From: sender@example.com\r\nTo: recipient@example.com\r\n"
                    b"Subject: Test Email\r\n\r\nHello from Graph!"
                ),
                headers={"Content-Type": "message/rfc822"},
                request=request,
            )

        # Message metadata
        if "/v1.0/me/messages/" in path:
            msg_id = path.split("/")[-1]
            return httpx.Response(
                200,
                json={
                    "id": msg_id,
                    "conversationId": "conv-contract-001",
                    "receivedDateTime": "2026-09-16T12:00:00Z",
                },
                request=request,
            )

        # Thread messages query
        if path == "/v1.0/me/messages" and request.url.query:
            return httpx.Response(
                200,
                json={"value": [{"id": "graph-msg-sync-1", "conversationId": "conv-contract-001"}]},
                request=request,
            )

        # Create draft
        if path == "/v1.0/me/messages" and method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "draft-graph-contract-001",
                    "conversationId": "conv-contract-001",
                },
                request=request,
            )

        # Send mail
        if path == "/v1.0/me/sendMail" and method == "POST":
            return httpx.Response(
                202,
                headers={"client-request-id": "sent-graph-contract-001"},
                request=request,
            )

        return httpx.Response(404, json={"error": "Not Found"}, request=request)

    return httpx.MockTransport(handler)


class TestGraphProviderAdapterContract(MailProviderAdapterContractSuite):
    """Prove GraphProviderAdapter fully satisfies MailProviderAdapterContractSuite."""

    def create_adapter(self) -> MailProviderAdapter:
        client = httpx.AsyncClient(transport=create_mock_graph_transport())
        return GraphProviderAdapter(client=client, base_url="https://graph.microsoft.com/v1.0/me")


def test_graph_adapter_registered_in_registry() -> None:
    """Verify GraphProviderAdapter auto-registers under provider key 'graph' (R1.3)."""
    adapter = get_adapter("graph")
    assert isinstance(adapter, GraphProviderAdapter)

    mailbox = Mailbox(
        id="mbx-graph-01",
        organization_id="org-test",
        address="test@domain.com",
        provider="graph",
        credentials_ref="vault://secret",
    )
    resolved = get_adapter_for_mailbox(mailbox)
    assert isinstance(resolved, GraphProviderAdapter)
    assert MicrosoftGraphProviderAdapter is GraphProviderAdapter


# ---------------------------------------------------------------------------
# Step 3: Delta query pagination & token expiry detection (R2.6, R2.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_pagination_follows_next_link_to_delta_link() -> None:
    """Verify synchronize() traverses @odata.nextLink and persists deltaLink (R2.6)."""
    calls: list[str] = []

    def multi_page_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        calls.append(url_str)

        if "delta_page_2" in url_str:
            # Terminal page
            return httpx.Response(
                200,
                json={
                    "@odata.deltaLink": (
                        "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=terminal-token-999"
                    ),
                    "value": [{"id": "msg-page-2"}],
                },
                request=request,
            )
        if "messages/delta" in url_str:
            # First page
            return httpx.Response(
                200,
                json={
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages/delta?delta_page_2",
                    "value": [{"id": "msg-page-1"}],
                },
                request=request,
            )
        if url_str.endswith("/$value"):
            return httpx.Response(200, content=b"MIME payload", request=request)
        if "/messages/" in url_str:
            return httpx.Response(
                200,
                json={
                    "id": "mid",
                    "conversationId": "cid",
                    "receivedDateTime": "2026-09-16T12:00:00Z",
                },
                request=request,
            )
        return httpx.Response(404, json={}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(multi_page_handler))
    adapter = GraphProviderAdapter(client=client, base_url="https://graph.microsoft.com/v1.0/me")
    mailbox = Mailbox(
        id="mbx-graph-01",
        organization_id="org-test",
        address="test@domain.com",
        provider="graph",
        credentials_ref="vault://secret",
    )
    cp = Checkpoint(mailbox_id=mailbox.id)

    res = await adapter.synchronize(mailbox, cp)
    assert len(res.messages) == 2
    assert res.requires_full_resync is False
    assert res.has_more is False
    assert (
        res.new_checkpoint.delta_link
        == "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=terminal-token-999"
    )
    assert res.new_checkpoint.sync_state == "idle"


@pytest.mark.asyncio
async def test_delta_token_expired_triggers_full_resync_410() -> None:
    """Verify HTTP 410 Gone triggers bounded full resync with requires_full_resync=True (R2.7)."""

    def handler_410(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            410,
            json={
                "error": {"code": "ResyncRequired", "message": "Delta token is expired or invalid"}
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler_410))
    adapter = GraphProviderAdapter(client=client)
    mailbox = Mailbox(
        id="mbx-graph-01",
        organization_id="org-test",
        address="test@domain.com",
        provider="graph",
        credentials_ref="vault://secret",
    )
    cp = Checkpoint(
        mailbox_id=mailbox.id,
        delta_link="https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=stale",
    )

    res = await adapter.synchronize(mailbox, cp)
    assert res.requires_full_resync is True
    assert res.new_checkpoint.sync_state == "full_resync"
    assert res.new_checkpoint.delta_link is None
    assert len(res.messages) == 0


@pytest.mark.asyncio
async def test_delta_token_resync_required_error_code() -> None:
    """Verify ResyncRequired error in 400 response also triggers full resync (R2.7)."""

    def handler_resync(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "ResyncRequired",
                    "message": "Delta token invalid, resync required",
                }
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler_resync))
    adapter = GraphProviderAdapter(client=client)
    mailbox = Mailbox(
        id="mbx-graph-01",
        organization_id="org-test",
        address="test@domain.com",
        provider="graph",
        credentials_ref="vault://secret",
    )
    cp = Checkpoint(
        mailbox_id=mailbox.id,
        delta_link="https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=bad",
    )

    res = await adapter.synchronize(mailbox, cp)
    assert res.requires_full_resync is True
    assert res.new_checkpoint.sync_state == "full_resync"
    assert res.new_checkpoint.delta_link is None

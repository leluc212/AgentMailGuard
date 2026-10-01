"""Unit tests for GraphProviderAdapter.

Requirements:
- R1.2: GraphProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.6: Delta query following @odata.nextLink to terminal @odata.deltaLink.
- R2.7: Invalid delta token detection triggering bounded full resync.
- design.md §5.1: MailProviderAdapter protocol conformance.
"""

import json
from typing import Any

import httpx
import pytest

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    PermanentProviderError,
    RateLimited,
    RetryableProviderError,
    Transient,
)
from packages.adapters.graph import (
    IMMUTABLE_ID_PREFER,
    GraphProviderAdapter,
    MicrosoftGraphProviderAdapter,
    parse_graph_notification,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter, get_adapter_for_mailbox
from packages.adapters.testing import MailProviderAdapterContractSuite
from packages.domain import ProviderDraftStatus
from packages.domain.entities import Checkpoint, EmailAddress, Mailbox, OutboundReply

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
    """Recorded Microsoft Graph responses; drafts are stateful so send changes isDraft."""
    drafts: dict[str, bool] = {}  # draft id -> sent?
    rfc_ids: dict[str, str] = {}  # draft id -> internetMessageId set by createReply

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

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
        if path == "/v1.0/me/mailFolders/Inbox/messages/delta":
            return httpx.Response(
                200,
                json={
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages/delta?$deltatoken=initial-token",
                    "value": [{"id": "graph-msg-sync-1"}],
                },
                request=request,
            )
        if path == "/v1.0/me/messages/msg-001/createReply" and method == "POST":
            assert request.headers.get("Prefer") == IMMUTABLE_ID_PREFER
            draft_id = "draft-graph-contract-001"
            drafts[draft_id] = False
            sent_message = json.loads(request.content or b"{}").get("message") or {}
            rfc_ids[draft_id] = str(
                sent_message.get("internetMessageId") or "<AM0PR01MB0001@eurprd01.prod.outlook.com>"
            )
            return httpx.Response(
                201,
                json={"id": draft_id, "conversationId": "conv-contract-001", "isDraft": True},
                request=request,
            )
        if path.endswith("/send") and method == "POST":
            draft_id = path.split("/")[-2]
            if draft_id not in drafts or drafts[draft_id]:
                return httpx.Response(
                    404, json={"error": {"code": "ErrorItemNotFound"}}, request=request
                )
            drafts[draft_id] = True
            return httpx.Response(202, request=request)
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
        if path.startswith("/v1.0/me/messages/draft-") and method == "GET":
            draft_id = path.split("/")[-1]
            if draft_id not in drafts:
                return httpx.Response(
                    404, json={"error": {"code": "ErrorItemNotFound"}}, request=request
                )
            return httpx.Response(
                200, json={"id": draft_id, "isDraft": not drafts[draft_id]}, request=request
            )
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
        if path == "/v1.0/me/messages" and method == "GET" and request.url.query:
            value: list[dict[str, Any]] = [
                {
                    "id": "graph-msg-sync-1",
                    "conversationId": "conv-contract-001",
                    "isDraft": False,
                    "internetMessageId": "<orig-001@example.com>",
                    "sentDateTime": "2026-09-16T12:00:00Z",
                }
            ]
            for draft_id, sent in drafts.items():
                value.append(
                    {
                        "id": draft_id,
                        "conversationId": "conv-contract-001",
                        "isDraft": not sent,
                        "internetMessageId": rfc_ids[draft_id],
                        "sentDateTime": "2026-09-28T12:00:00Z" if sent else None,
                    }
                )
            return httpx.Response(200, json={"value": value}, request=request)
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


# ---------------------------------------------------------------------------
# 6.3a: createReply + send with immutable ids; status; sent lookup; errors.
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"


def _graph_mailbox() -> Mailbox:
    return Mailbox(
        id="mbx-graph-63a",
        organization_id="org-63a",
        address="support@example.com",
        provider="graph",
    )


def _graph_reply(original_id: str | None = "AAMkAGI2-orig=") -> OutboundReply:
    return OutboundReply(
        thread_id="AAQkAGI2-conv=",
        mailbox_id="mbx-graph-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="customer@example.com", name="Customer")],
        cc=[EmailAddress(email="lead@example.com")],
        body_text="Your order shipped.",
        subject="Re: Order",
        reply_to_provider_message_id=original_id,
    )


def _graph_client(
    routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        recorded = routes.get((request.method, request.url.path))
        if recorded is None:
            return httpx.Response(
                404,
                json={"error": {"code": "ErrorItemNotFound", "message": "Not found."}},
                request=request,
            )
        return httpx.Response(
            recorded.status_code,
            headers=recorded.headers,
            content=recorded.content,
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_graph_create_draft_uses_create_reply_with_immutable_ids() -> None:
    """6.3a / R17.2: the draft is a createReply on the original, not a new message."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/v1.0/me/messages/AAMkAGI2-orig=/createReply"): httpx.Response(
            201,
            json={
                "id": "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Ad-draft",
                "conversationId": "AAQkAGI2-conv=",
                "isDraft": True,
            },
        )
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)

    draft = await adapter.create_draft(_graph_mailbox(), _graph_reply())

    assert draft.provider_draft_id == "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Ad-draft"
    assert draft.provider_message_id == draft.provider_draft_id
    assert draft.provider_thread_id == "AAQkAGI2-conv="
    request = seen[0]
    assert request.headers["Prefer"] == 'IdType="ImmutableId"'
    body = json.loads(request.content)
    assert "comment" not in body
    assert body["message"]["body"] == {"contentType": "Text", "content": "Your order shipped."}
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "customer@example.com"
    assert body["message"]["ccRecipients"][0]["emailAddress"]["address"] == "lead@example.com"


@pytest.mark.asyncio
async def test_graph_create_draft_without_original_id_is_permanent() -> None:
    """Without the original message id there is nothing to reply to: permanent."""
    seen: list[httpx.Request] = []
    adapter = GraphProviderAdapter(client=_graph_client({}, seen), base_url=GRAPH_BASE)
    with pytest.raises(Permanent):
        await adapter.create_draft(_graph_mailbox(), _graph_reply(None))
    assert seen == []


@pytest.mark.asyncio
async def test_graph_send_draft_posts_send_and_returns_the_immutable_id() -> None:
    """6.3a / R17.4: send returns 202 with no body; the stored id is the real message id."""
    seen: list[httpx.Request] = []
    routes = {("POST", "/v1.0/me/messages/draft-imm-1/send"): httpx.Response(202)}
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)

    sent = await adapter.send_draft(_graph_mailbox(), "draft-imm-1")

    assert sent.provider_message_id == "draft-imm-1"
    assert seen[0].headers["Prefer"] == 'IdType="ImmutableId"'


@pytest.mark.asyncio
async def test_graph_send_reply_never_calls_send_mail() -> None:
    """6.3a / design §5.8: send_reply is createReply + send; sendMail starts a new conversation."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/v1.0/me/messages/AAMkAGI2-orig=/createReply"): httpx.Response(
            201, json={"id": "draft-imm-2", "conversationId": "AAQkAGI2-conv=", "isDraft": True}
        ),
        ("POST", "/v1.0/me/messages/draft-imm-2/send"): httpx.Response(202),
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)

    sent = await adapter.send_reply(_graph_mailbox(), _graph_reply())

    assert [r.url.path for r in seen] == [
        "/v1.0/me/messages/AAMkAGI2-orig=/createReply",
        "/v1.0/me/messages/draft-imm-2/send",
    ]
    assert sent.provider_message_id == "draft-imm-2"
    assert sent.provider_thread_id == "AAQkAGI2-conv="


@pytest.mark.parametrize(
    ("is_draft", "expected"),
    [(True, ProviderDraftStatus.DRAFT), (False, ProviderDraftStatus.SENT)],
)
@pytest.mark.asyncio
async def test_graph_get_draft_status_reads_is_draft(
    is_draft: bool, expected: ProviderDraftStatus
) -> None:
    """6.3a: isDraft true is DRAFT, false is SENT (the Sent Items copy keeps the id)."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/v1.0/me/messages/draft-imm-3"): httpx.Response(
            200, json={"id": "draft-imm-3", "isDraft": is_draft}
        )
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    assert await adapter.get_draft_status(_graph_mailbox(), "draft-imm-3") is expected
    assert seen[0].url.params["$select"] == "id,isDraft"


@pytest.mark.asyncio
async def test_graph_get_draft_status_missing_on_404() -> None:
    """6.3a: a deleted draft is MISSING."""
    seen: list[httpx.Request] = []
    adapter = GraphProviderAdapter(client=_graph_client({}, seen), base_url=GRAPH_BASE)
    status = await adapter.get_draft_status(_graph_mailbox(), "draft-gone")
    assert status is ProviderDraftStatus.MISSING


@pytest.mark.asyncio
async def test_graph_find_sent_message_by_id_or_internet_message_id() -> None:
    """6.3a: only an isDraft=false item matches, by immutable id or internetMessageId."""
    seen: list[httpx.Request] = []
    listing: dict[str, Any] = {
        "value": [
            {"id": "draft-unsent", "isDraft": True, "internetMessageId": "<unsent@outlook.com>"},
            {
                "id": "draft-imm-4",
                "isDraft": False,
                "internetMessageId": "<AM0PR01@outlook.com>",
                "sentDateTime": "2026-09-28T12:00:00Z",
            },
        ]
    }
    routes = {("GET", "/v1.0/me/messages"): httpx.Response(200, json=listing)}
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    mailbox = _graph_mailbox()

    by_id = await adapter.find_sent_message(mailbox, "AAQk'conv", "draft-imm-4")
    assert by_id is not None and by_id.provider_message_id == "draft-imm-4"
    assert seen[0].url.params["$filter"] == "conversationId eq 'AAQk''conv'"
    by_rfc = await adapter.find_sent_message(mailbox, "AAQk'conv", "AM0PR01@outlook.com")
    assert by_rfc is not None and by_rfc.provider_message_id == "draft-imm-4"
    assert await adapter.find_sent_message(mailbox, "AAQk'conv", "draft-unsent") is None


@pytest.mark.asyncio
async def test_graph_create_draft_sets_our_message_id() -> None:
    """6.5 / D3: the draft carries our Message-ID as internetMessageId for find_draft."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/v1.0/me/messages/AAMkAGI2-orig=/createReply"): httpx.Response(
            201, json={"id": "draft-imm-5", "conversationId": "AAQkAGI2-conv=", "isDraft": True}
        )
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    reply = OutboundReply(
        thread_id="AAQkAGI2-conv=",
        mailbox_id="mbx-graph-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="customer@example.com")],
        body_text="Your order shipped.",
        subject="Re: Order",
        message_id="dispatch-abc@example.com",
        reply_to_provider_message_id="AAMkAGI2-orig=",
    )

    await adapter.create_draft(_graph_mailbox(), reply)

    body = json.loads(seen[0].content)
    assert body["message"]["internetMessageId"] == "<dispatch-abc@example.com>"


@pytest.mark.asyncio
async def test_graph_find_draft_matches_only_an_unsent_draft_with_our_message_id() -> None:
    """6.5: the orphan-draft lookup ignores sent copies and other drafts."""
    seen: list[httpx.Request] = []
    listing: dict[str, Any] = {
        "value": [
            {
                "id": "draft-other",
                "isDraft": True,
                "internetMessageId": "<someone-else@outlook.com>",
                "conversationId": "AAQk'conv",
            },
            {
                "id": "sent-ours",
                "isDraft": False,
                "internetMessageId": "<dispatch-abc@example.com>",
                "conversationId": "AAQk'conv",
            },
            {
                "id": "draft-ours",
                "isDraft": True,
                "internetMessageId": "<dispatch-abc@example.com>",
                "conversationId": "AAQk'conv",
            },
        ]
    }
    routes = {("GET", "/v1.0/me/messages"): httpx.Response(200, json=listing)}
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    mailbox = _graph_mailbox()

    found = await adapter.find_draft(mailbox, "AAQk'conv", "dispatch-abc@example.com")
    assert found is not None
    assert (found.provider_draft_id, found.provider_message_id) == ("draft-ours", "draft-ours")
    assert seen[0].url.params["$filter"] == "conversationId eq 'AAQk''conv'"
    assert seen[0].headers["Prefer"] == 'IdType="ImmutableId"'
    assert await adapter.find_draft(mailbox, "AAQk'conv", "<missing@example.com>") is None


@pytest.mark.asyncio
async def test_graph_503_retry_after_is_retryable_and_403_is_permanent() -> None:
    """6.3a / R17.5: 503 keeps Retry-After as retry_after_s; Graph 403 stays AuthExpired."""

    def handler_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"Retry-After": "10"},
            json={"error": {"code": "serviceNotAvailable"}},
            request=request,
        )

    adapter = GraphProviderAdapter(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler_503))
    )
    with pytest.raises(Transient) as exc_info:
        await adapter._request("GET", GRAPH_BASE)
    assert isinstance(exc_info.value, RetryableProviderError)
    assert exc_info.value.retry_after_s == 10.0

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}, request=request)

    adapter_403 = GraphProviderAdapter(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler_403))
    )
    with pytest.raises(AuthExpired) as exc_403:
        await adapter_403._request("GET", GRAPH_BASE)
    assert isinstance(exc_403.value, PermanentProviderError)

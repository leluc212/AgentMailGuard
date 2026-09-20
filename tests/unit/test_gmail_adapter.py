"""Unit tests for GmailProviderAdapter.

Requirements:
- R1.2: GmailProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.5: history.list() sync from stored historyId.
"""

import json
from email import message_from_bytes

import httpx
import pytest

from packages.adapters.exceptions import (
    AuthExpired,
    Permanent,
    RateLimited,
    Transient,
)
from packages.adapters.gmail import (
    GmailProviderAdapter,
    GmailPushNotification,
    build_rfc822_mime,
    decode_urlsafe_b64,
    encode_urlsafe_b64,
    parse_pubsub_notification,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter
from packages.adapters.testing import MailProviderAdapterContractSuite
from packages.domain.entities import Checkpoint, EmailAddress, Mailbox, OutboundReply


def test_urlsafe_b64_roundtrip() -> None:
    """Verify URL-safe base64 encoding and decoding with padding variations."""
    sample = b"Subject: Hello World!\r\n\r\nTest payload."
    encoded = encode_urlsafe_b64(sample)
    assert isinstance(encoded, str)
    assert "=" not in encoded or encoded.endswith("=")
    decoded = decode_urlsafe_b64(encoded)
    assert decoded == sample


def test_build_rfc822_mime() -> None:
    """Verify building compliant RFC822 MIME message from OutboundReply."""
    reply = OutboundReply(
        thread_id="th-gmail-01",
        mailbox_id="mbx-gmail-01",
        organization_id="org-01",
        to=[EmailAddress(email="customer@example.com", name="Valued Customer")],
        cc=[EmailAddress(email="supervisor@example.com")],
        subject="Re: Order #441",
        body_text="Your order has shipped today.",
        in_reply_to="<orig-123@example.com>",
        references=["<orig-123@example.com>"],
    )
    raw = build_rfc822_mime(reply)
    parsed = message_from_bytes(raw)

    assert parsed["To"] == "Valued Customer <customer@example.com>"
    assert parsed["Cc"] == "supervisor@example.com"
    assert parsed["Subject"] == "Re: Order #441"
    assert parsed["In-Reply-To"] == "<orig-123@example.com>"
    assert parsed["References"] == "<orig-123@example.com>"
    assert "Your order has shipped today." in parsed.get_payload()


@pytest.mark.asyncio
async def test_error_translation_rate_limited() -> None:
    """Verify 429 response is translated to RateLimited with Retry-After header (R1.6)."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={"error": {"message": "User rate limit exceeded", "code": 429}},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GmailProviderAdapter(client=client)

    with pytest.raises(RateLimited) as exc_info:
        await adapter._request("GET", "https://gmail.googleapis.com/test")
    assert exc_info.value.retry_after == 60.0
    assert exc_info.value.provider == "gmail"


@pytest.mark.asyncio
async def test_error_translation_auth_expired() -> None:
    """Verify 401 response is translated to AuthExpired."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "Invalid Credentials", "status": "UNAUTHENTICATED"}},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GmailProviderAdapter(client=client)

    with pytest.raises(AuthExpired) as exc_info:
        await adapter._request("GET", "https://gmail.googleapis.com/test")
    assert exc_info.value.provider == "gmail"


@pytest.mark.asyncio
async def test_error_translation_transient_and_permanent() -> None:
    """Verify 503 and 400 translations."""

    def mock_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "Backend Error"}, request=request)

    client_503 = httpx.AsyncClient(transport=httpx.MockTransport(mock_503))
    adapter_503 = GmailProviderAdapter(client=client_503)
    with pytest.raises(Transient):
        await adapter_503._request("GET", "https://gmail.googleapis.com/test")

    def mock_400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "Invalid Argument"}, request=request)

    client_400 = httpx.AsyncClient(transport=httpx.MockTransport(mock_400))
    adapter_400 = GmailProviderAdapter(client=client_400)
    with pytest.raises(Permanent):
        await adapter_400._request("GET", "https://gmail.googleapis.com/test")


def create_mock_gmail_transport() -> httpx.MockTransport:
    """Create a mock HTTP transport returning standard Gmail API JSON payloads."""
    raw_b64 = encode_urlsafe_b64(b"From: user@example.com\r\nSubject: Contract\r\n\r\nBody")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method

        if "/watch" in url:
            return httpx.Response(
                200,
                json={"historyId": "100", "expiration": "1789000000000"},
                request=request,
            )
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "msg-001", "threadId": "th-001"}}]}
                    ],
                    "historyId": "105",
                },
                request=request,
            )
        if "/messages?" in url or url.rstrip("/").endswith("/messages"):
            return httpx.Response(
                200,
                json={"messages": [{"id": "msg-001", "threadId": "th-001"}]},
                request=request,
            )
        if "/messages/msg-001" in url:
            return httpx.Response(
                200,
                json={
                    "id": "msg-001",
                    "threadId": "th-001",
                    "raw": raw_b64,
                    "internalDate": "1726500000000",
                },
                request=request,
            )
        if "/threads/th-001" in url:
            return httpx.Response(
                200,
                json={
                    "id": "th-001",
                    "messages": [{"id": "msg-001", "threadId": "th-001", "raw": raw_b64}],
                },
                request=request,
            )
        if "/drafts" in url and method == "POST":
            return httpx.Response(
                200,
                json={"id": "draft-101", "message": {"id": "msg-draft-101", "threadId": "th-001"}},
                request=request,
            )
        if "/messages/send" in url and method == "POST":
            return httpx.Response(
                200,
                json={"id": "sent-msg-101", "threadId": "th-001"},
                request=request,
            )

        return httpx.Response(404, json={"error": "Not Found"}, request=request)

    return httpx.MockTransport(handler)


class TestGmailProviderAdapterContract(MailProviderAdapterContractSuite):
    """Verify GmailProviderAdapter passes all 8 protocol contract tests."""

    def create_adapter(self) -> MailProviderAdapter:
        client = httpx.AsyncClient(transport=create_mock_gmail_transport())
        return GmailProviderAdapter(client=client)


def test_gmail_adapter_registered() -> None:
    """Verify GmailProviderAdapter is registered in the provider registry."""
    adapter = get_adapter("gmail")
    assert isinstance(adapter, GmailProviderAdapter)


def test_parse_pubsub_notification_standard_envelope() -> None:
    """Verify parsing Google Cloud Pub/Sub nested base64 data envelope (R1.2, R2.1)."""
    inner_payload = json.dumps({"emailAddress": "ops@example.com", "historyId": "987654"}).encode()
    b64_data = encode_urlsafe_b64(inner_payload)
    pubsub_msg = {
        "message": {
            "data": b64_data,
            "messageId": "pubsub-msg-123",
            "publishTime": "2026-09-16T12:00:00Z",
        },
        "subscription": "projects/my-org/subscriptions/gmail-watch",
    }
    notif = parse_pubsub_notification(pubsub_msg)
    assert isinstance(notif, GmailPushNotification)
    assert notif.email_address == "ops@example.com"
    assert notif.history_id == "987654"


def test_parse_pubsub_notification_direct_format() -> None:
    """Verify parsing direct dictionary format."""
    direct_payload = {"emailAddress": "support@example.com", "historyId": "112233"}
    notif = parse_pubsub_notification(direct_payload)
    assert notif.email_address == "support@example.com"
    assert notif.history_id == "112233"


def test_parse_pubsub_notification_bytes_and_string() -> None:
    """Verify parsing string and bytes input JSON representations."""
    inner = json.dumps({"emailAddress": "raw@example.com", "historyId": "555"}).encode()
    b64_data = encode_urlsafe_b64(inner)
    envelope = json.dumps({"message": {"data": b64_data}})

    notif_str = parse_pubsub_notification(envelope)
    assert notif_str.email_address == "raw@example.com"
    assert notif_str.history_id == "555"

    notif_bytes = parse_pubsub_notification(envelope.encode("utf-8"))
    assert notif_bytes.email_address == "raw@example.com"
    assert notif_bytes.history_id == "555"


def test_parse_pubsub_notification_invalid() -> None:
    """Verify ValueError is raised on malformed or missing fields."""
    with pytest.raises(ValueError):
        parse_pubsub_notification({})

    with pytest.raises(ValueError):
        parse_pubsub_notification({"message": {}})


@pytest.mark.asyncio
async def test_history_expired_returns_full_resync() -> None:
    """Verify 404 on history.list returns SyncResult with requires_full_resync=True (R2.7)."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url:
            return httpx.Response(
                404,
                json={"error": {"message": "Requested entity was not found.", "code": 404}},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GmailProviderAdapter(client=client)
    mailbox = Mailbox(
        id="mbx-exp-01",
        organization_id="org-01",
        provider="gmail",
        address="user@example.com",
    )
    cp = Checkpoint(mailbox_id="mbx-exp-01", history_id="old-history-id-1")

    res = await adapter.synchronize(mailbox, cp)
    assert res.requires_full_resync is True
    assert res.messages == []
    assert res.new_checkpoint.history_id is None
    assert res.new_checkpoint.sync_state == "full_resync"


@pytest.mark.asyncio
async def test_history_expired_permanent_error_returns_full_resync() -> None:
    """Verify 400 on invalid/expired historyId triggers full resync (R2.7)."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url:
            return httpx.Response(
                400,
                json={"error": {"message": "historyId too old or not found", "code": 400}},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GmailProviderAdapter(client=client)
    mailbox = Mailbox(
        id="mbx-exp-02",
        organization_id="org-01",
        provider="gmail",
        address="user@example.com",
    )
    cp = Checkpoint(mailbox_id="mbx-exp-02", history_id="ancient-id")

    res = await adapter.synchronize(mailbox, cp)
    assert res.requires_full_resync is True
    assert res.new_checkpoint.sync_state == "full_resync"


@pytest.mark.asyncio
async def test_history_list_pagination() -> None:
    """Verify history.list follows nextPageToken across pages (R2.5)."""
    raw_b64_1 = encode_urlsafe_b64(b"Subject: Page 1\r\n\r\nMessage 1")
    raw_b64_2 = encode_urlsafe_b64(b"Subject: Page 2\r\n\r\nMessage 2")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url and "pageToken=page-tok-2" in url:
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "msg-p2", "threadId": "th-p2"}}]}
                    ],
                    "historyId": "300",
                },
                request=request,
            )
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "msg-p1", "threadId": "th-p1"}}]}
                    ],
                    "historyId": "250",
                    "nextPageToken": "page-tok-2",
                },
                request=request,
            )
        if "/messages/msg-p1" in url:
            return httpx.Response(
                200,
                json={"id": "msg-p1", "threadId": "th-p1", "raw": raw_b64_1},
                request=request,
            )
        if "/messages/msg-p2" in url:
            return httpx.Response(
                200,
                json={"id": "msg-p2", "threadId": "th-p2", "raw": raw_b64_2},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = GmailProviderAdapter(client=client)
    mailbox = Mailbox(
        id="mbx-page-01",
        organization_id="org-01",
        provider="gmail",
        address="user@example.com",
    )
    cp = Checkpoint(mailbox_id="mbx-page-01", history_id="200")

    res = await adapter.synchronize(mailbox, cp)
    assert res.requires_full_resync is False
    assert len(res.messages) == 2
    ids = [m.provider_message_id for m in res.messages]
    assert ids == ["msg-p1", "msg-p2"]
    assert res.new_checkpoint.history_id == "300"
    assert res.has_more is False

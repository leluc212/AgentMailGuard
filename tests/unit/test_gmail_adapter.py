"""Unit tests for GmailProviderAdapter.

Requirements:
- R1.2: GmailProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.5: history.list() sync from stored historyId.
"""

import base64
import json
from email import message_from_bytes
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
from packages.adapters.gmail import (
    GmailProviderAdapter,
    GmailPushNotification,
    build_rfc822_mime,
    classify_gmail_error,
    decode_urlsafe_b64,
    encode_urlsafe_b64,
    parse_pubsub_notification,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter
from packages.adapters.testing import MailProviderAdapterContractSuite
from packages.domain import ProviderDraftStatus
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
    """Recorded Gmail API responses; drafts.send deletes the draft and adds a SENT message."""
    raw_b64 = encode_urlsafe_b64(b"From: user@example.com\r\nSubject: Contract\r\n\r\nBody")
    state: dict[str, bool] = {"draft_exists": False, "sent": False}

    def meta(msg_id: str, labels: list[str], rfc_id: str) -> dict[str, Any]:
        return {
            "id": msg_id,
            "threadId": "th-001",
            "labelIds": labels,
            "internalDate": "1790000000000",
            "payload": {"headers": [{"name": "Message-ID", "value": rfc_id}]},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        method = request.method

        if "/watch" in url:
            return httpx.Response(
                200, json={"historyId": "100", "expiration": "1789000000000"}, request=request
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
        if path.endswith("/drafts/send") and method == "POST":
            if not state["draft_exists"]:
                return httpx.Response(
                    404, json={"error": {"code": 404, "message": "Not Found"}}, request=request
                )
            state["draft_exists"] = False
            state["sent"] = True
            return httpx.Response(
                200,
                json={"id": "sent-msg-102", "threadId": "th-001", "labelIds": ["SENT"]},
                request=request,
            )
        if path.endswith("/drafts") and method == "GET":
            # drafts.list?q=rfc822msgid:<id> (find_draft): only the unsent contract draft matches.
            query = request.url.params.get("q", "")
            listed = (
                [{"id": "draft-101", "message": {"id": "msg-draft-101", "threadId": "th-001"}}]
                if state["draft_exists"] and query == "rfc822msgid:contract-reply-001@example.com"
                else []
            )
            return httpx.Response(
                200, json={"drafts": listed, "resultSizeEstimate": len(listed)}, request=request
            )
        if path.endswith("/drafts") and method == "POST":
            state["draft_exists"] = True
            return httpx.Response(
                200,
                json={"id": "draft-101", "message": {"id": "msg-draft-101", "threadId": "th-001"}},
                request=request,
            )
        if path.endswith("/drafts/draft-101") and method == "GET" and state["draft_exists"]:
            return httpx.Response(
                200, json={"id": "draft-101", "message": {"id": "msg-draft-101"}}, request=request
            )
        if "/messages?" in url or path.rstrip("/").endswith("/messages"):
            return httpx.Response(
                200, json={"messages": [{"id": "msg-001", "threadId": "th-001"}]}, request=request
            )
        if "/messages/send" in url and method == "POST":
            return httpx.Response(
                200, json={"id": "sent-msg-101", "threadId": "th-001"}, request=request
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
        if path.endswith("/threads/th-001") and request.url.params.get("format") == "metadata":
            messages = [meta("msg-001", ["INBOX"], "<orig-001@example.com>")]
            if state["draft_exists"]:
                messages.append(
                    meta("msg-draft-101", ["DRAFT"], "<contract-reply-001@example.com>")
                )
            if state["sent"]:
                messages.append(meta("sent-msg-102", ["SENT"], "<contract-reply-001@example.com>"))
            return httpx.Response(200, json={"id": "th-001", "messages": messages}, request=request)
        if "/threads/th-001" in url:
            return httpx.Response(
                200,
                json={
                    "id": "th-001",
                    "messages": [{"id": "msg-001", "threadId": "th-001", "raw": raw_b64}],
                },
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


# ---------------------------------------------------------------------------
# 6.3a: Message-ID, error classification, draft send / status / sent lookup.
# Every HTTP response below is a recorded Gmail REST shape; no live calls (R24.5).
# ---------------------------------------------------------------------------

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _gmail_mailbox() -> Mailbox:
    return Mailbox(
        id="mbx-gmail-63a",
        organization_id="org-63a",
        provider="gmail",
        address="support@example.com",
    )


def _gmail_reply(message_id: str | None = "<reply-63a@mail.example.com>") -> OutboundReply:
    return OutboundReply(
        thread_id="18f2c0ffee000001",
        mailbox_id="mbx-gmail-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="customer@example.com")],
        body_text="Thanks, your order has shipped.",
        subject="Re: Order #441",
        in_reply_to="<orig-441@example.com>",
        references=["<orig-441@example.com>"],
        message_id=message_id,
    )


def _gmail_error(status: int, reason: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "usageLimits", "reason": reason}],
        }
    }


def test_build_rfc822_mime_sets_message_id() -> None:
    """6.3a / R17.2: the Gmail reply carries our own Message-ID, bracketed."""
    parsed = message_from_bytes(build_rfc822_mime(_gmail_reply()))
    assert parsed["Message-ID"] == "<reply-63a@mail.example.com>"


def test_build_rfc822_mime_brackets_a_bare_message_id() -> None:
    """A Message-ID stored without angle brackets is written as a valid msg-id."""
    parsed = message_from_bytes(build_rfc822_mime(_gmail_reply("reply-63a@mail.example.com")))
    assert parsed["Message-ID"] == "<reply-63a@mail.example.com>"


def test_build_rfc822_mime_without_message_id_omits_header() -> None:
    """No message_id on the reply means no Message-ID header from us."""
    parsed = message_from_bytes(build_rfc822_mime(_gmail_reply(None)))
    assert parsed["Message-ID"] is None


@pytest.mark.parametrize(
    ("status", "headers", "payload", "expected", "retry_after_s"),
    [
        (
            429,
            {"Retry-After": "30"},
            _gmail_error(429, "rateLimitExceeded", "Too many"),
            RateLimited,
            30.0,
        ),
        (
            403,
            {"Retry-After": "12"},
            _gmail_error(403, "rateLimitExceeded", "Rate Limit Exceeded"),
            RateLimited,
            12.0,
        ),
        (
            403,
            {},
            _gmail_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
            RateLimited,
            None,
        ),
        (
            503,
            {"Retry-After": "120"},
            _gmail_error(503, "backendError", "Backend Error"),
            Transient,
            120.0,
        ),
        (500, {}, _gmail_error(500, "backendError", "Backend Error"), Transient, None),
    ],
)
@pytest.mark.asyncio
async def test_gmail_retryable_errors(
    status: int,
    headers: dict[str, str],
    payload: dict[str, Any],
    expected: type[RetryableProviderError],
    retry_after_s: float | None,
) -> None:
    """6.3a / R17.5: 429, 5xx and rate-limit 403s are retryable and keep Retry-After."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json=payload, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(expected) as exc_info:
        await adapter._request("GET", f"{GMAIL_BASE}/drafts/d-1", mailbox_id="mbx-1")
    assert isinstance(exc_info.value, RetryableProviderError)
    assert exc_info.value.retry_after_s == retry_after_s
    assert exc_info.value.provider == "gmail"
    assert exc_info.value.mailbox_id == "mbx-1"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, _gmail_error(400, "invalidArgument", "Invalid To header"), Permanent),
        (401, _gmail_error(401, "authError", "Invalid Credentials"), AuthExpired),
        (403, _gmail_error(403, "insufficientPermissions", "Insufficient Permission"), AuthExpired),
        (403, _gmail_error(403, "dailyLimitExceeded", "Daily Limit Exceeded"), Permanent),
        (403, {"error": "forbidden"}, AuthExpired),
        (404, _gmail_error(404, "notFound", "Requested entity was not found."), NotFound),
    ],
)
@pytest.mark.asyncio
async def test_gmail_permanent_errors(
    status: int, payload: dict[str, Any], expected: type[PermanentProviderError]
) -> None:
    """6.3a / R17.5: 400, 404, auth and non-rate-limit 403s are permanent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(expected) as exc_info:
        await adapter._request("GET", f"{GMAIL_BASE}/drafts/d-1")
    assert isinstance(exc_info.value, PermanentProviderError)


def test_classify_gmail_error_is_pure() -> None:
    """The mapping is a pure function of status, header and body."""
    err = classify_gmail_error(
        403,
        retry_after_header=None,
        raw_payload=_gmail_error(403, "userRateLimitExceeded", "slow"),
        mailbox_id="m",
    )
    assert isinstance(err, RateLimited)
    assert err.retry_after is None


def _recording_transport(
    routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]
) -> httpx.MockTransport:
    """Recorded responses keyed by (method, path); every request is kept for assertions."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(
                404, json=_gmail_error(404, "notFound", "Not Found"), request=request
            )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_gmail_create_draft_sends_message_id_and_returns_both_ids() -> None:
    """6.3a / R17.1: drafts.create carries threadId + our Message-ID; DraftRef has both ids."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/gmail/v1/users/me/drafts"): httpx.Response(
            200,
            json={
                "id": "r-4410001",
                "message": {
                    "id": "18f2d0000000abcd",
                    "threadId": "18f2c0ffee000001",
                    "labelIds": ["DRAFT"],
                },
            },
        )
    }
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )

    draft = await adapter.create_draft(_gmail_mailbox(), _gmail_reply())

    assert draft.provider_draft_id == "r-4410001"
    assert draft.provider_message_id == "18f2d0000000abcd"
    assert draft.provider_thread_id == "18f2c0ffee000001"
    body = json.loads(seen[0].content)
    assert body["message"]["threadId"] == "18f2c0ffee000001"
    raw = base64.urlsafe_b64decode(body["message"]["raw"] + "==")
    assert message_from_bytes(raw)["Message-ID"] == "<reply-63a@mail.example.com>"


@pytest.mark.asyncio
async def test_gmail_create_draft_without_ids_is_permanent() -> None:
    """A draft we cannot track must not be reported as created (it could never be reused)."""
    seen: list[httpx.Request] = []
    routes = {("POST", "/gmail/v1/users/me/drafts"): httpx.Response(200, json={"message": {}})}
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )
    with pytest.raises(Permanent):
        await adapter.create_draft(_gmail_mailbox(), _gmail_reply())


@pytest.mark.asyncio
async def test_gmail_send_draft_posts_draft_id_and_returns_new_message_id() -> None:
    """6.3a / R17.3: drafts.send sends by draft id; the sent copy has a NEW message id."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/gmail/v1/users/me/drafts/send"): httpx.Response(
            200,
            json={"id": "18f2e11111110001", "threadId": "18f2c0ffee000001", "labelIds": ["SENT"]},
        )
    }
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )

    sent = await adapter.send_draft(_gmail_mailbox(), "r-4410001")

    assert json.loads(seen[0].content) == {"id": "r-4410001"}
    assert sent.provider_message_id == "18f2e11111110001"
    assert sent.provider_thread_id == "18f2c0ffee000001"


@pytest.mark.asyncio
async def test_gmail_send_draft_without_message_id_is_ambiguous_transient() -> None:
    """A 200 without an id may have sent; retry so dispatch reconciles instead of dead-lettering."""
    seen: list[httpx.Request] = []
    routes = {("POST", "/gmail/v1/users/me/drafts/send"): httpx.Response(200, json={})}
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )
    with pytest.raises(Transient):
        await adapter.send_draft(_gmail_mailbox(), "r-4410001")


@pytest.mark.asyncio
async def test_gmail_get_draft_status_draft_and_missing() -> None:
    """6.3a: drafts.get 200 is DRAFT; 404 (sent or deleted) is MISSING. Gmail never reports SENT."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/drafts/r-4410001"): httpx.Response(
            200, json={"id": "r-4410001", "message": {"id": "18f2d0000000abcd"}}
        )
    }
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )

    assert (
        await adapter.get_draft_status(_gmail_mailbox(), "r-4410001") is ProviderDraftStatus.DRAFT
    )
    assert await adapter.get_draft_status(_gmail_mailbox(), "r-gone") is ProviderDraftStatus.MISSING
    assert seen[0].url.params["format"] == "minimal"


def _thread_metadata(*messages: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"id": "18f2c0ffee000001", "messages": list(messages)})


def _meta_message(msg_id: str, labels: list[str], rfc_id: str) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": "18f2c0ffee000001",
        "labelIds": labels,
        "internalDate": "1790000000000",
        "payload": {"headers": [{"name": "Message-Id", "value": rfc_id}]},
    }


@pytest.mark.asyncio
async def test_gmail_find_sent_message_matches_our_message_id() -> None:
    """6.3a / design §5.8 step 4: the SENT copy is found by our Message-ID, not the draft copy."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/threads/18f2c0ffee000001"): _thread_metadata(
            _meta_message("18f2c0ffee000001", ["INBOX"], "<orig-441@example.com>"),
            _meta_message("18f2d0000000abcd", ["DRAFT"], "<reply-63a@mail.example.com>"),
            _meta_message("18f2e11111110001", ["SENT"], "<reply-63a@mail.example.com>"),
        )
    }
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )

    found = await adapter.find_sent_message(
        _gmail_mailbox(), "18f2c0ffee000001", "reply-63a@mail.example.com"
    )

    assert found is not None
    assert found.provider_message_id == "18f2e11111110001"
    assert found.provider_thread_id == "18f2c0ffee000001"
    assert seen[0].url.params["format"] == "metadata"
    assert seen[0].url.params.get_list("metadataHeaders") == ["Message-ID"]


@pytest.mark.asyncio
async def test_gmail_find_sent_message_matches_provider_id_and_ignores_drafts() -> None:
    """A provider id matches only a SENT message; an unsent draft or unknown id is None."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/threads/18f2c0ffee000001"): _thread_metadata(
            _meta_message("18f2d0000000abcd", ["DRAFT"], "<reply-63a@mail.example.com>"),
            _meta_message("18f2e11111110001", ["SENT"], "<reply-63a@mail.example.com>"),
        )
    }
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )
    mailbox = _gmail_mailbox()

    by_id = await adapter.find_sent_message(mailbox, "18f2c0ffee000001", "18f2e11111110001")
    assert by_id is not None and by_id.provider_message_id == "18f2e11111110001"
    assert await adapter.find_sent_message(mailbox, "18f2c0ffee000001", "18f2d0000000abcd") is None
    assert await adapter.find_sent_message(mailbox, "18f2c0ffee000001", "<other@x>") is None
    assert (
        await adapter.find_sent_message(mailbox, "thread-gone", "<reply-63a@mail.example.com>")
        is None
    )


@pytest.mark.asyncio
async def test_gmail_find_draft_searches_drafts_by_rfc822msgid() -> None:
    """6.5: a redelivery adopts the unrecorded draft carrying our Message-ID (no second draft)."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/drafts"): httpx.Response(
            200,
            json={
                "drafts": [
                    {
                        "id": "r-4410001",
                        "message": {"id": "18f2d0000000abcd", "threadId": "18f2c0ffee000001"},
                    }
                ],
                "resultSizeEstimate": 1,
            },
        )
    }
    adapter = GmailProviderAdapter(
        client=httpx.AsyncClient(transport=_recording_transport(routes, seen))
    )
    mailbox = _gmail_mailbox()

    found = await adapter.find_draft(mailbox, "18f2c0ffee000001", "<reply-63a@mail.example.com>")

    assert found is not None
    assert (found.provider_draft_id, found.provider_message_id, found.provider_thread_id) == (
        "r-4410001",
        "18f2d0000000abcd",
        "18f2c0ffee000001",
    )
    assert seen[0].url.params["q"] == "rfc822msgid:reply-63a@mail.example.com"
    # A draft in another thread is not ours to adopt.
    assert (
        await adapter.find_draft(mailbox, "18f2c0ffee999999", "<reply-63a@mail.example.com>")
        is None
    )
    assert await adapter.find_draft(mailbox, "18f2c0ffee000001", "  ") is None


@pytest.mark.asyncio
async def test_incremental_sync_asks_only_for_inbox_messages() -> None:
    """6.7: our own drafts and sent copies carry DRAFT/SENT, not INBOX; sync skips them."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/history" in str(request.url):
            return httpx.Response(200, json={"history": [], "historyId": "501"}, request=request)
        return httpx.Response(404, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mailbox = Mailbox(id="mbx-inbox", organization_id="org-01", provider="gmail", address="a@b.c")
    await adapter.synchronize(mailbox, Checkpoint(mailbox_id="mbx-inbox", history_id="500"))

    history_calls = [u for u in seen if "/history" in u]
    assert len(history_calls) == 1
    assert "labelId=INBOX" in history_calls[0]


@pytest.mark.asyncio
async def test_initial_sync_lists_only_inbox_messages() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []}, request=request)
        return httpx.Response(404, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mailbox = Mailbox(id="mbx-init", organization_id="org-01", provider="gmail", address="a@b.c")
    await adapter.synchronize(mailbox, Checkpoint(mailbox_id="mbx-init", history_id=None))

    assert any("labelIds=INBOX" in u for u in seen), seen


@pytest.mark.asyncio
async def test_sync_skips_a_message_deleted_after_history_listed_it() -> None:
    """drafts.send deletes the draft; a 404 on one message must not fail the whole sync (6.7)."""
    raw_ok = encode_urlsafe_b64(b"Subject: kept\r\n\r\nbody")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "gone-1"}}]},
                        {"messagesAdded": [{"message": {"id": "kept-1"}}]},
                    ],
                    "historyId": "700",
                },
                request=request,
            )
        if "/messages/kept-1" in url:
            return httpx.Response(
                200, json={"id": "kept-1", "threadId": "th-k", "raw": raw_ok}, request=request
            )
        return httpx.Response(404, json={"error": {"code": 404}}, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mailbox = Mailbox(id="mbx-gone", organization_id="org-01", provider="gmail", address="a@b.c")
    res = await adapter.synchronize(mailbox, Checkpoint(mailbox_id="mbx-gone", history_id="650"))

    assert [m.provider_message_id for m in res.messages] == ["kept-1"]
    assert res.requires_full_resync is False
    assert res.new_checkpoint.history_id == "700"

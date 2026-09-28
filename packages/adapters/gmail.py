"""Gmail provider adapter implementation.

Requirements:
- R1.2: GmailProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.5: history.list() incremental sync from stored historyId.
- design.md §5.1: MailProviderAdapter protocol conformance.
- R17.1, R17.2, R17.3, R17.5: reply Message-ID, draft send/status/sent lookup,
  retryable vs permanent errors (task 6.3a).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
    parse_retry_after,
)
from packages.adapters.registry import register_adapter
from packages.domain import ProviderDraftStatus
from packages.domain.entities import (
    Checkpoint,
    DraftRef,
    Mailbox,
    OutboundReply,
    RawMessage,
    RawThread,
    SentRef,
    Subscription,
    SyncResult,
)

logger = logging.getLogger(__name__)

# Gmail "Resolve errors" guide: these 403 reasons are rate limits, retried with backoff.
_GMAIL_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
# A spent daily quota does not recover within the retry ladder (30 s / 5 m / 30 m).
_GMAIL_DAILY_QUOTA_REASON = "dailyLimitExceeded"


@dataclass(frozen=True)
class GmailPushNotification:
    """Parsed Gmail Cloud Pub/Sub push notification (R1.2, R2.1)."""

    email_address: str
    history_id: str


def parse_pubsub_notification(
    payload: dict[str, Any] | bytes | str,
) -> GmailPushNotification:
    """Parse a Google Cloud Pub/Sub push notification payload.

    Extracts emailAddress and historyId from the nested, base64-encoded message.data
    field according to the Gmail push notification specification.
    """
    data_dict: dict[str, Any]
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        data_dict = json.loads(payload)
    elif isinstance(payload, dict):
        data_dict = payload
    else:
        raise ValueError(f"Unsupported Pub/Sub payload type: {type(payload)}")

    # Standard Pub/Sub push envelope: {"message": {"data": "...", "messageId": "..."}, ...}
    if "message" in data_dict and isinstance(data_dict["message"], dict):
        raw_b64 = data_dict["message"].get("data", "")
        if raw_b64:
            decoded_bytes = decode_urlsafe_b64(raw_b64)
            inner = json.loads(decoded_bytes.decode("utf-8"))
            return GmailPushNotification(
                email_address=inner.get("emailAddress", ""),
                history_id=str(inner.get("historyId", "")),
            )

    # Direct data format: {"emailAddress": "...", "historyId": "..."}
    if "emailAddress" in data_dict and "historyId" in data_dict:
        return GmailPushNotification(
            email_address=str(data_dict["emailAddress"]),
            history_id=str(data_dict["historyId"]),
        )

    raise ValueError("Payload missing valid Pub/Sub message data or emailAddress/historyId")


def encode_urlsafe_b64(data: bytes) -> str:
    """Encode bytes into standard URL-safe base64 string without newlines."""
    return base64.urlsafe_b64encode(data).decode("ascii")


def decode_urlsafe_b64(data: str) -> bytes:
    """Decode URL-safe base64 string, restoring trailing padding if stripped."""
    clean = data.strip().replace("-", "+").replace("_", "/")
    padding = len(clean) % 4
    if padding:
        clean += "=" * (4 - padding)
    return base64.b64decode(clean)


def _bare_message_id(value: str) -> str:
    """Strip whitespace and angle brackets from an RFC 5322 msg-id."""
    return value.strip().strip("<>").strip()


def _bracketed_message_id(value: str) -> str:
    """Return a msg-id in its RFC 5322 `<id-left@id-right>` form."""
    return f"<{_bare_message_id(value)}>"


def _gmail_error_reasons(raw_payload: Any) -> set[str]:
    """Collect `error.errors[].reason` from a Google API error body."""
    if not isinstance(raw_payload, dict):
        return set()
    error = raw_payload.get("error")
    if not isinstance(error, dict):
        return set()
    errors = error.get("errors")
    if not isinstance(errors, list):
        return set()
    return {str(item["reason"]) for item in errors if isinstance(item, dict) and item.get("reason")}


def classify_gmail_error(
    status: int,
    *,
    retry_after_header: str | None,
    raw_payload: Any,
    mailbox_id: str | None,
) -> ProviderError:
    """Map a failed Gmail response to the common taxonomy (R1.5, R1.6, R17.5).

    429, 5xx and 403 rateLimitExceeded/userRateLimitExceeded are retryable and keep
    Retry-After; 400, 404, 401 and every other 403 are permanent.
    """
    retry_after_s = parse_retry_after(retry_after_header)
    reasons = _gmail_error_reasons(raw_payload)

    if status == 429 or (status == 403 and reasons & _GMAIL_RATE_LIMIT_REASONS):
        return RateLimited(
            f"Gmail rate limit exceeded (HTTP {status})",
            retry_after=retry_after_s,
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status == 403 and _GMAIL_DAILY_QUOTA_REASON in reasons:
        return Permanent(
            "Gmail daily quota exhausted (HTTP 403 dailyLimitExceeded)",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status in (401, 403):
        return AuthExpired(
            f"Gmail authentication failed or token expired (HTTP {status})",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status == 404:
        return NotFound(
            "Gmail resource not found",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status >= 500:
        return Transient(
            f"Gmail temporary server error (HTTP {status})",
            retry_after_s=retry_after_s,
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    return Permanent(
        f"Gmail API permanent failure (HTTP {status}): {raw_payload}",
        provider="gmail",
        mailbox_id=mailbox_id,
        raw_error=raw_payload,
    )


def _gmail_internal_date(data: dict[str, Any]) -> datetime:
    """Convert Gmail `internalDate` (epoch ms) to an aware datetime, falling back to now."""
    try:
        return datetime.fromtimestamp(int(data["internalDate"]) / 1000.0, UTC)
    except (KeyError, TypeError, ValueError):
        return datetime.now(UTC)


def build_rfc822_mime(reply: OutboundReply) -> bytes:
    """Build compliant RFC822 MIME byte message from OutboundReply."""
    msg = EmailMessage()
    msg["To"] = ", ".join(str(addr) for addr in reply.to)
    if reply.cc:
        msg["Cc"] = ", ".join(str(addr) for addr in reply.cc)
    if reply.subject:
        msg["Subject"] = reply.subject
    if reply.in_reply_to:
        msg["In-Reply-To"] = reply.in_reply_to
    if reply.references:
        msg["References"] = " ".join(reply.references)
    if reply.message_id:
        msg["Message-ID"] = _bracketed_message_id(reply.message_id)

    if reply.body_html:
        msg.set_content(reply.body_text)
        msg.add_alternative(reply.body_html, subtype="html")
    else:
        msg.set_content(reply.body_text)

    return msg.as_bytes()


class GmailProviderAdapter:
    """Gmail REST API adapter implementing MailProviderAdapter protocol (R1.1, R1.2)."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://gmail.googleapis.com/gmail/v1/users/me",
        access_token: str | None = None,
        topic_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.client = client or httpx.AsyncClient()
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.topic_name = topic_name
        self.kwargs = kwargs

    async def _request(
        self,
        method: str,
        url: str,
        mailbox_id: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute HTTP request and map Google API errors to common taxonomy."""
        headers = kwargs.pop("headers", {})
        if self.access_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            resp = await self.client.request(method, url, headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as err:
            raise Transient(
                f"Gmail API network error: {err}",
                provider="gmail",
                mailbox_id=mailbox_id,
                raw_error=err,
            ) from err

        if resp.is_success:
            return resp

        raw_payload = None
        try:
            raw_payload = resp.json()
        except Exception:
            raw_payload = resp.text

        raise classify_gmail_error(
            resp.status_code,
            retry_after_header=resp.headers.get("Retry-After"),
            raw_payload=raw_payload,
            mailbox_id=mailbox_id,
        )

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        """Create a push notification watch on the mailbox (R1.1, R2.1)."""
        url = f"{self.base_url}/watch"
        topic = self.topic_name or f"projects/{mailbox.organization_id}/topics/gmail-push"
        body = {"topicName": topic, "labelIds": ["INBOX"]}

        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        exp_ms = data.get("expiration")
        expires_at = (
            datetime.fromtimestamp(int(exp_ms) / 1000.0, UTC)
            if exp_ms
            else datetime.now(UTC) + timedelta(days=7)
        )

        return Subscription(
            mailbox_id=mailbox.id,
            subscription_id=f"gmail-watch-{mailbox.id}",
            expires_at=expires_at,
            provider="gmail",
            resource=topic,
        )

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        """Renew an existing push notification watch (R1.1, R2.10)."""
        url = f"{self.base_url}/watch"
        topic = sub.resource or self.topic_name or "projects/default/topics/gmail-push"
        body = {"topicName": topic, "labelIds": ["INBOX"]}

        resp = await self._request("POST", url, mailbox_id=str(sub.mailbox_id), json=body)
        data = resp.json()

        exp_ms = data.get("expiration")
        expires_at = (
            datetime.fromtimestamp(int(exp_ms) / 1000.0, UTC)
            if exp_ms
            else datetime.now(UTC) + timedelta(days=7)
        )

        return Subscription(
            mailbox_id=sub.mailbox_id,
            subscription_id=sub.subscription_id,
            expires_at=expires_at,
            provider="gmail",
            resource=topic,
            client_state=sub.client_state,
        )

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        """Incrementally sync Gmail INBOX additions using history.list (R1.1, R2.5, R17.7)."""
        if cp.history_id:
            base_history_url = (
                f"{self.base_url}/history?startHistoryId={cp.history_id}"
                "&historyTypes=messageAdded&labelId=INBOX"
            )
            page_token = None
            msg_ids: list[str] = []
            latest_history_id = str(cp.history_id)

            while True:
                url = base_history_url
                if page_token:
                    url += f"&pageToken={page_token}"

                try:
                    resp = await self._request("GET", url, mailbox_id=str(mailbox.id))
                except (NotFound, Permanent) as exc:
                    # Expired or too old historyId triggers full resync (R2.7, design.md §5.1)
                    if isinstance(exc, NotFound) or "history" in str(exc).lower():
                        return SyncResult(
                            messages=[],
                            new_checkpoint=Checkpoint(
                                mailbox_id=mailbox.id,
                                history_id=None,
                                sync_state="full_resync",
                                last_sync_at=datetime.now(UTC),
                            ),
                            requires_full_resync=True,
                            has_more=False,
                        )
                    raise

                data = resp.json()
                if "historyId" in data:
                    latest_history_id = str(data["historyId"])

                for item in data.get("history", []):
                    for added in item.get("messagesAdded", []):
                        mid = added.get("message", {}).get("id")
                        if mid and mid not in msg_ids:
                            msg_ids.append(mid)
                    for m in item.get("messages", []):
                        mid = m.get("id")
                        if mid and mid not in msg_ids:
                            msg_ids.append(mid)

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            fetched_messages: list[RawMessage] = []
            for mid in msg_ids:
                try:
                    fetched_messages.append(await self.get_message(mailbox, mid))
                except NotFound:
                    # Deleted between history.list and messages.get (a draft that drafts.send
                    # replaced, or mail the user deleted): nothing left to ingest (6.7).
                    logger.warning(
                        "Gmail message %s vanished before fetch; skipped",
                        mid,
                        extra={"mailbox_id": str(mailbox.id)},
                    )

            new_cp = Checkpoint(
                mailbox_id=mailbox.id,
                history_id=latest_history_id,
                sync_state="idle",
                last_sync_at=datetime.now(UTC),
            )
            return SyncResult(
                messages=fetched_messages,
                new_checkpoint=new_cp,
                requires_full_resync=False,
                has_more=False,
            )

        # Initial synchronization fallback
        url = f"{self.base_url}/messages?maxResults=50&labelIds=INBOX"
        resp = await self._request("GET", url, mailbox_id=str(mailbox.id))
        data = resp.json()

        fetched: list[RawMessage] = []
        for item in data.get("messages", []):
            fetched.append(await self.get_message(mailbox, item["id"]))

        new_cp = Checkpoint(
            mailbox_id=mailbox.id,
            history_id=data.get("nextPageToken", "initial"),
            sync_state="idle",
            last_sync_at=datetime.now(UTC),
        )
        return SyncResult(
            messages=fetched,
            new_checkpoint=new_cp,
            requires_full_resync=False,
            has_more=bool(data.get("nextPageToken")),
        )

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        """Fetch raw message payload by ID (R1.1, R1.4)."""
        url = f"{self.base_url}/messages/{provider_message_id}?format=raw"
        resp = await self._request("GET", url, mailbox_id=str(mailbox.id))
        data = resp.json()

        raw_bytes = decode_urlsafe_b64(data.get("raw", ""))
        internal_date = _gmail_internal_date(data) if "internalDate" in data else None

        return RawMessage(
            provider_message_id=provider_message_id,
            provider_thread_id=data.get("threadId"),
            raw_payload=raw_bytes,
            internal_date=internal_date,
            history_id=str(data.get("historyId", "")),
        )

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        """Fetch raw messages belonging to a thread (R1.1, R1.4)."""
        url = f"{self.base_url}/threads/{provider_thread_id}?format=raw"
        resp = await self._request("GET", url, mailbox_id=str(mailbox.id))
        data = resp.json()

        messages: list[RawMessage] = []
        for msg_data in data.get("messages", []):
            mid = msg_data.get("id", "")
            raw_b64 = msg_data.get("raw", "")
            raw_b = decode_urlsafe_b64(raw_b64) if raw_b64 else b""
            messages.append(
                RawMessage(
                    provider_message_id=mid,
                    provider_thread_id=provider_thread_id,
                    raw_payload=raw_b,
                )
            )

        return RawThread(
            provider_thread_id=provider_thread_id,
            messages=messages,
        )

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Create a reply draft in the Gmail thread (R1.1, R1.4, R17.1).

        Returns the stable draft id and the draft message's id; a response without
        both cannot be reused on redelivery, so it is permanent.
        """
        url = f"{self.base_url}/drafts"
        raw_b64 = encode_urlsafe_b64(build_rfc822_mime(reply))
        body = {"message": {"raw": raw_b64, "threadId": str(reply.thread_id)}}
        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        msg_obj = data.get("message") or {}
        draft_id = data.get("id")
        message_id = msg_obj.get("id")
        if not draft_id or not message_id:
            raise Permanent(
                "Gmail drafts.create returned no draft id or message id",
                provider="gmail",
                mailbox_id=str(mailbox.id),
                raw_error=data,
            )
        return DraftRef(
            provider_draft_id=str(draft_id),
            provider_message_id=str(message_id),
            provider_thread_id=msg_obj.get("threadId", str(reply.thread_id)),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Send outbound reply email through Gmail (R1.1, R1.4, R17.4)."""
        url = f"{self.base_url}/messages/send"
        raw_mime = build_rfc822_mime(reply)
        raw_b64 = encode_urlsafe_b64(raw_mime)

        body = {
            "raw": raw_b64,
            "threadId": str(reply.thread_id),
        }
        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        return SentRef(
            provider_message_id=data.get("id", ""),
            provider_thread_id=data.get("threadId", str(reply.thread_id)),
            sent_at=datetime.now(UTC),
        )

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        """Send an existing draft with users.drafts.send (R17.3, design.md §5.8 step 3).

        Gmail deletes the draft and returns a NEW message carrying the SENT label; its
        id is the one mailbox sync will later see.
        """
        url = f"{self.base_url}/drafts/send"
        resp = await self._request(
            "POST", url, mailbox_id=str(mailbox.id), json={"id": provider_draft_id}
        )
        data = resp.json()
        sent_id = data.get("id")
        if not sent_id:
            # The send may have happened: retry, and let dispatch reconcile via get_draft_status.
            raise Transient(
                "Gmail drafts.send returned no message id (outcome unknown)",
                provider="gmail",
                mailbox_id=str(mailbox.id),
                raw_error=data,
            )
        return SentRef(
            provider_message_id=str(sent_id),
            provider_thread_id=data.get("threadId"),
            sent_at=datetime.now(UTC),
        )

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        """Report whether the draft still exists (R17.3, design.md §5.8 step 4).

        Gmail deletes a draft when it is sent, so a sent draft and a draft a person
        deleted both answer 404: MISSING. SENT is never returned by Gmail.
        """
        url = f"{self.base_url}/drafts/{quote(provider_draft_id, safe='')}"
        try:
            await self._request(
                "GET", url, mailbox_id=str(mailbox.id), params={"format": "minimal"}
            )
        except NotFound:
            return ProviderDraftStatus.MISSING
        return ProviderDraftStatus.DRAFT

    async def find_sent_message(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        provider_message_id: str,
    ) -> SentRef | None:
        """Find our sent reply in the thread (R17.3, design.md §5.8 step 4).

        Matches a SENT-labelled, non-draft message whose Gmail id equals the key or whose
        RFC 5322 Message-ID equals the key (angle brackets optional). A missing thread
        returns None.
        """
        url = f"{self.base_url}/threads/{quote(provider_thread_id, safe='')}"
        try:
            resp = await self._request(
                "GET",
                url,
                mailbox_id=str(mailbox.id),
                params=[("format", "metadata"), ("metadataHeaders", "Message-ID")],
            )
        except NotFound:
            return None

        wanted = _bare_message_id(provider_message_id)
        for msg in resp.json().get("messages", []):
            labels = msg.get("labelIds") or []
            if "SENT" not in labels or "DRAFT" in labels:
                continue
            headers = (msg.get("payload") or {}).get("headers") or []
            header_ids = {
                _bare_message_id(str(h.get("value", "")))
                for h in headers
                if str(h.get("name", "")).lower() == "message-id"
            }
            if msg.get("id") == provider_message_id or wanted in header_ids:
                return SentRef(
                    provider_message_id=str(msg["id"]),
                    provider_thread_id=provider_thread_id,
                    sent_at=_gmail_internal_date(msg),
                )
        return None

    async def find_draft(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        message_id: str,
    ) -> DraftRef | None:
        """Find an unsent draft carrying our RFC 5322 Message-ID (tasks.md 6.5, §5.8 step 2).

        A redelivery after a crash between drafts.create and recording the draft id adopts
        this draft instead of creating a second one. users.drafts.list takes Gmail search
        syntax, where ``rfc822msgid:`` matches the Message-ID header; each listed draft
        carries only its id and its message's id and threadId. A draft in another thread
        is ignored.
        """
        wanted = _bare_message_id(message_id)
        if not wanted:
            return None
        url = f"{self.base_url}/drafts"
        resp = await self._request(
            "GET",
            url,
            mailbox_id=str(mailbox.id),
            params={"q": f"rfc822msgid:{wanted}", "maxResults": "10"},
        )
        for entry in resp.json().get("drafts") or []:
            message = entry.get("message") or {}
            draft_id = entry.get("id")
            draft_message_id = message.get("id")
            thread = message.get("threadId")
            if not draft_id or not draft_message_id:
                continue
            if thread and provider_thread_id and thread != provider_thread_id:
                continue
            return DraftRef(
                provider_draft_id=str(draft_id),
                provider_message_id=str(draft_message_id),
                provider_thread_id=str(thread or provider_thread_id),
            )
        return None


# Register GmailProviderAdapter in registry
register_adapter("gmail", GmailProviderAdapter)

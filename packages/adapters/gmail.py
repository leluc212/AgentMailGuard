"""Gmail provider adapter implementation.

Requirements:
- R1.2: GmailProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.5: history.list() incremental sync from stored historyId.
- design.md §5.1: MailProviderAdapter protocol conformance.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import httpx

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    RateLimited,
    Transient,
)
from packages.adapters.registry import register_adapter
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

        status = resp.status_code
        if status == 429:
            retry_after_hdr = resp.headers.get("Retry-After")
            retry_after_val: float | None = None
            if retry_after_hdr:
                try:
                    retry_after_val = float(retry_after_hdr)
                except ValueError:
                    retry_after_val = 60.0
            raise RateLimited(
                "Gmail rate limit exceeded",
                retry_after=retry_after_val,
                provider="gmail",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status in (401, 403):
            raise AuthExpired(
                f"Gmail authentication failed or token expired (HTTP {status})",
                provider="gmail",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status == 404:
            raise NotFound(
                "Gmail resource not found",
                provider="gmail",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status >= 500:
            raise Transient(
                f"Gmail temporary server error (HTTP {status})",
                provider="gmail",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        raise Permanent(
            f"Gmail API permanent failure (HTTP {status}): {raw_payload}",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
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
        """Incrementally synchronize Gmail changes using history.list (R1.1, R2.5)."""
        if cp.history_id:
            base_history_url = (
                f"{self.base_url}/history?startHistoryId={cp.history_id}&historyTypes=messageAdded"
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
                fetched_messages.append(await self.get_message(mailbox, mid))

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
        url = f"{self.base_url}/messages?maxResults=50"
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
        internal_date = None
        if "internalDate" in data:
            try:
                internal_date = datetime.fromtimestamp(int(data["internalDate"]) / 1000.0, UTC)
            except Exception:
                internal_date = datetime.now(UTC)

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
        """Create draft email in Gmail (R1.1, R1.4, R17.1)."""
        url = f"{self.base_url}/drafts"
        raw_mime = build_rfc822_mime(reply)
        raw_b64 = encode_urlsafe_b64(raw_mime)

        body = {
            "message": {
                "raw": raw_b64,
                "threadId": str(reply.thread_id),
            }
        }
        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        msg_obj = data.get("message", {})
        return DraftRef(
            provider_draft_id=data.get("id", ""),
            provider_message_id=msg_obj.get("id"),
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


# Register GmailProviderAdapter in registry
register_adapter("gmail", GmailProviderAdapter)

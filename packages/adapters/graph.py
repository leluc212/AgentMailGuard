"""Microsoft Graph provider adapter implementation.

Requirements:
- R1.2: GraphProviderAdapter / MicrosoftGraphProviderAdapter implementation.
- R1.6: Honour retry-after on rate limiting.
- R2.6: Delta query following @odata.nextLink to terminal @odata.deltaLink.
- R2.7: Invalid delta token detection triggering bounded full resync.
- design.md §5.1: MailProviderAdapter protocol conformance.
"""

from __future__ import annotations

import contextlib
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
class GraphChangeNotification:
    """Parsed Microsoft Graph change notification (R1.2, R2.1)."""

    subscription_id: str
    change_type: str
    resource: str
    resource_id: str | None = None
    client_state: str | None = None
    subscription_expiration_datetime: str | None = None


def parse_graph_notification(
    payload: dict[str, Any] | bytes | str,
) -> list[GraphChangeNotification]:
    """Parse a Microsoft Graph webhook change notification payload.

    Extracts items from the 'value' array in the Graph notification payload.
    """
    data_dict: dict[str, Any]
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        data_dict = json.loads(payload)
    elif isinstance(payload, dict):
        data_dict = payload
    else:
        raise ValueError(f"Unsupported Graph notification payload type: {type(payload)}")

    notifications: list[GraphChangeNotification] = []

    # Graph webhook sends {"value": [ { ... notification ... } ]}
    items = data_dict.get("value")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                sub_id = str(item.get("subscriptionId", ""))
                c_type = str(item.get("changeType", ""))
                resource = str(item.get("resource", ""))
                res_data = item.get("resourceData", {})
                res_id = (
                    str(res_data.get("id", ""))
                    if isinstance(res_data, dict) and "id" in res_data
                    else None
                )
                client_state = item.get("clientState")
                exp_dt = item.get("subscriptionExpirationDateTime")

                notifications.append(
                    GraphChangeNotification(
                        subscription_id=sub_id,
                        change_type=c_type,
                        resource=resource,
                        resource_id=res_id,
                        client_state=client_state,
                        subscription_expiration_datetime=exp_dt,
                    )
                )
        return notifications

    # If payload is a single notification dictionary
    if "subscriptionId" in data_dict and "changeType" in data_dict:
        sub_id = str(data_dict.get("subscriptionId", ""))
        c_type = str(data_dict.get("changeType", ""))
        resource = str(data_dict.get("resource", ""))
        res_data = data_dict.get("resourceData", {})
        res_id = (
            str(res_data.get("id", "")) if isinstance(res_data, dict) and "id" in res_data else None
        )
        client_state = data_dict.get("clientState")
        exp_dt = data_dict.get("subscriptionExpirationDateTime")

        return [
            GraphChangeNotification(
                subscription_id=sub_id,
                change_type=c_type,
                resource=resource,
                resource_id=res_id,
                client_state=client_state,
                subscription_expiration_datetime=exp_dt,
            )
        ]

    raise ValueError("Payload missing valid Microsoft Graph 'value' array or notification fields")


def build_rfc822_mime_from_reply(reply: OutboundReply) -> bytes:
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


class GraphProviderAdapter:
    """Microsoft Graph REST API adapter implementing MailProviderAdapter protocol (R1.1, R1.2)."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://graph.microsoft.com/v1.0/me",
        access_token: str | None = None,
        notification_url: str | None = None,
        client_state: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.client = client or httpx.AsyncClient()
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.notification_url = notification_url
        self.client_state = client_state
        self.kwargs = kwargs

    async def _request(
        self,
        method: str,
        url: str,
        mailbox_id: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute HTTP request and map Microsoft Graph errors to common taxonomy."""
        headers = kwargs.pop("headers", {})
        if self.access_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            resp = await self.client.request(method, url, headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as err:
            raise Transient(
                f"Microsoft Graph API network error: {err}",
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=err,
            ) from err

        if resp.is_success:
            return resp

        raw_payload: Any = None
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
                "Microsoft Graph rate limit exceeded",
                retry_after=retry_after_val,
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status in (401, 403):
            raise AuthExpired(
                f"Microsoft Graph authentication failed or token expired (HTTP {status})",
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status == 404:
            raise NotFound(
                "Microsoft Graph resource not found",
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status == 410:
            # Gone: e.g. Delta token expired (R2.7)
            raise Permanent(
                "Microsoft Graph resource gone / delta token expired (HTTP 410)",
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        if status >= 500:
            raise Transient(
                f"Microsoft Graph temporary server error (HTTP {status})",
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )

        raise Permanent(
            f"Microsoft Graph API permanent failure (HTTP {status}): {raw_payload}",
            provider="graph",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        """Create a webhook change notification subscription on the mailbox (R1.1, R2.1)."""
        sub_endpoint = "https://graph.microsoft.com/v1.0/subscriptions"
        notification_url = (
            self.notification_url or f"https://api.example.com/webhooks/graph/{mailbox.id}"
        )
        expires_at = datetime.now(UTC) + timedelta(minutes=4200)

        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": "/me/mailFolders('Inbox')/messages",
            "expirationDateTime": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "clientState": self.client_state or f"secret-{mailbox.id}",
        }

        resp = await self._request("POST", sub_endpoint, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        exp_str = data.get("expirationDateTime")
        if exp_str:
            with contextlib.suppress(Exception):
                expires_at = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))

        return Subscription(
            mailbox_id=mailbox.id,
            subscription_id=data.get("id", f"graph-sub-{mailbox.id}"),
            expires_at=expires_at,
            provider="graph",
            resource=data.get("resource", body["resource"]),
            client_state=data.get("clientState", body["clientState"]),
        )

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        """Renew an existing push notification subscription (R1.1, R2.10)."""
        sub_endpoint = f"https://graph.microsoft.com/v1.0/subscriptions/{sub.subscription_id}"
        expires_at = datetime.now(UTC) + timedelta(minutes=4200)
        body = {
            "expirationDateTime": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

        resp = await self._request("PATCH", sub_endpoint, mailbox_id=str(sub.mailbox_id), json=body)
        data = resp.json()

        exp_str = data.get("expirationDateTime")
        if exp_str:
            with contextlib.suppress(Exception):
                expires_at = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))

        return Subscription(
            mailbox_id=sub.mailbox_id,
            subscription_id=sub.subscription_id,
            expires_at=expires_at,
            provider="graph",
            resource=sub.resource,
            client_state=sub.client_state,
        )

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        """Incrementally synchronize Graph changes using delta query (R1.1, R2.6, R2.7)."""
        url: str = cp.delta_link or f"{self.base_url}/mailFolders/Inbox/messages/delta"

        msg_ids: list[str] = []
        next_url: str | None = url
        final_delta_link: str | None = None
        loop_guard = 0
        max_pages = 50

        while next_url and loop_guard < max_pages:
            loop_guard += 1
            try:
                resp = await self._request("GET", next_url, mailbox_id=str(mailbox.id))
            except (Permanent, NotFound) as exc:
                err_str = str(exc).lower()
                is_delta_error = (
                    "410" in err_str
                    or "resyncrequired" in err_str
                    or "invaliddeltatoken" in err_str
                    or "delta token" in err_str
                )
                if is_delta_error:
                    return SyncResult(
                        messages=[],
                        new_checkpoint=Checkpoint(
                            mailbox_id=mailbox.id,
                            delta_link=None,
                            sync_state="full_resync",
                            last_sync_at=datetime.now(UTC),
                        ),
                        requires_full_resync=True,
                        has_more=False,
                    )
                raise

            data = resp.json()

            for item in data.get("value", []):
                if "@removed" not in item and "id" in item:
                    mid = str(item["id"])
                    if mid not in msg_ids:
                        msg_ids.append(mid)

            next_url = data.get("@odata.nextLink")
            if "@odata.deltaLink" in data:
                final_delta_link = data["@odata.deltaLink"]

        fetched_messages: list[RawMessage] = []
        for mid in msg_ids:
            fetched_messages.append(await self.get_message(mailbox, mid))

        new_cp = Checkpoint(
            mailbox_id=mailbox.id,
            delta_link=final_delta_link or cp.delta_link,
            sync_state="idle",
            last_sync_at=datetime.now(UTC),
        )

        return SyncResult(
            messages=fetched_messages,
            new_checkpoint=new_cp,
            requires_full_resync=False,
            has_more=bool(next_url),
        )

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        """Fetch raw message payload by ID (R1.1, R1.4)."""
        url = f"{self.base_url}/messages/{provider_message_id}/$value"
        resp = await self._request("GET", url, mailbox_id=str(mailbox.id))

        raw_bytes = resp.content
        internal_date = None

        metadata_url = f"{self.base_url}/messages/{provider_message_id}"
        thread_id: str | None = None
        try:
            meta_resp = await self._request("GET", metadata_url, mailbox_id=str(mailbox.id))
            meta_data = meta_resp.json()
            thread_id = meta_data.get("conversationId")
            rec_dt = meta_data.get("receivedDateTime")
            if rec_dt:
                try:
                    internal_date = datetime.fromisoformat(rec_dt.replace("Z", "+00:00"))
                except Exception:
                    internal_date = datetime.now(UTC)
        except Exception:
            internal_date = datetime.now(UTC)

        return RawMessage(
            provider_message_id=provider_message_id,
            provider_thread_id=thread_id,
            raw_payload=raw_bytes,
            internal_date=internal_date or datetime.now(UTC),
        )

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        """Fetch raw messages belonging to a conversation/thread (R1.1, R1.4)."""
        url = f"{self.base_url}/messages?$filter=conversationId eq '{provider_thread_id}'"
        resp = await self._request("GET", url, mailbox_id=str(mailbox.id))
        data = resp.json()

        messages: list[RawMessage] = []
        for msg_meta in data.get("value", []):
            mid = msg_meta.get("id")
            if mid:
                messages.append(await self.get_message(mailbox, mid))

        return RawThread(
            provider_thread_id=provider_thread_id,
            messages=messages,
        )

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Create draft email in Microsoft Graph (R1.1, R1.4, R17.1)."""
        url = f"{self.base_url}/messages"

        body = {
            "subject": reply.subject or "",
            "body": {
                "contentType": "HTML" if reply.body_html else "Text",
                "content": reply.body_html or reply.body_text or "",
            },
            "toRecipients": [
                {"emailAddress": {"address": str(r.email), "name": r.name or str(r.email)}}
                for r in reply.to
            ],
            "ccRecipients": [
                {"emailAddress": {"address": str(r.email), "name": r.name or str(r.email)}}
                for r in reply.cc
            ],
            "conversationId": str(reply.thread_id),
        }

        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        return DraftRef(
            provider_draft_id=data.get("id", ""),
            provider_message_id=data.get("id"),
            provider_thread_id=data.get("conversationId", str(reply.thread_id)),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Send outbound reply email through Microsoft Graph (R1.1, R1.4, R17.4)."""
        url = f"{self.base_url}/sendMail"

        message_obj = {
            "subject": reply.subject or "",
            "body": {
                "contentType": "HTML" if reply.body_html else "Text",
                "content": reply.body_html or reply.body_text or "",
            },
            "toRecipients": [
                {"emailAddress": {"address": str(r.email), "name": r.name or str(r.email)}}
                for r in reply.to
            ],
            "ccRecipients": [
                {"emailAddress": {"address": str(r.email), "name": r.name or str(r.email)}}
                for r in reply.cc
            ],
            "conversationId": str(reply.thread_id),
        }

        body = {
            "message": message_obj,
            "saveToSentItems": True,
        }

        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        sent_id = resp.headers.get("client-request-id", f"msg-sent-{reply.thread_id}")

        return SentRef(
            provider_message_id=sent_id,
            provider_thread_id=str(reply.thread_id),
            sent_at=datetime.now(UTC),
        )


# Alias according to R1.2
MicrosoftGraphProviderAdapter = GraphProviderAdapter

# Register GraphProviderAdapter under provider key 'graph'
register_adapter("graph", GraphProviderAdapter)

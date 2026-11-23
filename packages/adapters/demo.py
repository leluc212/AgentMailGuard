"""Demo mailbox adapter for the demo console (ADR-0018 decision 3, design.md §5.9, R25.1).

The demo console injects an email through ``POST /v1/demo/messages``. The mail-connector hands it
to ``SyncOrchestrator.sync_mailbox`` with an inbound adapter that holds just that email, exactly as
a fetch would, so the archive, the idempotency key, the job row and the publish to
``email.normalize`` stay the connector's own code.

A demo mailbox has its own provider, ``demo`` (migration 0007), because the dispatch-worker picks
the adapter by ``mailbox.provider``: the ``demo`` adapter records an approved reply in the
dispatch-worker's memory and sends nothing, so a demo draft never reaches a real mailbox
(task 8.10). The provider name stays inside ``packages/adapters/`` (AGENTS.md §4); every other
module calls the helpers below.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from typing import TYPE_CHECKING, Any
from uuid import UUID

from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.registry import register_adapter
from packages.domain.entities import Mailbox

if TYPE_CHECKING:
    from packages.db.mailbox import MailboxStore

DEMO_MAILBOX_PROVIDER = "demo"
DEMO_MESSAGE_DOMAIN = "demo.invalid"
"""Domain of every demo Message-ID; ``.invalid`` never resolves (RFC 2606)."""


class DemoMailAdapter(FakeProviderAdapter):
    """The ``demo`` provider: in-memory inbound messages, recorded (never sent) outbound mail."""


_DISPATCH_ADAPTERS: dict[str, DemoMailAdapter] = {}


def demo_dispatch_adapter(mailbox_id: str = "mbx-demo", **_: Any) -> DemoMailAdapter:
    """Registry factory: one recording adapter per demo mailbox, for the life of the process.

    Dispatch resolves the adapter again on a redelivery and then looks the earlier provider draft
    up (design.md §5.8), so the adapter must outlive one call. It lives in the dispatch-worker's
    memory: a restart forgets the recorded mail, which is all a demo needs.
    """
    adapter = _DISPATCH_ADAPTERS.get(mailbox_id)
    if adapter is None:
        adapter = DemoMailAdapter(mailbox_id=mailbox_id)
        _DISPATCH_ADAPTERS[mailbox_id] = adapter
    return adapter


@dataclass(frozen=True)
class DemoInboundMessage:
    """One email the demo console injects, as raw RFC 5322 bytes."""

    provider_message_id: str
    provider_thread_id: str
    raw_mime: bytes
    received_at: datetime


def inbound_adapter(
    mailbox_id: UUID | str, messages: Sequence[DemoInboundMessage]
) -> DemoMailAdapter:
    """A fresh adapter holding only ``messages``, in order, for one ``sync_mailbox`` call.

    Each message gets a history id of its own, so the demo mailbox's stored cursor (the previous
    demo message) never matches and the sync delivers every message given here.
    """
    adapter = DemoMailAdapter(mailbox_id=str(mailbox_id))
    for message in messages:
        adapter.seed_message(
            provider_message_id=message.provider_message_id,
            provider_thread_id=message.provider_thread_id,
            raw_payload=message.raw_mime,
            internal_date=message.received_at,
            history_id=f"demo-history-{message.provider_message_id}",
        )
    return adapter


DEMO_INJECT_JOB_TYPE = "demo_inject"
"""``job_type`` of the ``mail.demo.inject`` envelope (API -> mail-connector)."""


def inject_payload(
    mailbox_id: UUID | str, messages: Sequence[DemoInboundMessage]
) -> dict[str, Any]:
    """The ``mail.demo.inject`` envelope payload: the demo mailbox and its messages, in order."""
    return {
        "mailbox_id": str(mailbox_id),
        "messages": [
            {
                "provider_message_id": m.provider_message_id,
                "provider_thread_id": m.provider_thread_id,
                "raw_mime_b64": base64.b64encode(m.raw_mime).decode("ascii"),
                "received_at": m.received_at.isoformat(),
            }
            for m in messages
        ],
    }


def messages_from_payload(payload: dict[str, Any]) -> list[DemoInboundMessage]:
    """The messages of a ``mail.demo.inject`` payload.

    Raises:
        ValueError: If the payload holds no message or a malformed one.
    """
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ValueError("demo inject payload holds no message")
    try:
        return [
            DemoInboundMessage(
                provider_message_id=str(item["provider_message_id"]),
                provider_thread_id=str(item["provider_thread_id"]),
                raw_mime=base64.b64decode(item["raw_mime_b64"], validate=True),
                received_at=datetime.fromisoformat(item["received_at"]),
            )
            for item in raw
        ]
    except (KeyError, TypeError, ValueError, binascii.Error) as err:
        raise ValueError(f"malformed demo inject message: {err}") from err


def is_demo_mailbox(mailbox: Mailbox) -> bool:
    """Whether the mailbox is a demo one (the only kind the demo door may inject into)."""
    return str(mailbox.provider).strip().lower() == DEMO_MAILBOX_PROVIDER


async def find_demo_mailbox(store: MailboxStore, organization_id: UUID | str) -> Mailbox | None:
    """The organization's demo mailbox (``make seed`` creates one per tenant), or None."""
    mailboxes, _ = await store.list_mailboxes(
        organization_id, limit=1, provider=DEMO_MAILBOX_PROVIDER
    )
    return mailboxes[0] if mailboxes else None


def demo_seed_mailbox(
    *, mailbox_id: UUID, organization_id: UUID, address: str, display_name: str
) -> dict[str, Any]:
    """A seed row for one tenant's demo mailbox (packages/db/seed.py)."""
    return {
        "id": mailbox_id,
        "organization_id": organization_id,
        "provider": DEMO_MAILBOX_PROVIDER,
        "address": address,
        "display_name": display_name,
        "status": "active",
    }


def demo_message_id(local_part: str) -> str:
    """An RFC 5322 Message-ID (with angle brackets) in the demo domain."""
    return f"<{local_part}@{DEMO_MESSAGE_DOMAIN}>"


def _header_text(value: str) -> str:
    """A header value with every line break or other control character made a space."""
    return "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value)


def build_demo_mime(
    *,
    sender_email: str,
    sender_name: str | None,
    recipient: str,
    subject: str,
    body: str,
    message_id: str,
    date: datetime,
    in_reply_to: str | None = None,
    references: Sequence[str] = (),
) -> bytes:
    """A text/plain UTF-8 email as RFC 5322 bytes; the body is base64 so it arrives exact.

    Header values lose their control characters, so a demo subject can never add a header.
    """
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((_header_text(sender_name or ""), _header_text(sender_email)))
    message["To"] = _header_text(recipient)
    message["Subject"] = _header_text(subject)
    message["Date"] = format_datetime(date)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = " ".join(references)
    message.set_content(body.encode("utf-8"), maintype="text", subtype="plain", cte="base64")
    message.set_param("charset", "utf-8")
    return message.as_bytes()


register_adapter(DEMO_MAILBOX_PROVIDER, demo_dispatch_adapter)

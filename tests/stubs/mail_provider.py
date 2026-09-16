"""Fake mail provider adapter for hermetic, credential-free testing (R24.5).

Implements an in-memory mail provider adapter simulating IMAP, Gmail, and
Microsoft Graph operations without external network connectivity.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass
class FakeStoredMessage:
    """In-memory representation of an email message in the fake provider."""

    provider_message_id: str
    provider_thread_id: str
    sender: str
    recipients: list[str]
    subject: str
    body_text: str
    body_html: str | None = None
    raw_mime: bytes = b""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    flags: list[str] = field(default_factory=list)


class FakeMailProviderAdapter:
    """In-memory MailProviderAdapter test double.

    Requires zero credentials, makes zero network calls, and deterministically
    records all outgoing mail and draft operations.
    """

    def __init__(self, mailbox_id: str = "mbx-test") -> None:
        self.mailbox_id = mailbox_id
        self.is_connected = False
        self._messages: dict[str, FakeStoredMessage] = {}
        self._sent_messages: list[dict[str, Any]] = []
        self._created_drafts: list[dict[str, Any]] = []

    async def connect(self) -> None:
        """Establish in-memory session."""
        self.is_connected = True

    async def disconnect(self) -> None:
        """Close in-memory session."""
        self.is_connected = False

    def add_message(
        self,
        sender: str,
        recipients: list[str],
        subject: str,
        body_text: str,
        provider_message_id: str | None = None,
        provider_thread_id: str | None = None,
        body_html: str | None = None,
        raw_mime: bytes | None = None,
    ) -> FakeStoredMessage:
        """Seed a message into the fake provider mailbox."""
        msg_id = provider_message_id or f"msg-{uuid4().hex[:12]}"
        thread_id = provider_thread_id or f"thread-{uuid4().hex[:12]}"
        default_mime = (
            f"From: {sender}\r\n"
            f"To: {', '.join(recipients)}\r\n"
            f"Subject: {subject}\r\n\r\n"
            f"{body_text}"
        ).encode()

        msg = FakeStoredMessage(
            provider_message_id=msg_id,
            provider_thread_id=thread_id,
            sender=sender,
            recipients=recipients,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            raw_mime=raw_mime or default_mime,
        )
        self._messages[msg_id] = msg
        return msg

    async def sync_messages(
        self,
        checkpoint: str | None = None,
        batch_size: int = 50,
    ) -> AsyncIterator[FakeStoredMessage]:
        """Stream stored messages newer than checkpoint."""
        all_msgs = sorted(self._messages.values(), key=lambda m: m.received_at)
        count = 0
        for msg in all_msgs:
            if count >= batch_size:
                break
            if checkpoint is None or msg.provider_message_id > checkpoint:
                yield msg
                count += 1

    async def fetch_raw_mime(self, provider_message_id: str) -> bytes:
        """Retrieve raw MIME byte payload for a message."""
        msg = self._messages.get(provider_message_id)
        if not msg:
            raise KeyError(f"Message '{provider_message_id}' not found in fake provider.")
        return msg.raw_mime

    async def send_message(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Record outbound message dispatch and return generated provider ID."""
        new_id = f"out-{uuid4().hex[:12]}"
        self._sent_messages.append(
            {
                "provider_message_id": new_id,
                "to": to,
                "subject": subject,
                "body_text": body_text,
                "reply_to_message_id": reply_to_message_id,
                "sent_at": datetime.now(UTC),
            }
        )
        return new_id

    async def create_draft(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Record draft creation in the provider."""
        draft_id = f"draft-{uuid4().hex[:12]}"
        self._created_drafts.append(
            {
                "draft_id": draft_id,
                "to": to,
                "subject": subject,
                "body_text": body_text,
                "reply_to_message_id": reply_to_message_id,
                "created_at": datetime.now(UTC),
            }
        )
        return draft_id

    @property
    def sent_count(self) -> int:
        """Number of messages sent through this fake adapter."""
        return len(self._sent_messages)

    @property
    def draft_count(self) -> int:
        """Number of drafts created through this fake adapter."""
        return len(self._created_drafts)

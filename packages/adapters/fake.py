"""Fake mail provider adapter for offline, fixture-driven testing.

Requirements:
- R1.7: FakeProviderAdapter driven by fixture files, usable in CI with no network access.
- R24.5: Offline, credential-free provider double.
- design.md §5.1: MailProviderAdapter protocol conformance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.adapters.exceptions import NotFound
from packages.adapters.protocol import MailProviderAdapter
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


class FakeProviderAdapter:
    """In-memory, deterministic mail provider adapter implementing MailProviderAdapter."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._messages: dict[str, RawMessage] = {}
        self._threads: dict[str, list[str]] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._drafts: dict[str, OutboundReply] = {}
        self._sent_messages: list[tuple[SentRef, OutboundReply]] = []
        self._draft_counter = 0
        self._sent_counter = 0
        self._sub_counter = 0

    def seed_message(
        self,
        provider_message_id: str,
        provider_thread_id: str | None = None,
        raw_payload: bytes | str = b"",
        internal_date: datetime | None = None,
        history_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RawMessage:
        """Seed a message directly into the in-memory store."""
        thread_id = provider_thread_id or f"th-{provider_message_id}"
        msg = RawMessage(
            provider_message_id=provider_message_id,
            provider_thread_id=thread_id,
            raw_payload=raw_payload,
            internal_date=internal_date or datetime.now(UTC),
            history_id=history_id,
            metadata=metadata or {},
        )
        self._messages[provider_message_id] = msg
        if thread_id not in self._threads:
            self._threads[thread_id] = []
        if provider_message_id not in self._threads[thread_id]:
            self._threads[thread_id].append(provider_message_id)
        return msg

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        """Create a mock webhook subscription for the mailbox."""
        self._sub_counter += 1
        sub_id = f"sub-fake-{self._sub_counter}"
        sub = Subscription(
            mailbox_id=mailbox.id,
            subscription_id=sub_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            provider="fake",
            resource=f"users/{mailbox.address}",
        )
        self._subscriptions[sub_id] = sub
        return sub

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        """Renew an existing subscription with extended expiry."""
        renewed = Subscription(
            mailbox_id=sub.mailbox_id,
            subscription_id=sub.subscription_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            provider=sub.provider,
            resource=sub.resource,
            client_state=sub.client_state,
        )
        self._subscriptions[sub.subscription_id] = renewed
        return renewed

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        """Synchronize stored messages incrementally."""
        messages = list(self._messages.values())
        new_cp = Checkpoint(
            mailbox_id=mailbox.id,
            history_id=str(len(messages)),
            last_sync_at=datetime.now(UTC),
        )
        return SyncResult(
            messages=messages,
            new_checkpoint=new_cp,
            requires_full_resync=False,
            has_more=False,
        )

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        """Retrieve stored raw message or raise NotFound."""
        msg = self._messages.get(provider_message_id)
        if msg is None:
            raise NotFound(
                f"Message '{provider_message_id}' not found.",
                provider="fake",
                mailbox_id=str(mailbox.id),
            )
        return msg

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        """Retrieve all stored raw messages for a thread."""
        msg_ids = self._threads.get(provider_thread_id, [])
        thread_messages = [self._messages[mid] for mid in msg_ids if mid in self._messages]
        return RawThread(
            provider_thread_id=provider_thread_id,
            messages=thread_messages,
        )

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Store draft in-memory and return DraftRef."""
        self._draft_counter += 1
        draft_id = f"draft-fake-{self._draft_counter}"
        self._drafts[draft_id] = reply
        return DraftRef(
            provider_draft_id=draft_id,
            provider_thread_id=str(reply.thread_id),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Record outbound message dispatch and return SentRef."""
        self._sent_counter += 1
        sent_id = f"sent-fake-{self._sent_counter}"
        ref = SentRef(
            provider_message_id=sent_id,
            provider_thread_id=str(reply.thread_id),
            sent_at=datetime.now(UTC),
        )
        self._sent_messages.append((ref, reply))
        return ref


# Register FakeProviderAdapter in the default registry
register_adapter("fake", FakeProviderAdapter)

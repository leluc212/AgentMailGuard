"""Fake mail provider adapter for offline, fixture-driven testing.

Requirements:
- R1.7: FakeProviderAdapter driven by fixture files, usable in CI with no network access.
- R24.5: Offline, credential-free provider double.
- design.md §5.1: MailProviderAdapter protocol conformance and sync algorithm.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
)
from packages.adapters.registry import register_adapter
from packages.domain.entities import (
    Checkpoint,
    DraftRef,
    EmailAddress,
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

    def __init__(self, batch_size: int = 50, mailbox_id: str = "mbx-fake", **kwargs: Any) -> None:
        self.batch_size = batch_size
        self.mailbox_id = mailbox_id
        self.is_connected = False
        self.kwargs = kwargs
        self._messages: dict[str, RawMessage] = {}
        self._message_order: list[str] = []
        self._threads: dict[str, list[str]] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._drafts: dict[str, OutboundReply] = {}
        self._sent_messages: list[tuple[SentRef, OutboundReply]] = []
        self._expired_checkpoints: set[str] = set()
        self._injected_faults: list[dict[str, Any]] = []
        self._draft_counter = 0
        self._sent_counter = 0
        self._sub_counter = 0
        self._history_counter = 0

    @property
    def all_messages(self) -> list[RawMessage]:
        """Return all seeded messages in the adapter."""
        return list(self._messages.values())

    @property
    def sent_count(self) -> int:
        """Count of outbound messages dispatched."""
        return len(self._sent_messages)

    @property
    def draft_count(self) -> int:
        """Count of drafts created."""
        return len(self._drafts)

    async def connect(self) -> None:
        """Simulate establishing connection."""
        self.is_connected = True

    async def disconnect(self) -> None:
        """Simulate closing connection."""
        self.is_connected = False

    def get_seeded_message(self, provider_message_id: str) -> RawMessage | None:
        """Lookup seeded raw message by ID."""
        return self._messages.get(provider_message_id)

    def expire_checkpoint(self, history_id: str) -> None:
        """Mark a checkpoint history_id as expired to simulate stale sync tokens."""
        self._expired_checkpoints.add(history_id)

    def inject_rate_limit(
        self,
        retry_after: float = 30.0,
        calls: int = 1,
        message: str = "Provider rate limit exceeded",
    ) -> None:
        """Inject a RateLimited fault carrying retry_after."""
        self._injected_faults.append(
            {
                "type": "rate_limited",
                "retry_after": retry_after,
                "calls": calls,
                "message": message,
            }
        )

    def inject_auth_expired(
        self,
        calls: int = 1,
        message: str = "Credentials expired or revoked",
    ) -> None:
        """Inject an AuthExpired fault."""
        self._injected_faults.append({"type": "auth_expired", "calls": calls, "message": message})

    def inject_transient_failure(
        self,
        message: str = "Temporary network error",
        calls: int = 1,
    ) -> None:
        """Inject a Transient fault."""
        self._injected_faults.append({"type": "transient", "calls": calls, "message": message})

    def inject_permanent_failure(
        self,
        message: str = "Fatal request format error",
        calls: int = 1,
    ) -> None:
        """Inject a Permanent fault."""
        self._injected_faults.append({"type": "permanent", "calls": calls, "message": message})

    def inject_not_found(
        self,
        message: str = "Resource not found",
        calls: int = 1,
    ) -> None:
        """Inject a NotFound fault."""
        self._injected_faults.append({"type": "not_found", "calls": calls, "message": message})

    def clear_injected_faults(self) -> None:
        """Clear all active failure injections."""
        self._injected_faults.clear()

    def _maybe_raise_fault(self, method_name: str, mailbox: Mailbox | None = None) -> None:
        """Inspect and execute active fault injections."""
        if not self._injected_faults:
            return

        fault = self._injected_faults[0]
        fault["calls"] -= 1
        if fault["calls"] <= 0:
            self._injected_faults.pop(0)

        mbx_id = str(mailbox.id) if mailbox else None
        ftype = fault["type"]
        msg = fault["message"]

        if ftype == "rate_limited":
            raise RateLimited(
                msg,
                retry_after=fault.get("retry_after"),
                provider="fake",
                mailbox_id=mbx_id,
            )
        if ftype == "auth_expired":
            raise AuthExpired(msg, provider="fake", mailbox_id=mbx_id)
        if ftype == "transient":
            raise Transient(msg, provider="fake", mailbox_id=mbx_id)
        if ftype == "permanent":
            raise Permanent(msg, provider="fake", mailbox_id=mbx_id)
        if ftype == "not_found":
            raise NotFound(msg, provider="fake", mailbox_id=mbx_id)
        raise ProviderError(msg, provider="fake", mailbox_id=mbx_id)

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
        self._history_counter += 1
        hid = history_id or f"hist-{self._history_counter:06d}"

        msg = RawMessage(
            provider_message_id=provider_message_id,
            provider_thread_id=thread_id,
            raw_payload=raw_payload,
            internal_date=internal_date or datetime.now(UTC),
            history_id=hid,
            metadata=metadata or {},
        )
        self._messages[provider_message_id] = msg
        if provider_message_id not in self._message_order:
            self._message_order.append(provider_message_id)

        if thread_id not in self._threads:
            self._threads[thread_id] = []
        if provider_message_id not in self._threads[thread_id]:
            self._threads[thread_id].append(provider_message_id)
        return msg

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
    ) -> RawMessage:
        """Convenience method for creating and seeding a message."""
        msg_id = provider_message_id or f"msg-{self._history_counter + 1:04d}"
        thread_id = provider_thread_id or f"th-{msg_id}"
        raw = raw_mime
        if raw is None:
            raw = (
                f"From: {sender}\r\n"
                f"To: {', '.join(recipients)}\r\n"
                f"Subject: {subject}\r\n\r\n"
                f"{body_text}"
            ).encode()
        return self.seed_message(
            provider_message_id=msg_id,
            provider_thread_id=thread_id,
            raw_payload=raw,
            metadata={
                "sender": sender,
                "recipients": recipients,
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
            },
        )

    async def fetch_raw_mime(self, provider_message_id: str) -> bytes:
        """Retrieve raw MIME byte payload for a message."""
        msg = self._messages.get(provider_message_id)
        if msg is None:
            raise KeyError(f"Message '{provider_message_id}' not found.")
        if isinstance(msg.raw_payload, str):
            return msg.raw_payload.encode("utf-8")
        return msg.raw_payload

    async def send_message(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Simplified string-based send helper for test doubles."""
        new_id = f"out-{uuid4().hex[:12]}"
        reply = OutboundReply(
            thread_id="th-default",
            mailbox_id=self.mailbox_id,
            organization_id="org-default",
            to=[EmailAddress(email=addr) for addr in to],
            subject=subject,
            body_text=body_text,
            in_reply_to=reply_to_message_id,
        )
        ref = SentRef(
            provider_message_id=new_id,
            provider_thread_id="th-default",
            sent_at=datetime.now(UTC),
        )
        self._sent_messages.append((ref, reply))
        return new_id

    async def sync_messages(
        self,
        checkpoint: str | None = None,
        batch_size: int = 50,
    ) -> AsyncIterator[RawMessage]:
        """Stream stored messages newer than checkpoint."""
        count = 0
        for mid in self._message_order:
            if count >= batch_size:
                break
            msg = self._messages[mid]
            if checkpoint is None or msg.provider_message_id > checkpoint:
                yield msg
                count += 1

    def load_fixtures_from_dict(self, fixtures: list[dict[str, Any]]) -> int:
        """Load email fixtures from a list of dictionaries (R1.7)."""
        count = 0
        for item in fixtures:
            msg_id = item["provider_message_id"]
            thread_id = item.get("provider_thread_id") or f"th-{msg_id}"
            sender = item.get("sender", "sender@example.com")
            recipients = item.get("recipients", ["recipient@example.com"])
            subject = item.get("subject", "No subject")
            body_text = item.get("body_text", "")

            raw = item.get("raw_payload")
            if raw is None:
                raw = (
                    f"From: {sender}\r\n"
                    f"To: {', '.join(recipients)}\r\n"
                    f"Subject: {subject}\r\n\r\n"
                    f"{body_text}"
                ).encode()
            elif isinstance(raw, str):
                raw = raw.encode("utf-8")

            date_val = None
            if "received_at" in item:
                date_val = datetime.fromisoformat(item["received_at"])

            self.seed_message(
                provider_message_id=msg_id,
                provider_thread_id=thread_id,
                raw_payload=raw,
                internal_date=date_val,
                history_id=item.get("history_id"),
                metadata=item.get("metadata"),
            )
            count += 1
        return count

    def load_fixtures_from_json(self, file_path: Path | str) -> int:
        """Load email fixtures from a JSON file (R1.7)."""
        path = Path(file_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Expected list of fixtures in JSON file {path}")
        return self.load_fixtures_from_dict(data)

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        """Create a mock webhook subscription for the mailbox."""
        self._maybe_raise_fault("subscribe", mailbox)
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
        self._maybe_raise_fault("renew_subscription")
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
        """Synchronize stored messages incrementally with window pagination."""
        self._maybe_raise_fault("synchronize", mailbox)

        cursor = cp.history_id
        # Detect expired checkpoint triggering full resync
        if cursor and cursor in self._expired_checkpoints:
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

        start_idx = 0
        if cursor:
            for idx, mid in enumerate(self._message_order):
                msg = self._messages[mid]
                if msg.history_id == cursor or mid == cursor:
                    start_idx = idx + 1
                    break

        remaining = [self._messages[mid] for mid in self._message_order[start_idx:]]
        batch = remaining[: self.batch_size]
        has_more = len(remaining) > self.batch_size

        new_cursor = cursor
        if batch:
            new_cursor = batch[-1].history_id or batch[-1].provider_message_id

        new_cp = Checkpoint(
            mailbox_id=mailbox.id,
            history_id=new_cursor,
            sync_state="idle",
            last_sync_at=datetime.now(UTC),
            pending_followup=False,
        )
        return SyncResult(
            messages=batch,
            new_checkpoint=new_cp,
            requires_full_resync=False,
            has_more=has_more,
        )

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        """Retrieve stored raw message or raise NotFound."""
        self._maybe_raise_fault("get_message", mailbox)
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
        self._maybe_raise_fault("get_thread", mailbox)
        msg_ids = self._threads.get(provider_thread_id, [])
        thread_messages = [self._messages[mid] for mid in msg_ids if mid in self._messages]
        return RawThread(
            provider_thread_id=provider_thread_id,
            messages=thread_messages,
        )

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Store draft in-memory and return DraftRef."""
        self._maybe_raise_fault("create_draft", mailbox)
        self._draft_counter += 1
        draft_id = f"draft-fake-{self._draft_counter}"
        self._drafts[draft_id] = reply
        return DraftRef(
            provider_draft_id=draft_id,
            provider_thread_id=str(reply.thread_id),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Record outbound message dispatch and return SentRef."""
        self._maybe_raise_fault("send_reply", mailbox)
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

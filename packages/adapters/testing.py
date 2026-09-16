"""Reusable contract test suite for MailProviderAdapter implementations.

Requirements:
- Task 1.1: Shared contract test suite every adapter must pass.
- R1.1, R1.4, R1.5, R1.6: Interface contract and error translation guarantees.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pytest

from packages.adapters.protocol import MailProviderAdapter
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


class MailProviderAdapterContractSuite(ABC):
    """Abstract contract test suite for MailProviderAdapter implementations.

    Any adapter (FakeProviderAdapter, GmailProviderAdapter, etc.)
    must inherit from this suite and implement `create_adapter()`.
    """

    @abstractmethod
    def create_adapter(self) -> MailProviderAdapter:
        """Create and return a configured instance of the adapter under test."""
        raise NotImplementedError

    def make_test_mailbox(self, provider: str = "test") -> Mailbox:
        """Create a default test mailbox fixture."""
        return Mailbox(
            id="mbx-contract-01",
            organization_id="org-contract-01",
            provider=provider,
            address="test.mailbox@example.com",
            display_name="Test Mailbox",
        )

    @pytest.mark.asyncio
    async def test_satisfies_protocol(self) -> None:
        """Assert the adapter conforms to the MailProviderAdapter protocol."""
        adapter = self.create_adapter()
        assert isinstance(adapter, MailProviderAdapter)

    @pytest.mark.asyncio
    async def test_subscribe_returns_subscription(self) -> None:
        """Assert subscribe() returns a valid Subscription domain object."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        sub = await adapter.subscribe(mailbox)
        assert isinstance(sub, Subscription)
        assert sub.mailbox_id == mailbox.id
        assert bool(sub.subscription_id)
        assert isinstance(sub.expires_at, datetime)

    @pytest.mark.asyncio
    async def test_renew_subscription_returns_subscription(self) -> None:
        """Assert renew_subscription() returns an active Subscription."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        sub = await adapter.subscribe(mailbox)
        renewed = await adapter.renew_subscription(sub)
        assert isinstance(renewed, Subscription)
        assert renewed.mailbox_id == sub.mailbox_id

    @pytest.mark.asyncio
    async def test_synchronize_returns_sync_result(self) -> None:
        """Assert synchronize() returns a valid SyncResult domain object."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        cp = Checkpoint(mailbox_id=mailbox.id)
        result = await adapter.synchronize(mailbox, cp)
        assert isinstance(result, SyncResult)
        assert isinstance(result.messages, list)
        assert isinstance(result.new_checkpoint, Checkpoint)
        assert isinstance(result.requires_full_resync, bool)
        assert isinstance(result.has_more, bool)

    @pytest.mark.asyncio
    async def test_get_message_returns_raw_message(self) -> None:
        """Assert get_message() returns a valid RawMessage domain object."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        msg = await adapter.get_message(mailbox, "msg-001")
        assert isinstance(msg, RawMessage)
        assert msg.provider_message_id == "msg-001"
        assert msg.raw_payload is not None

    @pytest.mark.asyncio
    async def test_get_thread_returns_raw_thread(self) -> None:
        """Assert get_thread() returns a RawThread containing messages."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        thread = await adapter.get_thread(mailbox, "th-001")
        assert isinstance(thread, RawThread)
        assert thread.provider_thread_id == "th-001"
        assert isinstance(thread.messages, list)

    @pytest.mark.asyncio
    async def test_create_draft_returns_draft_ref(self) -> None:
        """Assert create_draft() returns DraftRef without sending message."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = OutboundReply(
            thread_id="th-001",
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            to=[EmailAddress(email="recipient@example.com")],
            body_text="Draft message body",
            subject="Re: Test subject",
        )
        draft = await adapter.create_draft(mailbox, reply)
        assert isinstance(draft, DraftRef)
        assert bool(draft.provider_draft_id)

    @pytest.mark.asyncio
    async def test_send_reply_returns_sent_ref(self) -> None:
        """Assert send_reply() returns SentRef with message ID and timestamp."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = OutboundReply(
            thread_id="th-001",
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            to=[EmailAddress(email="recipient@example.com")],
            body_text="Sent message body",
            subject="Re: Test subject",
        )
        sent = await adapter.send_reply(mailbox, reply)
        assert isinstance(sent, SentRef)
        assert bool(sent.provider_message_id)
        assert isinstance(sent.sent_at, datetime)

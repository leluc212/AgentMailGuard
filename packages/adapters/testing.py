"""Reusable contract test suite for MailProviderAdapter implementations.

Requirements:
- Task 1.1: Shared contract test suite every adapter must pass.
- R1.1, R1.4, R1.5, R1.6: Interface contract and error translation guarantees.
- R17.1, R17.3 (task 6.3a): draft ids, send_draft, get_draft_status, find_sent_message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pytest

from packages.adapters.protocol import MailProviderAdapter
from packages.domain import ProviderDraftStatus
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

    UNKNOWN_DRAFT_ID = "draft-does-not-exist"

    def make_test_reply(self, mailbox: Mailbox) -> OutboundReply:
        """A reply to seeded message msg-001 in thread th-001 (all adapters accept it)."""
        return OutboundReply(
            thread_id="th-001",
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            to=[EmailAddress(email="recipient@example.com")],
            body_text="Contract reply body",
            subject="Re: Test subject",
            in_reply_to="<orig-001@example.com>",
            references=["<orig-001@example.com>"],
            message_id="<contract-reply-001@example.com>",
            reply_to_provider_message_id="msg-001",
        )

    async def _find_sent(
        self,
        adapter: MailProviderAdapter,
        mailbox: Mailbox,
        reply: OutboundReply,
        draft: DraftRef,
    ) -> SentRef | None:
        """The dispatch lookup rule: stored draft message id first, then our Message-ID."""
        found = await adapter.find_sent_message(mailbox, "th-001", draft.provider_message_id or "")
        if found is None and reply.message_id:
            found = await adapter.find_sent_message(mailbox, "th-001", reply.message_id)
        return found

    @pytest.mark.asyncio
    async def test_create_draft_returns_draft_ref(self) -> None:
        """Assert create_draft() returns the draft id AND the draft's message id (R17.1)."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        assert isinstance(draft, DraftRef)
        assert bool(draft.provider_draft_id)
        assert bool(draft.provider_message_id)

    @pytest.mark.asyncio
    async def test_send_reply_returns_sent_ref(self) -> None:
        """Assert send_reply() returns SentRef with message ID and timestamp."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        sent = await adapter.send_reply(mailbox, self.make_test_reply(mailbox))
        assert isinstance(sent, SentRef)
        assert bool(sent.provider_message_id)
        assert isinstance(sent.sent_at, datetime)

    @pytest.mark.asyncio
    async def test_send_draft_returns_sent_ref(self) -> None:
        """6.3a: send_draft() sends an existing draft and returns the sent message ref."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        sent = await adapter.send_draft(mailbox, draft.provider_draft_id)
        assert isinstance(sent, SentRef)
        assert bool(sent.provider_message_id)
        assert isinstance(sent.sent_at, datetime)

    @pytest.mark.asyncio
    async def test_get_draft_status_of_new_draft_is_draft(self) -> None:
        """6.3a: an unsent draft reports DRAFT."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        status = await adapter.get_draft_status(mailbox, draft.provider_draft_id)
        assert status is ProviderDraftStatus.DRAFT

    @pytest.mark.asyncio
    async def test_get_draft_status_after_send_is_sent_or_missing(self) -> None:
        """6.3a: after send a draft is SENT (Graph-like) or MISSING (Gmail deletes it)."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        await adapter.send_draft(mailbox, draft.provider_draft_id)
        status = await adapter.get_draft_status(mailbox, draft.provider_draft_id)
        assert status in (ProviderDraftStatus.SENT, ProviderDraftStatus.MISSING)

    @pytest.mark.asyncio
    async def test_get_draft_status_of_unknown_draft_is_missing(self) -> None:
        """6.3a: a draft id the provider does not know reports MISSING, not an error."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        status = await adapter.get_draft_status(mailbox, self.UNKNOWN_DRAFT_ID)
        assert status is ProviderDraftStatus.MISSING

    @pytest.mark.asyncio
    async def test_find_sent_message_locates_sent_draft(self) -> None:
        """6.3a / design §5.8 step 4: after send, the sent copy is found in the thread."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = self.make_test_reply(mailbox)
        draft = await adapter.create_draft(mailbox, reply)
        sent = await adapter.send_draft(mailbox, draft.provider_draft_id)
        found = await self._find_sent(adapter, mailbox, reply, draft)
        assert found is not None
        assert found.provider_message_id == sent.provider_message_id

    @pytest.mark.asyncio
    async def test_find_sent_message_is_none_before_send(self) -> None:
        """6.3a: an unsent draft is never reported as sent (no false 'already sent')."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = self.make_test_reply(mailbox)
        draft = await adapter.create_draft(mailbox, reply)
        assert await self._find_sent(adapter, mailbox, reply, draft) is None

    @pytest.mark.asyncio
    async def test_find_draft_locates_the_unsent_draft_by_message_id(self) -> None:
        """6.5: a redelivery adopts the draft an earlier delivery created but never recorded."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = self.make_test_reply(mailbox)
        draft = await adapter.create_draft(mailbox, reply)
        assert reply.message_id is not None
        found = await adapter.find_draft(
            mailbox, draft.provider_thread_id or "th-001", reply.message_id
        )
        assert found is not None
        assert found.provider_draft_id == draft.provider_draft_id

    @pytest.mark.asyncio
    async def test_find_draft_is_none_for_an_unknown_message_id(self) -> None:
        """6.5: no draft carries this Message-ID, so dispatch creates one."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        found = await adapter.find_draft(mailbox, "th-001", "<never-created-001@example.com>")
        assert found is None

"""Unit tests proving the shared MailProviderAdapter contract test suite.

Requirements:
- Task 1.1: Shared contract test suite every adapter must pass.
- R1.1, R1.4, R1.5, R1.6: Interface contract and return objects.
"""

from datetime import UTC, datetime, timedelta

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.testing import MailProviderAdapterContractSuite
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


class ConformingMockAdapter:
    """Minimal conforming adapter implementation for contract suite verification."""

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        return Subscription(
            mailbox_id=mailbox.id,
            subscription_id="sub-mock-1",
            expires_at=datetime.now(UTC) + timedelta(days=7),
            provider=mailbox.provider,
        )

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        return Subscription(
            mailbox_id=sub.mailbox_id,
            subscription_id=sub.subscription_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            provider=sub.provider,
        )

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        return SyncResult(
            messages=[
                RawMessage(
                    provider_message_id="msg-sync-1",
                    raw_payload=b"MIME data",
                    internal_date=datetime.now(UTC),
                )
            ],
            new_checkpoint=Checkpoint(
                mailbox_id=mailbox.id,
                history_id="hist-999",
                last_sync_at=datetime.now(UTC),
            ),
            requires_full_resync=False,
            has_more=False,
        )

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        return RawMessage(
            provider_message_id=provider_message_id,
            raw_payload=b"Content-Type: text/plain\r\n\r\nHello World",
            internal_date=datetime.now(UTC),
        )

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        msg = await self.get_message(mailbox, "msg-001")
        return RawThread(
            provider_thread_id=provider_thread_id,
            messages=[msg],
        )

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        return DraftRef(
            provider_draft_id="draft-001",
            provider_thread_id=str(reply.thread_id),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        return SentRef(
            provider_message_id="sent-001",
            provider_thread_id=str(reply.thread_id),
            sent_at=datetime.now(UTC),
        )


class TestConformingAdapterContract(MailProviderAdapterContractSuite):
    """Subclass the contract suite using ConformingMockAdapter to prove the suite passes."""

    def create_adapter(self) -> MailProviderAdapter:
        return ConformingMockAdapter()


def test_non_conforming_class_fails_protocol_check() -> None:
    """Verify that a class missing required adapter methods fails isinstance check."""

    class IncompleteAdapter:
        async def subscribe(self, mailbox: Mailbox) -> Subscription:
            raise NotImplementedError

    assert not isinstance(IncompleteAdapter(), MailProviderAdapter)


def test_error_taxonomy_catchable_as_provider_error() -> None:
    """Verify all taxonomy errors are catchable via ProviderError base class."""
    errors = [
        RateLimited("429 Too Many Requests", retry_after=15.0),
        AuthExpired("OAuth token expired"),
        NotFound("Resource not found"),
        Transient("Gateway timeout"),
        Permanent("Bad request format"),
    ]

    for err in errors:
        try:
            raise err
        except ProviderError as caught:
            assert isinstance(caught, ProviderError)

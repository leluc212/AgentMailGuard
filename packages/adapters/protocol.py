"""Mail provider adapter protocol interface.

Requirements:
- R1.1: Single MailProviderAdapter interface exposing subscribe(),
  renew_subscription(), synchronize(), get_message(), get_thread(),
  create_draft(), send_reply(), plus send_draft(), get_draft_status() and
  find_sent_message() (and find_draft() for the orphan-draft lookup).
- R17.1, R17.3 (task 6.3a): draft send, status and sent-message lookup for exactly-once dispatch.
- R1.4: Return provider-neutral domain objects and never raw provider payloads.
- design.md §5.1: MailProviderAdapter protocol contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class MailProviderAdapter(Protocol):
    """Protocol defining the interface for all email provider adapters (R1.1, R1.4).

    Confined strictly within packages/adapters/. No implementation details or
    provider SDKs should leak beyond adapter boundaries.

    Errors: retryable failures raise RetryableProviderError subclasses (RateLimited,
    Transient) with `retry_after_s`; permanent ones raise PermanentProviderError
    subclasses (AuthExpired, NotFound, Permanent).
    """

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        """Create or initialize a webhook/push notification subscription."""
        ...

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        """Renew an existing subscription before expiration."""
        ...

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        """Fetch incremental change batch since the provided checkpoint."""
        ...

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        """Retrieve full raw message payload by provider message ID."""
        ...

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        """Retrieve all raw messages in a provider thread."""
        ...

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Create a reply draft; the DraftRef carries the draft id and its message id."""
        ...

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Dispatch an outbound reply through the provider mailbox."""
        ...

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        """Send an existing provider draft (design.md §5.8 step 3)."""
        ...

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        """Report DRAFT, SENT or MISSING for a draft (design.md §5.8 step 4)."""
        ...

    async def find_sent_message(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        provider_message_id: str,
    ) -> SentRef | None:
        """Find a sent message in the thread by provider id or RFC 5322 Message-ID."""
        ...

    async def find_draft(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        message_id: str,
    ) -> DraftRef | None:
        """Find an unsent draft in the thread carrying this RFC 5322 Message-ID (tasks.md 6.5).

        Dispatch adopts it after a crash between create_draft and recording the handle, so
        a redelivery never leaves a second provider draft.
        """
        ...

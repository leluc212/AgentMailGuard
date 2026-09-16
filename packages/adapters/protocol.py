"""Mail provider adapter protocol interface.

Requirements:
- R1.1: Single MailProviderAdapter interface exposing subscribe(),
  renew_subscription(), synchronize(), get_message(), get_thread(),
  create_draft(), send_reply().
- R1.4: Return provider-neutral domain objects and never raw provider payloads.
- design.md §5.1: MailProviderAdapter protocol contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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
        """Create an email draft in the provider mailbox."""
        ...

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Dispatch an outbound reply through the provider mailbox."""
        ...

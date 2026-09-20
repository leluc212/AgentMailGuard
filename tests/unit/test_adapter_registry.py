"""Unit tests for MailProviderAdapter protocol and provider registry.

Requirements:
- R1.1: Single MailProviderAdapter interface exposing the 7 methods.
- R1.3: Adapter registry keyed by mailbox.provider.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.adapters.exceptions import NotFound
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import (
    clear_registry,
    get_adapter,
    get_adapter_for_mailbox,
    is_provider_registered,
    list_registered_providers,
    register_adapter,
    register_default_adapters,
)
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


class ConformingDummyAdapter:
    """Dummy adapter implementing the full MailProviderAdapter protocol."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        return Subscription(
            mailbox_id=mailbox.id,
            subscription_id="sub-1",
            expires_at=datetime.now(UTC),
            provider=mailbox.provider,
        )

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        return sub

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        return SyncResult(
            messages=[],
            new_checkpoint=cp,
            requires_full_resync=False,
            has_more=False,
        )

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        return RawMessage(provider_message_id=provider_message_id, raw_payload=b"dummy")

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        return RawThread(provider_thread_id=provider_thread_id, messages=[])

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        return DraftRef(provider_draft_id="draft-1")

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        return SentRef(provider_message_id="sent-1")


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()
    register_default_adapters()


def test_protocol_runtime_checkable() -> None:
    """Verify ConformingDummyAdapter satisfies MailProviderAdapter runtime check."""
    adapter = ConformingDummyAdapter()
    assert isinstance(adapter, MailProviderAdapter)


def test_registry_register_and_get() -> None:
    """Verify registering an adapter factory and retrieving it (R1.3)."""
    register_adapter("dummy", ConformingDummyAdapter)

    assert is_provider_registered("dummy")
    assert "dummy" in list_registered_providers()

    adapter = get_adapter("dummy", extra_param=123)
    assert isinstance(adapter, ConformingDummyAdapter)
    assert adapter.kwargs == {"extra_param": 123}


def test_registry_get_for_mailbox() -> None:
    """Verify resolving adapter via Mailbox object (R1.3)."""
    register_adapter("test_prov", ConformingDummyAdapter)

    mbx = Mailbox(
        id="mbx-001",
        organization_id="org-001",
        provider="test_prov",
        address="test@example.com",
    )
    adapter = get_adapter_for_mailbox(mbx)
    assert isinstance(adapter, ConformingDummyAdapter)


def test_registry_unregistered_raises_not_found() -> None:
    """Verify querying an unregistered provider raises NotFound."""
    with pytest.raises(NotFound) as exc_info:
        get_adapter("unknown_provider")
    assert "unknown_provider" in str(exc_info.value)


def test_registry_case_insensitivity() -> None:
    """Verify registry normalizes provider names."""
    register_adapter("MyProvider", ConformingDummyAdapter)
    assert is_provider_registered("myprovider")
    adapter = get_adapter("MYPROVIDER")
    assert isinstance(adapter, ConformingDummyAdapter)

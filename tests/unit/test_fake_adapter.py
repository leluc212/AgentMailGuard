"""Unit and contract tests for FakeProviderAdapter.

Requirements:
- R1.7: FakeProviderAdapter driven by fixture files, usable in CI with no network access.
- R24.5: Offline, credential-free provider double.
"""

from pathlib import Path

import pytest

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    RateLimited,
    Transient,
)
from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter
from packages.adapters.testing import MailProviderAdapterContractSuite
from packages.domain.entities import Checkpoint, Mailbox


class TestFakeProviderAdapterContract(MailProviderAdapterContractSuite):
    """Verify FakeProviderAdapter satisfies all 8 shared adapter contract tests."""

    def create_adapter(self) -> MailProviderAdapter:
        adapter = FakeProviderAdapter()
        # Seed default test data required by contract assertions
        adapter.seed_message(
            provider_message_id="msg-001",
            provider_thread_id="th-001",
            raw_payload=b"From: test@example.com\r\nSubject: Test\r\n\r\nHello",
        )
        return adapter


def test_fake_adapter_registered() -> None:
    """Verify FakeProviderAdapter is auto-registered under 'fake'."""
    adapter = get_adapter("fake")
    assert isinstance(adapter, FakeProviderAdapter)


def test_fixture_loading_from_json() -> None:
    """Verify loading fixtures from JSON file into FakeProviderAdapter."""
    adapter = FakeProviderAdapter()
    fixture_path = Path("tests/fixtures/mail/sample_messages.json")
    loaded_count = adapter.load_fixtures_from_json(fixture_path)
    assert loaded_count == 3
    assert len(adapter.all_messages) == 3

    msg = adapter.get_seeded_message("fixture-msg-001")
    assert msg is not None
    assert msg.provider_thread_id == "fixture-th-001"
    assert b"Inquiry about delivery date" in msg.raw_payload


@pytest.mark.asyncio
async def test_sync_windows_pagination() -> None:
    """Verify incremental sync pagination, batch_size limits, and has_more flag."""
    adapter = FakeProviderAdapter(batch_size=2)
    for i in range(1, 6):
        adapter.seed_message(f"page-msg-{i:03d}", raw_payload=f"Payload {i}".encode())

    mbx = Mailbox(id="mbx-1", organization_id="org-1", provider="fake", address="test@example.com")
    cp = Checkpoint(mailbox_id=mbx.id)

    # Page 1: messages 1 and 2
    res1 = await adapter.synchronize(mbx, cp)
    assert len(res1.messages) == 2
    assert res1.has_more is True
    assert [m.provider_message_id for m in res1.messages] == ["page-msg-001", "page-msg-002"]

    # Page 2: messages 3 and 4
    res2 = await adapter.synchronize(mbx, res1.new_checkpoint)
    assert len(res2.messages) == 2
    assert res2.has_more is True
    assert [m.provider_message_id for m in res2.messages] == ["page-msg-003", "page-msg-004"]

    # Page 3: message 5 (final page)
    res3 = await adapter.synchronize(mbx, res2.new_checkpoint)
    assert len(res3.messages) == 1
    assert res3.has_more is False
    assert [m.provider_message_id for m in res3.messages] == ["page-msg-005"]

    # Page 4: no new messages
    res4 = await adapter.synchronize(mbx, res3.new_checkpoint)
    assert len(res4.messages) == 0
    assert res4.has_more is False


@pytest.mark.asyncio
async def test_expired_checkpoint_triggers_full_resync() -> None:
    """Verify expired checkpoint triggers requires_full_resync=True (design.md §5.1)."""
    adapter = FakeProviderAdapter()
    adapter.seed_message("msg-1")
    mbx = Mailbox(id="mbx-1", organization_id="org-1", provider="fake", address="test@example.com")

    # Expire checkpoint 'old-hist-123'
    adapter.expire_checkpoint("old-hist-123")

    expired_cp = Checkpoint(mailbox_id=mbx.id, history_id="old-hist-123")
    res = await adapter.synchronize(mbx, expired_cp)

    assert res.requires_full_resync is True
    assert res.has_more is False
    assert len(res.messages) == 0


@pytest.mark.asyncio
async def test_failure_injection_rate_limited_with_retry_after() -> None:
    """Verify injected RateLimited error carries retry_after and auto-recovers (R1.5, R1.6)."""
    adapter = FakeProviderAdapter()
    adapter.seed_message("msg-1")
    mbx = Mailbox(id="mbx-1", organization_id="org-1", provider="fake", address="test@example.com")

    # Inject RateLimited for 1 call with retry_after=45.0
    adapter.inject_rate_limit(retry_after=45.0, calls=1)

    # 1st call: raises RateLimited
    with pytest.raises(RateLimited) as exc_info:
        await adapter.get_message(mbx, "msg-1")
    assert exc_info.value.retry_after == 45.0
    assert exc_info.value.provider == "fake"

    # 2nd call: succeeds because calls counter decremented to 0
    msg = await adapter.get_message(mbx, "msg-1")
    assert msg.provider_message_id == "msg-1"


@pytest.mark.asyncio
async def test_failure_injection_transient_and_auth_expired() -> None:
    """Verify Transient and AuthExpired fault injection."""
    adapter = FakeProviderAdapter()
    mbx = Mailbox(id="mbx-1", organization_id="org-1", provider="fake", address="test@example.com")

    # Inject Transient
    adapter.inject_transient_failure("Network timeout", calls=1)
    with pytest.raises(Transient) as exc_info:
        await adapter.subscribe(mbx)
    assert "Network timeout" in str(exc_info.value)

    # Subsequent call succeeds
    sub = await adapter.subscribe(mbx)
    assert sub.subscription_id.startswith("sub-fake-")

    # Inject AuthExpired
    adapter.inject_auth_expired(calls=1)
    with pytest.raises(AuthExpired):
        await adapter.renew_subscription(sub)


@pytest.mark.asyncio
async def test_failure_injection_permanent_not_found_and_clear() -> None:
    """Verify Permanent, NotFound injection and clearing faults."""
    adapter = FakeProviderAdapter()
    mbx = Mailbox(id="mbx-1", organization_id="org-1", provider="fake", address="test@example.com")

    # Permanent fault
    adapter.inject_permanent_failure("Unrecoverable error", calls=1)
    with pytest.raises(Permanent):
        await adapter.get_thread(mbx, "th-1")

    # NotFound fault
    adapter.inject_not_found("Resource missing", calls=1)
    with pytest.raises(NotFound):
        await adapter.get_thread(mbx, "th-1")

    # Inject and then clear
    adapter.inject_transient_failure("Will be cleared", calls=5)
    adapter.clear_injected_faults()
    th = await adapter.get_thread(mbx, "th-1")
    assert th.provider_thread_id == "th-1"

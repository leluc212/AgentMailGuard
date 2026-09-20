"""Unit tests for mailbox sync orchestrator and checkpoint store.

Requirements:
- R2.4: Checkpoint persistence (history_id, delta_link, last_sync_at, sync_state).
- R2.7: Bounded full synchronization on expired/rejected checkpoint (sync_state='full_resync').
- R2.8: Checkpoint advanced ONLY AFTER all fetched messages are durably persisted & published.
- R2.9: In-flight sync coalescing (concurrent notifications collapse to one pending follow-up).
- R1.5: AuthExpired updates mailbox status to needs_reauth and halts without spinning.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from packages.adapters.fake import FakeProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.core.storage import FakeObjectStorageClient, ObjectKeyBuilder
from packages.db.checkpoint import InMemoryCheckpointStore
from packages.db.mailbox import InMemoryMailboxStore
from packages.domain.entities import Checkpoint, Mailbox
from services.mail_connector.orchestrator import SyncOrchestrator


class MockPublisher:
    """Mock AMQP publisher recording published envelopes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, JobEnvelope]] = []
        self.should_fail = False

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        if self.should_fail:
            raise RuntimeError("Broker connection lost during publish")
        self.published.append((exchange_name, routing_key, envelope))


@pytest.fixture
def test_mailbox() -> Mailbox:
    return Mailbox(
        id=uuid4(),
        organization_id=uuid4(),
        provider="fake",
        address="user@example.com",
        display_name="Test User",
        status="active",
    )


@pytest.fixture
def fake_adapter() -> FakeProviderAdapter:
    adapter = FakeProviderAdapter()
    adapter.seed_message(
        provider_message_id="msg-001",
        raw_payload=b"From: a@b.com\n\nMsg 1",
        history_id="h-001",
    )
    adapter.seed_message(
        provider_message_id="msg-002",
        raw_payload=b"From: c@d.com\n\nMsg 2",
        history_id="h-002",
    )
    adapter.seed_message(
        provider_message_id="msg-003",
        raw_payload=b"From: e@f.com\n\nMsg 3",
        history_id="h-003",
    )
    return adapter


@pytest.mark.asyncio
async def test_orchestrator_initial_sync(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    """Verify initial sync stores blobs, publishes jobs, and saves checkpoint (R2.4, R2.8)."""
    cp_store = InMemoryCheckpointStore()
    storage = FakeObjectStorageClient()
    publisher = MockPublisher()
    mailbox_store = InMemoryMailboxStore([test_mailbox])

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
        mailbox_store=mailbox_store,
    )

    outcome = await orchestrator.sync_mailbox(test_mailbox)

    assert not outcome.coalesced
    assert outcome.messages_synced == 3
    assert outcome.status == "success"

    # Verify object storage contains all 3 raw MIME blobs
    for mid in ["msg-001", "msg-002", "msg-003"]:
        key = ObjectKeyBuilder.raw_mime(test_mailbox.organization_id, test_mailbox.id, mid)
        assert await storage.object_exists("raw-mime", key)

    # Verify RabbitMQ published 3 normalize_email envelopes
    assert len(publisher.published) == 3
    for _exchange, rk, env in publisher.published:
        assert rk == "email.normalize"
        assert env.job_type == "normalize_email"
        assert env.organization_id == str(test_mailbox.organization_id)
        assert env.mailbox_id == str(test_mailbox.id)
        assert "raw_object_key" in env.payload

    # Verify checkpoint updated with cursor and idle state
    saved_cp = await cp_store.get(test_mailbox.id)
    assert saved_cp is not None
    assert saved_cp.history_id == "h-003"
    assert saved_cp.sync_state == "idle"
    assert saved_cp.last_sync_at is not None


@pytest.mark.asyncio
async def test_orchestrator_checkpoint_ordering_failure(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    """Verify checkpoint is NOT updated if broker publication fails mid-window (R2.8)."""
    cp_store = InMemoryCheckpointStore()
    storage = FakeObjectStorageClient()
    publisher = MockPublisher()
    publisher.should_fail = True

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
    )

    with pytest.raises(RuntimeError, match="Broker connection lost"):
        await orchestrator.sync_mailbox(test_mailbox)

    # Checkpoint must NOT be advanced to h-003
    saved_cp = await cp_store.get(test_mailbox.id)
    assert saved_cp is not None
    # Because failure happened during processing before save_checkpoint, history_id remains None
    assert saved_cp.history_id is None
    assert saved_cp.sync_state == "error"


@pytest.mark.asyncio
async def test_orchestrator_expired_checkpoint_full_resync(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    """Verify expired checkpoint triggers full resync recording sync_state='full_resync' (R2.7)."""
    fake_adapter.expire_checkpoint("expired-cursor-999")

    cp_store = InMemoryCheckpointStore()
    # Save initial expired checkpoint
    await cp_store.save(
        Checkpoint(
            mailbox_id=test_mailbox.id,
            organization_id=test_mailbox.organization_id,
            history_id="expired-cursor-999",
            sync_state="idle",
        )
    )

    storage = FakeObjectStorageClient()
    publisher = MockPublisher()
    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
    )

    outcome = await orchestrator.sync_mailbox(test_mailbox)

    assert not outcome.coalesced
    assert outcome.messages_synced == 3
    assert outcome.status == "success"

    saved_cp = await cp_store.get(test_mailbox.id)
    assert saved_cp is not None
    assert saved_cp.history_id == "h-003"
    assert saved_cp.last_full_sync_at is not None
    assert saved_cp.sync_state == "idle"


@pytest.mark.asyncio
async def test_orchestrator_inflight_coalescing(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    """Verify concurrent sync collapses to pending follow-up (R2.9)."""
    cp_store = InMemoryCheckpointStore()
    storage = FakeObjectStorageClient()
    publisher = MockPublisher()
    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
    )

    # Simulate an active in-flight sync by pre-locking
    assert await cp_store.try_acquire_lock(test_mailbox.id, test_mailbox.organization_id)

    # Attempt concurrent sync
    outcome = await orchestrator.sync_mailbox(test_mailbox)

    assert outcome.coalesced is True
    assert outcome.messages_synced == 0
    assert outcome.status == "coalesced"
    assert await cp_store.has_pending_followup(test_mailbox.id) is True

    # Now release lock and run a sync: it should clear pending follow-up
    await cp_store.release_lock(test_mailbox.id, next_state="idle")
    second_outcome = await orchestrator.sync_mailbox(test_mailbox)
    assert second_outcome.coalesced is False
    assert second_outcome.messages_synced == 3
    assert await cp_store.has_pending_followup(test_mailbox.id) is False


@pytest.mark.asyncio
async def test_orchestrator_auth_expired(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    """Verify AuthExpired updates mailbox status to needs_reauth and halts (R1.5, R2.10)."""
    fake_adapter.inject_auth_expired(message="OAuth token revoked")

    cp_store = InMemoryCheckpointStore()
    storage = FakeObjectStorageClient()
    publisher = MockPublisher()
    mailbox_store = InMemoryMailboxStore([test_mailbox])

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
        mailbox_store=mailbox_store,
    )

    outcome = await orchestrator.sync_mailbox(test_mailbox)

    assert outcome.status == "needs_reauth"
    assert outcome.messages_synced == 0

    updated_mbx = await mailbox_store.get(test_mailbox.id)
    assert updated_mbx is not None
    assert updated_mbx.status == "needs_reauth"

    saved_cp = await cp_store.get(test_mailbox.id)
    assert saved_cp is not None
    assert saved_cp.sync_state == "error"


@pytest.mark.asyncio
async def test_orchestrator_rate_limited(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    """Verify RateLimited releases lock and returns retry delay metadata."""
    fake_adapter.inject_rate_limit(retry_after=45.0, message="429 Too Many Requests")

    cp_store = InMemoryCheckpointStore()
    storage = FakeObjectStorageClient()
    publisher = MockPublisher()

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
    )

    outcome = await orchestrator.sync_mailbox(test_mailbox)

    assert outcome.status == "rate_limited"
    assert outcome.retry_after_s == 45

    saved_cp = await cp_store.get(test_mailbox.id)
    assert saved_cp is not None
    assert saved_cp.sync_state == "error"


@pytest.mark.asyncio
async def test_orchestrator_multi_page_pagination(test_mailbox: Mailbox) -> None:
    """Verify orchestrator loops through multiple pages until has_more=False (R2.8)."""
    fake_adapter = FakeProviderAdapter(batch_size=2)
    for i in range(5):
        fake_adapter.seed_message(
            provider_message_id=f"page-msg-{i}",
            raw_payload=f"From: test@example.com\n\nMsg {i}".encode(),
            history_id=f"page-h-{i}",
        )

    cp_store = InMemoryCheckpointStore()
    storage = FakeObjectStorageClient()
    publisher = MockPublisher()

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage,
        publisher=publisher,
        adapter_resolver=lambda _: fake_adapter,
        max_pages=10,
    )

    outcome = await orchestrator.sync_mailbox(test_mailbox)

    assert outcome.status == "success"
    assert outcome.messages_synced == 5
    assert len(publisher.published) == 5

    final_cp = await cp_store.get(test_mailbox.id)
    assert final_cp is not None
    assert final_cp.history_id == "page-h-4"
    assert final_cp.sync_state == "idle"


@pytest.mark.asyncio
async def test_in_memory_stores(test_mailbox: Mailbox) -> None:
    """Verify InMemoryCheckpointStore and InMemoryMailboxStore atomic methods."""
    cp_store = InMemoryCheckpointStore()
    mbx_store = InMemoryMailboxStore([test_mailbox])

    # Mailbox store
    fetched_mbx = await mbx_store.get(test_mailbox.id)
    assert fetched_mbx is not None
    assert fetched_mbx.status == "active"
    await mbx_store.update_status(test_mailbox.id, "paused")
    updated_mbx = await mbx_store.get(test_mailbox.id)
    assert updated_mbx is not None
    assert updated_mbx.status == "paused"

    # Checkpoint lock acquisition & release
    assert await cp_store.try_acquire_lock(test_mailbox.id, test_mailbox.organization_id) is True
    assert await cp_store.try_acquire_lock(test_mailbox.id, test_mailbox.organization_id) is False

    # Pending follow-up
    assert await cp_store.has_pending_followup(test_mailbox.id) is False
    await cp_store.mark_pending_followup(test_mailbox.id)
    assert await cp_store.has_pending_followup(test_mailbox.id) is True
    assert await cp_store.clear_pending_followup(test_mailbox.id) is True
    assert await cp_store.has_pending_followup(test_mailbox.id) is False

    await cp_store.release_lock(test_mailbox.id, next_state="idle")
    cp = await cp_store.get(test_mailbox.id)
    assert cp is not None
    assert cp.sync_state == "idle"

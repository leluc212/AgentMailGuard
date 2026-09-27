"""Unit tests for MailSyncConsumer (RA.10; R2.1, R2.11, R3.5, R23.6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import CollectorRegistry

from packages.adapters.exceptions import NotFound
from packages.adapters.fake import FakeProviderAdapter
from packages.broker.consumer import FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.checkpoint import InMemoryCheckpointStore
from packages.db.mailbox import InMemoryMailboxStore
from packages.domain.entities import Checkpoint, Mailbox
from packages.observability.metrics import create_pipeline_metrics
from services.mail_connector.consumer import MailSyncConsumer
from services.mail_connector.orchestrator import SyncOrchestrator


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((exchange_name, routing_key, envelope))


def make_mock_message(envelope: JobEnvelope) -> MagicMock:
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = envelope.model_dump_json().encode("utf-8")
    msg.exchange = "mail.ingest"
    msg.routing_key = "mail.sync.requested"
    msg.headers = {}
    msg.ack = AsyncMock()
    return msg


def sync_envelope(mailbox_id: str | None, org_id: str, **payload: Any) -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"{org_id}:{mailbox_id}:sync:test",
        job_type="sync_mailbox",
        organization_id=org_id,
        mailbox_id=mailbox_id,
        payload={"provider": "fake", **payload},
    )


@pytest.fixture
def mailbox() -> Mailbox:
    return Mailbox(id=uuid4(), organization_id=uuid4(), provider="fake", address="ops@example.com")


@pytest.fixture
def adapter() -> FakeProviderAdapter:
    a = FakeProviderAdapter()
    a.seed_message(provider_message_id="m-1", raw_payload=b"From: a@b.com\n\n1", history_id="h-1")
    a.seed_message(provider_message_id="m-2", raw_payload=b"From: c@d.com\n\n2", history_id="h-2")
    return a


@pytest.fixture
def cp_store() -> InMemoryCheckpointStore:
    return InMemoryCheckpointStore()


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def mailbox_store(mailbox: Mailbox) -> InMemoryMailboxStore:
    return InMemoryMailboxStore([mailbox])


@pytest.fixture
def consumer(
    cp_store: InMemoryCheckpointStore,
    publisher: RecordingPublisher,
    mailbox_store: InMemoryMailboxStore,
    adapter: FakeProviderAdapter,
) -> MailSyncConsumer:
    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=FakeObjectStorageClient(),
        publisher=publisher,
        adapter_resolver=lambda _: adapter,
        mailbox_store=mailbox_store,
    )
    return MailSyncConsumer(
        orchestrator=orchestrator,
        mailbox_store=mailbox_store,
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )


async def test_sync_envelope_runs_orchestrator(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    publisher: RecordingPublisher,
    cp_store: InMemoryCheckpointStore,
) -> None:
    env = sync_envelope(
        str(mailbox.id), str(mailbox.organization_id), manual=True, full_resync=False
    )
    await consumer.process_job(env, make_mock_message(env))

    assert [rk for _, rk, _ in publisher.published] == ["email.normalize", "email.normalize"]
    assert {e.mailbox_id for _, _, e in publisher.published} == {str(mailbox.id)}
    cp = await cp_store.get(mailbox.id)
    assert cp is not None and cp.history_id == "h-2" and cp.sync_state == "idle"


@pytest.mark.parametrize("bad_id", [None, "", "unresolved-1a2b3c4d", "Users/u@x.com/Messages/m1"])
async def test_unresolvable_mailbox_id_is_fatal(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    publisher: RecordingPublisher,
    bad_id: str | None,
) -> None:
    env = sync_envelope(bad_id, str(mailbox.organization_id))
    with pytest.raises(FatalError, match="Unresolvable mailbox_id"):
        await consumer.process_job(env, make_mock_message(env))
    assert publisher.published == []


async def test_missing_mailbox_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, publisher: RecordingPublisher
) -> None:
    env = sync_envelope(str(uuid4()), str(mailbox.organization_id))
    with pytest.raises(FatalError, match="not found"):
        await consumer.process_job(env, make_mock_message(env))
    assert publisher.published == []


@pytest.mark.parametrize("org", ["", "not-a-uuid"])
async def test_invalid_organization_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, org: str
) -> None:
    env = sync_envelope(str(mailbox.id), org)
    with pytest.raises(FatalError, match="organization_id"):
        await consumer.process_job(env, make_mock_message(env))


async def test_tenant_mismatch_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, publisher: RecordingPublisher
) -> None:
    env = sync_envelope(str(mailbox.id), "00000000-0000-0000-0000-000000000000")
    with pytest.raises(FatalError, match="does not belong"):
        await consumer.process_job(env, make_mock_message(env))
    assert publisher.published == []


async def test_wrong_job_type_is_fatal(consumer: MailSyncConsumer, mailbox: Mailbox) -> None:
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id)).model_copy(
        update={"job_type": "normalize_email"}
    )
    with pytest.raises(FatalError, match="Unsupported job_type"):
        await consumer.process_job(env, make_mock_message(env))


@pytest.mark.parametrize("status", ["needs_reauth", "paused"])
async def test_non_syncing_status_is_skipped(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    mailbox_store: InMemoryMailboxStore,
    publisher: RecordingPublisher,
    cp_store: InMemoryCheckpointStore,
    status: str,
) -> None:
    await mailbox_store.update_status(mailbox.id, status)
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    await consumer.process_job(env, make_mock_message(env))  # no raise -> ack
    assert publisher.published == []
    assert await cp_store.get(mailbox.id) is None  # lock never taken


async def test_unregistered_provider_is_fatal_and_takes_no_lock(
    cp_store: InMemoryCheckpointStore,
    publisher: RecordingPublisher,
    mailbox: Mailbox,
    mailbox_store: InMemoryMailboxStore,
) -> None:
    def resolver(_: Mailbox) -> FakeProviderAdapter:
        raise NotFound("No mail provider adapter registered", provider="unknown")

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=FakeObjectStorageClient(),
        publisher=publisher,
        adapter_resolver=resolver,
    )
    consumer = MailSyncConsumer(
        orchestrator=orchestrator,
        mailbox_store=mailbox_store,
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(FatalError, match="No usable provider adapter"):
        await consumer.process_job(env, make_mock_message(env))
    assert await cp_store.try_acquire_lock(mailbox.id, mailbox.organization_id) is True


async def test_rate_limited_raises_transient(
    consumer: MailSyncConsumer, mailbox: Mailbox, adapter: FakeProviderAdapter
) -> None:
    adapter.inject_rate_limit(retry_after=12.0)
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(TransientError, match="rate limited"):
        await consumer.process_job(env, make_mock_message(env))


async def test_transient_provider_error_raises_transient(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    adapter: FakeProviderAdapter,
    cp_store: InMemoryCheckpointStore,
) -> None:
    adapter.inject_transient_failure()
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(TransientError):
        await consumer.process_job(env, make_mock_message(env))
    cp = await cp_store.get(mailbox.id)
    assert cp is not None and cp.sync_state == "error"  # lock released


async def test_permanent_provider_error_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, adapter: FakeProviderAdapter
) -> None:
    adapter.inject_permanent_failure()
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(FatalError, match="Permanent provider failure"):
        await consumer.process_job(env, make_mock_message(env))


async def test_auth_expired_acks_and_marks_mailbox(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    adapter: FakeProviderAdapter,
    mailbox_store: InMemoryMailboxStore,
) -> None:
    adapter.inject_auth_expired()
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    await consumer.process_job(env, make_mock_message(env))
    stored = await mailbox_store.get(mailbox.id)
    assert stored is not None and stored.status == "needs_reauth"


async def test_full_resync_resets_cursor(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    cp_store: InMemoryCheckpointStore,
    publisher: RecordingPublisher,
) -> None:
    await cp_store.save(
        Checkpoint(
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            history_id="h-2",
            sync_state="idle",
        )
    )
    env = sync_envelope(
        str(mailbox.id), str(mailbox.organization_id), manual=True, full_resync=True
    )
    await consumer.process_job(env, make_mock_message(env))
    assert len(publisher.published) == 2
    cp = await cp_store.get(mailbox.id)
    assert cp is not None and cp.last_full_sync_at is not None


async def test_full_resync_while_busy_is_deferred(
    consumer: MailSyncConsumer, mailbox: Mailbox, cp_store: InMemoryCheckpointStore
) -> None:
    assert await cp_store.try_acquire_lock(mailbox.id, mailbox.organization_id)
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id), full_resync=True)
    with pytest.raises(TransientError, match="busy"):
        await consumer.process_job(env, make_mock_message(env))
    assert await cp_store.has_pending_followup(mailbox.id) is True


async def test_handle_message_dead_letters_unresolvable_and_acks(
    consumer: MailSyncConsumer, mailbox: Mailbox
) -> None:
    pub = MagicMock(spec=MessagePublisher)
    pub.settings = BrokerSettings()
    pub.publish_to_retry = AsyncMock()
    pub.publish_to_dead_letter = AsyncMock()
    consumer._publisher = pub

    env = sync_envelope("unresolved-deadbeef", str(mailbox.organization_id))
    msg = make_mock_message(env)
    await consumer._handle_message(msg)

    pub.publish_to_dead_letter.assert_awaited_once()
    kwargs = pub.publish_to_dead_letter.call_args.kwargs
    assert kwargs["origin_exchange"] == "mail.ingest"
    assert kwargs["origin_routing_key"] == "mail.sync.requested"
    pub.publish_to_retry.assert_not_awaited()
    msg.ack.assert_awaited_once()

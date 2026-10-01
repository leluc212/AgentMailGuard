"""Unit tests for JobEnvelope, JobStore, and pipeline job state transitions.

Requirements:
- R7.3: Classification snapshot & correlation IDs carried in envelope.
- R18.1–R18.5: Job states, illegal transition rejection, atomic processing events.
- R19.2: Deterministic idempotency key derivation and deduplication.
"""

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from packages.adapters.fake import FakeProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.core.storage import FakeObjectStorageClient
from packages.db.checkpoint import InMemoryCheckpointStore
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.db.thread import InMemoryThreadStore
from packages.domain.entities import Classification, Job, Mailbox
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister
from services.mail_connector.orchestrator import SyncOrchestrator


def test_job_envelope_serialization_and_classification_snapshot() -> None:
    """Verify JobEnvelope serialization, snapshot retention, and AMQP round-trip (R7.3)."""
    org_id = str(uuid4())
    mbx_id = str(uuid4())
    msg_id = "msg-12345"
    thd_id = "thd-98765"

    envelope = JobEnvelope(
        idempotency_key=f"test:{org_id}:{msg_id}",
        job_type="generate_reply",
        organization_id=org_id,
        mailbox_id=mbx_id,
        message_id=msg_id,
        thread_id=thd_id,
        attempt=0,
    )

    # Initial state without classification
    assert envelope.category is None
    assert envelope.priority == "normal"
    assert envelope.reply_required is True

    # Attach classification snapshot via domain Classification entity (R7.3)
    classification = Classification(
        category="billing",
        intent="invoice_inquiry",
        priority="high",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
        confidence=0.98,
        decided_by="rule",
    )
    enriched = envelope.with_classification(classification)

    assert enriched.category == "billing"
    assert enriched.priority == "high"
    assert enriched.reply_required is True
    assert enriched.workflow_hint == "template"
    assert enriched.retrieval_required is False
    assert enriched.classification["confidence"] == 0.98

    # Convert to AMQP persistent message
    amqp_msg = enriched.to_message()
    assert amqp_msg.delivery_mode.value == 2  # PERSISTENT
    assert amqp_msg.content_type == "application/json"
    assert amqp_msg.headers["organization_id"] == org_id
    assert amqp_msg.headers["category"] == "billing"
    assert amqp_msg.headers["priority"] == "high"
    assert amqp_msg.headers["trace_id"] == enriched.trace_id

    # Reconstitute from AMQP incoming message
    mock_incoming = AsyncMock()
    mock_incoming.body = amqp_msg.body
    reconstituted = JobEnvelope.from_message(mock_incoming)

    assert reconstituted.job_id == enriched.job_id
    assert reconstituted.idempotency_key == enriched.idempotency_key
    assert reconstituted.category == "billing"
    assert reconstituted.priority == "high"
    assert reconstituted.workflow_hint == "template"


@pytest.mark.asyncio
async def test_in_memory_job_store_creation_and_idempotency() -> None:
    """Verify Job creation in RECEIVED state and deduplication (R18.1, R19.2, R19.4)."""
    store = InMemoryJobStore()
    org_id = uuid4()
    job_id = uuid4()
    key = f"pipeline:{org_id}:msg-001"

    job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="email_pipeline",
        state=JobState.RECEIVED.value,
        idempotency_key=key,
        trace_id="trace-abc",
    )

    # 1. Initial insert
    created, is_new = await store.create_job(job, initial_event_payload={"source": "sync"})
    assert is_new is True
    assert created.id == job_id
    assert created.state == "RECEIVED"

    # Verify initial ProcessingEvent recorded (R18.4)
    events = await store.list_events_for_job(org_id, job_id)
    assert len(events) == 1
    assert events[0].state_from is None
    assert events[0].state_to == "RECEIVED"
    assert events[0].payload["source"] == "sync"

    # 2. Duplicate insert with same idempotency_key
    duplicate_job = Job(
        id=uuid4(),
        organization_id=org_id,
        job_type="email_pipeline",
        state=JobState.RECEIVED.value,
        idempotency_key=key,
    )
    dup_res, is_new2 = await store.create_job(duplicate_job)
    assert is_new2 is False
    assert dup_res.id == job_id  # Returns original job
    assert dup_res.state == "RECEIVED"

    # Verify no duplicate event was added
    events_after = await store.list_events_for_job(org_id, job_id)
    assert len(events_after) == 1


@pytest.mark.asyncio
async def test_job_store_state_machine_transitions() -> None:
    """Verify legal state transitions and illegal rejection (R18.1, R18.3–R18.5)."""
    store = InMemoryJobStore()
    org_id = uuid4()
    job_id = uuid4()

    job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="email_pipeline",
        state=JobState.RECEIVED.value,
        idempotency_key=f"pipe:{job_id}",
    )
    await store.create_job(job)

    # Legal transition: RECEIVED -> NORMALIZED
    msg_id = uuid4()
    thd_id = uuid4()
    upd_job, event = await store.transition_job_state(
        organization_id=org_id,
        job_id=job_id,
        target_state=JobState.NORMALIZED,
        message_id=msg_id,
        thread_id=thd_id,
        payload={"normalized_bytes": 1024},
    )
    assert upd_job.state == "NORMALIZED"
    assert upd_job.message_id == msg_id
    assert event.state_from == "RECEIVED"
    assert event.state_to == "NORMALIZED"

    # Illegal transition: NORMALIZED -> GENERATING (skipping intermediate states)
    with pytest.raises(IllegalStateTransitionError):
        await store.transition_job_state(
            organization_id=org_id,
            job_id=job_id,
            target_state=JobState.GENERATING,
        )

    # Verify state was not modified after illegal transition
    current = await store.get_job(org_id, job_id)
    assert current is not None
    assert current.state == "NORMALIZED"

    # Legal sequence: NORMALIZED -> CLASSIFIED -> QUEUED
    await store.transition_job_state(org_id, job_id, JobState.CLASSIFIED)
    await store.transition_job_state(org_id, job_id, JobState.QUEUED)

    events = await store.list_events_for_job(org_id, job_id)
    assert len(events) == 4
    assert [e.state_to for e in events] == ["RECEIVED", "NORMALIZED", "CLASSIFIED", "QUEUED"]


@pytest.mark.asyncio
async def test_sync_orchestrator_creates_job_in_received_state() -> None:
    """Verify SyncOrchestrator creates a Job in state RECEIVED at ingestion (Task 2.1, R18.1)."""
    org_id = uuid4()
    mbx_id = uuid4()
    mailbox = Mailbox(
        id=mbx_id,
        organization_id=org_id,
        provider="fake",
        address="user@example.com",
    )

    cp_store = InMemoryCheckpointStore()
    storage_client = FakeObjectStorageClient()
    job_store = InMemoryJobStore()

    published_envelopes: list[JobEnvelope] = []

    class MockPublisher:
        async def publish(
            self,
            exchange_name: str,
            routing_key: str,
            envelope: JobEnvelope,
            headers: dict[str, Any] | None = None,
        ) -> None:
            published_envelopes.append(envelope)

    fake_adapter = FakeProviderAdapter()
    fake_adapter.seed_message(
        provider_message_id="msg-sync-100",
        provider_thread_id="thd-sync-200",
        raw_payload=b"Subject: Test\r\n\r\nBody text",
        history_id="12345",
    )

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=storage_client,
        publisher=MockPublisher(),
        adapter_resolver=lambda _: fake_adapter,
        job_store=job_store,
    )

    outcome = await orchestrator.sync_mailbox(mailbox)
    assert outcome.messages_synced == 1
    assert len(published_envelopes) == 1

    envelope = published_envelopes[0]
    job_uuid = UUID(envelope.job_id)

    # Assert job was persisted in state RECEIVED in JobStore
    persisted_job = await job_store.get_job(org_id, job_uuid)
    assert persisted_job is not None
    assert persisted_job.state == "RECEIVED"
    assert persisted_job.idempotency_key == envelope.idempotency_key

    # Assert initial ProcessingEvent was logged
    events = await job_store.list_events_for_job(org_id, job_uuid)
    assert len(events) == 1
    assert events[0].state_to == "RECEIVED"
    assert events[0].payload["provider_message_id"] == "msg-sync-100"


@pytest.mark.asyncio
async def test_email_normalization_consumer_transitions_to_normalized() -> None:
    """Verify consumer transitions job from RECEIVED to NORMALIZED (Task 2.1, R18.1, R18.4)."""
    org_id = uuid4()
    mbx_id = uuid4()
    job_id = uuid4()

    job_store = InMemoryJobStore()
    storage_client = FakeObjectStorageClient()
    message_store = InMemoryMessageStore()
    thread_store = InMemoryThreadStore()

    # Pre-populate raw email in storage
    raw_key = "raw/test-email.eml"
    raw_content = b"From: sender@example.com\r\nTo: me@example.com\r\nSubject: Hi\r\n\r\nHello"
    await storage_client.put_bytes(
        bucket="raw-emails",
        key=raw_key,
        data=raw_content,
    )

    # Pre-create job in RECEIVED state (matching ingestion)
    ingest_job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="email_pipeline",
        state=JobState.RECEIVED.value,
        idempotency_key=f"pipe:{org_id}:msg-test-1",
        trace_id="trace-test-norm",
    )
    await job_store.create_job(ingest_job)

    published_triage: list[JobEnvelope] = []

    class MockPublisher:
        async def publish(
            self,
            exchange_name: str,
            routing_key: str,
            envelope: JobEnvelope,
            headers: dict[str, Any] | None = None,
        ) -> None:
            published_triage.append(envelope)

    persister = EmailPersister(message_store=message_store, thread_store=thread_store)
    normalizer = EmailNormalizer()

    consumer = EmailNormalizationConsumer(
        normalizer=normalizer,
        persister=persister,
        storage_client=storage_client,
        publisher=MockPublisher(),
        job_store=job_store,
    )

    # Process job envelope
    envelope = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=f"pipe:{org_id}:msg-test-1",
        organization_id=str(org_id),
        mailbox_id=str(mbx_id),
        message_id="msg-test-1",
        thread_id="",
        job_type="normalize_email",
        trace_id="trace-test-norm",
        payload={
            "job_id": str(job_id),
            "raw_object_key": raw_key,
            "raw_bucket": "raw-emails",
            "provider": "fake",
            "provider_message_id": "msg-test-1",
        },
    )

    mock_msg = AsyncMock()
    await consumer.process_job(envelope, mock_msg)

    # Verify job transitioned to NORMALIZED
    updated_job = await job_store.get_job(org_id, job_id)
    assert updated_job is not None
    assert updated_job.state == "NORMALIZED"
    assert updated_job.message_id is not None
    assert updated_job.thread_id is not None

    # Verify chronological ProcessingEvents
    events = await job_store.list_events_for_job(org_id, job_id)
    assert len(events) == 2
    assert events[0].state_to == "RECEIVED"
    assert events[1].state_from == "RECEIVED"
    assert events[1].state_to == "NORMALIZED"

    # Verify triage envelope preserves job_id
    assert len(published_triage) == 1
    triage_env = published_triage[0]
    assert triage_env.job_id == str(job_id)
    assert triage_env.trace_id == "trace-test-norm"
    assert triage_env.message_id == str(updated_job.message_id)
    assert triage_env.thread_id == str(updated_job.thread_id)

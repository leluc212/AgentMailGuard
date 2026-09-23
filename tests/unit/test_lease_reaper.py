"""Unit tests for lease acquisition, renewal, and reaper recovery (R19.8, design.md §9)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from packages.db.job import InMemoryJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState


@pytest.mark.asyncio
async def test_acquire_and_renew_lease() -> None:
    """Verify lease acquisition and renewal on active jobs."""
    store = InMemoryJobStore()
    org_id = uuid4()
    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)

    # Acquire lease
    leased = await store.acquire_lease(org_id, created.id, lease_timeout_s=300)
    assert leased is not None
    assert leased.lease_expires_at is not None
    assert leased.lease_expires_at > datetime.now(UTC)

    old_expiry = leased.lease_expires_at

    # Renew lease with longer duration
    renewed = await store.renew_lease(org_id, created.id, extension_s=600)
    assert renewed is not None
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > old_expiry

    # Attempting to acquire lease on terminal job returns None
    created.state = JobState.COMPLETED.value
    assert await store.acquire_lease(org_id, created.id, lease_timeout_s=300) is None

    # Attempting to acquire lease on non-existent job returns None
    assert await store.acquire_lease(org_id, uuid4(), lease_timeout_s=300) is None


@pytest.mark.asyncio
async def test_reap_expired_jobs_generating_to_retry_pending() -> None:
    """Verify stuck job in GENERATING transitions to RETRY_PENDING on retry (R19.8, R19.5)."""
    store = InMemoryJobStore()
    org_id = uuid4()
    past_time = datetime.now(UTC) - timedelta(seconds=60)

    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=0,
        max_attempts=3,
        lease_expires_at=past_time,
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)
    # Ensure lease_expires_at is preserved in the past
    created.lease_expires_at = past_time

    reaped = await store.reap_expired_jobs(batch_size=10)
    assert len(reaped) == 1

    updated_job, event, action = reaped[0]
    assert action == "reclaimed"
    assert updated_job.id == created.id
    assert updated_job.state == JobState.RETRY_PENDING.value
    assert updated_job.attempt == 1
    assert updated_job.lease_expires_at is None
    assert "past timeout" in str(updated_job.last_error)

    assert event.state_from == JobState.GENERATING.value
    assert event.state_to == JobState.RETRY_PENDING.value
    assert event.payload["attempt"] == 1


@pytest.mark.asyncio
async def test_reap_expired_jobs_retry_exhausted_to_dead_letter() -> None:
    """Verify stuck job with exhausted attempts transitions to DEAD_LETTER (R19.8, R19.6)."""
    store = InMemoryJobStore()
    org_id = uuid4()
    past_time = datetime.now(UTC) - timedelta(seconds=60)

    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=2,
        max_attempts=3,
        lease_expires_at=past_time,
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)
    created.lease_expires_at = past_time

    reaped = await store.reap_expired_jobs(batch_size=10)
    assert len(reaped) == 1

    updated_job, event, action = reaped[0]
    assert action == "dead_letter"
    assert updated_job.id == created.id
    assert updated_job.state == JobState.DEAD_LETTER.value
    assert updated_job.attempt == 3
    assert updated_job.lease_expires_at is None
    assert "attempts exceeded" in str(updated_job.last_error)

    assert event.state_to == JobState.DEAD_LETTER.value
    assert event.payload.get("terminal") is True


@pytest.mark.asyncio
async def test_reap_expired_jobs_non_expired_untouched() -> None:
    """Verify jobs with active, unexpired leases are untouched by reaper."""
    store = InMemoryJobStore()
    org_id = uuid4()
    future_time = datetime.now(UTC) + timedelta(seconds=300)

    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=0,
        max_attempts=3,
        lease_expires_at=future_time,
        idempotency_key=str(uuid4()),
    )
    await store.create_job(job)

    reaped = await store.reap_expired_jobs(batch_size=10)
    assert len(reaped) == 0


@pytest.mark.asyncio
async def test_reap_stuck_unleased_job_fallback() -> None:
    """Verify unleased jobs stuck in active states past timeout are reaped when enabled."""
    store = InMemoryJobStore()
    org_id = uuid4()
    past_time = datetime.now(UTC) - timedelta(seconds=120)

    job = Job(
        organization_id=org_id,
        state=JobState.CONTEXT_READY.value,
        attempt=0,
        max_attempts=3,
        lease_expires_at=None,
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)
    created.updated_at = past_time

    # With unleased_timeout_s=60, it should be reaped
    reaped = await store.reap_expired_jobs(batch_size=10, unleased_timeout_s=60)
    assert len(reaped) == 1

    updated_job, event, action = reaped[0]
    assert action == "reclaimed"
    assert updated_job.state == JobState.RETRY_PENDING.value
    assert updated_job.attempt == 1


@pytest.mark.asyncio
async def test_lease_reaper_sweep_reclaims_and_publishes_retry(mocker: pytest.MonkeyPatch) -> None:
    """Verify LeaseReaper sweeps expired jobs, updates metrics, and publishes to retry exchange."""
    from unittest.mock import AsyncMock, MagicMock

    from packages.broker.lease_reaper import LeaseReaper
    from packages.core.settings import BrokerSettings, LeaseReaperSettings
    from packages.observability.metrics import create_pipeline_metrics

    store = InMemoryJobStore()
    metrics = create_pipeline_metrics()
    org_id = uuid4()
    past_time = datetime.now(UTC) - timedelta(seconds=60)

    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=0,
        max_attempts=3,
        lease_expires_at=past_time,
        queue_name="email.support.normal",
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)
    created.lease_expires_at = past_time

    mock_publisher = MagicMock()
    mock_publisher.settings = BrokerSettings()
    mock_publisher.publish_to_retry = AsyncMock()
    mock_publisher.publish_to_dead_letter = AsyncMock()

    settings = LeaseReaperSettings(lease_timeout_s=30, batch_size=10)
    reaper = LeaseReaper(
        job_store=store,
        settings=settings,
        publisher=mock_publisher,
        metrics=metrics,
    )

    reaped = await reaper.reap_once()
    assert len(reaped) == 1
    assert reaped[0][2] == "reclaimed"

    # Verify metrics incremented
    metric_val = metrics.reaped_leases_total.labels(
        action="reclaimed", state="GENERATING"
    )._value.get()
    assert metric_val == 1.0

    # Verify published to retry exchange
    mock_publisher.publish_to_retry.assert_awaited_once()
    call_kwargs = mock_publisher.publish_to_retry.await_args.kwargs
    assert call_kwargs["envelope"].job_id == str(created.id)
    assert call_kwargs["envelope"].attempt == 1
    assert call_kwargs["origin_routing_key"] == "email.support.normal"


@pytest.mark.asyncio
async def test_lease_reaper_sweep_dead_letters_and_publishes_dlq() -> None:
    """Verify LeaseReaper publishes exhausted stuck jobs to DLQ."""
    from unittest.mock import AsyncMock, MagicMock

    from packages.broker.lease_reaper import LeaseReaper
    from packages.core.settings import BrokerSettings
    from packages.observability.metrics import create_pipeline_metrics

    store = InMemoryJobStore()
    metrics = create_pipeline_metrics()
    org_id = uuid4()
    past_time = datetime.now(UTC) - timedelta(seconds=60)

    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        attempt=2,
        max_attempts=3,
        lease_expires_at=past_time,
        queue_name="email.billing.priority",
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)
    created.lease_expires_at = past_time

    mock_publisher = MagicMock()
    mock_publisher.settings = BrokerSettings()
    mock_publisher.publish_to_retry = AsyncMock()
    mock_publisher.publish_to_dead_letter = AsyncMock()

    reaper = LeaseReaper(
        job_store=store,
        publisher=mock_publisher,
        metrics=metrics,
    )

    reaped = await reaper.reap_once()
    assert len(reaped) == 1
    assert reaped[0][2] == "dead_letter"

    metric_val = metrics.reaped_leases_total.labels(
        action="dead_letter", state="GENERATING"
    )._value.get()
    assert metric_val == 1.0

    mock_publisher.publish_to_dead_letter.assert_awaited_once()
    call_kwargs = mock_publisher.publish_to_dead_letter.await_args.kwargs
    assert call_kwargs["envelope"].job_id == str(created.id)
    assert call_kwargs["origin_routing_key"] == "email.billing.priority"


@pytest.mark.asyncio
async def test_lease_reaper_start_stop_lifecycle() -> None:
    """Verify start() and stop() lifecycle of LeaseReaper background task."""
    import asyncio

    from packages.broker.lease_reaper import LeaseReaper
    from packages.core.settings import LeaseReaperSettings

    store = InMemoryJobStore()
    settings = LeaseReaperSettings(reaper_interval_s=0.05)
    reaper = LeaseReaper(job_store=store, settings=settings)

    assert reaper._running is False
    assert reaper._task is None

    reaper.start()
    assert reaper._running is True
    assert reaper._task is not None
    assert not reaper._task.done()

    # Allow task to run a cycle
    await asyncio.sleep(0.08)

    await reaper.stop()
    assert reaper._running is False
    assert reaper._task is None


@pytest.mark.asyncio
async def test_consumer_acquires_lease_on_claim() -> None:
    """Verify BaseConsumer automatically acquires a lease on claim when job_store is provided."""
    from unittest.mock import AsyncMock, MagicMock

    from aio_pika.abc import AbstractIncomingMessage

    from packages.broker.consumer import BaseConsumer
    from packages.broker.envelope import JobEnvelope

    class TestConsumer(BaseConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            pass

    store = InMemoryJobStore()
    org_id = uuid4()
    job = Job(
        organization_id=org_id,
        state=JobState.GENERATING.value,
        idempotency_key=str(uuid4()),
    )
    created, _ = await store.create_job(job)
    assert created.lease_expires_at is None

    consumer = TestConsumer(
        queue_name="email.support.normal",
        job_store=store,
        lease_timeout_s=180,
    )

    envelope = JobEnvelope(
        job_id=str(created.id),
        organization_id=str(org_id),
        message_id="",
        thread_id="",
        job_type="generate_reply",
        attempt=0,
        idempotency_key=created.idempotency_key,
    )

    raw_msg = MagicMock()
    raw_msg.headers = {}
    raw_msg.exchange = "ex.email"
    raw_msg.routing_key = "email.support.normal"
    raw_msg.body = envelope.model_dump_json().encode("utf-8")
    raw_msg.ack = AsyncMock()

    await consumer._handle_message(raw_msg)
    raw_msg.ack.assert_awaited_once()

    stored_job = await store.get_job(org_id, created.id)
    assert stored_job is not None
    assert stored_job.lease_expires_at is not None
    assert stored_job.lease_expires_at > datetime.now(UTC)

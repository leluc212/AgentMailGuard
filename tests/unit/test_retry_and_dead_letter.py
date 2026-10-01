"""Unit tests for retry ladder, exponential backoff, and dead-letter state coordination.

Requirements:
- R19.5: GENERATING -> RETRY_PENDING -> GENERATING with exponential backoff and jitter.
- R19.6: FAILED -> DEAD_LETTER when retry limit exceeded.
- R3.5: Dead-letter exchange headers preservation (x-original-routing-key, x-failure-reason, etc.).
- R18.2: Support failure states RETRY_PENDING, FAILED, DEAD_LETTER.
- R18.4, R18.5: Atomically record ProcessingEvent on transition in the same transaction.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.batch_consumer import BaseBatchConsumer
from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.retry import (
    handle_job_recovery,
    handle_job_terminal_failure,
    handle_job_transient_failure,
)
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import InMemoryJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState


def make_mock_message(envelope: JobEnvelope) -> AbstractIncomingMessage:
    """Create a mock AMQP IncomingMessage with serialized envelope."""
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = envelope.model_dump_json().encode("utf-8")
    msg.routing_key = "email.billing.priority"
    msg.exchange = "email.route"
    msg.headers = {
        "x-original-routing-key": "email.billing.priority",
        "x-original-exchange": "email.route",
    }
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    return msg


def assert_msg_acked(msg: AbstractIncomingMessage) -> None:
    """Helper to assert async message ack under static type checking."""
    cast(AsyncMock, msg.ack).assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_job_transient_failure_state_and_publishing() -> None:
    """Verify transient error transitions GENERATING -> RETRY_PENDING and publishes to ladder.

    Requirements: R19.5, R18.2.
    """
    job_store = InMemoryJobStore()
    org_id = uuid4()
    job_id = uuid4()
    message_id = uuid4()

    job = Job(
        id=job_id,
        organization_id=org_id,
        message_id=message_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:{job_id}",
        trace_id="trace-retry-1",
    )
    await job_store.create_job(job)

    mock_publisher = MagicMock(spec=MessagePublisher)
    mock_publisher.settings = BrokerSettings()
    mock_publisher.publish_to_retry = AsyncMock()

    retry_settings = RetryLadderSettings(
        tier_1_delay_s=30,
        tier_2_delay_s=300,
        tier_3_delay_s=1800,
        max_retries=3,
        backoff_base_s=1.0,
    )

    envelope = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=job.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        message_id=str(message_id),
        attempt=0,
        trace_id=job.trace_id,
    )

    exc = ConnectionError("OpenAI API connection reset")

    updated_envelope = await handle_job_transient_failure(
        envelope=envelope,
        exception=exc,
        publisher=mock_publisher,
        retry_settings=retry_settings,
        origin_exchange="email.route",
        origin_routing_key="email.billing.priority",
        queue_name="email.billing.priority",
        job_store=job_store,
    )

    # 1. Envelope attempt incremented
    assert updated_envelope.attempt == 1

    # 2. Database job state transitioned to RETRY_PENDING
    updated_job = await job_store.get_job(org_id, job_id)
    assert updated_job is not None
    assert updated_job.state == JobState.RETRY_PENDING.value

    # 3. ProcessingEvent recorded with attempt and delay
    events = await job_store.list_events_for_job(org_id, job_id)
    assert len(events) >= 2  # create_job + state transition
    transition_event = events[-1]
    assert transition_event.state_from == JobState.GENERATING.value
    assert transition_event.state_to == JobState.RETRY_PENDING.value
    assert transition_event.payload["attempt"] == 1
    assert transition_event.payload["delay_s"] == 30
    assert "ConnectionError" in transition_event.payload["error_type"]

    # 4. AMQP publish_to_retry called
    mock_publisher.publish_to_retry.assert_awaited_once()
    call_args = mock_publisher.publish_to_retry.call_args[1]
    assert call_args["tier_delay_s"] == 30
    assert call_args["origin_exchange"] == "email.route"
    assert call_args["origin_routing_key"] == "email.billing.priority"
    assert "ConnectionError" in call_args["failure_reason"]


@pytest.mark.asyncio
async def test_handle_job_recovery_transitions_to_generating() -> None:
    """Verify redelivered message transitions RETRY_PENDING -> GENERATING (R19.5, R18.2)."""
    job_store = InMemoryJobStore()
    org_id = uuid4()
    job_id = uuid4()

    job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.RETRY_PENDING.value,
        idempotency_key=f"idem:{job_id}",
        trace_id="trace-rec-1",
    )
    await job_store.create_job(job)

    envelope = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=job.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=1,
    )

    await handle_job_recovery(envelope=envelope, job_store=job_store)

    # State transitioned to GENERATING
    updated_job = await job_store.get_job(org_id, job_id)
    assert updated_job is not None
    assert updated_job.state == JobState.GENERATING.value

    # Verify event generated
    events = await job_store.list_events_for_job(org_id, job_id)
    assert events[-1].state_from == JobState.RETRY_PENDING.value
    assert events[-1].state_to == JobState.GENERATING.value
    assert events[-1].payload["resumed_at_attempt"] == 1


@pytest.mark.asyncio
async def test_handle_job_terminal_failure_transitions_to_failed_and_dead_letter() -> None:
    """Verify fatal failure transitions FAILED -> DEAD_LETTER and publishes to DLQ (R19.6, R3.5)."""
    job_store = InMemoryJobStore()
    org_id = uuid4()
    job_id = uuid4()

    job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:{job_id}",
        trace_id="trace-fatal-1",
    )
    await job_store.create_job(job)

    mock_publisher = MagicMock(spec=MessagePublisher)
    mock_publisher.publish_to_dead_letter = AsyncMock()

    envelope = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=job.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=3,
    )

    exc = FatalError("Corrupt payload schema: missing required fields")

    await handle_job_terminal_failure(
        envelope=envelope,
        exception=exc,
        publisher=mock_publisher,
        origin_exchange="email.route",
        origin_routing_key="email.billing.priority",
        queue_name="email.billing.priority",
        job_store=job_store,
    )

    # 1. Database job state transitioned to DEAD_LETTER
    updated_job = await job_store.get_job(org_id, job_id)
    assert updated_job is not None
    assert updated_job.state == JobState.DEAD_LETTER.value

    # 2. Events include both -> FAILED and FAILED -> DEAD_LETTER
    events = await job_store.list_events_for_job(org_id, job_id)
    states = [(e.state_from, e.state_to) for e in events if e.state_from is not None]
    assert (JobState.GENERATING.value, JobState.FAILED.value) in states
    assert (JobState.FAILED.value, JobState.DEAD_LETTER.value) in states

    # 3. Message published to dead letter exchange
    mock_publisher.publish_to_dead_letter.assert_awaited_once()
    dlq_args = mock_publisher.publish_to_dead_letter.call_args[1]
    assert dlq_args["origin_routing_key"] == "email.billing.priority"
    assert dlq_args["origin_exchange"] == "email.route"
    assert "FatalError" in dlq_args["failure_reason"]


@pytest.mark.asyncio
async def test_dlq_headers_preservation() -> None:
    """Verify DLQ message carries all diagnostic headers per R3.5."""
    publisher = MessagePublisher()
    mock_exchange = AsyncMock()
    publisher._get_exchange = AsyncMock(return_value=mock_exchange)  # type: ignore[method-assign]

    envelope = JobEnvelope(
        job_id=str(uuid4()),
        idempotency_key="test-key",
        job_type="generate_reply",
        organization_id=str(uuid4()),
        attempt=3,
    )

    await publisher.publish_to_dead_letter(
        envelope=envelope,
        failure_reason="AttemptsExhausted: maximum retries reached",
        origin_routing_key="email.support.priority",
        origin_exchange="email.route",
    )

    mock_exchange.publish.assert_awaited_once()
    published_msg = mock_exchange.publish.call_args[0][0]
    headers = published_msg.headers

    assert headers["x-original-routing-key"] == "email.support.priority"
    assert headers["x-original-exchange"] == "email.route"
    assert headers["x-failure-reason"] == "AttemptsExhausted: maximum retries reached"
    assert headers["x-attempt"] == 3
    assert "x-failed-at" in headers


@pytest.mark.asyncio
async def test_base_consumer_retry_and_dead_letter_lifecycle() -> None:
    """Verify BaseConsumer full retry cycle:
    GENERATING -> RETRY_PENDING -> GENERATING -> DEAD_LETTER.
    """
    job_store = InMemoryJobStore()
    org_id = uuid4()
    job_id = uuid4()

    job = Job(
        id=job_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:{job_id}",
        trace_id="trace-cycle-1",
    )
    await job_store.create_job(job)

    mock_publisher = MagicMock(spec=MessagePublisher)
    mock_publisher.settings = BrokerSettings()
    mock_publisher.publish_to_retry = AsyncMock()
    mock_publisher.publish_to_dead_letter = AsyncMock()

    class FlakyConsumer(BaseConsumer):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.executions: list[int] = []

        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            self.executions.append(envelope.attempt)
            raise TransientError("Simulated LLM rate limit (429)")

    consumer = FlakyConsumer(
        queue_name="email.billing.priority",
        retry_settings=RetryLadderSettings(
            max_retries=2, tier_1_delay_s=30, tier_2_delay_s=300, tier_3_delay_s=1800
        ),
        job_store=job_store,
    )
    consumer._publisher = mock_publisher

    # --- Attempt 0: First delivery failure ---
    env0 = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=job.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=0,
    )
    msg0 = make_mock_message(env0)

    await consumer._handle_message(msg0)

    assert_msg_acked(msg0)
    mock_publisher.publish_to_retry.assert_awaited_once()
    j0 = await job_store.get_job(org_id, job_id)
    assert j0 is not None
    assert j0.state == JobState.RETRY_PENDING.value

    # --- Attempt 1: Redelivery (recovered to GENERATING, then fails again) ---
    env1 = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=job.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=1,
    )
    msg1 = make_mock_message(env1)

    await consumer._handle_message(msg1)

    assert_msg_acked(msg1)
    assert mock_publisher.publish_to_retry.await_count == 2
    j1 = await job_store.get_job(org_id, job_id)
    assert j1 is not None
    assert j1.state == JobState.RETRY_PENDING.value

    # --- Attempt 2: Max retries exceeded (attempt=2 == max_retries=2) -> Terminal DLQ ---
    env2 = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=job.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=2,
    )
    msg2 = make_mock_message(env2)

    await consumer._handle_message(msg2)

    assert_msg_acked(msg2)
    mock_publisher.publish_to_dead_letter.assert_awaited_once()
    j2 = await job_store.get_job(org_id, job_id)
    assert j2 is not None
    assert j2.state == JobState.DEAD_LETTER.value


@pytest.mark.asyncio
async def test_batch_consumer_fault_isolation_with_job_store() -> None:
    """Verify BaseBatchConsumer isolates transient failure from successful sibling (R3.3, R3.7)."""
    job_store = InMemoryJobStore()
    org_id = uuid4()
    job1_id = uuid4()
    job2_id = uuid4()

    job1 = Job(
        id=job1_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:{job1_id}",
    )
    job2 = Job(
        id=job2_id,
        organization_id=org_id,
        job_type="generate_reply",
        state=JobState.GENERATING.value,
        idempotency_key=f"idem:{job2_id}",
    )
    await job_store.create_job(job1)
    await job_store.create_job(job2)

    mock_publisher = MagicMock(spec=MessagePublisher)
    mock_publisher.settings = BrokerSettings()
    mock_publisher.publish_to_retry = AsyncMock()

    class MixedBatchConsumer(BaseBatchConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            if envelope.job_id == str(job1_id):
                # Job 1 succeeds and transitions to DRAFTED
                await self.job_store.transition_job_state(  # type: ignore[union-attr]
                    organization_id=envelope.organization_id,
                    job_id=UUID(envelope.job_id),
                    target_state=JobState.DRAFTED,
                )
            else:
                # Job 2 raises transient error
                raise TransientError("Inference timed out")

    consumer = MixedBatchConsumer(
        queue_name="email.billing.priority",
        batch_size=2,
        batch_timeout_s=0.1,
        job_store=job_store,
    )
    consumer._publisher = mock_publisher

    env1 = JobEnvelope(
        job_id=str(job1_id),
        idempotency_key=job1.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=0,
    )
    env2 = JobEnvelope(
        job_id=str(job2_id),
        idempotency_key=job2.idempotency_key,
        job_type="generate_reply",
        organization_id=str(org_id),
        attempt=0,
    )
    from packages.broker.batch_consumer import BatchItem

    msg1 = make_mock_message(env1)
    msg2 = make_mock_message(env2)
    item1 = BatchItem(envelope=env1, raw_message=msg1)
    item2 = BatchItem(envelope=env2, raw_message=msg2)
    await consumer._handle_single_item(item1)
    await consumer._handle_single_item(item2)

    # Both messages acked
    assert_msg_acked(msg1)
    assert_msg_acked(msg2)

    # Job 1 succeeded to DRAFTED
    res1 = await job_store.get_job(org_id, job1_id)
    assert res1 is not None
    assert res1.state == JobState.DRAFTED.value

    # Job 2 failed transiently to RETRY_PENDING
    res2 = await job_store.get_job(org_id, job2_id)
    assert res2 is not None
    assert res2.state == JobState.RETRY_PENDING.value

    # Retry publisher called for Job 2 only
    mock_publisher.publish_to_retry.assert_awaited_once()

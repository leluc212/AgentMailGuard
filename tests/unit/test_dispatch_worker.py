"""dispatch-worker composition, failure policy and consumer hooks (tasks 6.5, 6.6).

Requirements: R17.5 (retry with Retry-After, dead-letter with the provider error),
R18.3 (legal transitions only), R18.7 (operator replay RETRY_PENDING -> DISPATCHED),
design.md §5.8 (the dispatch-worker takes no leases).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.adapters.exceptions import AuthExpired, NotFound, Permanent, RateLimited, Transient
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings, DispatchWorkerSettings, RetryLadderSettings
from packages.db.dispatch import DispatchKeyConflictError
from packages.dispatch.reply import MissingProviderThreadError, MissingRecipientError
from packages.dispatch.service import DispatchPermanentError, DispatchService
from packages.domain import DispatchMode
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from services.dispatch_worker.consumer import DispatchConsumer
from services.dispatch_worker.failure_policy import (
    Disposition,
    classify_dispatch_failure,
    provider_retry_after,
)
from services.dispatch_worker.main import build_consumer
from tests.stubs.dispatch_fakes import DispatchWorld, build_dispatch_world
from tests.stubs.worker_resources import fake_worker_resources

LADDER = RetryLadderSettings(
    tier_1_delay_s=30, tier_2_delay_s=300, tier_3_delay_s=1800, max_retries=3
)


def test_build_consumer_uses_shared_resources() -> None:
    settings = DispatchWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    consumer = build_consumer(res)

    assert consumer.queue_name == settings.broker.queue_dispatch == "email.dispatch"
    assert consumer.prefetch_count == settings.concurrency.dispatch_worker_concurrency
    assert consumer._connection is res.connection
    assert consumer.shutdown_coordinator is res.shutdown
    assert consumer.job_store is not None
    assert isinstance(consumer.service, DispatchService)


def test_build_consumer_passes_the_adapter_resolver_to_the_service() -> None:
    """Task 11 injects the fake adapter here instead of patching the adapter registry."""
    res = fake_worker_resources(DispatchWorkerSettings(_env_file=None))
    resolver = MagicMock(name="adapter_resolver")
    consumer = build_consumer(res, adapter_resolver=resolver)
    assert consumer.service._adapter_for is resolver


@pytest.mark.parametrize(
    ("exc", "disposition"),
    [
        (RateLimited("429", retry_after=12.0), Disposition.RETRY),
        (Transient("503"), Disposition.RETRY),
        (Transient("503", retry_after_s=30.0), Disposition.RETRY),
        (MissingRecipientError(draft_id="d-1"), Disposition.DEAD_LETTER),
        (IllegalStateTransitionError("DRAFTED", "DISPATCHED"), Disposition.RETRY),
        (RuntimeError("unknown"), Disposition.RETRY),
        (Permanent("400 Bad Request"), Disposition.DEAD_LETTER),
        (NotFound("404 on send"), Disposition.DEAD_LETTER),
        (AuthExpired("401"), Disposition.DEAD_LETTER),
        (MissingProviderThreadError(draft_id="d-1", thread_id="t-1"), Disposition.DEAD_LETTER),
        (DispatchPermanentError("draft deleted"), Disposition.DEAD_LETTER),
        (DispatchKeyConflictError("key taken"), Disposition.DEAD_LETTER),
    ],
)
def test_failure_policy(exc: Exception, disposition: Disposition) -> None:
    decision = classify_dispatch_failure(exc)
    assert decision.disposition is disposition
    assert type(exc).__name__ in decision.reason


def test_provider_retry_after_reads_only_retryable_provider_errors() -> None:
    assert provider_retry_after(RateLimited("429", retry_after=12.0)) == 12.0
    assert provider_retry_after(RateLimited("429")) is None
    assert provider_retry_after(Transient("503")) is None
    assert provider_retry_after(Transient("503", retry_after_s=30.0)) == 30.0
    assert provider_retry_after(ValueError("x")) is None


def _consumer(world: DispatchWorld) -> tuple[DispatchConsumer, MagicMock]:
    consumer = DispatchConsumer(
        "email.dispatch",
        service=world.service,
        job_store=world.store.jobs,
        retry_settings=LADDER,
    )
    publisher = MagicMock(spec=MessagePublisher)
    publisher.settings = BrokerSettings()
    publisher.publish_to_retry = AsyncMock()
    publisher.publish_to_dead_letter = AsyncMock()
    consumer._publisher = cast(MessagePublisher, publisher)
    return consumer, publisher


def _delivery(world: DispatchWorld, attempt: int = 0) -> AbstractIncomingMessage:
    envelope = JobEnvelope(
        job_id=str(world.job_id),
        idempotency_key="dispatch-key",
        job_type="dispatch",
        organization_id=str(world.org_id),
        attempt=attempt,
    )
    message = MagicMock(spec=AbstractIncomingMessage)
    message.body = envelope.model_dump_json().encode("utf-8")
    message.routing_key = "email.dispatch"
    message.exchange = "email.dispatch"
    message.headers = {}
    message.ack = AsyncMock()
    message.nack = AsyncMock()
    return cast(AbstractIncomingMessage, message)


async def _job_state(world: DispatchWorld) -> str:
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    return job.state


async def test_rate_limit_retries_on_the_retry_after_tier_and_stays_dispatched() -> None:
    world = await build_dispatch_world()
    world.fake.inject_rate_limit(retry_after=200.0)
    consumer, publisher = _consumer(world)

    await consumer._handle_message(_delivery(world))

    call = cast(AsyncMock, publisher.publish_to_retry).await_args
    assert call is not None and call.kwargs["tier_delay_s"] == 300
    assert await _job_state(world) == JobState.DISPATCHED.value
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None and job.lease_expires_at is None  # no lease taken


async def test_permanent_failure_dead_letters_with_the_provider_error() -> None:
    world = await build_dispatch_world()
    world.fake.inject_permanent_failure("400 Bad Request: invalid To header")
    consumer, publisher = _consumer(world)

    await consumer._handle_message(_delivery(world))

    cast(AsyncMock, publisher.publish_to_dead_letter).assert_awaited_once()
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    assert job.state == JobState.DEAD_LETTER.value
    assert job.last_error is not None and "invalid To header" in job.last_error
    states = [
        e.state_to for e in await world.store.jobs.list_events_for_job(world.org_id, world.job_id)
    ]
    assert states[-3:] == ["DISPATCHED", "FAILED", "DEAD_LETTER"]


async def test_replayed_dispatch_goes_to_dispatched_not_generating() -> None:
    world = await build_dispatch_world(job_state=JobState.RETRY_PENDING)
    consumer, _ = _consumer(world)

    await consumer._handle_message(_delivery(world))

    states = [
        e.state_to for e in await world.store.jobs.list_events_for_job(world.org_id, world.job_id)
    ]
    assert JobState.GENERATING.value not in states
    assert states[-2:] == ["DISPATCHED", "COMPLETED"]


async def test_send_reply_delivery_acks_once_after_completion() -> None:
    world = await build_dispatch_world(mode=DispatchMode.SEND_REPLY)
    consumer, publisher = _consumer(world)
    message = _delivery(world)

    await consumer._handle_message(message)

    cast(AsyncMock, message.ack).assert_awaited_once()
    cast(AsyncMock, publisher.publish_to_retry).assert_not_awaited()
    assert world.fake.calls["send_draft"] == 1
    assert await _job_state(world) == JobState.COMPLETED.value


async def test_busy_job_defers_the_delivery_without_an_attempt_or_a_transition() -> None:
    """R19.3: a second delivery of a job being dispatched is re-queued, not processed."""
    world = await build_dispatch_world(mode=DispatchMode.SEND_REPLY)
    consumer, publisher = _consumer(world)
    message = _delivery(world, attempt=1)

    async with world.store.job_lock(world.org_id, world.job_id) as held:
        assert held
        await consumer._handle_message(message)

    call = cast(AsyncMock, publisher.publish_to_retry).await_args
    assert call is not None
    assert call.kwargs["tier_delay_s"] == LADDER.tier_1_delay_s
    assert call.args[0].attempt == 1  # no attempt used
    assert call.kwargs["origin_routing_key"] == "email.dispatch"
    cast(AsyncMock, message.ack).assert_awaited_once()
    cast(AsyncMock, publisher.publish_to_dead_letter).assert_not_awaited()
    assert world.fake.calls["create_draft"] == 0
    assert await _job_state(world) == JobState.DRAFTED.value


async def test_failure_before_the_claim_routes_the_job_to_email_dispatch() -> None:
    """R18.7: a dispatch that dead-letters before the claim still replays to this worker."""
    world = await build_dispatch_world()
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    job.queue_name = "email.triage"  # what the generation pipeline left there
    world.service.dispatch = AsyncMock(  # type: ignore[method-assign]
        side_effect=DispatchKeyConflictError("key taken by another draft")
    )
    consumer, publisher = _consumer(world)

    await consumer._handle_message(_delivery(world))

    cast(AsyncMock, publisher.publish_to_dead_letter).assert_awaited_once()
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    assert job.state == JobState.DEAD_LETTER.value
    assert job.queue_name == "email.dispatch"

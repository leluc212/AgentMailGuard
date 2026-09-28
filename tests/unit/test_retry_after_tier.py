"""Retry-After picks the first ladder tier >= its value, capped at the last (task 6.6; R17.5)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from packages.broker.backoff import resolve_retry_after_tier_delay
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.retry import handle_job_transient_failure
from packages.core.settings import BrokerSettings, RetryLadderSettings

LADDER = RetryLadderSettings(tier_1_delay_s=30, tier_2_delay_s=300, tier_3_delay_s=1800)


@pytest.mark.parametrize(
    ("retry_after", "tier"),
    [
        (-5.0, 30),
        (0.0, 30),
        (1.0, 30),
        (30.0, 30),
        (30.2, 300),
        (299.0, 300),
        (300.0, 300),
        (301.0, 1800),
        (1800.0, 1800),
        (86_400.0, 1800),
    ],
)
def test_retry_after_maps_to_first_tier_at_least_as_long(retry_after: float, tier: int) -> None:
    assert resolve_retry_after_tier_delay(retry_after, LADDER) == tier


def _publisher() -> MessagePublisher:
    publisher = MagicMock(spec=MessagePublisher)
    publisher.settings = BrokerSettings()
    publisher.publish_to_retry = AsyncMock()
    return cast(MessagePublisher, publisher)


def _envelope(attempt: int) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(uuid4()),
        idempotency_key="dispatch-key",
        job_type="dispatch",
        organization_id=str(uuid4()),
        attempt=attempt,
    )


async def _published_delay(retry_after_s: float | None, attempt: int = 0) -> int:
    publisher = _publisher()
    await handle_job_transient_failure(
        envelope=_envelope(attempt),
        exception=RuntimeError("429 Too Many Requests"),
        publisher=publisher,
        retry_settings=LADDER,
        origin_exchange="email.dispatch",
        origin_routing_key="email.dispatch",
        queue_name="email.dispatch",
        retry_after_s=retry_after_s,
    )
    call = cast(AsyncMock, publisher.publish_to_retry).await_args
    assert call is not None
    delay: int = call.kwargs["tier_delay_s"]
    return delay


async def test_transient_failure_uses_the_retry_after_tier() -> None:
    assert await _published_delay(45.0) == 300
    assert await _published_delay(45.0, attempt=2) == 300  # Retry-After wins over the attempt


async def test_transient_failure_without_retry_after_keeps_the_attempt_ladder() -> None:
    assert await _published_delay(None) == 30
    assert await _published_delay(None, attempt=1) == 300

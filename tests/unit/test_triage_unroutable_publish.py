"""An actionable triage result whose route has no bound queue must dead-letter, not retry.

Final-review finding (R3.1, Review Focus 2): the gate commits CLASSIFIED -> QUEUED before the
route publish, so retrying an unroutable publish only re-runs the cascade and then fails on an
illegal QUEUED transition with a misleading reason. It must fail fatally, naming the key.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from aio_pika.exceptions import PublishError
from pamqp.commands import Basic

from packages.broker.consumer import FatalError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.gate import EarlyExitGate


class _UnroutablePublisher(MessagePublisher):
    """Publisher whose broker returns every mandatory publish as unroutable."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.attempts.append(routing_key)
        returned = MagicMock()
        returned.delivery = Basic.Return(
            reply_code=312, reply_text="NO_ROUTE", exchange=exchange_name, routing_key=routing_key
        )
        raise PublishError(returned, MagicMock())


def _billing_envelope() -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"idem-unroutable-{uuid4()}",
        job_type="triage",
        organization_id=str(uuid4()),
        message_id=str(uuid4()),
        payload={
            "subject": "Urgent: Overdue payment failure on account",
            "body_text": "Your account balance is past due with repeated payment failure.",
            "sender_email": "client@enterprise.com",
        },
    )


async def test_unroutable_route_publish_is_fatal_and_names_routing_key() -> None:
    publisher = _UnroutablePublisher()
    consumer = TriageConsumer(
        cascade=CascadingTriageEngine(), gate=EarlyExitGate(), publisher=publisher
    )

    with pytest.raises(FatalError) as excinfo:
        await consumer.process_job(_billing_envelope(), MagicMock())

    assert publisher.attempts, "the actionable result must attempt the route publish"
    routing_key = publisher.attempts[0]
    assert routing_key.startswith("email.billing.")
    assert routing_key in str(excinfo.value)
    assert not consumer.is_transient_error(excinfo.value)

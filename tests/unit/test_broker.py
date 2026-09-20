"""Unit tests for broker messaging abstractions (R3.1, R3.3, R3.4, R3.5, R7.3)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aio_pika

from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings, RetryLadderSettings


class DummyConsumer(BaseConsumer):
    """Concrete dummy consumer for unit testing base class helpers."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(queue_name="test.queue", **kwargs)
        self.jobs_processed: list[str] = []

    async def process_job(
        self, envelope: JobEnvelope, raw_message: aio_pika.abc.AbstractIncomingMessage
    ) -> None:
        self.jobs_processed.append(envelope.job_id)


def test_job_envelope_defaults_and_serialization() -> None:
    """Verify JobEnvelope defaults, JSON serialization, and message formatting (R3.1, R7.3)."""
    org_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    thd_id = str(uuid.uuid4())

    envelope = JobEnvelope(
        idempotency_key=f"{org_id}:{msg_id}:triage",
        job_type="triage",
        organization_id=org_id,
        message_id=msg_id,
        thread_id=thd_id,
        classification={"category": "billing", "confidence": 0.95},
    )

    assert envelope.job_id is not None
    assert envelope.trace_id is not None
    assert envelope.attempt == 0
    assert envelope.classification["category"] == "billing"

    # Convert to AMQP message
    custom_headers = {"x-test-header": "test-val"}
    amqp_msg = envelope.to_message(headers=custom_headers)

    assert amqp_msg.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert amqp_msg.content_type == "application/json"
    assert amqp_msg.correlation_id == envelope.trace_id
    assert amqp_msg.message_id == envelope.job_id
    assert amqp_msg.headers["x-test-header"] == "test-val"
    assert amqp_msg.headers["trace_id"] == envelope.trace_id
    assert amqp_msg.headers["organization_id"] == org_id

    # Verify JSON round-trip
    payload = json.loads(amqp_msg.body.decode("utf-8"))
    assert payload["job_id"] == envelope.job_id
    assert payload["organization_id"] == org_id
    assert payload["classification"]["confidence"] == 0.95


def test_job_envelope_from_message(mocker: Any) -> None:
    """Verify deserialization from an incoming AMQP message."""
    mock_msg = mocker.MagicMock(spec=aio_pika.abc.AbstractIncomingMessage)
    raw_data = {
        "job_id": "11111111-1111-1111-1111-111111111111",
        "idempotency_key": "test:key",
        "job_type": "normalize",
        "organization_id": "22222222-2222-2222-2222-222222222222",
        "message_id": "33333333-3333-3333-3333-333333333333",
        "thread_id": "44444444-4444-4444-4444-444444444444",
        "trace_id": "abcdef123456",
        "attempt": 2,
        "classification": {"category": "support"},
        "enqueued_at": datetime.now(UTC).isoformat(),
    }
    mock_msg.body = json.dumps(raw_data).encode("utf-8")

    envelope = JobEnvelope.from_message(mock_msg)
    assert envelope.job_id == "11111111-1111-1111-1111-111111111111"
    assert envelope.attempt == 2
    assert envelope.job_type == "normalize"
    assert envelope.classification["category"] == "support"


def test_consumer_retry_delay_calculation() -> None:
    """Verify delay progression across retry tiers (R3.4, R19.5)."""
    retry_cfg = RetryLadderSettings(
        tier_1_delay_s=30,
        tier_2_delay_s=300,
        tier_3_delay_s=1800,
        max_retries=3,
    )
    consumer = DummyConsumer(retry_settings=retry_cfg)

    assert consumer.get_retry_delay_s(attempt=1) == 30
    assert consumer.get_retry_delay_s(attempt=2) == 300
    assert consumer.get_retry_delay_s(attempt=3) == 1800
    assert consumer.get_retry_delay_s(attempt=4) == 1800


def test_consumer_error_classification() -> None:
    """Verify TransientError qualifies for retry whereas FatalError qualifies for DLX (R3.5)."""
    consumer = DummyConsumer()

    assert consumer.is_transient_error(TransientError("Connection timed out")) is True
    assert consumer.is_transient_error(RuntimeError("Unexpected glitch")) is True
    assert consumer.is_transient_error(FatalError("Schema violation")) is False


def test_publisher_initialization() -> None:
    """Verify publisher initializes with default settings."""
    b_cfg = BrokerSettings(host="broker.example.com", port=5672)
    pub = MessagePublisher(broker_settings=b_cfg)
    assert pub.settings.host == "broker.example.com"
    assert "amqp://" in pub.settings.url


def test_job_envelope_sync_mailbox_defaults() -> None:
    """Verify JobEnvelope handles mailbox-level sync jobs without message UUIDs (R2.1, R7.3)."""
    env = JobEnvelope(
        idempotency_key="org1:mbx1:sync:123",
        job_type="sync_mailbox",
        organization_id="11111111-1111-1111-1111-111111111111",
        mailbox_id="mbx-12345",
        payload={"provider": "graph", "change_type": "created"},
    )
    assert env.job_type == "sync_mailbox"
    assert env.mailbox_id == "mbx-12345"
    assert env.message_id == ""
    assert env.thread_id == ""
    assert env.payload["provider"] == "graph"

    msg = env.to_message()
    assert msg.headers["mailbox_id"] == "mbx-12345"
    assert msg.headers["organization_id"] == "11111111-1111-1111-1111-111111111111"

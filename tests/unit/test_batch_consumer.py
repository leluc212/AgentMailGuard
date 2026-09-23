"""Unit tests for worker-level micro-batching and prompt isolation (R3.6, R3.7).

Verifies that workers pull N jobs together to amortize operations (R3.6), each email
receives strictly isolated prompt and inference execution (R3.7), and single-job
failures do not compromise sibling jobs in the batch.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.batch_consumer import BaseBatchConsumer, BatchItem
from packages.broker.consumer import FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.prompt_safety import (
    PromptContaminationError,
    PromptExecutionRecord,
    assert_prompt_isolation,
)
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import ChatMessage


def assert_message_acked(msg: AbstractIncomingMessage) -> None:
    """Type-safe assertion verifying that a mock message was acknowledged."""
    cast(AsyncMock, msg.ack).assert_awaited_once()


def make_mock_amqp_message(
    envelope: JobEnvelope | None = None,
    raw_body: bytes | None = None,
) -> AbstractIncomingMessage:
    """Create a mock AMQP message for unit testing."""
    msg = MagicMock(spec=AbstractIncomingMessage)
    if raw_body is not None:
        msg.body = raw_body
    elif envelope is not None:
        msg.body = envelope.model_dump_json().encode("utf-8")
    else:
        msg.body = b"{}"

    msg.exchange = "test.exchange"
    msg.routing_key = "test.queue"
    msg.headers = {}
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    msg.reject = AsyncMock()
    return msg


class DummyBatchConsumer(BaseBatchConsumer):
    """Test concrete implementation of BaseBatchConsumer."""

    def __init__(
        self,
        batch_size: int = 5,
        batch_timeout_s: float = 0.05,
        publisher: MessagePublisher | None = None,
        job_handler: Any = None,
    ) -> None:
        super().__init__(
            queue_name="test.batch.queue",
            broker_settings=BrokerSettings(),
            retry_settings=RetryLadderSettings(),
            prefetch_count=10,
            batch_size=batch_size,
            batch_timeout_s=batch_timeout_s,
        )
        self.batches_received: list[list[BatchItem]] = []
        self.jobs_processed: list[str] = []
        self._custom_job_handler = job_handler
        if publisher:
            self._publisher = publisher

    async def pre_batch_hook(self, items: list[BatchItem]) -> None:
        self.batches_received.append(list(items))

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        self.jobs_processed.append(envelope.job_id)
        if self._custom_job_handler:
            await self._custom_job_handler(envelope, raw_message)


class TestPromptIsolationContract:
    """Explicit tests asserting no prompt ever contains two distinct emails (R3.7)."""

    def test_clean_prompts_pass_isolation_assertion(self) -> None:
        """When each prompt contains only its own email, isolation assertion succeeds."""
        records = [
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-1",
                    "message_id": "msg-111",
                    "subject": "Alpha release schedule",
                    "sender_email": "alpha@enterprise.com",
                    "body_text": "Please provide alpha timeline and deliverable milestones.",
                },
                messages="User asking about Alpha release schedule timeline.",
            ),
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-2",
                    "message_id": "msg-222",
                    "subject": "Invoice dispute INV-2026-9999",
                    "sender_email": "finance@client.org",
                    "body_text": "Disputing charge on invoice INV-2026-9999 immediately.",
                },
                messages="Drafting billing resolution for invoice dispute INV-2026-9999.",
            ),
        ]
        # Should not raise
        assert_prompt_isolation(records)

    def test_cross_contamination_detected_on_foreign_message_id(self) -> None:
        """Contamination error raised if prompt contains sibling email's message_id."""
        records = [
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-1",
                    "message_id": "msg-111-secret",
                    "subject": "Alpha",
                    "sender_email": "alpha@test.com",
                },
                messages="Normal prompt for alpha.",
            ),
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-2",
                    "message_id": "msg-222-leaked",
                    "subject": "Beta",
                    "sender_email": "beta@test.com",
                },
                # Contamination: prompt 2 mentions message ID from email 1
                messages="Prompt for beta with ref to msg-111-secret",
            ),
        ]
        with pytest.raises(PromptContaminationError, match="contains foreign message ID"):
            assert_prompt_isolation(records)

    def test_cross_contamination_detected_on_foreign_subject(self) -> None:
        """Contamination error raised if prompt contains sibling email's subject."""
        records = [
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-1",
                    "message_id": "msg-1",
                    "subject": "Urgent Server Breach In Progress",
                    "sender_email": "alice@corp.com",
                },
                messages="Prompt 1 for breach.",
            ),
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-2",
                    "message_id": "msg-2",
                    "subject": "Routine Newsletter",
                    "sender_email": "bob@corp.com",
                },
                # Contamination: prompt 2 mentions subject from email 1
                messages="Routine newsletter context with Urgent Server Breach In Progress",
            ),
        ]
        with pytest.raises(PromptContaminationError, match="contains foreign subject"):
            assert_prompt_isolation(records)

    def test_cross_contamination_detected_on_foreign_sender(self) -> None:
        """Contamination error raised if prompt contains sibling email's sender."""
        records = [
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-1",
                    "message_id": "msg-1",
                    "subject": "Sub 1",
                    "sender_email": "vip_ceo@boardroom.com",
                },
                messages="Prompt 1.",
            ),
            PromptExecutionRecord.from_call(
                envelope_or_context={
                    "job_id": "job-2",
                    "message_id": "msg-2",
                    "subject": "Sub 2",
                    "sender_email": "intern@desk.com",
                },
                # Contamination: prompt 2 mentions VIP CEO address from email 1
                messages="Prompt 2 replying to intern referencing vip_ceo@boardroom.com",
            ),
        ]
        with pytest.raises(PromptContaminationError, match="contains foreign sender"):
            assert_prompt_isolation(records)


class TestBatchAggregationAndExecution:
    """Validate micro-batching size, timeout, and independent inference (R3.6, R3.7)."""

    @pytest.mark.asyncio
    async def test_micro_batch_accumulates_up_to_batch_size(self) -> None:
        """When N messages arrive, they are assembled into a single micro-batch of size N (R3.6)."""
        consumer = DummyBatchConsumer(batch_size=5, batch_timeout_s=0.1)
        consumer._is_consuming = True

        # Enqueue 5 messages into inbound queue
        envelopes = [
            JobEnvelope(
                job_id=f"job-{i}",
                idempotency_key=f"idem-{i}",
                job_type="generate_reply",
                organization_id=str(uuid4()),
                message_id=f"msg-{i}",
            )
            for i in range(5)
        ]
        for env in envelopes:
            msg = make_mock_amqp_message(env)
            await consumer._on_message_delivered(msg)

        # Run loop for one batch iteration
        task = asyncio.create_task(consumer._batch_worker_loop())
        await asyncio.sleep(0.05)
        consumer._is_consuming = False
        await task

        # Assert exactly 1 batch was formed containing all 5 jobs (R3.6)
        assert len(consumer.batches_received) == 1
        assert len(consumer.batches_received[0]) == 5
        assert consumer.jobs_processed == [f"job-{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_micro_batch_flushes_on_timeout(self) -> None:
        """When fewer than N messages arrive, batch flushes when timeout expires (R3.6)."""
        consumer = DummyBatchConsumer(batch_size=5, batch_timeout_s=0.03)
        consumer._is_consuming = True

        # Enqueue only 2 messages (less than batch_size=5)
        for i in range(2):
            env = JobEnvelope(
                job_id=f"partial-{i}",
                idempotency_key=f"idem-{i}",
                job_type="generate_reply",
                organization_id=str(uuid4()),
            )
            msg = make_mock_amqp_message(env)
            await consumer._on_message_delivered(msg)

        task = asyncio.create_task(consumer._batch_worker_loop())
        # Wait slightly longer than batch_timeout_s (30ms)
        await asyncio.sleep(0.06)
        consumer._is_consuming = False
        await task

        # Assert 1 batch was flushed with the 2 available messages
        assert len(consumer.batches_received) == 1
        assert len(consumer.batches_received[0]) == 2
        assert consumer.jobs_processed == ["partial-0", "partial-1"]

    @pytest.mark.asyncio
    async def test_independent_inference_per_job_in_micro_batch(self) -> None:
        """Every email job in micro-batch gets its own LLM call with isolated prompt (R3.7)."""
        llm = FakeLLMProvider()
        recorded_prompts: list[PromptExecutionRecord] = []

        async def inference_handler(
            envelope: JobEnvelope,
            raw_msg: AbstractIncomingMessage,
        ) -> None:
            # Simulate per-job prompt assembly (never combining distinct emails)
            payload = envelope.payload or {}
            prompt_content = (
                f"Respond to email {envelope.message_id} from {payload.get('sender_email')}: "
                f"Subject: {payload.get('subject')}\nBody: {payload.get('body_text')}"
            )
            messages = [
                ChatMessage(role="system", content="You are an enterprise email assistant."),
                ChatMessage(role="user", content=prompt_content),
            ]
            await llm.generate(messages=messages)
            recorded_prompts.append(PromptExecutionRecord.from_call(envelope, messages))

        consumer = DummyBatchConsumer(
            batch_size=4,
            batch_timeout_s=0.1,
            job_handler=inference_handler,
        )
        consumer._is_consuming = True

        # Feed 4 distinct emails with unique contexts
        email_fixtures = [
            (
                "alpha-1",
                "Security Audit Inquiry",
                "sec@firm.com",
                "Requesting SOC2 compliance report.",
            ),
            (
                "beta-2",
                "Invoice Dispute Q3",
                "billing@client.com",
                "Overdue charge discrepancy.",
            ),
            ("gamma-3", "Feature Request", "user@saas.io", "Need export to CSV capability."),
            ("delta-4", "Meeting Reschedule", "exec@partner.org", "Move Thursday sync to 3pm."),
        ]

        for msg_id, sub, sender, body in email_fixtures:
            env = JobEnvelope(
                job_id=f"job-{msg_id}",
                idempotency_key=f"idem-{msg_id}",
                job_type="generate_reply",
                organization_id=str(uuid4()),
                message_id=msg_id,
                payload={"subject": sub, "sender_email": sender, "body_text": body},
            )
            await consumer._on_message_delivered(make_mock_amqp_message(env))

        task = asyncio.create_task(consumer._batch_worker_loop())
        await asyncio.sleep(0.08)
        consumer._is_consuming = False
        await task

        # Assert exactly 4 independent inference calls made (1 per email, R3.7)
        assert len(llm.recorded_calls) == 4
        assert len(recorded_prompts) == 4

        # Exhaustive prompt isolation verification: NO prompt contains two distinct emails!
        assert_prompt_isolation(recorded_prompts)

        # Pairwise verification across all 4 prompts
        for i, record_i in enumerate(recorded_prompts):
            for j, record_j in enumerate(recorded_prompts):
                if i != j:
                    assert record_j.message_id not in record_i.prompt_text
                    assert record_j.subject not in record_i.prompt_text
                    assert record_j.sender_email not in record_i.prompt_text


class TestBatchFaultIsolation:
    """Verify that failures in individual jobs do not fail or duplicate sibling jobs."""

    @pytest.mark.asyncio
    async def test_transient_failure_retries_single_job_without_failing_siblings(self) -> None:
        """A transient error on job 2 routes to retry ladder; jobs 1, 3, 4, 5 succeed and ACK."""
        mock_publisher = MagicMock(spec=MessagePublisher)
        mock_publisher.publish_to_retry = AsyncMock()
        mock_publisher.publish_to_dead_letter = AsyncMock()

        async def partial_fail_handler(
            envelope: JobEnvelope,
            raw_msg: AbstractIncomingMessage,
        ) -> None:
            if envelope.job_id == "job-fail":
                raise TransientError("Downstream LLM gateway timed out")

        consumer = DummyBatchConsumer(
            batch_size=5,
            batch_timeout_s=0.1,
            publisher=mock_publisher,
            job_handler=partial_fail_handler,
        )
        consumer._is_consuming = True

        messages: list[AbstractIncomingMessage] = []
        for i in range(5):
            job_id = "job-fail" if i == 2 else f"job-ok-{i}"
            env = JobEnvelope(
                job_id=job_id,
                idempotency_key=f"idem-{i}",
                job_type="generate_reply",
                organization_id=str(uuid4()),
                message_id=f"msg-{i}",
                attempt=0,
            )
            msg = make_mock_amqp_message(env)
            messages.append(msg)
            await consumer._on_message_delivered(msg)

        task = asyncio.create_task(consumer._batch_worker_loop())
        await asyncio.sleep(0.08)
        consumer._is_consuming = False
        await task

        # Assert all 5 messages were individually acknowledged (R3.3)
        for msg in messages:
            assert_message_acked(msg)

        # Assert only the failed job was routed to the retry ladder
        mock_publisher.publish_to_retry.assert_awaited_once()
        retry_call = mock_publisher.publish_to_retry.call_args[1]
        assert retry_call["envelope"].job_id == "job-fail"
        assert retry_call["envelope"].attempt == 1

        # Assert no jobs were sent to dead-letter
        mock_publisher.publish_to_dead_letter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fatal_failure_routes_to_dlq_without_failing_siblings(self) -> None:
        """A fatal error on job 1 dead-letters job 1; job 2 succeeds and ACKs."""
        mock_publisher = MagicMock(spec=MessagePublisher)
        mock_publisher.publish_to_dead_letter = AsyncMock()

        async def fatal_handler(
            envelope: JobEnvelope,
            raw_msg: AbstractIncomingMessage,
        ) -> None:
            if envelope.job_id == "job-corrupt":
                raise FatalError("Corrupt payload schema")

        consumer = DummyBatchConsumer(
            batch_size=2,
            batch_timeout_s=0.1,
            publisher=mock_publisher,
            job_handler=fatal_handler,
        )
        consumer._is_consuming = True

        env_corrupt = JobEnvelope(
            job_id="job-corrupt",
            idempotency_key="idem-corrupt",
            job_type="generate_reply",
            organization_id=str(uuid4()),
        )
        msg_corrupt = make_mock_amqp_message(env_corrupt)

        env_ok = JobEnvelope(
            job_id="job-ok",
            idempotency_key="idem-ok",
            job_type="generate_reply",
            organization_id=str(uuid4()),
        )
        msg_ok = make_mock_amqp_message(env_ok)

        await consumer._on_message_delivered(msg_corrupt)
        await consumer._on_message_delivered(msg_ok)

        task = asyncio.create_task(consumer._batch_worker_loop())
        await asyncio.sleep(0.08)
        consumer._is_consuming = False
        await task

        # Both messages ACKed to prevent blocking prefetch
        assert_message_acked(msg_corrupt)
        assert_message_acked(msg_ok)

        # Fatal job routed to DLQ
        mock_publisher.publish_to_dead_letter.assert_awaited_once()
        dlq_call = mock_publisher.publish_to_dead_letter.call_args[1]
        assert dlq_call["envelope"].job_id == "job-corrupt"
        assert "FatalError: Corrupt payload schema" in dlq_call["failure_reason"]

    @pytest.mark.asyncio
    async def test_malformed_envelope_dead_lettered_immediately(self) -> None:
        """Invalid JSON message is dead-lettered and ACKed, valid items proceed."""
        mock_publisher = MagicMock(spec=MessagePublisher)
        mock_publisher.publish_to_dead_letter = AsyncMock()

        consumer = DummyBatchConsumer(
            batch_size=2,
            batch_timeout_s=0.1,
            publisher=mock_publisher,
        )
        consumer._is_consuming = True

        msg_malformed = make_mock_amqp_message(raw_body=b"NOT VALID JSON")
        env_valid = JobEnvelope(
            job_id="job-valid",
            idempotency_key="idem-valid",
            job_type="generate_reply",
            organization_id=str(uuid4()),
        )
        msg_valid = make_mock_amqp_message(env_valid)

        await consumer._on_message_delivered(msg_malformed)
        await consumer._on_message_delivered(msg_valid)

        task = asyncio.create_task(consumer._batch_worker_loop())
        await asyncio.sleep(0.08)
        consumer._is_consuming = False
        await task

        # Both ACKed so broker does not redeliver unparseable payload
        assert_message_acked(msg_malformed)
        assert_message_acked(msg_valid)

        # Malformed message routed to DLQ
        mock_publisher.publish_to_dead_letter.assert_awaited_once()
        dlq_call = mock_publisher.publish_to_dead_letter.call_args[1]
        assert "EnvelopeParseError" in dlq_call["failure_reason"]
        assert consumer.jobs_processed == ["job-valid"]

    @pytest.mark.asyncio
    async def test_batch_consumer_stop_drains_inbound_queue(self) -> None:
        """Calling stop() gracefully processes remaining items in buffer."""
        consumer = DummyBatchConsumer(batch_size=3, batch_timeout_s=0.2)
        consumer._is_consuming = True

        env = JobEnvelope(
            job_id="job-drain",
            idempotency_key="idem-drain",
            job_type="generate_reply",
            organization_id=str(uuid4()),
        )
        msg = make_mock_amqp_message(env)
        await consumer._on_message_delivered(msg)

        task = asyncio.create_task(consumer._batch_worker_loop())
        consumer._batch_loop_task = task

        # Stop consumer immediately
        await consumer.stop()

        # Item was processed during drain
        assert "job-drain" in consumer.jobs_processed
        assert_message_acked(msg)

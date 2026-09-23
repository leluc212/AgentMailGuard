"""Integration tests for category-aware routing, dynamic category queues, and alerting.

Fulfills requirements:
- R7.1: Publish to email.<category>.<priority> topic exchange.
- R7.2: Normal and priority lanes with independent queue isolation.
- R7.4: Dynamic declarative category creation on startup without code changes.
- R7.6: Log startup warning naming any category queue with no configured consumer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.routing import format_routing_key
from packages.broker.topology import setup_topology
from packages.core.settings import (
    BrokerSettings,
    CategoryRoutingSettings,
)
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.gate import EarlyExitGate

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    """Provide a dedicated robust connection and channel for testing."""
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await conn.channel()
    yield channel
    if not channel.is_closed:
        await channel.close()
    if not conn.is_closed:
        await conn.close()


@pytest.mark.asyncio
async def test_setup_topology_declares_category_queues_and_bindings(
    broker_channel: AbstractChannel,
) -> None:
    """Verify setup_topology declares category and priority queues on email.route (R7.1, R7.2)."""
    b_cfg = BrokerSettings()
    rt_cfg = CategoryRoutingSettings()

    topo = await setup_topology(broker_channel, broker_settings=b_cfg, routing_settings=rt_cfg)

    # 1. Category queues must be present in topology
    assert len(topo.category_queues) >= 18  # 9 canonical categories * 2 lanes

    expected_category_queues = [
        "email.support.normal",
        "email.support.priority",
        "email.billing.normal",
        "email.billing.priority",
        "email.sales.normal",
        "email.sales.priority",
        "email.general_inquiry.normal",
        "email.general_inquiry.priority",
        "email.administration.normal",
        "email.administration.priority",
        "email.scheduling.normal",
        "email.scheduling.priority",
    ]
    for q_name in expected_category_queues:
        assert q_name in topo.category_queues
        assert q_name in topo.queues

        # Assert queue exists on RabbitMQ channel passively
        q = await broker_channel.get_queue(q_name)
        assert q is not None


@pytest.mark.asyncio
async def test_category_topic_exchange_message_routing(
    broker_channel: AbstractChannel,
) -> None:
    """Publish actionable messages to email.route and verify exact queue delivery (R7.1–R7.3)."""
    b_cfg = BrokerSettings()
    await setup_topology(broker_channel, broker_settings=b_cfg)

    publisher = MessagePublisher(broker_settings=b_cfg, channel=broker_channel)
    org_id = str(uuid4())
    msg_id = str(uuid4())

    # 1. Publish Billing Priority job
    billing_envelope = JobEnvelope(
        idempotency_key=f"idem-bill-{uuid4()}",
        job_type="generate_reply",
        organization_id=org_id,
        message_id=msg_id,
        classification={
            "category": "billing",
            "priority": "urgent",
            "intent": "payment_failure",
            "reply_required": True,
            "workflow_hint": "ai",
        },
    )
    routing_key_bill = format_routing_key("billing", "urgent")
    assert routing_key_bill == "email.billing.priority"

    await publisher.publish(
        exchange_name=b_cfg.exchange_email_route,
        routing_key=routing_key_bill,
        envelope=billing_envelope,
    )

    # 2. Publish Support Normal job
    support_envelope = JobEnvelope(
        idempotency_key=f"idem-supp-{uuid4()}",
        job_type="generate_reply",
        organization_id=org_id,
        message_id=str(uuid4()),
        classification={
            "category": "support",
            "priority": "normal",
            "intent": "bug_report",
            "reply_required": True,
            "workflow_hint": "ai",
        },
    )
    routing_key_supp = format_routing_key("support", "normal")
    assert routing_key_supp == "email.support.normal"

    await publisher.publish(
        exchange_name=b_cfg.exchange_email_route,
        routing_key=routing_key_supp,
        envelope=support_envelope,
    )

    # 3. Verify Billing Priority message arrives in email.billing.priority
    q_billing_prio = await broker_channel.get_queue("email.billing.priority")
    msg_received = await q_billing_prio.get(timeout=5.0)
    assert msg_received is not None
    await msg_received.ack()

    received_envelope = JobEnvelope.from_message(msg_received)
    assert received_envelope.idempotency_key == billing_envelope.idempotency_key
    assert received_envelope.category == "billing"
    assert received_envelope.priority == "urgent"
    assert received_envelope.classification["intent"] == "payment_failure"

    # 4. Verify Support Normal message arrives in email.support.normal
    q_support_norm = await broker_channel.get_queue("email.support.normal")
    msg_supp = await q_support_norm.get(timeout=5.0)
    assert msg_supp is not None
    await msg_supp.ack()

    received_supp_env = JobEnvelope.from_message(msg_supp)
    assert received_supp_env.idempotency_key == support_envelope.idempotency_key
    assert received_supp_env.category == "support"
    assert received_supp_env.priority == "normal"


@pytest.mark.asyncio
async def test_dynamic_category_declaration_from_custom_yaml(
    broker_channel: AbstractChannel,
) -> None:
    """Verify adding category in config creates queues on startup without code changes (R7.4)."""
    with TemporaryDirectory() as tmpdir:
        custom_yaml = Path(tmpdir) / "categories.yaml"
        custom_yaml.write_text(
            """
categories:
  - category: compliance_audit
    description: "Regulatory compliance and security audit inquiries."
    default_reply_required: true
    default_retrieval_required: true
    default_workflow_hint: ai
    default_priority: high
    intents:
      - soc2_request
      - gdpr_export
            """,
            encoding="utf-8",
        )

        b_cfg = BrokerSettings()
        rt_cfg = CategoryRoutingSettings(
            categories_config_path=str(custom_yaml),
            priority_lanes=["normal", "priority"],
            configured_consumers=["email.compliance_audit.*"],
        )

        topo = await setup_topology(
            broker_channel,
            broker_settings=b_cfg,
            routing_settings=rt_cfg,
        )

        # Assert new queues were declared dynamically
        assert "email.compliance_audit.normal" in topo.category_queues
        assert "email.compliance_audit.priority" in topo.category_queues

        # Assert queues are live on RabbitMQ broker
        q_norm = await broker_channel.get_queue("email.compliance_audit.normal")
        q_prio = await broker_channel.get_queue("email.compliance_audit.priority")
        assert q_norm is not None
        assert q_prio is not None


@pytest.mark.asyncio
async def test_unconsumed_category_queue_startup_warning(
    broker_channel: AbstractChannel,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify warning log emitted naming any category queue with no configured consumer (R7.6)."""
    with TemporaryDirectory() as tmpdir:
        custom_yaml = Path(tmpdir) / "categories.yaml"
        custom_yaml.write_text(
            """
categories:
  - category: unconsumed_service
    description: "Service with no consumer listening."
    default_reply_required: true
    default_retrieval_required: false
    default_workflow_hint: ai
    default_priority: normal
            """,
            encoding="utf-8",
        )

        b_cfg = BrokerSettings()
        # Explicitly configure consumers that do NOT cover unconsumed_service
        rt_cfg = CategoryRoutingSettings(
            categories_config_path=str(custom_yaml),
            priority_lanes=["normal", "priority"],
            configured_consumers=["email.support.*", "email.billing.*"],
        )

        with caplog.at_level(logging.WARNING):
            topo = await setup_topology(
                broker_channel,
                broker_settings=b_cfg,
                routing_settings=rt_cfg,
            )

        # Assert warning was logged for unconsumed queues naming the queue (R7.6)
        assert "email.unconsumed_service.normal" in topo.unconsumed_queues
        assert "email.unconsumed_service.priority" in topo.unconsumed_queues

        warning_messages = [
            record.message for record in caplog.records if record.levelname == "WARNING"
        ]
        assert any(
            "Category queue 'email.unconsumed_service.normal' has no configured consumer (R7.6)"
            in msg
            for msg in warning_messages
        )
        assert any(
            "Category queue 'email.unconsumed_service.priority' has no configured consumer (R7.6)"
            in msg
            for msg in warning_messages
        )


@pytest.mark.asyncio
async def test_triage_consumer_end_to_end_routing(
    broker_channel: AbstractChannel,
) -> None:
    """Verify TriageConsumer routes actionable mail to email.<cat>.<prio> (R7.1)."""
    b_cfg = BrokerSettings()
    await setup_topology(broker_channel, broker_settings=b_cfg)

    publisher = MessagePublisher(broker_settings=b_cfg, channel=broker_channel)
    engine = CascadingTriageEngine()
    gate = EarlyExitGate()

    # Purge queues before test for clean isolation
    q_triage = await broker_channel.get_queue(b_cfg.queue_triage)
    await q_triage.purge()
    q_billing_prio = await broker_channel.get_queue("email.billing.priority")
    await q_billing_prio.purge()
    q_billing_norm = await broker_channel.get_queue("email.billing.normal")
    await q_billing_norm.purge()

    consumer = TriageConsumer(
        cascade=engine,
        gate=gate,
        publisher=publisher,
        broker_settings=b_cfg,
    )
    await consumer.start()

    try:
        org_id = str(uuid4())

        # 1. Publish urgent billing notice (matches urgent-billing rule -> priority lane)
        urgent_envelope = JobEnvelope(
            idempotency_key=f"idem-e2e-urgent-{uuid4()}",
            job_type="triage",
            organization_id=org_id,
            message_id=str(uuid4()),
            payload={
                "subject": "Urgent: Overdue payment failure on account",
                "body_text": (
                    "Your account balance is past due with repeated payment failure. "
                    "Please advise immediately."
                ),
                "sender_email": "client@enterprise.com",
            },
        )
        await publisher.publish(
            exchange_name=b_cfg.exchange_email_triage,
            routing_key=b_cfg.queue_triage,
            envelope=urgent_envelope,
        )

        # 2. Publish standard invoice inquiry (matches invoice-reference rule -> normal lane)
        normal_envelope = JobEnvelope(
            idempotency_key=f"idem-e2e-normal-{uuid4()}",
            job_type="triage",
            organization_id=org_id,
            message_id=str(uuid4()),
            payload={
                "subject": "Inquiry regarding invoice INV-2026-00042",
                "body_text": "Please find invoice INV-2026-00042 for review.",
                "sender_email": "client@enterprise.com",
            },
        )
        await publisher.publish(
            exchange_name=b_cfg.exchange_email_triage,
            routing_key=b_cfg.queue_triage,
            envelope=normal_envelope,
        )

        # Poll email.billing.priority queue for urgent message
        routed_prio_msg = None
        for _ in range(50):
            try:
                routed_prio_msg = await q_billing_prio.get(timeout=0.2)
                if routed_prio_msg is not None:
                    break
            except Exception:
                await asyncio.sleep(0.1)

        assert routed_prio_msg is not None, "Expected message in email.billing.priority"
        await routed_prio_msg.ack()

        routed_prio_env = JobEnvelope.from_message(routed_prio_msg)
        assert routed_prio_env.job_type == "generate_reply"
        assert routed_prio_env.category == "billing"
        assert routed_prio_env.priority == "urgent"
        assert routed_prio_env.reply_required is True

        # Poll email.billing.normal queue for normal message
        routed_norm_msg = None
        for _ in range(50):
            try:
                routed_norm_msg = await q_billing_norm.get(timeout=0.2)
                if routed_norm_msg is not None:
                    break
            except Exception:
                await asyncio.sleep(0.1)

        assert routed_norm_msg is not None, "Expected message in email.billing.normal"
        await routed_norm_msg.ack()

        routed_norm_env = JobEnvelope.from_message(routed_norm_msg)
        assert routed_norm_env.job_type == "generate_reply"
        assert routed_norm_env.category == "billing"
        assert routed_norm_env.priority == "normal"
        assert routed_norm_env.reply_required is True
    finally:
        await consumer.stop()

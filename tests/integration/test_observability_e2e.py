"""End-to-end integration test for observability over live containers.

Verifies full trace context propagation across RabbitMQ, correlation log binding,
Prometheus metrics scraping, and graceful shutdown (R21.1–R21.4, R20.7, R20.8).
"""

import asyncio
import uuid

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.observability.context import get_correlation_context
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload
from packages.observability.server import ObservabilityServer
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import (
    get_current_trace_id,
    init_tracer,
    trace_span,
)


class ObservabilityTestConsumer(BaseConsumer):
    """Concrete consumer recording received trace IDs and correlation contexts."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.processed_jobs: list[dict[str, str | None]] = []
        self.completion_event: asyncio.Event = asyncio.Event()

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        """Capture active trace_id and correlation context during execution."""
        current_trace = get_current_trace_id()
        current_ctx = get_correlation_context()

        self.processed_jobs.append(
            {
                "envelope_trace_id": envelope.trace_id,
                "active_trace_id": current_trace,
                "context_trace_id": current_ctx.get("trace_id"),
                "context_msg_id": current_ctx.get("message_id"),
                "context_thread_id": current_ctx.get("thread_id"),
                "context_job_id": current_ctx.get("job_id"),
                "context_org_id": current_ctx.get("organization_id"),
            }
        )
        self.completion_event.set()


@pytest.mark.asyncio
async def test_end_to_end_observability_pipeline() -> None:
    """Verify trace context, logging, metrics, and health over live RabbitMQ and PostgreSQL."""
    settings = AppSettings()
    init_tracer("e2e-observability-test")

    # 1. Database readiness check against live PostgreSQL (port 5433)
    db_pool = await create_pool_from_settings(settings.database)
    assert db_pool is not None

    async def check_db() -> tuple[bool, str]:
        try:
            val = await db_pool.fetchval("SELECT 1")
            if val == 1:
                return True, f"postgres ok ({val})"
            return False, "unexpected query result"
        except Exception as e:
            return False, str(e)

    # 2. Broker setup against live RabbitMQ (port 5672)
    rmq_conn = await aio_pika.connect_robust(settings.broker.url)
    channel = await rmq_conn.channel()
    await setup_topology(channel, settings.broker, settings.retry)

    async def check_broker() -> tuple[bool, str]:
        return not rmq_conn.is_closed, "rabbitmq connection active"

    # 3. Observability & Health Registry
    health_reg = HealthRegistry("e2e-worker")
    health_reg.register_readiness_check("database", check_db)
    health_reg.register_readiness_check("broker", check_broker)

    test_metrics = create_pipeline_metrics()

    # 4. Graceful shutdown coordinator
    coord = GracefulShutdownCoordinator(drain_timeout_s=3.0, health_registry=health_reg)

    # 5. Start background Observability HTTP server on test port 8199
    server = ObservabilityServer(
        host="127.0.0.1",
        port=8199,
        health_registry=health_reg,
        metrics_registry=test_metrics.registry,
    )
    await server.start()

    # 6. Initialize consumer attached to shutdown coordinator and queue
    consumer = ObservabilityTestConsumer(
        queue_name=settings.broker.queue_normalize,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        connection=rmq_conn,
        shutdown_coordinator=coord,
    )
    await consumer.start()

    publisher = MessagePublisher(
        broker_settings=settings.broker,
        connection=rmq_conn,
        channel=channel,
    )

    try:
        # Verify /healthz and /readyz report healthy over HTTP
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8199") as client:
            h_res = await client.get("/healthz")
            assert h_res.status_code == 200
            assert h_res.json()["status"] == "ok"

            r_res = await client.get("/readyz")
            assert r_res.status_code == 200
            ready_data = r_res.json()
            assert ready_data["status"] == "ok"
            assert "postgres ok" in ready_data["checks"]["database"]
            assert "rabbitmq connection active" in ready_data["checks"]["broker"]

        # 7. Publish job within an active root trace span
        org_id = str(uuid.uuid4())
        msg_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())

        with trace_span("mail_connector.sync") as root_span:
            expected_trace_hex = f"{root_span.get_span_context().trace_id:032x}"

            envelope = JobEnvelope(
                job_id=job_id,
                idempotency_key=f"idem-{job_id}",
                job_type="normalize",
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                trace_id=expected_trace_hex,
            )

            await publisher.publish(
                exchange_name=settings.broker.exchange_email_process,
                routing_key="email.normalize",
                envelope=envelope,
            )

        # 8. Wait for consumer to process message
        await asyncio.wait_for(consumer.completion_event.wait(), timeout=5.0)

        assert len(consumer.processed_jobs) == 1
        record = consumer.processed_jobs[0]

        # Verify trace context propagation across AMQP hop (R21.1)
        assert record["envelope_trace_id"] == expected_trace_hex
        assert record["active_trace_id"] == expected_trace_hex
        assert record["context_trace_id"] == expected_trace_hex

        # Verify correlation context populated (R21.3)
        assert record["context_msg_id"] == msg_id
        assert record["context_thread_id"] == thread_id
        assert record["context_job_id"] == job_id
        assert record["context_org_id"] == org_id

        # 9. Verify Prometheus /metrics payload
        test_metrics.emails_classified_total.labels(
            organization=org_id,
            category="billing",
            priority="urgent",
            decided_by="rules",
        ).inc()

        payload_bytes, content_type = generate_metrics_payload(test_metrics.registry)
        assert b"emails_classified_total" in payload_bytes
        assert b"billing" in payload_bytes

        # 10. Verify graceful shutdown draining (R20.8)
        await coord.trigger_shutdown("E2E_TEST")

        async with httpx.AsyncClient(base_url="http://127.0.0.1:8199") as client:
            draining_res = await client.get("/readyz")
            assert draining_res.status_code == 503
            assert draining_res.json()["status"] == "draining"

    finally:
        await server.stop()
        await consumer.stop()
        if not rmq_conn.is_closed:
            await rmq_conn.close()
        await db_pool.close()

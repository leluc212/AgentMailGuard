"""WorkerRuntime lifecycle against the isolated test DB and a scratch vhost (RA.8)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.cli import declare_from_settings
from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AppSettings
from services.knowledge_worker.main import assert_vector_dimension
from tests.integration.isolation import scratch_vhost


class _RecordingConsumer(BaseConsumer):
    def __init__(self, received: asyncio.Queue[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.received = received

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        await self.received.put(envelope.job_id)


async def test_worker_runtime_declares_topology_serves_health_and_shuts_down(
    unused_tcp_port: int,
) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    async with scratch_vhost(AppSettings().broker, "runtime") as broker:
        settings = AppSettings().model_copy(update={"broker": broker})

        async def build(res: WorkerResources) -> list[StartFn]:
            consumer = _RecordingConsumer(
                received,
                queue_name=broker.queue_normalize,
                broker_settings=broker,
                retry_settings=res.settings.retry,
                connection=res.connection,
                shutdown_coordinator=res.shutdown,
            )
            return [consumer.start]

        runtime = WorkerRuntime(
            service_name="runtime_it",
            settings=settings,
            port=unused_tcp_port,
            host="127.0.0.1",
            build=build,
            install_signal_handlers=False,
            configure_telemetry=False,
        )
        res = await runtime.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{unused_tcp_port}") as http:
                ready = await http.get("/readyz")
                metrics = await http.get("/metrics")
            assert ready.status_code == 200, ready.text
            assert set(ready.json()["checks"]) == {"database", "broker"}
            assert metrics.status_code == 200

            envelope = JobEnvelope(
                idempotency_key=f"rt-{uuid4()}",
                job_type="normalize_email",
                organization_id=str(uuid4()),
            )
            await res.publisher.publish(
                exchange_name=broker.exchange_email_process,
                routing_key=broker.queue_normalize,
                envelope=envelope,
            )
            assert await asyncio.wait_for(received.get(), timeout=5) == envelope.job_id
        finally:
            await runtime.stop("TEST")

    assert res.connection.is_closed
    assert res.db_pool.is_closing()


async def test_worker_runtime_releases_resources_when_build_fails(unused_tcp_port: int) -> None:
    captured: list[WorkerResources] = []
    async with scratch_vhost(AppSettings().broker, "runtimefail") as broker:
        settings = AppSettings().model_copy(update={"broker": broker})

        async def build(res: WorkerResources) -> list[StartFn]:
            captured.append(res)
            raise RuntimeError("bad worker config")

        runtime = WorkerRuntime(
            service_name="runtime_fail_it",
            settings=settings,
            port=unused_tcp_port,
            host="127.0.0.1",
            build=build,
            install_signal_handlers=False,
            configure_telemetry=False,
        )
        with pytest.raises(RuntimeError, match="bad worker config"):
            await runtime.start()

    assert captured[0].connection.is_closed
    assert captured[0].db_pool.is_closing()


async def test_cli_declare_creates_topology() -> None:
    async with scratch_vhost(AppSettings().broker, "cli") as broker:
        await declare_from_settings(AppSettings().model_copy(update={"broker": broker}))
        conn = await aio_pika.connect_robust(broker.url)
        try:
            channel = await conn.channel()
            for queue in (broker.queue_normalize, broker.queue_triage, broker.queue_mail_sync):
                await channel.declare_queue(queue, passive=True)  # raises if missing
        finally:
            await conn.close()


async def test_vector_dimension_check_accepts_match_and_rejects_mismatch() -> None:
    settings = AppSettings()
    await assert_vector_dimension(settings)  # migrations declare VECTOR(1536)
    wrong = settings.model_copy(
        update={"embedding": settings.embedding.model_copy(update={"dimension": 768})}
    )
    with pytest.raises(ValueError, match="Refusing to start"):
        await assert_vector_dimension(wrong)

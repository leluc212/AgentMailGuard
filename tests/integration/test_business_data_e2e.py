"""Phase 5 scenario on a real broker and Postgres: business facts reach the draft (5.6).

The stub LLM cannot state a status or cite a chunk (packages/llm/fake.py FAKE_REPLY), so these
tests prove the context side: the draft prompt the model receives, the CONTEXT_READY replay
payload and the business metrics. The live draft is checked by scripts/phase5_gate.py.

Each test copies the demo tenant's business rows, one fixture email and the order-status
procedure chunk into a fresh organization. The integration database is shared across the
package run, so the fixed ids of packages/db/seed.py are not reused. The procedure document is
filed under the envelope's category, because retrieval filters documents by the classification
category (packages/retrieval/query_builder.py:389).

Requirements: R13.3, R13.4, R13.5, R13.6, R16.1
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.settings import (
    AIWorkerSettings,
    AppSettings,
    BrokerSettings,
    RetryLadderSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDERS,
    BUSINESS_TICKETS,
    CUST_ALICE_ID,
    CUST_DANA_ID,
    CUST_EDWARD_ID,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
    KNOWLEDGE_DOCS,
    EmailFixture,
)
from packages.db.job import PostgresJobStore
from packages.db.seed import deterministic_embed
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.knowledge.embedder import FakeEmbedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.observability.health import HealthRegistry
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.main import build_consumers
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
PROCEDURE_DOC_TITLE = "Acme Order Fulfillment & Tracking Guidelines"
PROCEDURE = next(
    chunk
    for doc in KNOWLEDGE_DOCS
    if doc.org_id == DEMO_ORG_ID and doc.title == PROCEDURE_DOC_TITLE
    for chunk in doc.chunks
    if chunk.chunk_index == 0
)
ALICE_EMAIL = next(
    f
    for f in FIXTURE_EMAILS
    if f.sender_email == "alice.smith@clientcorp.com" and "82915" in f.body_text
)
EDWARD_EMAIL = next(f for f in FIXTURE_EMAILS if f.key == "identifier_order_ticket")
ORDER_82915 = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-82915")
ORDER_9901 = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-9901")
TICKET_4402 = next(t for t in BUSINESS_TICKETS if t["ticket_number"] == "TICK-4402")


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "bizsvc") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


async def _seed_tenant(
    pool: asyncpg.Pool, fixture: EmailFixture, *, category: str
) -> tuple[Job, uuid.UUID]:
    """Copy the demo tenant's business rows, one email and the procedure chunk into a new org.

    Returns the QUEUED job for the email and the procedure chunk id.
    """
    org_id, mbx_id, thread_id, msg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    doc_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    customer_ids = {
        c["id"]: uuid.uuid4() for c in BUSINESS_CUSTOMERS if c["organization_id"] == DEMO_ORG_ID
    }
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"biz-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Box', 'active')",
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        for c in BUSINESS_CUSTOMERS:
            if c["id"] not in customer_ids:
                continue
            await conn.execute(
                "INSERT INTO customer (id, organization_id, email, name, account_status, tier)"
                " VALUES ($1, $2, $3, $4, $5, $6)",
                customer_ids[c["id"]],
                org_id,
                c["email"],
                c["name"],
                c.get("account_status", "active"),
                c.get("tier", "standard"),
            )
        for o in BUSINESS_ORDERS:
            if o["customer_id"] not in customer_ids:
                continue
            await conn.execute(
                'INSERT INTO "order" (id, organization_id, customer_id, order_number, status,'
                " total) VALUES ($1, $2, $3, $4, $5, $6)",
                uuid.uuid4(),
                org_id,
                customer_ids[o["customer_id"]],
                o["order_number"],
                o["status"],
                o["total"],
            )
        for t in BUSINESS_TICKETS:
            if t["customer_id"] not in customer_ids:
                continue
            await conn.execute(
                "INSERT INTO ticket (id, organization_id, customer_id, ticket_number, status,"
                " priority, subject) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.uuid4(),
                org_id,
                customer_ids[t["customer_id"]],
                t["ticket_number"],
                t["status"],
                t["priority"],
                t["subject"],
            )
        await conn.execute(
            "INSERT INTO knowledge_document (id, organization_id, title, category, status)"
            " VALUES ($1, $2, $3, $4, 'active')",
            doc_id,
            org_id,
            PROCEDURE_DOC_TITLE,
            category,
        )
        await conn.execute(
            "INSERT INTO knowledge_chunk (id, document_id, organization_id, chunk_index, section,"
            " category, content, version, content_tsv)"
            " VALUES ($1, $2, $3, 0, $4, $5, $6, 1, to_tsvector('english', $6))",
            chunk_id,
            doc_id,
            org_id,
            PROCEDURE.title,
            category,
            PROCEDURE.content,
        )
        await conn.execute(
            "INSERT INTO embedding_record (chunk_id, organization_id, model, dim, embedding)"
            " VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)",
            chunk_id,
            org_id,
            deterministic_embed(PROCEDURE.content, dim=1536),
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text,
                received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', $6, $7, '[]', $8, $9, $10)
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            fixture.sender_email,
            fixture.sender_name,
            fixture.subject,
            fixture.body_text,
            datetime.now(UTC),
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.QUEUED.value,
            idempotency_key=f"bizsvc-{uuid.uuid4()}",
        )
    )
    return job, chunk_id


def _envelope(job: Job, category: str) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(job.organization_id),
        message_id=str(job.message_id),
        classification={"category": category, "priority": "normal", "retrieval_required": True},
    )


async def _resources(
    broker: BrokerSettings, pool: asyncpg.Pool, metrics: PipelineMetrics
) -> WorkerResources:
    settings = AIWorkerSettings(broker=broker, retry=FAST_RETRY)
    connection = await aio_pika.connect_robust(broker.url)
    publisher = MessagePublisher(
        broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
    )
    return WorkerResources(
        settings=settings,
        db_pool=pool,
        connection=connection,
        publisher=publisher,
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=metrics,
    )


async def _publish(broker: BrokerSettings, lane: str, envelope: JobEnvelope) -> None:
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(broker.exchange_email_route, lane, envelope)
    finally:
        await publisher.close()


async def _wait_for_state(pool: asyncpg.Pool, job: Job, state: JobState, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    current = None
    while asyncio.get_running_loop().time() < deadline:
        stored = await PostgresJobStore(pool).get_job(job.organization_id, job.id)
        current = stored.state if stored else None
        if current == state.value:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job.id} ended {current}, expected {state.value}")


async def _draft_through_worker(
    broker: BrokerSettings,
    pool: asyncpg.Pool,
    job: Job,
    category: str,
    metrics: PipelineMetrics,
) -> FakeLLMProvider:
    """Run the composed ai-worker lane for `category` until the job is DRAFTED."""
    fake = FakeLLMProvider()
    res = await _resources(broker, pool, metrics)
    lane = f"email.{category}.normal"
    consumers = build_consumers(
        res, llm_provider=fake, token_counter=TokenCounter(), embedder=FakeEmbedder()
    )
    consumer = next(c for c in consumers if c.queue_name == lane)
    await consumer.start()
    try:
        await _publish(broker, lane, _envelope(job, category))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await consumer.stop()
        await res.connection.close()
    return fake


def _draft_prompt(fake: FakeLLMProvider) -> str:
    calls = [
        c for c in fake.recorded_calls if "draft" in set((c["schema"] or {}).get("required", []))
    ]
    assert len(calls) == 1, f"expected one draft call, got {len(calls)}"
    return "\n".join(str(m.content) for m in calls[0]["messages"])


def _split_prompt(prompt: str) -> tuple[str, str]:
    """(everything before the business block, the business block and what follows it).

    The precedence rule and the v2 templates' instruction lines also name [BUSINESS DATA], so
    split on the block header `[BUSINESS DATA] source=`, which only render() writes.
    """
    head, sep, business = prompt.rpartition("[BUSINESS DATA] source=")
    assert sep, "the draft prompt has no [BUSINESS DATA] section"
    return head, business


def _fact_line(business: str, reference: str) -> str:
    lines = [ln for ln in business.splitlines() if reference in ln]
    assert len(lines) == 1, f"expected one {reference} line in [BUSINESS DATA]: {business!r}"
    return lines[0]


async def _context_ready_payload(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    events = await PostgresJobStore(pool).list_events_for_job(job.organization_id, job.id)
    event = next(e for e in events if e.state_to == JobState.CONTEXT_READY.value)
    return dict(event.payload or {})


def _payload_fact(payload: dict[str, Any], reference: str) -> dict[str, Any]:
    facts = [f for f in payload["business_fact_statuses"] if f["reference"] == reference]
    assert len(facts) == 1, f"expected one {reference} fact: {payload['business_fact_statuses']}"
    return dict(facts[0])


def _count(metrics: PipelineMetrics, name: str, **labels: str) -> float:
    total = 0.0
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
    return total


async def _draft_count(pool: asyncpg.Pool, job: Job) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
                job.organization_id,
                job.id,
            )
        )


def test_scenario_fixtures_hold_what_the_gate_assumes() -> None:
    """The seeded pairs behind both scenarios, and a procedure chunk that carries no status."""
    assert ORDER_82915["customer_id"] == CUST_ALICE_ID
    assert ORDER_9901["customer_id"] == CUST_DANA_ID
    assert TICKET_4402["customer_id"] == CUST_EDWARD_ID
    assert TICKET_4402["status"] == "open"
    # R13.3: the status can only reach the draft from the business tables, never from RAG.
    assert str(ORDER_82915["status"]).lower() not in PROCEDURE.content.lower()


async def test_order_status_email_carries_the_seeded_status_and_the_procedure_chunk(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Alice's "status of order 82915" email: ORD-82915 FOUND in [BUSINESS DATA], procedure
    chunk in the retrieved knowledge, job DRAFTED with one schema-valid draft."""
    category = "general_inquiry"  # thread_plus_rag: the typed ID alone plans the lookup
    metrics = create_pipeline_metrics()
    job, chunk_id = await _seed_tenant(pool, ALICE_EMAIL, category=category)

    fake = await _draft_through_worker(broker, pool, job, category, metrics)

    head, business = _split_prompt(_draft_prompt(fake))
    line = _fact_line(business, "ORD-82915")
    assert re.search(r"\bFOUND\b", line), line
    assert re.search(rf"\b{re.escape(str(ORDER_82915['status']))}\b", line), line
    assert f"[CITATION: {chunk_id}]" in head
    assert PROCEDURE.content in head

    payload = await _context_ready_payload(pool, job)
    assert payload["business_data_degraded"] is False
    assert payload["customer_status"] == "FOUND"
    assert _payload_fact(payload, "ORD-82915")["status"] == "FOUND"
    assert payload["retrieved_chunks_count"] >= 1
    assert _count(metrics, "business_lookups_total", entity="order", status="FOUND") == 1
    assert await _draft_count(pool, job) == 1


async def test_cross_customer_order_is_not_found_and_own_ticket_is_found(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Edward asks about Dana's ORD-9901 and his own TICK-4402 (R13.4, R13.6)."""
    category = "support"
    metrics = create_pipeline_metrics()
    job, _ = await _seed_tenant(pool, EDWARD_EMAIL, category=category)

    fake = await _draft_through_worker(broker, pool, job, category, metrics)

    _, business = _split_prompt(_draft_prompt(fake))
    order_line = _fact_line(business, "ORD-9901")
    assert "NOT_FOUND" in order_line, order_line
    # Dana's order exists in this tenant; its status must not leak into Edward's context.
    assert str(ORDER_9901["status"]) not in order_line
    ticket_line = _fact_line(business, "TICK-4402")
    assert re.search(r"\bFOUND\b", ticket_line), ticket_line
    assert re.search(r"\bopen\b", ticket_line), ticket_line

    payload = await _context_ready_payload(pool, job)
    assert payload["business_data_degraded"] is False
    assert payload["customer_status"] == "FOUND"
    assert _payload_fact(payload, "ORD-9901")["status"] == "NOT_FOUND"
    assert _payload_fact(payload, "TICK-4402")["status"] == "FOUND"
    assert _count(metrics, "business_lookups_total", entity="order", status="NOT_FOUND") == 1
    assert _count(metrics, "business_lookups_total", entity="ticket", status="FOUND") == 1
    assert await _draft_count(pool, job) == 1

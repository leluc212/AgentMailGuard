"""Demo ingest door end to end on fakes (task 9.3, R25.1, R25.3, R23.1, R23.6, R3.1).

POST /v1/demo/messages -> mail.demo.inject -> DemoInjectConsumer -> SyncOrchestrator (demo
adapter) -> email-worker -> triage-worker -> ai-worker (fake LLM, mock embedder, stub reranker)
-> DRAFTED, on a scratch vhost and the isolated test database. Three tenants send the same
email at once into their own demo mailboxes, so a tenant mix-up or a lost email shows.
DEMO__ENABLED=false gives 404 on the same app factory.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel
from httpx import ASGITransport, AsyncClient

from packages.adapters.demo import demo_seed_mailbox
from packages.broker.topology import setup_topology
from packages.core.settings import APISettings, AppSettings, BrokerSettings, DemoSettings
from packages.core.storage import MinioObjectStorageClient, get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.classification import PostgresClassificationStore
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.db.mailbox import PostgresMailboxStore
from packages.db.message import PostgresMessageStore
from packages.db.seed import deterministic_embed
from packages.domain.state_machine import JobState
from packages.knowledge.embedder import FakeEmbedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.retrieval.rerank import RerankService, StubReranker
from services.ai_worker.main import build_consumers as build_ai_consumers
from services.api.main import create_app
from services.email_worker.main import build_consumer as build_email_consumer
from services.mail_connector.demo_consumer import DemoInjectConsumer
from services.mail_connector.orchestrator import SyncOrchestrator
from services.triage_worker.main import build_triage_consumer
from tests.integration.isolation import scratch_vhost
from tests.integration.test_phase6_pipeline_e2e import (
    BILLING_CHUNK,
    BODY,
    FAST_RETRY,
    PIPELINE_TIMEOUT_S,
    SUBJECT,
    _resources,
    _wait_for_state,
)

RUN_OPTIONS = {"model_profile": "fake", "guard_mode": "C0"}


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "demo93") as fast:
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


async def _seed_demo_tenant(pool: asyncpg.Pool) -> uuid.UUID:
    """A fresh org with its demo mailbox and one billing chunk for the RAG step."""
    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    doc_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    row = demo_seed_mailbox(
        mailbox_id=mbx_id,
        organization_id=org_id,
        address=f"demo-{mbx_id.hex[:6]}@example.com",
        display_name="Demo",
    )
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"d93-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            row["id"],
            row["organization_id"],
            row["provider"],
            row["address"],
            row["display_name"],
            row["status"],
        )
        await conn.execute(
            "INSERT INTO knowledge_document (id, organization_id, title, category, status)"
            " VALUES ($1, $2, 'Overdue balance procedure', 'billing', 'active')",
            doc_id,
            org_id,
        )
        await conn.execute(
            "INSERT INTO knowledge_chunk (id, document_id, organization_id, chunk_index, section,"
            " category, content, version, content_tsv)"
            " VALUES ($1, $2, $3, 0, 'Overdue balances', 'billing', $4, 1,"
            " to_tsvector('english', $4))",
            chunk_id,
            doc_id,
            org_id,
            BILLING_CHUNK,
        )
        await conn.execute(
            "INSERT INTO embedding_record (chunk_id, organization_id, model, dim, embedding)"
            " VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)",
            chunk_id,
            org_id,
            deterministic_embed(BILLING_CHUNK, dim=1536),
        )
    return org_id


async def _wait_for_job(api: AsyncClient, org_id: uuid.UUID, request_id: str) -> uuid.UUID:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PIPELINE_TIMEOUT_S
    while loop.time() < deadline:
        response = await api.get(
            f"/v1/demo/requests/{request_id}", headers={"X-Organization-ID": str(org_id)}
        )
        assert response.is_success, response.text
        if response.json()["status"] == "ingested":
            return uuid.UUID(response.json()["job_id"])
        await asyncio.sleep(0.2)
    raise AssertionError(f"demo request {request_id} was never ingested")


async def test_demo_emails_of_three_tenants_reach_drafted_through_the_broker(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    orgs = [await _seed_demo_tenant(pool) for _ in range(3)]
    storage = get_storage_client(AppSettings().object_storage)
    assert isinstance(storage, MinioObjectStorageClient)
    await storage.bootstrap_buckets()
    res = await _resources(broker, pool)

    mailbox_store = PostgresMailboxStore(pool)
    demo_consumer = DemoInjectConsumer(
        orchestrator=SyncOrchestrator(
            checkpoint_store=PostgresCheckpointStore(pool),
            storage_client=storage,
            publisher=res.publisher,
            mailbox_store=mailbox_store,
            job_store=PostgresJobStore(pool),
            settings=res.settings,
        ),
        mailbox_store=mailbox_store,
        broker_settings=broker,
        retry_settings=FAST_RETRY,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )
    consumers = [
        demo_consumer,
        build_email_consumer(res),
        build_triage_consumer(
            res.settings,
            publisher=res.publisher,
            job_store=PostgresJobStore(pool),
            message_store=PostgresMessageStore(pool),
            draft_store=PostgresDraftStore(pool),
            classification_store=PostgresClassificationStore(pool),
            connection=res.connection,
            shutdown_coordinator=res.shutdown,
            metrics=res.metrics,
        ),
        *build_ai_consumers(
            res,
            llm_provider=FakeLLMProvider(),
            token_counter=TokenCounter(),
            embedder=FakeEmbedder(),
            rerank_service=RerankService(StubReranker()),
        ),
    ]
    app = create_app(
        settings=APISettings(_env_file=None, broker=broker, demo=DemoSettings(enabled=True)),
        lifespan_enabled=False,
    )
    app.state.db_pool = pool
    app.state.publisher = res.publisher

    for consumer in consumers:
        await consumer.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
            accepted = []
            for org_id in orgs:
                response = await api.post(
                    "/v1/demo/messages",
                    headers={"X-Organization-ID": str(org_id)},
                    json={
                        "sender_email": "casey@customer.example.com",
                        "subject": SUBJECT,
                        "body": BODY,
                        "run_options": RUN_OPTIONS,
                    },
                )
                assert response.status_code == 202, response.text
                accepted.append(response.json()["demo_request_id"])

            for org_id, request_id in zip(orgs, accepted, strict=True):
                job_id = await _wait_for_job(api, org_id, request_id)
                await _wait_for_state(pool, org_id, job_id, JobState.DRAFTED)
                row = await pool.fetchrow(
                    "SELECT run_options, organization_id FROM processing_job WHERE id = $1",
                    job_id,
                )
                assert row is not None
                assert row["organization_id"] == org_id
                assert row["run_options"] is not None and '"guard_mode": "C0"' in str(
                    row["run_options"]
                )
                drafts = await pool.fetchval(
                    "SELECT count(*) FROM generated_draft WHERE organization_id = $1",
                    org_id,
                )
                assert drafts == 1
    finally:
        for consumer in consumers:
            await consumer.stop()
        await res.connection.close()


async def test_the_demo_door_is_404_when_disabled(pool: asyncpg.Pool) -> None:
    org_id = await _seed_demo_tenant(pool)
    app = create_app(settings=APISettings(_env_file=None), lifespan_enabled=False)
    app.state.db_pool = pool
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
        response = await api.post(
            "/v1/demo/messages",
            headers={"X-Organization-ID": str(org_id)},
            json={"sender_email": "a@example.com", "subject": "s", "body": "b"},
        )
    assert response.status_code == 404

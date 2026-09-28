"""Phase 6 end to end on fakes: fixture email to one provider draft or one send (6.9, R24.7).

The chain is fixture email -> SyncOrchestrator (fake provider) -> email-worker -> triage-worker
(rules put the email in billing) -> ai-worker (fake LLM, mock embedder; context and RAG over one
billing chunk) -> POST /v1/drafts/{id}/approve -> dispatch-worker (fake provider).

Each service is built by its own production builder on a scratch vhost and the isolated
rag_email_test database. The fake provider records every create_draft and send call, so
"exactly one" is counted at the provider boundary. It does not depend on how the fake stores
drafts after a send.

Requirements: R24.7, R17.1, R17.3, R17.7, R19.2, R19.3
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractQueue
from httpx import ASGITransport, AsyncClient

from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.protocol import MailProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import (
    AIWorkerSettings,
    AppSettings,
    BrokerSettings,
    RetryLadderSettings,
)
from packages.core.storage import MinioObjectStorageClient, get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.classification import PostgresClassificationStore
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.db.mailbox import PostgresMailboxStore
from packages.db.message import PostgresMessageStore
from packages.db.seed import deterministic_embed
from packages.domain import DispatchMode
from packages.domain.entities import DraftRef, Mailbox, OutboundReply, SentRef
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import CategoryDefinition, get_default_registry
from packages.knowledge.embedder import FakeEmbedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.main import build_consumers as build_ai_consumers
from services.api.main import create_app
from services.dispatch_worker.main import build_consumer as build_dispatch_consumer
from services.email_worker.main import build_consumer as build_email_consumer
from services.mail_connector.orchestrator import SyncOrchestrator
from services.triage_worker.main import build_triage_consumer
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
PIPELINE_TIMEOUT_S = 60.0
# The urgent-billing rule (config/triage_rules.yaml, id urgent-billing) classifies this email
# as billing without an LLM stage, so the category is deterministic.
SUBJECT = "Overdue payment failure on account"
BODY = "My account balance shows a payment failure and is now past due. Please advise."
BILLING_CHUNK = (
    "Overdue balances: when a card payment fails, the account enters a 7-day grace period. "
    "Customers can retry the payment from the billing portal or ask for a payment plan."
)


class RecordingFakeAdapter(FakeProviderAdapter):
    """The fake provider, recording every provider-side write (R19.3 counts at this boundary)."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[OutboundReply, DraftRef]] = []
        self.draft_sends: list[tuple[str, SentRef]] = []
        self.direct_sends: list[OutboundReply] = []

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        ref = await super().create_draft(mailbox, reply)
        self.created.append((reply, ref))
        return ref

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        ref = await super().send_draft(mailbox, provider_draft_id)
        self.draft_sends.append((provider_draft_id, ref))
        return ref

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        self.direct_sends.append(reply)  # dispatch must go through the draft, never this
        return await super().send_reply(mailbox, reply)


@dataclasses.dataclass
class Tenant:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    provider_message_id: str
    provider_thread_id: str
    rfc822_id: str  # without angle brackets, as the parser stores it


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "p6e2e") as fast:
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


@pytest.fixture
def dispatch_mode() -> Iterator[Callable[[DispatchMode], None]]:
    """Set every category's dispatch_mode for one test; restore the registry afterwards."""
    registry = get_default_registry()
    originals: dict[str, CategoryDefinition] = {}

    def apply(mode: DispatchMode) -> None:
        for name in registry.all_categories():
            definition = registry.get(name)
            assert definition is not None
            originals.setdefault(name, definition)
            registry.register_category(dataclasses.replace(definition, dispatch_mode=mode))

    try:
        yield apply
    finally:
        for definition in originals.values():
            registry.register_category(definition)


def _fixture_mime(to_address: str, rfc822_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Casey Customer <casey@customer.example.com>"
    msg["To"] = to_address
    msg["Subject"] = SUBJECT
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = f"<{rfc822_id}>"
    msg.set_content(BODY)
    return msg.as_bytes()


async def _seed_tenant(pool: asyncpg.Pool) -> Tenant:
    """A fresh org and mailbox, plus one billing chunk so the RAG step has a document."""
    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    doc_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"p6-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Support', 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@example.com",
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
    return Tenant(
        org_id=org_id,
        mailbox_id=mbx_id,
        provider_message_id=f"fake-msg-{uuid.uuid4().hex[:10]}",
        provider_thread_id=f"fake-thread-{uuid.uuid4().hex[:10]}",
        rfc822_id=f"e2e-{uuid.uuid4().hex}@customer.example.com",
    )


async def _resources(broker: BrokerSettings, pool: asyncpg.Pool) -> WorkerResources:
    settings = AIWorkerSettings(broker=broker, retry=FAST_RETRY)
    connection = await aio_pika.connect_robust(broker.url)
    return WorkerResources(
        settings=settings,
        db_pool=pool,
        connection=connection,
        publisher=MessagePublisher(
            broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
        ),
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=create_pipeline_metrics(),
    )


async def _only_pipeline_job(pool: asyncpg.Pool, org_id: uuid.UUID) -> uuid.UUID:
    rows = await pool.fetch(
        "SELECT id FROM processing_job WHERE organization_id = $1 AND job_type = 'email_pipeline'",
        org_id,
    )
    assert len(rows) == 1, f"expected one pipeline job, got {len(rows)}"
    return uuid.UUID(str(rows[0]["id"]))


async def _wait_for_state(
    pool: asyncpg.Pool, org_id: uuid.UUID, job_id: uuid.UUID, state: JobState
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PIPELINE_TIMEOUT_S
    current: str | None = None
    while loop.time() < deadline:
        current = await pool.fetchval(
            "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
            job_id,
            org_id,
        )
        if current == state.value:
            return
        await asyncio.sleep(0.2)
    events = await pool.fetch(
        "SELECT state_from, state_to, payload FROM processing_event"
        " WHERE job_id = $1 AND organization_id = $2 ORDER BY created_at",
        job_id,
        org_id,
    )
    raise AssertionError(
        f"job {job_id} ended {current}, expected {state.value}; events {[dict(e) for e in events]}"
    )


async def _wait_until(predicate: Callable[[], bool], what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PIPELINE_TIMEOUT_S
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def _run_to_approved(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    tenant: Tenant,
    fake: RecordingFakeAdapter,
    before_approve: Callable[[], None],
    body: Callable[[uuid.UUID, uuid.UUID, AbstractQueue, list[str], AsyncClient], Awaitable[None]],
) -> None:
    """Run every worker, sync the fixture email to DRAFTED, approve it, then hand over to `body`.

    `body(job_id, draft_id, dispatch_probe, dispatch_deliveries, api)` runs while the workers
    are still up; `dispatch_deliveries` gets one job id per finished dispatch delivery.
    """
    storage = get_storage_client(AppSettings().object_storage)
    assert isinstance(storage, MinioObjectStorageClient)
    await storage.bootstrap_buckets()
    res = await _resources(broker, pool)

    email_consumer = build_email_consumer(res)
    triage_consumer = build_triage_consumer(
        res.settings,
        publisher=res.publisher,
        job_store=PostgresJobStore(pool),
        message_store=PostgresMessageStore(pool),
        draft_store=PostgresDraftStore(pool),
        classification_store=PostgresClassificationStore(pool),  # as build_components wires it
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )
    ai_consumers = build_ai_consumers(
        res, llm_provider=FakeLLMProvider(), token_counter=TokenCounter(), embedder=FakeEmbedder()
    )

    def resolve(_mailbox: Mailbox) -> MailProviderAdapter:
        return fake

    dispatch_consumer = build_dispatch_consumer(res, adapter_resolver=resolve)
    deliveries: list[str] = []
    original = dispatch_consumer.process_job

    async def tracked(envelope: JobEnvelope, raw_message: AbstractIncomingMessage) -> None:
        try:
            await original(envelope, raw_message)
        finally:
            deliveries.append(envelope.job_id)

    dispatch_consumer.process_job = tracked  # type: ignore[method-assign]
    # The dispatch mode is read from the default taxonomy registry at dispatch time; set it
    # after every builder ran, so a builder that reloads config/categories.yaml cannot undo it.
    before_approve()

    probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
    await probe.bind(broker.exchange_email_dispatch, routing_key=broker.queue_dispatch)

    consumers = [email_consumer, triage_consumer, *ai_consumers, dispatch_consumer]
    for consumer in consumers:
        await consumer.start()
    try:
        fake.seed_message(
            provider_message_id=tenant.provider_message_id,
            provider_thread_id=tenant.provider_thread_id,
            raw_payload=_fixture_mime(
                f"support-{tenant.mailbox_id.hex[:6]}@example.com", tenant.rfc822_id
            ),
        )
        mailbox = await PostgresMailboxStore(pool).get(tenant.mailbox_id)
        assert mailbox is not None
        outcome = await SyncOrchestrator(
            checkpoint_store=PostgresCheckpointStore(pool),
            storage_client=storage,
            publisher=res.publisher,
            mailbox_store=PostgresMailboxStore(pool),
            job_store=PostgresJobStore(pool),
            settings=res.settings,
        ).sync_mailbox(mailbox, adapter=fake)
        assert outcome.status == "success"
        assert outcome.messages_synced == 1

        job_id = await _only_pipeline_job(pool, tenant.org_id)
        await _wait_for_state(pool, tenant.org_id, job_id, JobState.DRAFTED)

        chunks = await pool.fetchval(
            "SELECT (payload->>'retrieved_chunks_count')::int FROM processing_event"
            " WHERE organization_id = $1 AND job_id = $2 AND state_to = 'CONTEXT_READY'"
            " ORDER BY created_at LIMIT 1",
            tenant.org_id,
            job_id,
        )
        assert chunks is not None and chunks >= 1, "the RAG step retrieved no chunk"
        draft_id = await pool.fetchval(
            "SELECT id FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
            tenant.org_id,
            job_id,
        )
        assert draft_id is not None

        app = create_app(lifespan_enabled=False)
        app.state.db_pool = pool
        app.state.publisher = res.publisher
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api:
            approved = await api.post(
                f"/v1/drafts/{draft_id}/approve",
                headers={"X-Organization-ID": str(tenant.org_id)},
                json={"reviewer": "e2e"},
            )
            assert approved.is_success, approved.text
            await body(job_id, uuid.UUID(str(draft_id)), probe, deliveries, api)
    finally:
        for consumer in consumers:
            await consumer.stop()
        await res.connection.close()


async def _replay_and_reapprove(
    broker: BrokerSettings,
    tenant: Tenant,
    draft_id: uuid.UUID,
    probe: AbstractQueue,
    deliveries: list[str],
    api: AsyncClient,
) -> None:
    """Replay the captured dispatch message, then approve again (6.1, R19.3)."""
    captured = await probe.get(no_ack=True, fail=False, timeout=5)
    assert captured is not None, "approve published nothing to email.dispatch"
    envelope = JobEnvelope.from_message(captured)
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(broker.exchange_email_dispatch, broker.queue_dispatch, envelope)
    finally:
        await publisher.close()
    await _wait_until(lambda: len(deliveries) >= 2, "the replayed dispatch delivery")
    # The probe is bound to the same exchange and key, so it holds a copy of the replay too.
    replayed = await probe.get(no_ack=True, fail=False, timeout=5)
    assert replayed is not None, "the probe did not see the replayed dispatch message"

    again = await api.post(
        f"/v1/drafts/{draft_id}/approve",
        headers={"X-Organization-ID": str(tenant.org_id)},
        json={"reviewer": "e2e"},
    )
    assert again.is_success, again.text
    await asyncio.sleep(0.5)
    # 6.1: a repeated approve re-publishes only while the job is not COMPLETED.
    assert await probe.get(no_ack=True, fail=False) is None


async def _state_transitions(pool: asyncpg.Pool, org_id: uuid.UUID, job_id: uuid.UUID) -> list[str]:
    rows = await pool.fetch(
        "SELECT state_to FROM processing_event WHERE organization_id = $1 AND job_id = $2"
        " AND event_type = 'state_transition' ORDER BY created_at",
        org_id,
        job_id,
    )
    return [str(r["state_to"]) for r in rows]


async def test_create_draft_mode_ends_with_one_provider_draft_and_no_outbound_message(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    dispatch_mode: Callable[[DispatchMode], None],
) -> None:
    """Default mode: one provider draft, no send, no outbound email_message (6.9, R17.1, R17.7)."""
    tenant = await _seed_tenant(pool)
    fake = RecordingFakeAdapter()
    try:

        async def body(
            job_id: uuid.UUID,
            draft_id: uuid.UUID,
            probe: AbstractQueue,
            deliveries: list[str],
            api: AsyncClient,
        ) -> None:
            await _wait_for_state(pool, tenant.org_id, job_id, JobState.COMPLETED)
            await _wait_until(lambda: len(deliveries) >= 1, "the first dispatch delivery")
            await _replay_and_reapprove(broker, tenant, draft_id, probe, deliveries, api)

            assert len(fake.created) == 1
            assert fake.draft_sends == [] and fake.direct_sends == []
            reply, ref = fake.created[0]
            assert reply.thread_id == tenant.provider_thread_id  # never our UUID (R17.2)
            assert reply.subject == f"Re: {SUBJECT}"
            assert reply.in_reply_to == f"<{tenant.rfc822_id}>"
            assert reply.references[-1] == f"<{tenant.rfc822_id}>"

            row = await pool.fetchrow(
                "SELECT status, provider_draft_id, provider_draft_message_id,"
                " dispatch_idempotency_key FROM generated_draft"
                " WHERE organization_id = $1 AND id = $2",
                tenant.org_id,
                draft_id,
            )
            assert row is not None
            assert row["status"] == "dispatched"
            assert row["provider_draft_id"] == ref.provider_draft_id
            assert row["provider_draft_message_id"] == ref.provider_message_id
            assert row["dispatch_idempotency_key"] == derive_idempotency_key(
                organization_id=tenant.org_id,
                mailbox_id=tenant.mailbox_id,
                provider_message_id=tenant.provider_message_id,
                operation_type="dispatch",
            )
            outbound = await pool.fetchval(
                "SELECT count(*) FROM email_message WHERE organization_id = $1"
                " AND direction = 'outbound'",
                tenant.org_id,
            )
            assert outbound == 0  # the customer has received nothing (design §5.8)
            feedback = await pool.fetchval(
                "SELECT count(*) FROM feedback WHERE organization_id = $1 AND draft_id = $2",
                tenant.org_id,
                draft_id,
            )
            assert feedback == 1
            transitions = await _state_transitions(pool, tenant.org_id, job_id)
            assert transitions.count(JobState.DISPATCHED.value) == 1
            assert transitions.count(JobState.COMPLETED.value) == 1
            assert transitions[-1] == JobState.COMPLETED.value

        await _run_to_approved(
            broker,
            channel,
            pool,
            tenant,
            fake,
            lambda: dispatch_mode(DispatchMode.CREATE_DRAFT),
            body,
        )
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", tenant.org_id)


async def test_send_reply_mode_ends_with_one_send_and_one_outbound_message(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    dispatch_mode: Callable[[DispatchMode], None],
) -> None:
    """send_reply: one draft, one send of that draft, one outbound message (6.9, R17.3, R17.7)."""
    tenant = await _seed_tenant(pool)
    fake = RecordingFakeAdapter()
    try:

        async def body(
            job_id: uuid.UUID,
            draft_id: uuid.UUID,
            probe: AbstractQueue,
            deliveries: list[str],
            api: AsyncClient,
        ) -> None:
            await _wait_for_state(pool, tenant.org_id, job_id, JobState.COMPLETED)
            await _wait_until(lambda: len(deliveries) >= 1, "the first dispatch delivery")
            await _replay_and_reapprove(broker, tenant, draft_id, probe, deliveries, api)

            assert len(fake.created) == 1
            assert fake.direct_sends == []
            assert len(fake.draft_sends) == 1
            reply, draft_ref = fake.created[0]
            sent_draft_id, sent_ref = fake.draft_sends[0]
            assert sent_draft_id == draft_ref.provider_draft_id
            assert reply.message_id is not None

            rows = await pool.fetch(
                "SELECT m.provider_message_id, m.thread_id, m.rfc822_message_id, m.in_reply_to,"
                " m.subject FROM email_message m WHERE m.organization_id = $1"
                " AND m.direction = 'outbound'",
                tenant.org_id,
            )
            assert len(rows) == 1
            out = rows[0]
            inbound_thread = await pool.fetchval(
                "SELECT thread_id FROM email_message WHERE organization_id = $1"
                " AND provider_message_id = $2",
                tenant.org_id,
                tenant.provider_message_id,
            )
            assert out["provider_message_id"] == sent_ref.provider_message_id
            assert out["thread_id"] == inbound_thread
            assert out["rfc822_message_id"] == reply.message_id.strip("<>")
            assert out["in_reply_to"] == tenant.rfc822_id
            assert out["subject"] == f"Re: {SUBJECT}"

            draft = await pool.fetchrow(
                "SELECT status, provider_ref FROM generated_draft"
                " WHERE organization_id = $1 AND id = $2",
                tenant.org_id,
                draft_id,
            )
            assert draft is not None
            assert draft["status"] == "dispatched"
            assert draft["provider_ref"] == sent_ref.provider_message_id
            transitions = await _state_transitions(pool, tenant.org_id, job_id)
            assert transitions.count(JobState.DISPATCHED.value) == 1
            assert transitions[-1] == JobState.COMPLETED.value

        await _run_to_approved(
            broker,
            channel,
            pool,
            tenant,
            fake,
            lambda: dispatch_mode(DispatchMode.SEND_REPLY),
            body,
        )
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", tenant.org_id)

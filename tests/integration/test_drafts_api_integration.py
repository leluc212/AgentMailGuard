"""Draft review API against PostgreSQL and RabbitMQ (task 6.1, 6.2; R16.6, R16.7, R18.5).

Proves on the real stores: keyset pagination and tenant scope, approve commits then publishes
to email.dispatch, a repeated approve re-publishes with one feedback row (UNIQUE (draft_id)),
the edit distance is measured from the generated body, and reject is DRAFTED -> COMPLETED.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft import insert_draft
from packages.db.job import PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.review import edit_distance
from packages.domain.state_machine import JobState
from packages.observability.metrics import create_pipeline_metrics
from services.api.main import create_app
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
GENERATED_BODY = "Your order ORD-82915 was dispatched on 24 September."


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "draftsapi") as fast:
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
def api_app(pool: asyncpg.Pool, broker: BrokerSettings, channel: AbstractChannel) -> FastAPI:
    app = create_app(lifespan_enabled=False)
    app.state.db_pool = pool
    app.state.publisher = MessagePublisher(broker_settings=broker, channel=channel)
    app.state.metrics = create_pipeline_metrics()
    return app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=api_app), base_url="http://testserver"
    ) as ac:
        yield ac


@dataclass(frozen=True)
class Seed:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    job_id: uuid.UUID
    draft_id: uuid.UUID
    chunk_id: uuid.UUID


async def _seed_org(pool: asyncpg.Pool) -> tuple[uuid.UUID, uuid.UUID]:
    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, status)"
            " VALUES ($1, $2, 'gmail', $3, 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@acme.example",
        )
    return org_id, mbx_id


async def _seed_draft(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    *,
    category: str = "billing",
    created_at: datetime | None = None,
) -> Seed:
    thread_id, msg_id, chunk_id, doc_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:8]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                rfc822_message_id, direction, sender_email, sender_name, recipients,
                subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'inbound', 'alice@customer.example', 'Alice',
                      '[]', 'Where is order 82915?', 'What is the status of order 82915?', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            f"orig-{msg_id.hex[:8]}@customer.example",
        )
        await conn.execute(
            """
            INSERT INTO classification_result (
                id, organization_id, message_id, category, priority, reply_required,
                retrieval_required, confidence, decided_by
            ) VALUES ($1, $2, $3, $4, 'normal', true, true, 0.9, 'rule')
            """,
            uuid.uuid4(),
            org_id,
            msg_id,
            category,
        )
        await conn.execute(
            "INSERT INTO thread_state (thread_id, organization_id, summary) VALUES ($1, $2, $3)",
            thread_id,
            org_id,
            "Alice asks where order 82915 is.",
        )
        await conn.execute(
            "INSERT INTO knowledge_document (id, organization_id, title)"
            " VALUES ($1, $2, 'Shipping')",
            doc_id,
            org_id,
        )
        await conn.execute(
            """
            INSERT INTO knowledge_chunk (
                id, document_id, organization_id, chunk_index, external_id, heading_path,
                content, version, content_tsv
            ) VALUES ($1, $2, $3, 0, 'DOC-7-01', ARRAY['Shipping'], $4, 1,
                      to_tsvector('english', $4))
            """,
            chunk_id,
            doc_id,
            org_id,
            "Orders ship within 2 business days.",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.DRAFTED.value,
            idempotency_key=f"draftsapi-{uuid.uuid4()}",
        )
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO processing_event (job_id, message_id, organization_id, event_type,
                                          state_from, state_to, payload)
            VALUES ($1, $2, $3, 'state_transition', 'QUEUED', 'CONTEXT_READY', $4::jsonb)
            """,
            job.id,
            msg_id,
            org_id,
            json.dumps(
                {
                    "customer_status": "FOUND",
                    "business_fact_statuses": [
                        {
                            "entity": "order",
                            "reference": "ORD-82915",
                            "status": "FOUND",
                            "reason": None,
                        }
                    ],
                    "business_data_degraded": False,
                }
            ),
        )
        draft = await insert_draft(
            conn,
            GeneratedDraft(
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                job_id=job.id,
                subject="Re: Where is order 82915?",
                body=GENERATED_BODY,
                citations=[
                    {
                        "citation_id": "DOC-7-01",
                        "chunk_id": str(chunk_id),
                        "document_id": str(doc_id),
                        "external_id": "DOC-7-01",
                    }
                ],
                created_at=created_at or datetime.now(UTC),
            ),
        )
    return Seed(org_id, mbx_id, thread_id, msg_id, uuid.UUID(str(job.id)), draft.id, chunk_id)


def _h(org_id: uuid.UUID) -> dict[str, str]:
    return {"X-Organization-Id": str(org_id)}


async def _dispatch_envelopes(
    channel: AbstractChannel, broker: BrokerSettings
) -> list[JobEnvelope]:
    queue = await channel.declare_queue(broker.queue_dispatch, passive=True)
    envelopes: list[JobEnvelope] = []
    while True:
        message = await queue.get(no_ack=True, fail=False, timeout=5)
        if message is None:
            return envelopes
        envelopes.append(JobEnvelope.model_validate_json(message.body))


async def test_list_detail_cursor_and_tenant_scope(client: AsyncClient, pool: asyncpg.Pool) -> None:
    org_id, mbx_id = await _seed_org(pool)
    base = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    older = await _seed_draft(pool, org_id, mbx_id, created_at=base)
    newer = await _seed_draft(
        pool, org_id, mbx_id, category="support", created_at=base + timedelta(minutes=1)
    )
    other_org, other_mbx = await _seed_org(pool)
    foreign = await _seed_draft(pool, other_org, other_mbx)
    try:
        page1 = (await client.get("/v1/drafts", params={"limit": 1}, headers=_h(org_id))).json()
        assert [i["id"] for i in page1["items"]] == [str(newer.draft_id)]
        page2 = (
            await client.get(
                "/v1/drafts",
                params={"limit": 1, "cursor": page1["next_cursor"]},
                headers=_h(org_id),
            )
        ).json()
        assert [i["id"] for i in page2["items"]] == [str(older.draft_id)]
        assert page2["next_cursor"] is None

        billing = (
            await client.get("/v1/drafts", params={"category": "billing"}, headers=_h(org_id))
        ).json()
        assert [i["id"] for i in billing["items"]] == [str(older.draft_id)]

        detail = (await client.get(f"/v1/drafts/{older.draft_id}", headers=_h(org_id))).json()
        assert detail["thread_summary"] == "Alice asks where order 82915 is."
        assert detail["cited_chunks"][0]["content"] == "Orders ship within 2 business days."
        assert detail["cited_chunks"][0]["heading_path"] == ["Shipping"]
        assert detail["business_data"]["facts"][0]["reference"] == "ORD-82915"
        assert detail["original"]["body_text"] == "What is the status of order 82915?"

        assert (
            await client.get(f"/v1/drafts/{foreign.draft_id}", headers=_h(org_id))
        ).status_code == 404
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM organization WHERE id = ANY($1::uuid[])", [org_id, other_org]
            )


async def test_approve_commits_then_publishes_and_repeats_without_second_feedback(
    client: AsyncClient, pool: asyncpg.Pool, channel: AbstractChannel, broker: BrokerSettings
) -> None:
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        url = f"/v1/drafts/{seed.draft_id}/approve"
        first = await client.post(
            url, json={"reviewer": "demo", "review_ms": 4200}, headers=_h(org_id)
        )
        assert first.status_code == 200
        second = await client.post(url, headers=_h(org_id))
        assert second.json()["created"] is False

        envelopes = await _dispatch_envelopes(channel, broker)
        assert [e.job_id for e in envelopes] == [str(seed.job_id), str(seed.job_id)]
        assert {e.job_type for e in envelopes} == {"dispatch"}

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT decision, edit_distance, review_ms, reviewer FROM feedback"
                " WHERE draft_id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
            status = await conn.fetchval(
                "SELECT status FROM generated_draft WHERE id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
            job_state = await conn.fetchval(
                "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                org_id,
            )
        assert [dict(r) for r in rows] == [
            {"decision": "accepted", "edit_distance": 0, "review_ms": 4200, "reviewer": "demo"}
        ]
        assert status == "approved"
        assert job_state == JobState.DRAFTED.value  # the dispatch-worker's claim moves it on
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_two_fast_approves_write_one_feedback_row(
    client: AsyncClient, pool: asyncpg.Pool, channel: AbstractChannel, broker: BrokerSettings
) -> None:
    """Review focus 2: approve clicked twice fast. Row locks serialise the two calls; both
    answer 200, exactly one is the decision, one feedback row exists, every envelope names
    the same job, and no UNIQUE (draft_id) violation surfaces as a 500."""
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        url = f"/v1/drafts/{seed.draft_id}/approve"
        first, second = await asyncio.gather(
            client.post(url, json={"reviewer": "demo", "review_ms": 900}, headers=_h(org_id)),
            client.post(url, json={"reviewer": "demo", "review_ms": 950}, headers=_h(org_id)),
        )
        assert (first.status_code, second.status_code) == (200, 200)
        assert sorted([first.json()["created"], second.json()["created"]]) == [False, True]
        assert first.json()["feedback_id"] == second.json()["feedback_id"]

        envelopes = await _dispatch_envelopes(channel, broker)
        assert envelopes
        assert {e.job_id for e in envelopes} == {str(seed.job_id)}

        async with pool.acquire() as conn:
            rows = await conn.fetchval(
                "SELECT count(*) FROM feedback WHERE draft_id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
        assert rows == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_edit_distance_is_measured_from_the_generated_body(
    client: AsyncClient, pool: asyncpg.Pool
) -> None:
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        url = f"/v1/drafts/{seed.draft_id}"
        await client.patch(url, json={"body": "Draft one."}, headers=_h(org_id))
        final = "Order ORD-82915 shipped on 24 September. Tracking follows."
        await client.patch(url, json={"body": final}, headers=_h(org_id))
        decided = (await client.post(f"{url}/approve", headers=_h(org_id))).json()
        assert decided["decision"] == "edited"
        assert decided["edit_distance"] == edit_distance(GENERATED_BODY, final)

        async with pool.acquire() as conn:
            payloads = [
                json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
                for r in await conn.fetch(
                    "SELECT payload FROM processing_event WHERE organization_id = $1"
                    " AND message_id = $2 AND event_type = 'draft_edited' ORDER BY id",
                    org_id,
                    seed.message_id,
                )
            ]
        assert [p.get("original_body") for p in payloads] == [GENERATED_BODY, None]
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_reject_moves_the_job_to_completed_in_one_transaction(
    client: AsyncClient, pool: asyncpg.Pool, channel: AbstractChannel, broker: BrokerSettings
) -> None:
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        resp = await client.post(f"/v1/drafts/{seed.draft_id}/reject", headers=_h(org_id))
        assert resp.status_code == 200
        assert resp.json()["job_state"] == "COMPLETED"

        async with pool.acquire() as conn:
            last = await conn.fetchrow(
                "SELECT state_from, state_to, payload FROM processing_event"
                " WHERE job_id = $1 AND organization_id = $2 AND event_type = 'state_transition'"
                " ORDER BY id DESC LIMIT 1",
                seed.job_id,
                org_id,
            )
            decision = await conn.fetchval(
                "SELECT decision FROM feedback WHERE draft_id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
        assert last is not None
        assert (last["state_from"], last["state_to"]) == ("DRAFTED", "COMPLETED")
        payload = (
            json.loads(last["payload"]) if isinstance(last["payload"], str) else last["payload"]
        )
        assert payload["decision"] == "rejected"
        assert decision == "rejected"
        assert await _dispatch_envelopes(channel, broker) == []
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)

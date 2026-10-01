"""Live integration tests for manual re-sync endpoint and mailbox API.

Covers R2.11, R23.2, R23.6, R5.3, and R20.1.
Runs against live PostgreSQL (port 5433) and RabbitMQ (port 5672) containers.
Verifies:
- Live database persistence with multi-tenant scoping across >=3 tenants.
- Asynchronous job publication to RabbitMQ exchange mail.ingest and queue mail.sync.requested.
- Operational status checks (needs_reauth) and operator force override in PostgreSQL.
- OpenAPI 3.1 schema completeness.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.mailbox import PostgresMailboxStore
from packages.db.migrator import apply_migrations
from services.api.main import create_app
from services.api.openapi import generate_openapi_spec, validate_openapi_spec


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide dedicated asyncpg connection pool connected to test database."""
    settings = AppSettings().database
    await apply_migrations(dsn=settings.asyncpg_dsn)
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def broker_channel() -> AsyncGenerator[AbstractChannel, None]:
    """Provide a dedicated RabbitMQ channel for inspecting enqueued messages."""
    broker_settings = AppSettings().broker
    conn = await aio_pika.connect_robust(broker_settings.url)
    channel = await conn.channel()
    await setup_topology(channel, broker_settings)
    yield channel
    if not channel.is_closed:
        await channel.close()
    if not conn.is_closed:
        await conn.close()


async def ensure_test_org(pool: asyncpg.Pool[Any], org_id: uuid.UUID, name: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org_id,
            name,
        )


async def ensure_test_mailbox(
    pool: asyncpg.Pool[Any],
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    address: str,
    provider: str = "gmail",
    status: str = "active",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, $3, $4, 'Integration Mailbox', $5)
            ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status;
            """,
            mbx_id,
            org_id,
            provider,
            address,
            status,
        )


@pytest.mark.asyncio
async def test_api_resync_e2e_flow_with_postgres_and_rabbitmq(
    db_pool: asyncpg.Pool[Any],
    broker_channel: AbstractChannel,
) -> None:
    """End-to-end integration test for POST /v1/mailboxes/{id}/resync and GET /v1/mailboxes/{id}."""
    broker_settings = AppSettings().broker
    sync_queue = await broker_channel.get_queue(broker_settings.queue_mail_sync)
    await sync_queue.purge()

    # 1. Multi-tenant setup: 3 tenants (R5.3 / testing strategy mandate)
    org1 = uuid.uuid4()
    org2 = uuid.uuid4()
    org3 = uuid.uuid4()

    mbx1 = uuid.uuid4()
    mbx2 = uuid.uuid4()
    mbx3 = uuid.uuid4()

    try:
        await ensure_test_org(db_pool, org1, "E2E Tenant 1")
        await ensure_test_org(db_pool, org2, "E2E Tenant 2")
        await ensure_test_org(db_pool, org3, "E2E Tenant 3")

        await ensure_test_mailbox(db_pool, org1, mbx1, "user1@e2e-tenant1.com", status="active")
        await ensure_test_mailbox(db_pool, org2, mbx2, "user2@e2e-tenant2.com", status="active")
        await ensure_test_mailbox(
            db_pool, org3, mbx3, "user3@e2e-tenant3.com", status="needs_reauth"
        )

        app = create_app()

        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
        ):
            # 2. Test GET /v1/mailboxes/{id}
            get_resp = await client.get(
                f"/v1/mailboxes/{mbx1}",
                headers={"X-Organization-ID": str(org1)},
            )
            assert get_resp.status_code == 200
            mbx_data = get_resp.json()
            assert mbx_data["id"] == str(mbx1)
            assert mbx_data["organization_id"] == str(org1)
            assert mbx_data["status"] == "active"
            assert mbx_data["address"] == "user1@e2e-tenant1.com"

            # Tenant isolation: tenant 2 accessing mailbox 1 returns 404
            cross_get = await client.get(
                f"/v1/mailboxes/{mbx1}",
                headers={"X-Organization-ID": str(org2)},
            )
            assert cross_get.status_code == 404

            # 3. Test POST /v1/mailboxes/{id}/resync with time window
            now = datetime.now(UTC)
            since = now - timedelta(days=3)
            until = now

            resync_resp = await client.post(
                f"/v1/mailboxes/{mbx1}/resync",
                headers={"X-Organization-ID": str(org1)},
                json={
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "full_resync": True,
                },
            )
            assert resync_resp.status_code == 202
            resync_data = resync_resp.json()
            assert resync_data["status"] == "enqueued"
            assert resync_data["mailbox_id"] == str(mbx1)
            job_id = resync_data["job_id"]

            # 4. Consume and verify message on RabbitMQ queue
            # Give broker a moment to process the publish
            await asyncio.sleep(0.5)

            amqp_msg = await sync_queue.get(no_ack=False, timeout=5.0)
            assert amqp_msg is not None
            try:
                assert amqp_msg.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
                envelope = JobEnvelope.from_message(amqp_msg)
                assert envelope.job_id == job_id
                assert envelope.job_type == "sync_mailbox"
                assert envelope.organization_id == str(org1)
                assert envelope.mailbox_id == str(mbx1)
                assert envelope.payload["full_resync"] is True
                assert envelope.payload["manual"] is True
                assert envelope.payload["since"] == since.isoformat()
                assert envelope.payload["until"] == until.isoformat()
            finally:
                await amqp_msg.ack()

            # 5. Cross-tenant resync attempt returns 404 and does not publish
            cross_resync = await client.post(
                f"/v1/mailboxes/{mbx2}/resync",
                headers={"X-Organization-ID": str(org1)},  # Org 1 attempting Org 2's mailbox
                json={},
            )
            assert cross_resync.status_code == 404

            # Verify no message was published to queue
            queue_status = await sync_queue.declare()
            assert queue_status.message_count == 0

            # 6. Operational status: needs_reauth rejected by default with 409
            reauth_resp = await client.post(
                f"/v1/mailboxes/{mbx3}/resync",
                headers={"X-Organization-ID": str(org3)},
                json={"force": False},
            )
            assert reauth_resp.status_code == 409
            assert reauth_resp.json()["code"] == "MAILBOX_NEEDS_REAUTH"

            # 7. Operational status: needs_reauth allowed with force=True
            forced_resp = await client.post(
                f"/v1/mailboxes/{mbx3}/resync",
                headers={"X-Organization-ID": str(org3)},
                json={"force": True},
            )
            assert forced_resp.status_code == 202

            # Verify DB updated mailbox status to active
            mbx_store = PostgresMailboxStore(db_pool)
            updated_mbx3 = await mbx_store.get(mbx3)
            assert updated_mbx3 is not None
            assert updated_mbx3.status == "active"

            # Verify broker received forced sync job
            await asyncio.sleep(0.5)
            forced_msg = await sync_queue.get(no_ack=True, timeout=5.0)
            assert forced_msg is not None
            forced_envelope = JobEnvelope.from_message(forced_msg)
            assert forced_envelope.mailbox_id == str(mbx3)
            assert forced_envelope.organization_id == str(org3)

    finally:
        # Cleanup test data
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM organization WHERE id IN ($1, $2, $3)", org1, org2, org3
            )
        await sync_queue.purge()


def test_openapi_schema_contains_mailbox_resync() -> None:
    """Verify generated OpenAPI 3.1 schema includes mailbox management and resync endpoints."""
    app = create_app(lifespan_enabled=False)
    spec = generate_openapi_spec(app)

    valid, errors = validate_openapi_spec(spec)
    assert valid, f"OpenAPI validation errors: {errors}"

    paths = spec["paths"]
    assert "/v1/mailboxes/{id}/resync" in paths
    assert "/v1/mailboxes/{id}" in paths

    resync_path = paths["/v1/mailboxes/{id}/resync"]
    assert "post" in resync_path
    post_op = resync_path["post"]
    assert "202" in post_op["responses"]

    get_path = paths["/v1/mailboxes/{id}"]
    assert "get" in get_path

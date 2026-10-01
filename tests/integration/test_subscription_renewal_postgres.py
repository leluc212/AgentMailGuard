"""Integration tests for PostgreSQL subscription store and renewal job (R2.10, R1.5, R5.3).

Verifies real PostgreSQL persistence, outcome recording, multi-tenant scoping across
>=3 tenants (testing strategy mandate), and automated transition to needs_reauth on AuthExpired.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from packages.adapters.fake import FakeProviderAdapter
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.mailbox import PostgresMailboxStore
from packages.db.migrator import apply_migrations
from packages.db.subscription import PostgresSubscriptionStore
from packages.domain.entities import Subscription
from packages.observability.metrics import create_pipeline_metrics
from services.mail_connector.renewal import SubscriptionRenewalJob


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Provide a dedicated asyncpg connection pool connected to test database."""
    settings = AppSettings().database
    await apply_migrations(dsn=settings.asyncpg_dsn)
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


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
            VALUES ($1, $2, $3, $4, 'Test Mailbox', $5)
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            provider,
            address,
            status,
        )


@pytest.mark.asyncio
async def test_postgres_subscription_crud_and_tenant_isolation(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify PostgreSQL subscription store CRUD and multi-tenant isolation (R5.3)."""
    sub_store = PostgresSubscriptionStore(db_pool)

    # 3 tenants (R5.3 / testing strategy mandate)
    org1 = uuid.uuid4()
    org2 = uuid.uuid4()
    org3 = uuid.uuid4()

    mbx1 = uuid.uuid4()
    mbx2 = uuid.uuid4()
    mbx3 = uuid.uuid4()

    try:
        await ensure_test_org(db_pool, org1, "Tenant 1")
        await ensure_test_org(db_pool, org2, "Tenant 2")
        await ensure_test_org(db_pool, org3, "Tenant 3")

        await ensure_test_mailbox(db_pool, org1, mbx1, "user1@tenant1.com")
        await ensure_test_mailbox(db_pool, org2, mbx2, "user2@tenant2.com")
        await ensure_test_mailbox(db_pool, org3, mbx3, "user3@tenant3.com")

        now = datetime.now(UTC)
        sub1 = Subscription(
            mailbox_id=mbx1,
            subscription_id=f"sub-t1-{uuid.uuid4().hex[:6]}",
            expires_at=now + timedelta(hours=6),
            provider="gmail",
            resource="inbox",
            client_state="secret-1",
            organization_id=org1,
        )
        sub2 = Subscription(
            mailbox_id=mbx2,
            subscription_id=f"sub-t2-{uuid.uuid4().hex[:6]}",
            expires_at=now + timedelta(hours=12),
            provider="gmail",
            resource="inbox",
            client_state="secret-2",
            organization_id=org2,
        )
        sub3 = Subscription(
            mailbox_id=mbx3,
            subscription_id=f"sub-t3-{uuid.uuid4().hex[:6]}",
            expires_at=now + timedelta(days=5),
            provider="gmail",
            resource="inbox",
            client_state="secret-3",
            organization_id=org3,
        )

        await sub_store.save(sub1)
        await sub_store.save(sub2)
        await sub_store.save(sub3)

        # 1. Fetch by subscription_id
        got1 = await sub_store.get(sub1.subscription_id, organization_id=org1)
        assert got1 is not None
        assert got1.subscription_id == sub1.subscription_id
        assert got1.organization_id == org1
        assert got1.mailbox_id == mbx1

        # Tenant isolation check: querying org1 subscription with org2 should return None
        assert await sub_store.get(sub1.subscription_id, organization_id=org2) is None
        assert await sub_store.get_by_mailbox(mbx1, organization_id=org2) is None

        # 2. List expiring within 24h
        expiring = await sub_store.list_expiring(before=now + timedelta(hours=24))
        expiring_ids = {s.subscription_id for s in expiring}
        assert sub1.subscription_id in expiring_ids
        assert sub2.subscription_id in expiring_ids
        assert sub3.subscription_id not in expiring_ids

        # 3. Record outcome
        new_exp = now + timedelta(days=7)
        await sub_store.record_renewal_outcome(
            subscription_id=sub1.subscription_id,
            status="success",
            new_expires_at=new_exp,
            organization_id=org1,
        )
        updated1 = await sub_store.get(sub1.subscription_id, organization_id=org1)
        assert updated1 is not None
        assert updated1.last_renewal_status == "success"
        assert updated1.last_renewed_at is not None

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM organization WHERE id IN ($1, $2, $3)", org1, org2, org3
            )


@pytest.mark.asyncio
async def test_postgres_subscription_renewal_job_auth_expired_flow(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """Verify renewal cycle with PostgreSQL, recording outcomes and AuthExpired (R2.10, R1.5)."""
    sub_store = PostgresSubscriptionStore(db_pool)
    mbx_store = PostgresMailboxStore(db_pool)
    metrics = create_pipeline_metrics()

    org1 = uuid.uuid4()
    org2 = uuid.uuid4()
    mbx1 = uuid.uuid4()
    mbx2 = uuid.uuid4()

    try:
        await ensure_test_org(db_pool, org1, "Renewal Org 1")
        await ensure_test_org(db_pool, org2, "Renewal Org 2")

        await ensure_test_mailbox(db_pool, org1, mbx1, "ok@renewal1.com", status="active")
        await ensure_test_mailbox(db_pool, org2, mbx2, "expired@renewal2.com", status="active")

        now = datetime.now(UTC)
        sub1 = Subscription(
            mailbox_id=mbx1,
            subscription_id=f"sub-ok-{uuid.uuid4().hex[:6]}",
            expires_at=now + timedelta(hours=3),
            provider="gmail",
            organization_id=org1,
        )
        sub2 = Subscription(
            mailbox_id=mbx2,
            subscription_id=f"sub-expired-{uuid.uuid4().hex[:6]}",
            expires_at=now + timedelta(hours=4),
            provider="gmail",
            organization_id=org2,
        )
        await sub_store.save(sub1)
        await sub_store.save(sub2)

        # Build adapter mapping: mbx1 succeeds, mbx2 raises AuthExpired
        adapter_ok = FakeProviderAdapter()
        adapter_expired = FakeProviderAdapter()
        adapter_expired.inject_auth_expired(calls=1, message="Credentials revoked by admin")

        def resolver(mailbox: Any) -> Any:
            if mailbox.id == mbx1:
                return adapter_ok
            return adapter_expired

        job = SubscriptionRenewalJob(
            subscription_store=sub_store,
            mailbox_store=mbx_store,
            adapter_resolver=resolver,
            metrics=metrics,
        )

        summary = await job.run_once(now=now)

        assert summary.total_evaluated == 2
        assert summary.succeeded == 1
        assert summary.needs_reauth == 1

        # Mailbox 1 remains active and subscription renewed in DB
        db_mbx1 = await mbx_store.get(mbx1)
        assert db_mbx1 is not None
        assert db_mbx1.status == "active"

        db_sub1 = await sub_store.get(sub1.subscription_id, organization_id=org1)
        assert db_sub1 is not None
        assert db_sub1.last_renewal_status == "success"
        assert db_sub1.expires_at > now + timedelta(days=6)

        # Mailbox 2 transitioned to 'needs_reauth' in DB (R1.5, R2.10)
        db_mbx2 = await mbx_store.get(mbx2)
        assert db_mbx2 is not None
        assert db_mbx2.status == "needs_reauth"

        db_sub2 = await sub_store.get(sub2.subscription_id, organization_id=org2)
        assert db_sub2 is not None
        assert db_sub2.last_renewal_status == "needs_reauth"
        assert "Credentials revoked by admin" in str(db_sub2.last_error)

        # Second pass: mbx2 is in 'needs_reauth', so it must be skipped
        summary2 = await job.run_once(now=now)
        assert summary2.skipped == 1

    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id IN ($1, $2)", org1, org2)

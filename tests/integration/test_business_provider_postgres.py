"""PostgresBusinessDataProvider on real PostgreSQL (rag_email_test).

Requirements:
- R13.1: Reads the migration-0001 customer / "order" / ticket tables.
- R13.2: Passes the same contract suite as the in-memory provider.
- R13.4: Resolution and scoping proven on real Postgres (the scoping cases span 3 tenants).
- R13.7: The transaction-local statement_timeout cancels a blocked statement.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from packages.business.postgres import PostgresBusinessDataProvider
from packages.business.protocol import BusinessDataProvider
from packages.business.testing import (
    BusinessDataProviderContractSuite,
    BusinessDataset,
    order_ref,
)
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.domain.business import FetchPlan


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Pool on the isolated test database (tests/integration/conftest.py)."""
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def seed_business_rows(pool: asyncpg.Pool[Any], dataset: BusinessDataset) -> None:
    """Insert the dataset's organizations, customers, orders and tickets."""
    async with pool.acquire() as conn, conn.transaction():
        for org_id in dataset.organization_ids:
            await conn.execute(
                "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                org_id,
                f"Business Org {org_id}",
            )
        for c in dataset.customers:
            await conn.execute(
                "INSERT INTO customer (id, organization_id, email, name, account_status, tier) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                c["id"],
                c["organization_id"],
                c["email"],
                c["name"],
                c["account_status"],
                c["tier"],
            )
        for o in dataset.orders:
            await conn.execute(
                'INSERT INTO "order" (id, organization_id, customer_id, order_number, status, '
                "total, placed_at, shipped_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                o["id"],
                o["organization_id"],
                o["customer_id"],
                o["order_number"],
                o["status"],
                o["total"],
                o["placed_at"],
                o["shipped_at"],
            )
        for t in dataset.tickets:
            await conn.execute(
                "INSERT INTO ticket (id, organization_id, customer_id, ticket_number, status, "
                "priority, subject, opened_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                t["id"],
                t["organization_id"],
                t["customer_id"],
                t["ticket_number"],
                t["status"],
                t["priority"],
                t["subject"],
                t["opened_at"],
            )


class TestPostgresBusinessDataProviderContract(BusinessDataProviderContractSuite):
    """Proves PostgresBusinessDataProvider passes the canonical suite on live PostgreSQL."""

    @pytest.fixture(autouse=True)
    def setup_provider(self, db_pool: asyncpg.Pool[Any]) -> None:
        self.pool = db_pool

    async def make_provider(
        self,
        dataset: BusinessDataset,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> BusinessDataProvider:
        await seed_business_rows(self.pool, dataset)
        return PostgresBusinessDataProvider(
            self.pool, snapshot_orders=snapshot_orders, snapshot_tickets=snapshot_tickets
        )


async def test_statement_timeout_cancels_a_blocked_lookup(db_pool: asyncpg.Pool[Any]) -> None:
    """A lookup stuck behind a lock is cancelled by statement_timeout, not left hanging."""
    org = uuid4()
    data = BusinessDataset()
    data.add_customer(org, "blocked@example.com")
    await seed_business_rows(db_pool, data)
    provider = PostgresBusinessDataProvider(db_pool, statement_timeout_ms=100)

    async with db_pool.acquire() as locker, locker.transaction():
        await locker.execute("LOCK TABLE customer IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(asyncpg.exceptions.QueryCanceledError):
            await asyncio.wait_for(
                provider.get_business_context(
                    org, "blocked@example.com", FetchPlan(refs=(order_ref("ORD-1"),))
                ),
                timeout=5,
            )


async def test_statement_timeout_does_not_leak_to_the_pooled_connection(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """set_config(..., is_local=true) ends with the transaction; the next user sees the default."""
    provider = PostgresBusinessDataProvider(db_pool, statement_timeout_ms=123)
    await provider.get_business_context(uuid4(), "nobody@example.com", FetchPlan())
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SHOW statement_timeout") != "123ms"


def test_constructor_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="statement_timeout_ms"):
        PostgresBusinessDataProvider(object(), statement_timeout_ms=0)

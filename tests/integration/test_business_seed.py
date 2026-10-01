"""Business seed data on real Postgres: ORD-82915 for Alice and the 3-tenant fixtures (5.1).

Requirements: R13.1, R5.9. Runs in the isolated rag_email_test database (tests/integration
conftest). Every query is scoped by organization_id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid5

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import (
    AMBIGUOUS_CUSTOMER_EMAIL,
    BIZ_DELTA_ORG_ID,
    BUSINESS_ORDERS,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_ORGS,
    BUSINESS_TENANT_TICKETS,
    CUST_ALICE_ID,
    DEMO_ORG_ID,
    ORDER_82915_ID,
    SHARED_CUSTOMER_EMAIL,
)
from packages.db.seed import SEED_NAMESPACE, seed_business_tenant_fixtures, seed_database


@pytest.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool[Any]]:
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def test_seed_puts_order_82915_with_items_on_alice(db_pool: asyncpg.Pool[Any]) -> None:
    await seed_database(pool=db_pool, clean=True)
    expected = next(o for o in BUSINESS_ORDERS if o["id"] == ORDER_82915_ID)

    async with db_pool.acquire() as conn:
        order = await conn.fetchrow(
            'SELECT o.id, o.status, o.total, o.placed_at, o.shipped_at, c.email FROM "order" o '
            "JOIN customer c ON c.id = o.customer_id AND c.organization_id = o.organization_id "
            "WHERE o.organization_id = $1 AND o.order_number = $2",
            DEMO_ORG_ID,
            "ORD-82915",
        )
        assert order is not None
        assert order["id"] == ORDER_82915_ID
        assert order["email"] == "alice.smith@clientcorp.com"
        assert order["status"] == "dispatched"
        assert order["placed_at"] == expected["placed_at"]
        assert order["shipped_at"] == expected["shipped_at"]

        items = await conn.fetch(
            "SELECT p.sku, i.quantity, i.unit_price FROM order_item i "
            "JOIN product p ON p.id = i.product_id AND p.organization_id = i.organization_id "
            "WHERE i.organization_id = $1 AND i.order_id = $2 ORDER BY p.sku",
            DEMO_ORG_ID,
            ORDER_82915_ID,
        )
        assert [(r["sku"], r["quantity"]) for r in items] == [
            ("SKU-CABLE-03", 4),
            ("SKU-SENSOR-02", 2),
        ]
        total = sum(r["quantity"] * r["unit_price"] for r in items)
        assert total == order["total"] == Decimal("290.00")

        newest_first = await conn.fetch(
            'SELECT order_number FROM "order" WHERE organization_id = $1 AND customer_id = $2 '
            "ORDER BY placed_at DESC",
            DEMO_ORG_ID,
            CUST_ALICE_ID,
        )
        assert [r["order_number"] for r in newest_first] == ["ORD-82915", "ORD-8820"]

        owner = await conn.fetchval(
            'SELECT c.email FROM "order" o JOIN customer c '
            "ON c.id = o.customer_id AND c.organization_id = o.organization_id "
            "WHERE o.organization_id = $1 AND o.order_number = $2",
            DEMO_ORG_ID,
            "ORD-9901",
        )
        assert owner == "dana.scully@fbi.gov"  # Edward's email cites Dana's order (R13.4)


async def test_seed_stores_alices_order_status_email(db_pool: asyncpg.Pool[Any]) -> None:
    await seed_database(pool=db_pool, clean=True)
    message = await db_pool.fetchrow(
        "SELECT sender_email, thread_id, body_text FROM email_message "
        "WHERE organization_id = $1 AND id = $2",
        DEMO_ORG_ID,
        uuid5(SEED_NAMESPACE, "msg:order_status_inquiry:primary"),
    )
    assert message is not None
    assert message["sender_email"] == "alice.smith@clientcorp.com"
    assert message["thread_id"] == uuid5(SEED_NAMESPACE, "thread:order_status_inquiry")
    assert "What is the status of order 82915?" in message["body_text"]


async def test_business_tenant_fixtures_load_into_three_tenants(
    db_pool: asyncpg.Pool[Any],
) -> None:
    await seed_business_tenant_fixtures(db_pool)
    await seed_business_tenant_fixtures(db_pool)  # idempotent
    orgs = [o["id"] for o in BUSINESS_TENANT_ORGS]

    async with db_pool.acquire() as conn:
        shared = await conn.fetch(
            "SELECT organization_id, count(*) AS n FROM customer "
            "WHERE organization_id = ANY($1::uuid[]) AND lower(email) = lower($2) "
            "GROUP BY organization_id",
            orgs,
            SHARED_CUSTOMER_EMAIL,
        )
        assert {r["organization_id"]: r["n"] for r in shared} == dict.fromkeys(orgs, 1)

        same_number = await conn.fetch(
            'SELECT organization_id, customer_id, status FROM "order" '
            "WHERE organization_id = ANY($1::uuid[]) AND order_number = $2",
            orgs,
            "ORD-82915",
        )
        assert {r["organization_id"] for r in same_number} == set(orgs)
        assert len({r["status"] for r in same_number}) == 3

        ambiguous = await conn.fetchval(
            "SELECT count(*) FROM customer WHERE organization_id = $1 AND lower(email) = lower($2)",
            BIZ_DELTA_ORG_ID,
            AMBIGUOUS_CUSTOMER_EMAIL,
        )
        assert ambiguous == 2

        crossed = await conn.fetchval(
            'SELECT count(*) FROM "order" o JOIN customer c ON c.id = o.customer_id '
            "WHERE o.organization_id = ANY($1::uuid[]) AND c.organization_id <> o.organization_id",
            orgs,
        )
        assert crossed == 0

        counts = await conn.fetchrow(
            "SELECT "
            "(SELECT count(*) FROM customer WHERE organization_id = ANY($1::uuid[])) AS customers, "
            '(SELECT count(*) FROM "order" WHERE organization_id = ANY($1::uuid[])) AS orders, '
            "(SELECT count(*) FROM ticket WHERE organization_id = ANY($1::uuid[])) AS tickets",
            orgs,
        )
        assert counts is not None
        assert (counts["customers"], counts["orders"], counts["tickets"]) == (
            len(BUSINESS_TENANT_CUSTOMERS),
            len(BUSINESS_TENANT_ORDERS),
            len(BUSINESS_TENANT_TICKETS),
        )

"""PostgreSQL BusinessDataProvider over the migration-0001 business tables (R13.1, R13.2).

Every query is a fixed, parameterised statement whose first predicate is
`organization_id = $1`; order and ticket queries also carry `customer_id = $2`. The whole
call runs in one read-only transaction with a transaction-local `statement_timeout`
(design §5.4 "Timeout and degradation"); the outer deadline is `fetch_business_context`'s.

Requirements:
- R13.2: The local implementation behind the replaceable interface.
- R13.4: Case-insensitive whole-address resolution inside the organization; lookups scoped
  to the resolved customer.
- R13.7: `statement_timeout` bounds each statement; errors propagate to the caller.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from packages.business.assemble import Row, assemble_business_context
from packages.domain.business import BusinessContext, FetchPlan

logger = logging.getLogger(__name__)

SET_STATEMENT_TIMEOUT_SQL = "SELECT set_config('statement_timeout', $1, true)"

CUSTOMERS_BY_EMAIL_SQL = """
SELECT id, name, account_status, tier
FROM customer
WHERE organization_id = $1 AND lower(email) = lower($2)
ORDER BY id
LIMIT 2
"""

ORDER_BY_NUMBER_SQL = """
SELECT order_number, status, total, placed_at, shipped_at
FROM "order"
WHERE organization_id = $1 AND customer_id = $2 AND order_number = $3
ORDER BY placed_at DESC, id DESC
LIMIT 1
"""

TICKET_BY_NUMBER_SQL = """
SELECT ticket_number, status, priority, subject, opened_at
FROM ticket
WHERE organization_id = $1 AND customer_id = $2 AND ticket_number = $3
ORDER BY opened_at DESC, id DESC
LIMIT 1
"""

RECENT_ORDERS_SQL = """
SELECT order_number, status, total, placed_at, shipped_at
FROM "order"
WHERE organization_id = $1 AND customer_id = $2
ORDER BY placed_at DESC, order_number COLLATE "C" DESC
LIMIT $3
"""

OPEN_TICKETS_SQL = """
SELECT ticket_number, status, priority, subject, opened_at
FROM ticket
WHERE organization_id = $1 AND customer_id = $2
  AND lower(status) NOT IN ('closed', 'resolved')
ORDER BY opened_at DESC, ticket_number COLLATE "C" DESC
LIMIT $3
"""

TENANT_SCOPED_QUERIES: tuple[str, ...] = (
    CUSTOMERS_BY_EMAIL_SQL,
    ORDER_BY_NUMBER_SQL,
    TICKET_BY_NUMBER_SQL,
    RECENT_ORDERS_SQL,
    OPEN_TICKETS_SQL,
)
"""Every business query; a unit test asserts each is scoped (R13.4, CLAUDE.md §4)."""


class _ConnectionLookups:
    """CustomerLookups bound to one connection inside the provider's transaction."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def customers_by_email(self, organization_id: UUID, sender_email: str) -> list[Row]:
        rows = await self._conn.fetch(CUSTOMERS_BY_EMAIL_SQL, organization_id, sender_email)
        return [dict(r) for r in rows]

    async def order_by_number(
        self, organization_id: UUID, customer_id: UUID, order_number: str
    ) -> Row | None:
        row = await self._conn.fetchrow(
            ORDER_BY_NUMBER_SQL, organization_id, customer_id, order_number
        )
        return None if row is None else dict(row)

    async def ticket_by_number(
        self, organization_id: UUID, customer_id: UUID, ticket_number: str
    ) -> Row | None:
        row = await self._conn.fetchrow(
            TICKET_BY_NUMBER_SQL, organization_id, customer_id, ticket_number
        )
        return None if row is None else dict(row)

    async def recent_orders(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> list[Row]:
        rows = await self._conn.fetch(RECENT_ORDERS_SQL, organization_id, customer_id, limit)
        return [dict(r) for r in rows]

    async def open_tickets(self, organization_id: UUID, customer_id: UUID, limit: int) -> list[Row]:
        rows = await self._conn.fetch(OPEN_TICKETS_SQL, organization_id, customer_id, limit)
        return [dict(r) for r in rows]


class PostgresBusinessDataProvider:
    """Business lookups against the local PostgreSQL business subsystem."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
        statement_timeout_ms: int = 500,
    ) -> None:
        if statement_timeout_ms < 1:
            raise ValueError("statement_timeout_ms must be >= 1")
        self._pool = pool
        self.snapshot_orders = snapshot_orders
        self.snapshot_tickets = snapshot_tickets
        self.statement_timeout_ms = statement_timeout_ms

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.fetchval(SET_STATEMENT_TIMEOUT_SQL, str(self.statement_timeout_ms))
            return await assemble_business_context(
                _ConnectionLookups(conn),
                organization_id=organization_id,
                sender_email=sender_email,
                plan=plan,
                snapshot_orders=self.snapshot_orders,
                snapshot_tickets=self.snapshot_tickets,
                as_of=datetime.now(UTC),
            )

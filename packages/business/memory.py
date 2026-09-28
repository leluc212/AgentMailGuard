"""In-memory BusinessDataProvider for unit tests (R13.2).

Holds customer/order/ticket dicts shaped like `packages/db/fixtures/business.py` records.
Optional keys: `placed_at` / `shipped_at` on orders and `opened_at` on tickets (aware
datetimes); a missing timestamp sorts oldest, as a stand-in for the database default.

Requirements:
- R13.2: A second implementation of the same interface, proven by the shared contract suite.
- R13.4: Every lookup filters on organization_id and the resolved customer_id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from packages.business.assemble import (
    CLOSED_TICKET_STATUSES,
    Row,
    assemble_business_context,
    normalise_email,
)
from packages.domain.business import BusinessContext, FetchPlan

_OLDEST = datetime.min.replace(tzinfo=UTC)


class InMemoryBusinessDataProvider:
    """Dict-backed provider with the same resolution and ordering rules as PostgreSQL."""

    def __init__(
        self,
        customers: Sequence[dict[str, Any]] = (),
        orders: Sequence[dict[str, Any]] = (),
        tickets: Sequence[dict[str, Any]] = (),
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> None:
        self._customers: list[Row] = [dict(c) for c in customers]
        self._orders: list[Row] = [dict(o) for o in orders]
        self._tickets: list[Row] = [dict(t) for t in tickets]
        self.snapshot_orders = snapshot_orders
        self.snapshot_tickets = snapshot_tickets

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        return await assemble_business_context(
            self,
            organization_id=organization_id,
            sender_email=sender_email,
            plan=plan,
            snapshot_orders=self.snapshot_orders,
            snapshot_tickets=self.snapshot_tickets,
            as_of=datetime.now(UTC),
        )

    # --- CustomerLookups -------------------------------------------------------------

    async def customers_by_email(self, organization_id: UUID, sender_email: str) -> list[Row]:
        wanted = normalise_email(sender_email)
        return [
            c
            for c in self._customers
            if c["organization_id"] == organization_id and str(c["email"]).lower() == wanted
        ][:2]

    async def order_by_number(
        self, organization_id: UUID, customer_id: UUID, order_number: str
    ) -> Row | None:
        rows = self._sorted_orders(organization_id, customer_id)
        return next((o for o in rows if o["order_number"] == order_number), None)

    async def ticket_by_number(
        self, organization_id: UUID, customer_id: UUID, ticket_number: str
    ) -> Row | None:
        rows = self._sorted_tickets(organization_id, customer_id)
        return next((t for t in rows if t["ticket_number"] == ticket_number), None)

    async def recent_orders(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> list[Row]:
        return self._sorted_orders(organization_id, customer_id)[:limit]

    async def open_tickets(self, organization_id: UUID, customer_id: UUID, limit: int) -> list[Row]:
        rows = self._sorted_tickets(organization_id, customer_id)
        return [t for t in rows if str(t["status"]).lower() not in CLOSED_TICKET_STATUSES][:limit]

    def _sorted_orders(self, organization_id: UUID, customer_id: UUID) -> list[Row]:
        rows = [
            o
            for o in self._orders
            if o["organization_id"] == organization_id and o["customer_id"] == customer_id
        ]
        return sorted(
            rows, key=lambda o: (o.get("placed_at") or _OLDEST, o["order_number"]), reverse=True
        )

    def _sorted_tickets(self, organization_id: UUID, customer_id: UUID) -> list[Row]:
        rows = [
            t
            for t in self._tickets
            if t["organization_id"] == organization_id and t["customer_id"] == customer_id
        ]
        return sorted(
            rows, key=lambda t: (t.get("opened_at") or _OLDEST, t["ticket_number"]), reverse=True
        )

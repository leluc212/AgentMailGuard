"""Reusable contract test suite for BusinessDataProvider implementations.

Requirements:
- R13.2: One shared contract suite that every implementation (in-memory, PostgreSQL, a future
  CRM/ERP adapter) must pass.
- R13.4: Sender → customer resolution and customer/tenant scoping.
- R13.6: Missing entities are explicit NOT_FOUND / NOT_LOOKED_UP facts.
- GEMINI.md §8: The scoping cases run over ≥3 tenants with overlapping emails and numbers.
- specs/design.md §5.4 "Business data (R13)".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from packages.business.protocol import BusinessDataProvider
from packages.domain.business import (
    BusinessContext,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
"""Base timestamp for seeded rows; tests offset from it so ordering is explicit."""


@dataclass
class BusinessDataset:
    """Customer/order/ticket dicts in the `packages/db/fixtures/business.py` record shape."""

    customers: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def organization_ids(self) -> list[UUID]:
        seen: dict[UUID, None] = {}
        for row in (*self.customers, *self.orders, *self.tickets):
            seen.setdefault(row["organization_id"], None)
        return list(seen)

    def add_customer(
        self,
        organization_id: UUID,
        email: str,
        name: str = "Test Customer",
        *,
        tier: str = "standard",
        account_status: str = "active",
    ) -> UUID:
        customer_id = uuid4()
        self.customers.append(
            {
                "id": customer_id,
                "organization_id": organization_id,
                "email": email,
                "name": name,
                "account_status": account_status,
                "tier": tier,
            }
        )
        return customer_id

    def add_order(
        self,
        organization_id: UUID,
        customer_id: UUID,
        order_number: str,
        status: str,
        *,
        total: Decimal = Decimal("10.00"),
        placed_at: datetime = T0,
        shipped_at: datetime | None = None,
    ) -> UUID:
        order_id = uuid4()
        self.orders.append(
            {
                "id": order_id,
                "organization_id": organization_id,
                "customer_id": customer_id,
                "order_number": order_number,
                "status": status,
                "total": total,
                "placed_at": placed_at,
                "shipped_at": shipped_at,
            }
        )
        return order_id

    def add_ticket(
        self,
        organization_id: UUID,
        customer_id: UUID,
        ticket_number: str,
        status: str,
        *,
        priority: str = "normal",
        subject: str = "Help needed",
        opened_at: datetime = T0,
    ) -> UUID:
        ticket_id = uuid4()
        self.tickets.append(
            {
                "id": ticket_id,
                "organization_id": organization_id,
                "customer_id": customer_id,
                "ticket_number": ticket_number,
                "subject": subject,
                "status": status,
                "priority": priority,
                "opened_at": opened_at,
            }
        )
        return ticket_id


def order_ref(number: str) -> EntityRef:
    return EntityRef(EntityType.ORDER, number)


def ticket_ref(number: str) -> EntityRef:
    return EntityRef(EntityType.TICKET, number)


def invoice_ref(number: str) -> EntityRef:
    return EntityRef(EntityType.INVOICE, number)


def fact_rows(ctx: BusinessContext) -> list[tuple[str, str | None, str, str | None]]:
    """(entity, reference, status, reason) per fact, for compact assertions."""
    return [(f.entity.value, f.reference, f.status.value, f.reason) for f in ctx.facts]


class BusinessDataProviderContractSuite(ABC):
    """Abstract contract suite. Subclasses implement `make_provider` only."""

    @abstractmethod
    async def make_provider(
        self,
        dataset: BusinessDataset,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> BusinessDataProvider:
        """Load `dataset` into the implementation under test and return a provider over it."""
        raise NotImplementedError

    async def test_satisfies_protocol(self) -> None:
        provider = await self.make_provider(BusinessDataset())
        assert isinstance(provider, BusinessDataProvider)

    async def test_typed_order_found_with_attributes(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        alice = data.add_customer(org, "alice@example.com", "Alice Smith", tier="enterprise")
        data.add_order(
            org,
            alice,
            "ORD-82915",
            "shipped",
            total=Decimal("450.5"),
            placed_at=T0,
            shipped_at=T0 + timedelta(days=2),
        )
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "alice@example.com", FetchPlan(refs=(order_ref("ORD-82915"),))
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ctx.customer == (
            ("name", "Alice Smith"),
            ("account_status", "active"),
            ("tier", "enterprise"),
        )
        assert ctx.source == "business_db"
        assert ctx.degraded is False
        assert ctx.as_of.tzinfo is not None
        assert len(ctx.facts) == 1
        fact = ctx.facts[0]
        assert (fact.entity, fact.reference, fact.status) == (
            EntityType.ORDER,
            "ORD-82915",
            FactStatus.FOUND,
        )
        assert fact.attributes == (
            ("status", "shipped"),
            ("total", "450.50"),
            ("placed_at", "2026-09-01"),
            ("shipped_at", "2026-09-03"),
        )

    async def test_typed_ticket_found_even_when_closed(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        ed = data.add_customer(org, "ed@example.com")
        data.add_ticket(org, ed, "TICK-4402", "closed", priority="high", subject="Late parcel")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "ed@example.com", FetchPlan(refs=(ticket_ref("TICK-4402"),))
        )

        assert fact_rows(ctx) == [("ticket", "TICK-4402", "FOUND", None)]
        assert ctx.facts[0].attributes == (
            ("status", "closed"),
            ("priority", "high"),
            ("subject", "Late parcel"),
            ("opened_at", "2026-09-01"),
        )

    async def test_missing_typed_references_are_not_found(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        data.add_customer(org, "alice@example.com")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "alice@example.com",
            FetchPlan(refs=(order_ref("ORD-11111"), ticket_ref("TICK-22222"))),
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert fact_rows(ctx) == [
            ("order", "ORD-11111", "NOT_FOUND", None),
            ("ticket", "TICK-22222", "NOT_FOUND", None),
        ]

    async def test_invoice_reference_is_not_looked_up(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        data.add_customer(org, "bob@example.com")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "bob@example.com", FetchPlan(refs=(invoice_ref("INV-2026-8891"),))
        )

        assert fact_rows(ctx) == [
            ("invoice", "INV-2026-8891", "NOT_LOOKED_UP", "unsupported_entity")
        ]

    async def test_order_snapshot_is_newest_first_and_limited(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        for day, number in enumerate(["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004"]):
            data.add_order(org, c, number, "processing", placed_at=T0 + timedelta(days=day))
        provider = await self.make_provider(data, snapshot_orders=3)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.ORDER}))
        )

        assert [f.reference for f in ctx.facts] == ["ORD-1004", "ORD-1003", "ORD-1002"]
        assert {f.status for f in ctx.facts} == {FactStatus.FOUND}

    async def test_snapshot_ties_break_by_number_descending(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        for number in ["ORD-2001", "ORD-2003", "ORD-2002"]:
            data.add_order(org, c, number, "processing", placed_at=T0)
        provider = await self.make_provider(data, snapshot_orders=2)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.ORDER}))
        )

        assert [f.reference for f in ctx.facts] == ["ORD-2003", "ORD-2002"]

    async def test_ticket_snapshot_skips_closed_and_resolved(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_ticket(org, c, "TICK-3001", "open", opened_at=T0)
        data.add_ticket(org, c, "TICK-3002", "Closed", opened_at=T0 + timedelta(days=1))
        data.add_ticket(org, c, "TICK-3003", "resolved", opened_at=T0 + timedelta(days=2))
        data.add_ticket(org, c, "TICK-3004", "pending", opened_at=T0 + timedelta(days=3))
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.TICKET}))
        )

        assert fact_rows(ctx) == [
            ("ticket", "TICK-3004", "FOUND", None),
            ("ticket", "TICK-3001", "FOUND", None),
        ]

    async def test_empty_snapshot_is_one_not_found_fact_per_entity(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_ticket(org, c, "TICK-3002", "closed")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "c@example.com",
            FetchPlan(snapshot=frozenset({EntityType.TICKET, EntityType.ORDER})),
        )

        assert fact_rows(ctx) == [
            ("order", None, "NOT_FOUND", None),
            ("ticket", None, "NOT_FOUND", None),
        ]

    async def test_facts_follow_plan_order_without_repeating_typed_rows(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_order(org, c, "ORD-4001", "shipped", placed_at=T0)
        data.add_order(org, c, "ORD-4002", "processing", placed_at=T0 + timedelta(days=1))
        data.add_ticket(org, c, "TICK-4001", "open")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "c@example.com",
            FetchPlan(
                refs=(ticket_ref("TICK-9999"), order_ref("ORD-4001")),
                snapshot=frozenset({EntityType.TICKET, EntityType.ORDER}),
            ),
        )

        assert fact_rows(ctx) == [
            ("ticket", "TICK-9999", "NOT_FOUND", None),
            ("order", "ORD-4001", "FOUND", None),
            ("order", "ORD-4002", "FOUND", None),
            ("ticket", "TICK-4001", "FOUND", None),
        ]

    async def test_snapshot_limit_zero_disables_that_entity(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_order(org, c, "ORD-5001", "shipped")
        provider = await self.make_provider(data, snapshot_orders=0)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.ORDER}))
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ctx.facts == ()

    async def test_unknown_sender_marks_every_planned_fact_not_looked_up(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "known@example.com")
        data.add_order(org, c, "ORD-6001", "shipped")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "stranger@example.com",
            FetchPlan(
                refs=(order_ref("ORD-6001"), invoice_ref("INV-2026-1")),
                snapshot=frozenset({EntityType.TICKET}),
            ),
        )

        assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER
        assert ctx.customer == ()
        assert fact_rows(ctx) == [
            ("order", "ORD-6001", "NOT_LOOKED_UP", "unknown_sender"),
            ("invoice", "INV-2026-1", "NOT_LOOKED_UP", "unsupported_entity"),
            ("ticket", None, "NOT_LOOKED_UP", "unknown_sender"),
        ]

    async def test_ambiguous_customer_marks_every_planned_fact_not_looked_up(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        first = data.add_customer(org, "shared@example.com", "First")
        data.add_customer(org, "Shared@Example.com", "Second")
        data.add_order(org, first, "ORD-7001", "shipped")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "shared@example.com",
            FetchPlan(refs=(order_ref("ORD-7001"),), snapshot=frozenset({EntityType.ORDER})),
        )

        assert ctx.customer_status is CustomerStatus.AMBIGUOUS_CUSTOMER
        assert ctx.customer == ()
        assert fact_rows(ctx) == [
            ("order", "ORD-7001", "NOT_LOOKED_UP", "ambiguous_customer"),
            ("order", None, "NOT_LOOKED_UP", "ambiguous_customer"),
        ]

    async def test_empty_plan_resolves_customer_and_returns_no_facts(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        data.add_customer(org, "c@example.com")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(org, "c@example.com", FetchPlan())

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ctx.facts == ()

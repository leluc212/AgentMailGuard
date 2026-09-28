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
from packages.db.fixtures.business_tenants import (
    AMBIGUOUS_CUSTOMER_EMAIL,
    BIZ_DELTA_ORG_ID,
    BIZ_HARBOR_ORG_ID,
    BIZ_SUMMIT_ORG_ID,
    SHARED_CUSTOMER_EMAIL,
)
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


SHARED_EMAIL = "pat.buyer@example.com"
"""One address that is a different customer in each of the three tenants."""
TWIN_EMAIL = "twin@example.com"
"""Two customers in tenant A, one in tenant C."""
RIVAL_EMAIL = "rival@example.com"
"""A second customer of tenant A only."""
SHARED_ORDER = "ORD-50001"
"""One order number stored in tenants A and B for different customers."""


@dataclass(frozen=True)
class MultiTenantBusinessData:
    """Three tenants with overlapping customer emails and order numbers (GEMINI.md §8)."""

    dataset: BusinessDataset
    org_a: UUID
    org_b: UUID
    org_c: UUID


def build_multi_tenant_dataset() -> MultiTenantBusinessData:
    """Fresh ids on every call, so it can be inserted into a database that is not reset.

    Tenant A: Pat (ORD-50001 shipped, TICK-7001 open); Rival (ORD-60002, TICK-7002);
              two customers sharing TWIN_EMAIL.
    Tenant B: Pat stored in mixed case (ORD-50001 cancelled). No Rival.
    Tenant C: Pat with only ORD-50002; one TWIN_EMAIL customer.
    """
    org_a, org_b, org_c = uuid4(), uuid4(), uuid4()
    data = BusinessDataset()

    pat_a = data.add_customer(org_a, SHARED_EMAIL, "Pat Alpha")
    data.add_order(org_a, pat_a, SHARED_ORDER, "shipped", placed_at=T0)
    data.add_ticket(org_a, pat_a, "TICK-7001", "open")
    rival_a = data.add_customer(org_a, RIVAL_EMAIL, "Rival Alpha")
    data.add_order(org_a, rival_a, "ORD-60002", "processing", placed_at=T0 + timedelta(days=1))
    data.add_ticket(org_a, rival_a, "TICK-7002", "open", opened_at=T0 + timedelta(days=1))
    data.add_customer(org_a, TWIN_EMAIL, "Twin One")
    data.add_customer(org_a, TWIN_EMAIL.upper(), "Twin Two")

    pat_b = data.add_customer(org_b, "Pat.Buyer@Example.COM", "Pat Beta")
    data.add_order(org_b, pat_b, SHARED_ORDER, "cancelled", placed_at=T0)

    pat_c = data.add_customer(org_c, SHARED_EMAIL, "Pat Gamma")
    data.add_order(org_c, pat_c, "ORD-50002", "delivered", placed_at=T0)
    data.add_customer(org_c, TWIN_EMAIL, "Twin Gamma")

    return MultiTenantBusinessData(dataset=data, org_a=org_a, org_b=org_b, org_c=org_c)


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

    # --- Sender resolution and scoping over three tenants (task 5.3, R13.4) -----------

    async def test_sender_match_ignores_case_and_surrounding_space(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(mt.org_b, "  PAT.BUYER@example.com ", FetchPlan())

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ("name", "Pat Beta") in ctx.customer

    async def test_sender_match_is_on_the_whole_address(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        for near_miss in ("at.buyer@example.com", "pat.buyer@example.co", "pat.buyer", ""):
            ctx = await provider.get_business_context(mt.org_a, near_miss, FetchPlan())
            assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER, near_miss

    async def test_same_email_resolves_to_each_tenants_own_customer_and_orders(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)
        plan = FetchPlan(refs=(order_ref(SHARED_ORDER),))

        seen = {}
        for org in (mt.org_a, mt.org_b, mt.org_c):
            ctx = await provider.get_business_context(org, SHARED_EMAIL, plan)
            assert ctx.customer_status is CustomerStatus.FOUND
            fact = ctx.facts[0]
            seen[org] = (
                dict(ctx.customer)["name"],
                fact.status,
                dict(fact.attributes).get("status"),
            )

        assert seen == {
            mt.org_a: ("Pat Alpha", FactStatus.FOUND, "shipped"),
            mt.org_b: ("Pat Beta", FactStatus.FOUND, "cancelled"),
            mt.org_c: ("Pat Gamma", FactStatus.NOT_FOUND, None),
        }

    async def test_another_customers_order_and_ticket_are_not_found(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(
            mt.org_a,
            SHARED_EMAIL,
            FetchPlan(refs=(order_ref("ORD-60002"), ticket_ref("TICK-7002"))),
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert fact_rows(ctx) == [
            ("order", "ORD-60002", "NOT_FOUND", None),
            ("ticket", "TICK-7002", "NOT_FOUND", None),
        ]
        assert all(f.attributes == () for f in ctx.facts)

    async def test_ambiguous_in_one_tenant_is_found_in_another(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)
        plan = FetchPlan(refs=(order_ref(SHARED_ORDER),))

        in_a = await provider.get_business_context(mt.org_a, TWIN_EMAIL, plan)
        in_c = await provider.get_business_context(mt.org_c, TWIN_EMAIL, plan)

        assert in_a.customer_status is CustomerStatus.AMBIGUOUS_CUSTOMER
        assert fact_rows(in_a) == [("order", SHARED_ORDER, "NOT_LOOKED_UP", "ambiguous_customer")]
        assert in_c.customer_status is CustomerStatus.FOUND
        assert ("name", "Twin Gamma") in in_c.customer
        assert fact_rows(in_c) == [("order", SHARED_ORDER, "NOT_FOUND", None)]

    async def test_customer_of_another_tenant_is_an_unknown_sender(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(
            mt.org_b,
            RIVAL_EMAIL,
            FetchPlan(refs=(order_ref("ORD-60002"),), snapshot=frozenset({EntityType.ORDER})),
        )

        assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER
        assert fact_rows(ctx) == [
            ("order", "ORD-60002", "NOT_LOOKED_UP", "unknown_sender"),
            ("order", None, "NOT_LOOKED_UP", "unknown_sender"),
        ]

    async def test_snapshot_holds_only_the_senders_rows_in_the_senders_tenant(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)
        plan = FetchPlan(snapshot=frozenset({EntityType.ORDER, EntityType.TICKET}))

        in_a = await provider.get_business_context(mt.org_a, SHARED_EMAIL, plan)
        in_b = await provider.get_business_context(mt.org_b, SHARED_EMAIL, plan)

        assert fact_rows(in_a) == [
            ("order", SHARED_ORDER, "FOUND", None),
            ("ticket", "TICK-7001", "FOUND", None),
        ]
        assert fact_rows(in_b) == [
            ("order", SHARED_ORDER, "FOUND", None),
            ("ticket", None, "NOT_FOUND", None),
        ]
        assert dict(in_b.facts[0].attributes)["status"] == "cancelled"

    async def test_unknown_organization_resolves_nobody(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(
            uuid4(), SHARED_EMAIL, FetchPlan(refs=(order_ref(SHARED_ORDER),))
        )

        assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER
        assert fact_rows(ctx) == [("order", SHARED_ORDER, "NOT_LOOKED_UP", "unknown_sender")]


async def check_business_tenant_fixtures(provider: BusinessDataProvider) -> None:
    """Scoping over the 5.1 fixtures (packages/db/fixtures/business_tenants.py, R13.4).

    `provider` must hold BUSINESS_TENANT_CUSTOMERS / _ORDERS / _TICKETS. ORD-82915 exists in all
    three tenants for three different customers; Summit stores Alice's address in mixed case;
    two Delta customers share AMBIGUOUS_CUSTOMER_EMAIL; Delta's Alice has no orders or tickets.
    """
    plan = FetchPlan(refs=(order_ref("ORD-82915"),))

    harbor = await provider.get_business_context(BIZ_HARBOR_ORG_ID, SHARED_CUSTOMER_EMAIL, plan)
    assert harbor.customer_status is CustomerStatus.FOUND
    assert fact_rows(harbor) == [("order", "ORD-82915", "FOUND", None)]
    assert dict(harbor.facts[0].attributes)["status"] == "shipped"

    summit = await provider.get_business_context(
        BIZ_SUMMIT_ORG_ID, SHARED_CUSTOMER_EMAIL.upper(), plan
    )
    assert summit.customer_status is CustomerStatus.FOUND
    assert dict(summit.facts[0].attributes)["status"] == "processing"

    delta = await provider.get_business_context(BIZ_DELTA_ORG_ID, SHARED_CUSTOMER_EMAIL, plan)
    assert delta.customer_status is CustomerStatus.FOUND
    assert fact_rows(delta) == [("order", "ORD-82915", "NOT_FOUND", None)]  # Brightline's order

    ambiguous = await provider.get_business_context(
        BIZ_DELTA_ORG_ID, AMBIGUOUS_CUSTOMER_EMAIL, plan
    )
    assert ambiguous.customer_status is CustomerStatus.AMBIGUOUS_CUSTOMER
    assert fact_rows(ambiguous) == [("order", "ORD-82915", "NOT_LOOKED_UP", "ambiguous_customer")]

    snapshot = FetchPlan(snapshot=frozenset({EntityType.ORDER, EntityType.TICKET}))
    harbor_snapshot = await provider.get_business_context(
        BIZ_HARBOR_ORG_ID, SHARED_CUSTOMER_EMAIL, snapshot
    )
    assert fact_rows(harbor_snapshot) == [  # 4 orders, limit 3; closed/resolved tickets skipped
        ("order", "ORD-82915", "FOUND", None),
        ("order", "ORD-7003", "FOUND", None),
        ("order", "ORD-7002", "FOUND", None),
        ("ticket", "TICK-4402", "FOUND", None),
    ]
    empty_snapshot = await provider.get_business_context(
        BIZ_DELTA_ORG_ID, SHARED_CUSTOMER_EMAIL, snapshot
    )
    assert fact_rows(empty_snapshot) == [  # a customer with zero orders and tickets
        ("order", None, "NOT_FOUND", None),
        ("ticket", None, "NOT_FOUND", None),
    ]

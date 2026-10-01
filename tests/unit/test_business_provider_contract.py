"""The in-memory BusinessDataProvider passes the shared contract suite (R13.2, R13.4, R13.6)."""

from __future__ import annotations

from packages.business.memory import InMemoryBusinessDataProvider
from packages.business.protocol import BusinessDataProvider
from packages.business.testing import (
    BusinessDataProviderContractSuite,
    BusinessDataset,
    check_business_tenant_fixtures,
    fact_rows,
    order_ref,
    ticket_ref,
)
from packages.db.fixtures import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDERS,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_TICKETS,
    BUSINESS_TICKETS,
    DEMO_ORG_ID,
)
from packages.domain.business import CustomerStatus, FetchPlan


class TestInMemoryBusinessDataProviderContract(BusinessDataProviderContractSuite):
    async def make_provider(
        self,
        dataset: BusinessDataset,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> BusinessDataProvider:
        return InMemoryBusinessDataProvider(
            dataset.customers,
            dataset.orders,
            dataset.tickets,
            snapshot_orders=snapshot_orders,
            snapshot_tickets=snapshot_tickets,
        )


async def test_seeded_fixtures_keep_the_cross_customer_case_not_found() -> None:
    """Edward's fixture email cites Dana's ORD-9901 and his own TICK-4402 (tasks.md 5.1/5.3)."""
    provider = InMemoryBusinessDataProvider(BUSINESS_CUSTOMERS, BUSINESS_ORDERS, BUSINESS_TICKETS)

    ctx = await provider.get_business_context(
        DEMO_ORG_ID,
        "Edward.Norton@FightClub.org",
        FetchPlan(refs=(order_ref("ORD-9901"), ticket_ref("TICK-4402"))),
    )

    assert ctx.customer_status is CustomerStatus.FOUND
    assert fact_rows(ctx) == [
        ("order", "ORD-9901", "NOT_FOUND", None),
        ("ticket", "TICK-4402", "FOUND", None),
    ]


async def test_seeded_business_tenant_fixtures_are_scoped_per_tenant() -> None:
    """tasks.md 5.3 over the 5.1 fixtures, in memory; PostgreSQL runs the same check."""
    await check_business_tenant_fixtures(
        InMemoryBusinessDataProvider(
            BUSINESS_TENANT_CUSTOMERS, BUSINESS_TENANT_ORDERS, BUSINESS_TENANT_TICKETS
        )
    )

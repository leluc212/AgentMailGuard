"""The in-memory BusinessDataProvider passes the shared contract suite (R13.2, R13.4, R13.6)."""

from __future__ import annotations

from packages.business.memory import InMemoryBusinessDataProvider
from packages.business.protocol import BusinessDataProvider
from packages.business.testing import BusinessDataProviderContractSuite, BusinessDataset


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

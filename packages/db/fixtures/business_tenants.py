"""Multi-tenant business fixtures for the business-data provider tests (R13.1, R13.4).

Three dedicated tenants, separate from the demo seed so `make seed` and its counts stay
unchanged, with deliberately overlapping content (CLAUDE.md §8):

- the same customer email in all three tenants (letter case differs in Summit);
- the same order number ORD-82915 and ticket TICK-4402 in all three tenants, each owned by
  a different customer with a different status;
- two customers sharing one email inside Delta (AMBIGUOUS_CUSTOMER, design §5.4);
- a Harbor customer with more orders than the default snapshot (3) and with closed and
  resolved tickets, and a Delta customer with no orders or tickets (snapshot NOT_FOUND).

Records have the same shape as packages/db/fixtures/business.py. Integration tests load them
with `packages.db.seed.seed_business_tenant_fixtures`; unit tests pass the lists to an
in-memory provider. `make seed` does not load them.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

BIZ_HARBOR_ORG_ID = UUID("00000000-0000-0000-0000-0000000000a1")  # Harbor Supply Co.
BIZ_SUMMIT_ORG_ID = UUID("00000000-0000-0000-0000-0000000000a2")  # Summit Retail
BIZ_DELTA_ORG_ID = UUID("00000000-0000-0000-0000-0000000000a3")  # Delta Parts

BUSINESS_TENANT_ORGS: list[dict[str, Any]] = [
    {
        "id": BIZ_HARBOR_ORG_ID,
        "name": "Harbor Supply Co.",
        "settings": {"tier": "standard", "domain": "harbor-supply.example"},
    },
    {
        "id": BIZ_SUMMIT_ORG_ID,
        "name": "Summit Retail",
        "settings": {"tier": "standard", "domain": "summit-retail.example"},
    },
    {
        "id": BIZ_DELTA_ORG_ID,
        "name": "Delta Parts",
        "settings": {"tier": "standard", "domain": "delta-parts.example"},
    },
]

SHARED_CUSTOMER_EMAIL = "alice.smith@clientcorp.com"
"""A customer of all three tenants (the demo org's Alice uses the same address)."""

AMBIGUOUS_CUSTOMER_EMAIL = "orders@brightline-hvac.com"
"""Two Delta customer records share this address, so resolution is AMBIGUOUS_CUSTOMER."""

HARBOR_ALICE_ID = UUID("11000000-0000-0000-0000-000000000001")
HARBOR_BOB_ID = UUID("11000000-0000-0000-0000-000000000002")
SUMMIT_ALICE_ID = UUID("11000000-0000-0000-0000-000000000003")
SUMMIT_CAROL_ID = UUID("11000000-0000-0000-0000-000000000004")
DELTA_ALICE_ID = UUID("11000000-0000-0000-0000-000000000005")
DELTA_BRIGHTLINE_OPS_ID = UUID("11000000-0000-0000-0000-000000000006")
DELTA_BRIGHTLINE_FIN_ID = UUID("11000000-0000-0000-0000-000000000007")

BUSINESS_TENANT_CUSTOMERS: list[dict[str, Any]] = [
    {
        "id": HARBOR_ALICE_ID,
        "organization_id": BIZ_HARBOR_ORG_ID,
        "email": SHARED_CUSTOMER_EMAIL,
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": HARBOR_BOB_ID,
        "organization_id": BIZ_HARBOR_ORG_ID,
        "email": "bob.jones@enterprises.org",
        "name": "Bob Jones",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": SUMMIT_ALICE_ID,
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "email": "Alice.Smith@ClientCorp.com",
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": SUMMIT_CAROL_ID,
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "email": "carol.white@summit-retail.example",
        "name": "Carol White",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": DELTA_ALICE_ID,
        "organization_id": BIZ_DELTA_ORG_ID,
        "email": SHARED_CUSTOMER_EMAIL,
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": DELTA_BRIGHTLINE_OPS_ID,
        "organization_id": BIZ_DELTA_ORG_ID,
        "email": AMBIGUOUS_CUSTOMER_EMAIL,
        "name": "Brightline HVAC Operations",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": DELTA_BRIGHTLINE_FIN_ID,
        "organization_id": BIZ_DELTA_ORG_ID,
        "email": AMBIGUOUS_CUSTOMER_EMAIL,
        "name": "Brightline HVAC Finance",
        "account_status": "active",
        "tier": "enterprise",
    },
]

BUSINESS_TENANT_ORDERS: list[dict[str, Any]] = [
    {
        "id": UUID("31000000-0000-0000-0000-000000000001"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-82915",
        "status": "shipped",
        "total": Decimal("640.00"),
        "placed_at": datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000002"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-7003",
        "status": "delivered",
        "total": Decimal("120.00"),
        "placed_at": datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 6, 9, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000003"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-7002",
        "status": "cancelled",
        "total": Decimal("75.50"),
        "placed_at": datetime(2026, 8, 18, 15, 30, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000004"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-7001",
        "status": "delivered",
        "total": Decimal("310.00"),
        "placed_at": datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 8, 1, 7, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000005"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_BOB_ID,
        "order_number": "ORD-9901",
        "status": "processing",
        "total": Decimal("980.00"),
        "placed_at": datetime(2026, 9, 26, 14, 0, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000006"),
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "customer_id": SUMMIT_ALICE_ID,
        "order_number": "ORD-82915",
        "status": "processing",
        "total": Decimal("215.00"),
        "placed_at": datetime(2026, 9, 24, 9, 45, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000007"),
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "customer_id": SUMMIT_CAROL_ID,
        "order_number": "ORD-9901",
        "status": "delivered",
        "total": Decimal("45.00"),
        "placed_at": datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000008"),
        "organization_id": BIZ_DELTA_ORG_ID,
        "customer_id": DELTA_BRIGHTLINE_OPS_ID,
        "order_number": "ORD-82915",
        "status": "on_hold",
        "total": Decimal("1875.00"),
        "placed_at": datetime(2026, 9, 19, 16, 20, tzinfo=UTC),
        "shipped_at": None,
    },
]

BUSINESS_TENANT_TICKETS: list[dict[str, Any]] = [
    {
        "id": UUID("41000000-0000-0000-0000-000000000001"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "ticket_number": "TICK-4402",
        "subject": "Courier has not scanned ORD-82915",
        "status": "open",
        "priority": "high",
        "opened_at": datetime(2026, 9, 25, 8, 30, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000002"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "ticket_number": "TICK-5001",
        "subject": "Invoice copy request",
        "status": "closed",
        "priority": "normal",
        "opened_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000003"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "ticket_number": "TICK-5002",
        "subject": "Damaged packaging on ORD-7001",
        "status": "resolved",
        "priority": "low",
        "opened_at": datetime(2026, 8, 20, 9, 15, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000004"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_BOB_ID,
        "ticket_number": "TICK-5003",
        "subject": "Change delivery address for ORD-9901",
        "status": "open",
        "priority": "normal",
        "opened_at": datetime(2026, 9, 26, 15, 0, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000005"),
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "customer_id": SUMMIT_ALICE_ID,
        "ticket_number": "TICK-4402",
        "subject": "Gift wrapping for ORD-82915",
        "status": "pending",
        "priority": "normal",
        "opened_at": datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000006"),
        "organization_id": BIZ_DELTA_ORG_ID,
        "customer_id": DELTA_BRIGHTLINE_FIN_ID,
        "ticket_number": "TICK-4402",
        "subject": "Credit hold on ORD-82915",
        "status": "open",
        "priority": "high",
        "opened_at": datetime(2026, 9, 22, 13, 0, tzinfo=UTC),
    },
]

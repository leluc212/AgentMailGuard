"""Business CRM and ERP record fixtures (R5.9, R13.1).

Defines realistic relational entities (customers, products, orders, items, tickets)
matching identifiers cited in fixture emails.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.db.fixtures.knowledge import DEMO_ORG_ID

# Stable customer UUIDs
CUST_ALICE_ID = UUID("10000000-0000-0000-0000-000000000001")
CUST_BOB_ID = UUID("10000000-0000-0000-0000-000000000002")
CUST_DANA_ID = UUID("10000000-0000-0000-0000-000000000003")
CUST_EDWARD_ID = UUID("10000000-0000-0000-0000-000000000004")

# Stable product UUIDs
PROD_WIDGET_ID = UUID("20000000-0000-0000-0000-000000000001")
PROD_SENSOR_ID = UUID("20000000-0000-0000-0000-000000000002")
PROD_CABLE_ID = UUID("20000000-0000-0000-0000-000000000003")

# Stable order UUIDs
ORDER_9901_ID = UUID("30000000-0000-0000-0000-000000000001")
ORDER_8820_ID = UUID("30000000-0000-0000-0000-000000000002")

# Stable ticket UUIDs
TICKET_4402_ID = UUID("40000000-0000-0000-0000-000000000001")
TICKET_1011_ID = UUID("40000000-0000-0000-0000-000000000002")

BUSINESS_CUSTOMERS: list[dict[str, Any]] = [
    {
        "id": CUST_ALICE_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "alice.smith@clientcorp.com",
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": CUST_BOB_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "bob.jones@enterprises.org",
        "name": "Bob Jones",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": CUST_DANA_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "dana.scully@fbi.gov",
        "name": "Dana Scully",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": CUST_EDWARD_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "edward.norton@fightclub.org",
        "name": "Edward Norton",
        "account_status": "active",
        "tier": "standard",
    },
]

BUSINESS_PRODUCTS: list[dict[str, Any]] = [
    {
        "id": PROD_WIDGET_ID,
        "organization_id": DEMO_ORG_ID,
        "sku": "SKU-WIDGET-01",
        "name": "Acme Enterprise Widget",
        "price": Decimal("450.00"),
        "status": "active",
    },
    {
        "id": PROD_SENSOR_ID,
        "organization_id": DEMO_ORG_ID,
        "sku": "SKU-SENSOR-02",
        "name": "Acme Pro Sensor",
        "price": Decimal("95.00"),
        "status": "active",
    },
    {
        "id": PROD_CABLE_ID,
        "organization_id": DEMO_ORG_ID,
        "sku": "SKU-CABLE-03",
        "name": "Industrial Connector Cable",
        "price": Decimal("25.00"),
        "status": "active",
    },
]

BUSINESS_ORDERS: list[dict[str, Any]] = [
    {
        "id": ORDER_9901_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_DANA_ID,
        "order_number": "ORD-9901",
        "status": "processing",
        "total": Decimal("1350.00"),
    },
    {
        "id": ORDER_8820_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_ALICE_ID,
        "order_number": "ORD-8820",
        "status": "shipped",
        "total": Decimal("450.00"),
    },
]

BUSINESS_ORDER_ITEMS: list[dict[str, Any]] = [
    {
        "id": UUID("35000000-0000-0000-0000-000000000001"),
        "organization_id": DEMO_ORG_ID,
        "order_id": ORDER_9901_ID,
        "product_id": PROD_WIDGET_ID,
        "quantity": 3,
        "unit_price": Decimal("450.00"),
    },
    {
        "id": UUID("35000000-0000-0000-0000-000000000002"),
        "organization_id": DEMO_ORG_ID,
        "order_id": ORDER_8820_ID,
        "product_id": PROD_WIDGET_ID,
        "quantity": 1,
        "unit_price": Decimal("450.00"),
    },
]

BUSINESS_TICKETS: list[dict[str, Any]] = [
    {
        "id": TICKET_4402_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_EDWARD_ID,
        "ticket_number": "TICK-4402",
        "subject": "Replacement shipment inquiry for delayed order",
        "status": "open",
        "priority": "high",
    },
    {
        "id": TICKET_1011_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_ALICE_ID,
        "ticket_number": "TICK-1011",
        "subject": "PDF export crash investigation",
        "status": "pending",
        "priority": "critical",
    },
]

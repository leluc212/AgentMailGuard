"""Unit tests for seed fixtures and deterministic embedding generation (R5.9)."""

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from email import message_from_bytes
from typing import Any

from packages.db.fixtures import (
    AMBIGUOUS_CUSTOMER_EMAIL,
    BETA_ORG_ID,
    BIZ_DELTA_ORG_ID,
    BIZ_HARBOR_ORG_ID,
    BIZ_SUMMIT_ORG_ID,
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDER_ITEMS,
    BUSINESS_ORDERS,
    BUSINESS_PRODUCTS,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_ORGS,
    BUSINESS_TENANT_TICKETS,
    BUSINESS_TICKETS,
    CUST_ALICE_ID,
    CUST_BOB_ID,
    CUST_DANA_ID,
    CUST_EDWARD_ID,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
    GAMMA_ORG_ID,
    HARBOR_ALICE_ID,
    KNOWLEDGE_DOCS,
    ORDER_9901_ID,
    ORDER_82915_ID,
    PROD_CABLE_ID,
    PROD_SENSOR_ID,
    PROD_WIDGET_ID,
    SHARED_CUSTOMER_EMAIL,
    TENANT_ORGS,
    TICKET_4402_ID,
)
from packages.db.seed import deterministic_embed


def test_fixture_emails_cover_mandatory_archetypes() -> None:
    """Validate that fixture emails cover all 6 required archetypes (R5.9)."""
    assert len(FIXTURE_EMAILS) >= 6

    expected_categories = {
        "support",
        "billing",
        "newsletter",
        "auto_reply",
        "long_thread",
        "identifier_bearing",
    }
    archetypes_found = {e.category for e in FIXTURE_EMAILS}
    assert expected_categories.issubset(archetypes_found)

    keys = {e.key for e in FIXTURE_EMAILS}
    assert "support_crash" in keys
    assert "billing_invoice" in keys
    assert "newsletter_marketing" in keys
    assert "auto_reply_vacation" in keys
    assert "long_thread_dispute" in keys
    assert "identifier_order_ticket" in keys


def test_email_fixture_mime_generation() -> None:
    """Validate RFC 822 MIME byte serialization for email fixtures."""
    for fixture in FIXTURE_EMAILS:
        mime_bytes = fixture.to_raw_mime(provider_message_id=f"test-{fixture.key}")
        assert isinstance(mime_bytes, bytes)
        assert len(mime_bytes) > 0

        # Parse with Python standard email parser
        parsed = message_from_bytes(mime_bytes)
        assert parsed["Subject"] == fixture.subject
        assert fixture.sender_name in parsed["From"]
        assert fixture.sender_email in parsed["From"]
        assert parsed["Message-ID"] is not None
        assert parsed["Date"] is not None


def test_newsletter_and_auto_reply_headers_and_flags() -> None:
    """Verify non-reply archetypes set correct headers and reply_required=False."""
    newsletter = next(e for e in FIXTURE_EMAILS if e.key == "newsletter_marketing")
    assert newsletter.reply_required is False
    assert newsletter.workflow_hint in ("none", "no_reply")
    newsletter_mime = message_from_bytes(newsletter.to_raw_mime())
    assert "List-Unsubscribe" in newsletter_mime

    auto_reply = next(e for e in FIXTURE_EMAILS if e.key == "auto_reply_vacation")
    assert auto_reply.reply_required is False
    assert auto_reply.workflow_hint in ("none", "no_reply")
    auto_reply_mime = message_from_bytes(auto_reply.to_raw_mime())
    assert auto_reply_mime.get("Auto-Submitted") == "auto-replied"


def test_long_thread_fixture_structure() -> None:
    """Verify multi-turn thread history fixtures and RFC 822 threading headers."""
    thread_fixture = next(e for e in FIXTURE_EMAILS if e.key == "long_thread_dispute")
    assert len(thread_fixture.thread_history) >= 2
    assert "In-Reply-To" in thread_fixture.headers
    assert "References" in thread_fixture.headers

    mime = message_from_bytes(thread_fixture.to_raw_mime())
    assert mime["In-Reply-To"] == thread_fixture.headers["In-Reply-To"]
    assert str(mime["References"]).split() == thread_fixture.headers["References"].split()


def test_multi_tenant_knowledge_corpus() -> None:
    """Verify 3-tenant knowledge corpus with overlapping content (R5.9, R10.11)."""
    assert len(TENANT_ORGS) == 3
    org_ids = {org["id"] for org in TENANT_ORGS}
    assert org_ids == {DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID}

    doc_orgs = {doc.org_id for doc in KNOWLEDGE_DOCS}
    assert doc_orgs == {DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID}

    # Verify each tenant has at least one document and chunks
    for org_id in [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID]:
        org_docs = [d for d in KNOWLEDGE_DOCS if d.org_id == org_id]
        assert len(org_docs) >= 1
        total_chunks = sum(len(d.chunks) for d in org_docs)
        assert total_chunks >= 1


def test_business_subsystem_record_consistency() -> None:
    """Verify business CRM/ERP entities and identifier matching (R13.1)."""
    # 4 customers
    assert len(BUSINESS_CUSTOMERS) == 4
    cust_ids = {c["id"] for c in BUSINESS_CUSTOMERS}
    assert {CUST_ALICE_ID, CUST_BOB_ID, CUST_DANA_ID}.issubset(cust_ids)

    # 3 products
    assert len(BUSINESS_PRODUCTS) == 3
    prod_ids = {p["id"] for p in BUSINESS_PRODUCTS}
    assert PROD_WIDGET_ID in prod_ids
    skus = {p["sku"] for p in BUSINESS_PRODUCTS}
    assert "SKU-WIDGET-01" in skus

    # Orders and items
    assert len(BUSINESS_ORDERS) >= 2
    order_ids = {o["id"] for o in BUSINESS_ORDERS}
    assert ORDER_9901_ID in order_ids

    order_numbers = {o["order_number"] for o in BUSINESS_ORDERS}
    assert "ORD-9901" in order_numbers

    # Verify order item foreign keys
    for item in BUSINESS_ORDER_ITEMS:
        assert item["order_id"] in order_ids
        assert item["product_id"] in prod_ids

    # Tickets
    assert len(BUSINESS_TICKETS) >= 2
    ticket_ids = {t["id"] for t in BUSINESS_TICKETS}
    assert TICKET_4402_ID in ticket_ids
    ticket_numbers = {t["ticket_number"] for t in BUSINESS_TICKETS}
    assert "TICK-4402" in ticket_numbers


def test_deterministic_embedding_generator() -> None:
    """Verify deterministic embedding generator output dimensions, norm, and repeatability."""
    text1 = "Acme Corporation offers a 30-day money-back guarantee."
    text2 = "Beta Industries standard warranty covers manufacturing defects for 14 days."

    vec1_a = deterministic_embed(text1, dim=1536)
    vec1_b = deterministic_embed(text1, dim=1536)
    vec2 = deterministic_embed(text2, dim=1536)

    # Dimensionality
    assert len(vec1_a) == 1536
    assert len(vec2) == 1536

    # Determinism
    assert vec1_a == vec1_b

    # Differentiation
    assert vec1_a != vec2

    # L2 unit normalization
    norm1 = math.sqrt(sum(x * x for x in vec1_a))
    norm2 = math.sqrt(sum(x * x for x in vec2))
    assert math.isclose(norm1, 1.0, rel_tol=1e-5)
    assert math.isclose(norm2, 1.0, rel_tol=1e-5)


def test_alice_order_82915_is_seeded_with_realistic_items() -> None:
    """5.1: ORD-82915 belongs to Alice in the demo org, and its items add up to its total."""
    order = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-82915")
    assert order["id"] == ORDER_82915_ID
    assert order["organization_id"] == DEMO_ORG_ID
    assert order["customer_id"] == CUST_ALICE_ID
    assert order["status"] == "dispatched"
    assert order["total"] == Decimal("290.00")
    assert order["shipped_at"] is not None and order["shipped_at"] > order["placed_at"]

    items = [i for i in BUSINESS_ORDER_ITEMS if i["order_id"] == ORDER_82915_ID]
    assert {(i["product_id"], i["quantity"]) for i in items} == {
        (PROD_SENSOR_ID, 2),
        (PROD_CABLE_ID, 4),
    }
    prices = {p["id"]: p["price"] for p in BUSINESS_PRODUCTS}
    for item in items:
        assert item["unit_price"] == prices[item["product_id"]]
        assert item["organization_id"] == DEMO_ORG_ID


def test_every_demo_order_total_equals_its_items() -> None:
    for order in BUSINESS_ORDERS:
        items = [i for i in BUSINESS_ORDER_ITEMS if i["order_id"] == order["id"]]
        assert items, order["order_number"]
        assert sum(i["quantity"] * i["unit_price"] for i in items) == order["total"]


def test_alice_order_status_fixture_email() -> None:
    """5.1: Alice asks for order 82915 in a non-billing email (support mailbox)."""
    email = next(e for e in FIXTURE_EMAILS if e.key == "order_status_inquiry")
    assert email.sender_email == "alice.smith@clientcorp.com"
    assert "What is the status of order 82915?" in email.body_text
    assert email.category == "order_status"
    assert email.identifiers == ["ORD-82915"]
    assert email.reply_required is True


def test_edward_still_asks_about_danas_order() -> None:
    """R13.4 cross-customer case: Edward's email cites ORD-9901, which is Dana's."""
    email = next(e for e in FIXTURE_EMAILS if e.key == "identifier_order_ticket")
    assert email.sender_email == "edward.norton@fightclub.org"
    assert "ORD-9901" in email.identifiers
    order = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-9901")
    assert order["customer_id"] == CUST_DANA_ID != CUST_EDWARD_ID


def _assert_dated(
    orders: Sequence[Mapping[str, Any]], tickets: Sequence[Mapping[str, Any]]
) -> None:
    for order in orders:
        placed = order["placed_at"]
        assert isinstance(placed, datetime) and placed.tzinfo is not None
    for ticket in tickets:
        opened = ticket["opened_at"]
        assert isinstance(opened, datetime) and opened.tzinfo is not None


def test_snapshot_ordering_is_deterministic_per_customer() -> None:
    """design §5.4 orders snapshots by placed_at / opened_at DESC: no ties within a customer."""
    for orders, tickets in (
        (BUSINESS_ORDERS, BUSINESS_TICKETS),
        (BUSINESS_TENANT_ORDERS, BUSINESS_TENANT_TICKETS),
    ):
        _assert_dated(orders, tickets)
        order_keys = [(o["organization_id"], o["customer_id"], o["placed_at"]) for o in orders]
        assert len(order_keys) == len(set(order_keys))
        ticket_keys = [(t["organization_id"], t["customer_id"], t["opened_at"]) for t in tickets]
        assert len(ticket_keys) == len(set(ticket_keys))


def test_business_tenant_fixtures_overlap_across_three_tenants() -> None:
    """CLAUDE.md §8: ≥3 tenants with overlapping customer emails and order numbers."""
    orgs = {o["id"] for o in BUSINESS_TENANT_ORGS}
    assert orgs == {BIZ_HARBOR_ORG_ID, BIZ_SUMMIT_ORG_ID, BIZ_DELTA_ORG_ID}
    assert orgs.isdisjoint({DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID})

    shared = [c for c in BUSINESS_TENANT_CUSTOMERS if c["email"].lower() == SHARED_CUSTOMER_EMAIL]
    assert {c["organization_id"] for c in shared} == orgs
    assert any(c["email"] != SHARED_CUSTOMER_EMAIL for c in shared)  # a case variant

    same_number = [o for o in BUSINESS_TENANT_ORDERS if o["order_number"] == "ORD-82915"]
    assert {o["organization_id"] for o in same_number} == orgs
    assert len({o["status"] for o in same_number}) == 3
    assert len({o["customer_id"] for o in same_number}) == 3

    same_ticket = [t for t in BUSINESS_TENANT_TICKETS if t["ticket_number"] == "TICK-4402"]
    assert {t["organization_id"] for t in same_ticket} == orgs

    ambiguous = [
        c
        for c in BUSINESS_TENANT_CUSTOMERS
        if c["organization_id"] == BIZ_DELTA_ORG_ID and c["email"] == AMBIGUOUS_CUSTOMER_EMAIL
    ]
    assert len(ambiguous) == 2


def test_business_tenant_fixtures_cover_snapshot_edges() -> None:
    """Harbor Alice has more orders than the default snapshot (3) and closed/resolved tickets."""
    alice_orders = [o for o in BUSINESS_TENANT_ORDERS if o["customer_id"] == HARBOR_ALICE_ID]
    assert len(alice_orders) > 3
    alice_ticket_statuses = {
        t["status"] for t in BUSINESS_TENANT_TICKETS if t["customer_id"] == HARBOR_ALICE_ID
    }
    assert {"closed", "resolved", "open"} <= alice_ticket_statuses

    with_rows = {o["customer_id"] for o in BUSINESS_TENANT_ORDERS} | {
        t["customer_id"] for t in BUSINESS_TENANT_TICKETS
    }
    assert any(c["id"] not in with_rows for c in BUSINESS_TENANT_CUSTOMERS)  # snapshot NOT_FOUND


def test_business_tenant_records_stay_inside_their_tenant() -> None:
    customer_org = {c["id"]: c["organization_id"] for c in BUSINESS_TENANT_CUSTOMERS}
    for record in (*BUSINESS_TENANT_ORDERS, *BUSINESS_TENANT_TICKETS):
        assert customer_org[record["customer_id"]] == record["organization_id"]
    records = (*BUSINESS_TENANT_CUSTOMERS, *BUSINESS_TENANT_ORDERS, *BUSINESS_TENANT_TICKETS)
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids))

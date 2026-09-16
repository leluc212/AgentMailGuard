"""Unit tests for seed fixtures and deterministic embedding generation (R5.9)."""

import math
from email import message_from_bytes

from packages.db.fixtures import (
    BETA_ORG_ID,
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDER_ITEMS,
    BUSINESS_ORDERS,
    BUSINESS_PRODUCTS,
    BUSINESS_TICKETS,
    CUST_ALICE_ID,
    CUST_BOB_ID,
    CUST_DANA_ID,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
    GAMMA_ORG_ID,
    KNOWLEDGE_DOCS,
    ORDER_9901_ID,
    PROD_WIDGET_ID,
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

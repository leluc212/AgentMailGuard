"""Multi-tenant knowledge corpus fixtures with overlapping lexical content (R5.9, R10.11, R13.1).

Provides realistic knowledge documents and chunks across 3 organizations
(Acme Corp, Beta Industries, Gamma Logistics) to test tenant isolation,
guard against filtered-ANN vector search under-fill, and serve as ground
truth for retrieval benchmarks.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid5

# Stable deterministic namespace UUID
SEED_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Stable demo organization IDs
DEMO_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")  # Acme Corp
BETA_ORG_ID = UUID("00000000-0000-0000-0000-000000000002")  # Beta Industries
GAMMA_ORG_ID = UUID("00000000-0000-0000-0000-000000000003")  # Gamma Logistics

TENANT_ORGS: list[dict[str, Any]] = [
    {
        "id": DEMO_ORG_ID,
        "name": "Acme Corporation",
        "settings": {"tier": "enterprise", "domain": "acme.com"},
    },
    {
        "id": BETA_ORG_ID,
        "name": "Beta Industries",
        "settings": {"tier": "standard", "domain": "beta.com"},
    },
    {
        "id": GAMMA_ORG_ID,
        "name": "Gamma Logistics",
        "settings": {"tier": "logistics", "domain": "gamma.com"},
    },
]


@dataclass(frozen=True)
class ChunkFixture:
    """Represents an individual searchable knowledge segment."""

    chunk_index: int
    title: str
    content: str


@dataclass(frozen=True)
class KnowledgeDocFixture:
    """Represents a source knowledge document with child chunks."""

    org_id: UUID
    title: str
    source_type: str
    chunks: list[ChunkFixture] = field(default_factory=list)


def compute_doc_id(org_id: UUID, doc_title: str) -> UUID:
    """Compute deterministic document UUID from tenant ID and title."""
    return uuid5(SEED_NAMESPACE, f"knowledge_doc:{org_id}:{doc_title}")


def compute_chunk_id(org_id: UUID, doc_title: str, chunk_index: int) -> UUID:
    """Compute deterministic chunk UUID from document ID and chunk index."""
    doc_id = compute_doc_id(org_id, doc_title)
    return uuid5(SEED_NAMESPACE, f"chunk:{doc_id}:{chunk_index}")


KNOWLEDGE_DOCS: list[KnowledgeDocFixture] = [
    # =========================================================================
    # Tenant A: Acme Corporation (Primary Demo Org)
    # =========================================================================
    # 1. Master Customer Support & Refund Policy
    KnowledgeDocFixture(
        org_id=DEMO_ORG_ID,
        title="Acme Master Customer Support & Refund Policy",
        source_type="policy",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="Acme Refund & Return Window",
                content=(
                    "Acme Corporation offers a 30-day money-back guarantee on all software "
                    "subscriptions and hardware products. Customers requesting a refund within "
                    "30 days of purchase will receive a full credit to their original "
                    "payment method. "
                    "To initiate a return or refund, submit ticket with invoice reference."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="Acme SLA & Priority Escalation",
                content=(
                    "Acme Enterprise SLA guarantees 99.9% uptime and a 1-hour priority response "
                    "window for critical severity outages. Tier-2 support handles technical bugs, "
                    "data export crashes, and billing disputes with dedicated engineering "
                    "escalation."
                ),
            ),
            ChunkFixture(
                chunk_index=2,
                title="Acme Order Fulfillment & Shipping",
                content=(
                    "Standard domestic shipping for hardware widgets takes 3 to 5 business days. "
                    "Once an order is placed, an order confirmation number (e.g. ORD-9901) "
                    "is issued. "
                    "Replacement shipments for damaged items are dispatched via express courier."
                ),
            ),
        ],
    ),
    # 2. Hardware Catalog & Technical Specifications (Entity SKU- Matching)
    KnowledgeDocFixture(
        org_id=DEMO_ORG_ID,
        title="Acme Hardware Catalog & Technical Specifications",
        source_type="catalog",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="SKU-WIDGET-01 Enterprise Widget Specifications",
                content=(
                    "The Acme Enterprise Widget (SKU-WIDGET-01) is rated for 24/7 continuous "
                    "industrial operation. It features dual redundant power inputs, operates "
                    "between -20C and 60C, and retails at $450.00 USD. Firmware updates are "
                    "delivered over HTTPS with SHA-256 binary verification."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="SKU-SENSOR-02 Acme Pro Sensor Datasheet",
                content=(
                    "Acme Pro Sensor (SKU-SENSOR-02) provides precision temperature and vibration "
                    "telemetry with +/- 0.1C accuracy. Configured with RS-485 Modbus and optical "
                    "isolation. Standard unit price is $95.00 USD. Replacement sensors carry a "
                    "12-month factory warranty."
                ),
            ),
            ChunkFixture(
                chunk_index=2,
                title="SKU-CABLE-03 Industrial Connector Cable",
                content=(
                    "The Industrial Connector Cable (SKU-CABLE-03) is a 3-meter shielded, "
                    "IP67-rated weatherized patch cable designed for connecting SKU-SENSOR-02 to "
                    "SKU-WIDGET-01 modules. Standard price is $25.00 USD."
                ),
            ),
        ],
    ),
    # 3. Invoicing & Billing Dispute Procedures (Entity INV- Matching)
    KnowledgeDocFixture(
        org_id=DEMO_ORG_ID,
        title="Acme Invoicing & Payment Terms Policy",
        source_type="billing_procedure",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="Invoice Dispute & Adjustment Procedures",
                content=(
                    "For invoice discrepancies, including duplicate charges on monthly "
                    "statements (e.g. INV-2026-8891), customers must submit a billing inquiry "
                    "within 60 days of issue. Verified overcharges are refunded immediately or "
                    "applied as credit notes toward the next billing cycle."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="Payment Terms and Accepted Methods",
                content=(
                    "Standard commercial payment terms are Net 30 from the invoice date. "
                    "Acme accepts ACH wire transfers, corporate credit cards (Visa, MasterCard, "
                    "Amex), and automated billing via client portal. Late payments accrue "
                    "1.5% interest per month."
                ),
            ),
        ],
    ),
    # 4. Order Tracking & Delivery Management (Entity ORD- Matching)
    KnowledgeDocFixture(
        org_id=DEMO_ORG_ID,
        title="Acme Order Fulfillment & Tracking Guidelines",
        source_type="fulfillment",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="Order Status & Tracking Numbers",
                content=(
                    "All placed orders (e.g. ORD-9901, ORD-8820) receive an automated courier "
                    "tracking number once packed at our central warehouse. Tracking links become "
                    "active within 12 hours. Customers can check real-time status online or via "
                    "support inquiry."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="Lost and Damaged Shipment Claims",
                content=(
                    "If an order shipment is delayed past 7 business days or arrives damaged, "
                    "Acme dispatches an expedited replacement at no additional cost. Claims must "
                    "cite the original order number and include photographic proof of damage."
                ),
            ),
        ],
    ),
    # 5. Incident Runbook & Ticket Escalation (Entity TICK- Matching)
    KnowledgeDocFixture(
        org_id=DEMO_ORG_ID,
        title="Acme Incident Runbook & Ticket Escalation",
        source_type="runbook",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="Tier-2 Ticket Escalation and Dispatch",
                content=(
                    "Support tickets tagged with high severity (e.g. TICK-4402, TICK-1011) "
                    "escalate automatically to the Tier-2 engineering rotation. Assigned "
                    "engineers must acknowledge the ticket within 30 minutes and post a status "
                    "update to the customer portal."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="Application Crash Diagnostics & Memory Errors",
                content=(
                    "For system crashes with errors like MemoryAllocationError or HTTP 500 "
                    "during PDF export, engineers must retrieve server stack traces and review "
                    "container memory limits. Temporary mitigation involves allocating extra heap "
                    "memory before permanent patch release."
                ),
            ),
        ],
    ),
    # =========================================================================
    # Tenant B: Beta Industries (Overlapping content, different terms)
    # =========================================================================
    KnowledgeDocFixture(
        org_id=BETA_ORG_ID,
        title="Beta Industries Warranty & Return Terms",
        source_type="policy",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="Beta Return Policy",
                content=(
                    "Beta Industries permits returns within 14 calendar days of delivery. "
                    "A 15% restocking fee applies to opened hardware. All refund requests "
                    "require prior authorization and an active Beta RMA number."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="Beta Standard Support SLA",
                content=(
                    "Beta Industries standard technical support provides 24-hour response time "
                    "during regular business hours. Critical issues receive attention "
                    "within 4 hours."
                ),
            ),
        ],
    ),
    # =========================================================================
    # Tenant C: Gamma Logistics (Freight and dispatch terminology)
    # =========================================================================
    KnowledgeDocFixture(
        org_id=GAMMA_ORG_ID,
        title="Gamma Logistics Shipping & Dispatch Guidelines",
        source_type="policy",
        chunks=[
            ChunkFixture(
                chunk_index=0,
                title="Gamma Freight Shipping Schedule",
                content=(
                    "Gamma Logistics processes palletized freight shipping within 48 hours. "
                    "Tracking numbers are updated at freight hub interchanges. Claims for lost "
                    "shipments must be filed within 60 days of dispatch."
                ),
            ),
            ChunkFixture(
                chunk_index=1,
                title="Gamma Hazardous Cargo Handling",
                content=(
                    "All dangerous goods shipments must carry Class 9 placards and standard "
                    "material safety data sheets (MSDS). Gamma Logistics reserves the right to "
                    "inspect packaging prior to loading."
                ),
            ),
        ],
    ),
]

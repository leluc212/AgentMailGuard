"""Deterministic generator for retrieval seed dataset (R22.2, R10.11, H1).

Generates >= 100 search queries deliberately split between:
1. Natural-language semantic questions (~50%)
2. Identifier-bearing queries (~50%) citing INV-, ORD-, TICK-, SKU- entities.

Every query maps to ground-truth gold chunk UUIDs from the multi-tenant
knowledge corpus in packages/db/fixtures/knowledge.py.
Outputs evaluation/datasets/retrieval/queries.jsonl.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from evaluation.datasets.schemas import QueryType, RetrievalDatasetItem
from packages.db.fixtures.knowledge import (
    BETA_ORG_ID,
    DEMO_ORG_ID,
    GAMMA_ORG_ID,
    compute_chunk_id,
)

RETRIEVAL_DIR = Path(__file__).resolve().parent / "retrieval"

# Target chunk references
CHUNK_ACME_REFUND = compute_chunk_id(DEMO_ORG_ID, "Acme Master Customer Support & Refund Policy", 0)
CHUNK_ACME_SLA = compute_chunk_id(DEMO_ORG_ID, "Acme Master Customer Support & Refund Policy", 1)
CHUNK_ACME_SHIPPING = compute_chunk_id(
    DEMO_ORG_ID, "Acme Master Customer Support & Refund Policy", 2
)

CHUNK_SKU_WIDGET = compute_chunk_id(
    DEMO_ORG_ID, "Acme Hardware Catalog & Technical Specifications", 0
)
CHUNK_SKU_SENSOR = compute_chunk_id(
    DEMO_ORG_ID, "Acme Hardware Catalog & Technical Specifications", 1
)
CHUNK_SKU_CABLE = compute_chunk_id(
    DEMO_ORG_ID, "Acme Hardware Catalog & Technical Specifications", 2
)

CHUNK_INV_DISPUTE = compute_chunk_id(DEMO_ORG_ID, "Acme Invoicing & Payment Terms Policy", 0)
CHUNK_PAYMENT_TERMS = compute_chunk_id(DEMO_ORG_ID, "Acme Invoicing & Payment Terms Policy", 1)

CHUNK_ORD_TRACKING = compute_chunk_id(
    DEMO_ORG_ID, "Acme Order Fulfillment & Tracking Guidelines", 0
)
CHUNK_LOST_SHIPMENT = compute_chunk_id(
    DEMO_ORG_ID, "Acme Order Fulfillment & Tracking Guidelines", 1
)

CHUNK_TICK_ESCALATION = compute_chunk_id(
    DEMO_ORG_ID, "Acme Incident Runbook & Ticket Escalation", 0
)
CHUNK_CRASH_DIAG = compute_chunk_id(DEMO_ORG_ID, "Acme Incident Runbook & Ticket Escalation", 1)

CHUNK_BETA_RETURN = compute_chunk_id(BETA_ORG_ID, "Beta Industries Warranty & Return Terms", 0)
CHUNK_BETA_SLA = compute_chunk_id(BETA_ORG_ID, "Beta Industries Warranty & Return Terms", 1)

CHUNK_GAMMA_FREIGHT = compute_chunk_id(
    GAMMA_ORG_ID, "Gamma Logistics Shipping & Dispatch Guidelines", 0
)
CHUNK_GAMMA_HAZARDOUS = compute_chunk_id(
    GAMMA_ORG_ID, "Gamma Logistics Shipping & Dispatch Guidelines", 1
)


@dataclass(frozen=True)
class NLQueryDef:
    """Specification for a natural language search query."""

    org_id: UUID
    query: str
    chunk_ids: list[UUID]
    diff: str


@dataclass(frozen=True)
class IDQueryDef:
    """Specification for an identifier-bearing search query."""

    org_id: UUID
    query: str
    chunk_ids: list[UUID]
    diff: str
    entity: str


NL_SPECS: list[NLQueryDef] = [
    # Refunds & Guarantees (Chunk 0)
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the return window for software subscriptions?",
        [CHUNK_ACME_REFUND],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How many days do I have to request a money back refund?",
        [CHUNK_ACME_REFUND],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Can I get a full refund to my credit card within 30 days?",
        [CHUNK_ACME_REFUND],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What documentation is required to initiate a product return?",
        [CHUNK_ACME_REFUND],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Does Acme offer a money-back guarantee on hardware purchases?",
        [CHUNK_ACME_REFUND],
        "easy",
    ),
    # Support SLAs & Escalations (Chunk 1)
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the uptime guarantee under the Enterprise SLA?",
        [CHUNK_ACME_SLA],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How quickly does Tier-2 support respond to critical outages?",
        [CHUNK_ACME_SLA],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the guaranteed response time for priority production incidents?",
        [CHUNK_ACME_SLA],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Who handles data export crashes and engineering escalation?",
        [CHUNK_ACME_SLA],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What priority SLA applies to billing disputes and technical bugs?",
        [CHUNK_ACME_SLA],
        "medium",
    ),
    # Hardware Specifications (Chunks 0, 1, 2)
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the operating temperature range for industrial widgets?",
        [CHUNK_SKU_WIDGET],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Does the enterprise widget support dual redundant power supplies?",
        [CHUNK_SKU_WIDGET],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How are firmware updates cryptographically verified on hardware?",
        [CHUNK_SKU_WIDGET],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the measurement precision of temperature telemetry sensors?",
        [CHUNK_SKU_SENSOR],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What communication interface and protocol does the Pro Sensor use?",
        [CHUNK_SKU_SENSOR],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How long is the factory warranty coverage for sensor replacement units?",
        [CHUNK_SKU_SENSOR],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the IP weather protection rating for outdoor connector cables?",
        [CHUNK_SKU_CABLE],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What cable connects the temperature sensor to the main widget controller?",
        [CHUNK_SKU_CABLE],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the length and shielding specification of connector cables?",
        [CHUNK_SKU_CABLE],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the retail price of the enterprise widget unit?",
        [CHUNK_SKU_WIDGET],
        "easy",
    ),
    # Invoicing, Billing & Payment Terms (Chunks 0, 1)
    NLQueryDef(
        DEMO_ORG_ID,
        "How long do customers have to dispute duplicate charges on an invoice?",
        [CHUNK_INV_DISPUTE],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Can verified overcharges be applied as credit notes toward the next cycle?",
        [CHUNK_INV_DISPUTE],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the procedure for disputing monthly invoice discrepancies?",
        [CHUNK_INV_DISPUTE],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What are the standard commercial payment terms for corporate accounts?",
        [CHUNK_PAYMENT_TERMS],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Does Acme accept ACH wire transfers and corporate credit cards?",
        [CHUNK_PAYMENT_TERMS],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What interest penalty applies to late payments past Net 30 days?",
        [CHUNK_PAYMENT_TERMS],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How do we submit an annual state tax exemption certificate?",
        [CHUNK_INV_DISPUTE],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Where can accounts payable access updated banking wire routing info?",
        [CHUNK_PAYMENT_TERMS],
        "medium",
    ),
    # Order Fulfillment & Tracking (Chunks 0, 1)
    NLQueryDef(
        DEMO_ORG_ID,
        "How many hours after packing does an order tracking link become active?",
        [CHUNK_ORD_TRACKING],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How can customers check the real-time delivery status of placed orders?",
        [CHUNK_ORD_TRACKING],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What happens if a hardware shipment is delayed more than 7 business days?",
        [CHUNK_LOST_SHIPMENT],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What proof is required to submit a claim for damaged courier delivery?",
        [CHUNK_LOST_SHIPMENT],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Are replacement shipments for damaged products sent via expedited courier?",
        [CHUNK_LOST_SHIPMENT],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "How many business days does standard domestic shipping take?",
        [CHUNK_ACME_SHIPPING],
        "easy",
    ),
    # Incident Runbook & Crash Diagnostics (Chunks 0, 1)
    NLQueryDef(
        DEMO_ORG_ID,
        "How quickly must engineers acknowledge high severity escalated tickets?",
        [CHUNK_TICK_ESCALATION],
        "medium",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the escalation workflow for priority customer tickets?",
        [CHUNK_TICK_ESCALATION],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What should engineers check when web app crashes with MemoryAllocationError?",
        [CHUNK_CRASH_DIAG],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the temporary mitigation for HTTP 500 crashes during PDF exports?",
        [CHUNK_CRASH_DIAG],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Where should diagnostic stack traces be retrieved during worker exits?",
        [CHUNK_CRASH_DIAG],
        "medium",
    ),
    # Tenant B: Beta Industries Comparison
    NLQueryDef(
        BETA_ORG_ID,
        "What is the return policy window for Beta Industries products?",
        [CHUNK_BETA_RETURN],
        "easy",
    ),
    NLQueryDef(
        BETA_ORG_ID,
        "Does Beta Industries charge a restocking fee on opened hardware returns?",
        [CHUNK_BETA_RETURN],
        "medium",
    ),
    NLQueryDef(
        BETA_ORG_ID,
        "What RMA authorization is required before sending back hardware to Beta?",
        [CHUNK_BETA_RETURN],
        "easy",
    ),
    NLQueryDef(
        BETA_ORG_ID,
        "What is the standard support response time for Beta technical inquiries?",
        [CHUNK_BETA_SLA],
        "easy",
    ),
    NLQueryDef(
        BETA_ORG_ID,
        "How quickly does Beta Industries respond to critical severity issues?",
        [CHUNK_BETA_SLA],
        "medium",
    ),
    # Tenant C: Gamma Logistics Comparison
    NLQueryDef(
        GAMMA_ORG_ID,
        "How many hours does Gamma Logistics take to process palletized freight?",
        [CHUNK_GAMMA_FREIGHT],
        "easy",
    ),
    NLQueryDef(
        GAMMA_ORG_ID,
        "What is the deadline for filing lost freight shipment claims with Gamma?",
        [CHUNK_GAMMA_FREIGHT],
        "medium",
    ),
    NLQueryDef(
        GAMMA_ORG_ID,
        "Where are courier tracking numbers updated during freight hub interchanges?",
        [CHUNK_GAMMA_FREIGHT],
        "medium",
    ),
    NLQueryDef(
        GAMMA_ORG_ID,
        "What placards and documentation are required for hazardous cargo transport?",
        [CHUNK_GAMMA_HAZARDOUS],
        "hard",
    ),
    NLQueryDef(
        GAMMA_ORG_ID,
        "Does Gamma Logistics inspect packaging before loading dangerous goods?",
        [CHUNK_GAMMA_HAZARDOUS],
        "medium",
    ),
    # Extra semantic questions to reach 55
    NLQueryDef(
        DEMO_ORG_ID,
        "Can a customer combine software refund credit with hardware returns?",
        [CHUNK_ACME_REFUND],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What is the uptime SLA percentage guaranteed to enterprise customers?",
        [CHUNK_ACME_SLA],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "What happens if a sensor cable seal is compromised by water?",
        [CHUNK_SKU_CABLE],
        "hard",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Who do I contact if my monthly statement has an unexplained charge?",
        [CHUNK_INV_DISPUTE],
        "easy",
    ),
    NLQueryDef(
        DEMO_ORG_ID,
        "Is there an additional charge for expedited replacement shipments?",
        [CHUNK_LOST_SHIPMENT],
        "medium",
    ),
]

ID_SPECS: list[IDQueryDef] = [
    # Invoice identifiers (INV-2026-8891, INV-2026-1044, INV-2026-7701)
    IDQueryDef(
        DEMO_ORG_ID,
        "INV-2026-8891 duplicate billing adjustment",
        [CHUNK_INV_DISPUTE],
        "easy",
        "INV-2026-8891",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Dispute on invoice INV-2026-8891 credit note",
        [CHUNK_INV_DISPUTE],
        "easy",
        "INV-2026-8891",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Invoice INV-2026-8891 payment terms Net 30",
        [CHUNK_PAYMENT_TERMS],
        "medium",
        "INV-2026-8891",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Status of invoice discrepancy INV-2026-8891",
        [CHUNK_INV_DISPUTE],
        "easy",
        "INV-2026-8891",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "INV-2026-1044 billing inquiry decommissioned mailboxes",
        [CHUNK_INV_DISPUTE],
        "medium",
        "INV-2026-1044",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "INV-2026-1044 adjustment request for sales tax",
        [CHUNK_INV_DISPUTE],
        "medium",
        "INV-2026-1044",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Invoice INV-2026-7701 late payment fee interest",
        [CHUNK_PAYMENT_TERMS],
        "medium",
        "INV-2026-7701",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "INV-2026-9011 credit note application",
        [CHUNK_INV_DISPUTE],
        "easy",
        "INV-2026-9011",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Remittance for invoice INV-2026-8891 via ACH",
        [CHUNK_PAYMENT_TERMS],
        "medium",
        "INV-2026-8891",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Overcharge claim on INV-2026-8891",
        [CHUNK_INV_DISPUTE],
        "easy",
        "INV-2026-8891",
    ),
    # Order identifiers (ORD-9901, ORD-8820, ORD-7715, ORD-6604)
    IDQueryDef(
        DEMO_ORG_ID,
        "Tracking courier number for order ORD-9901",
        [CHUNK_ORD_TRACKING],
        "easy",
        "ORD-9901",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Status update for order ORD-9901 shipment",
        [CHUNK_ORD_TRACKING],
        "easy",
        "ORD-9901",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "ORD-9901 replacement request damaged in transit",
        [CHUNK_LOST_SHIPMENT],
        "medium",
        "ORD-9901",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Order ORD-9901 delivery delay past 7 days",
        [CHUNK_LOST_SHIPMENT],
        "medium",
        "ORD-9901",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "ORD-8820 order cancellation refund confirmation",
        [CHUNK_ACME_REFUND],
        "medium",
        "ORD-8820",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Order confirmation details for ORD-8820",
        [CHUNK_ORD_TRACKING],
        "easy",
        "ORD-8820",
    ),
    IDQueryDef(
        DEMO_ORG_ID, "ORD-8820 tracking link not active", [CHUNK_ORD_TRACKING], "medium", "ORD-8820"
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Lost shipment claim for order ORD-7715",
        [CHUNK_LOST_SHIPMENT],
        "medium",
        "ORD-7715",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "ORD-7715 warehouse dispatch confirmation",
        [CHUNK_ORD_TRACKING],
        "easy",
        "ORD-7715",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Damaged goods photo upload for order ORD-9901",
        [CHUNK_LOST_SHIPMENT],
        "hard",
        "ORD-9901",
    ),
    # Ticket identifiers (TICK-4402, TICK-1011, TICK-8821, TICK-3042)
    IDQueryDef(
        DEMO_ORG_ID,
        "Tier-2 escalation for ticket TICK-4402",
        [CHUNK_TICK_ESCALATION],
        "easy",
        "TICK-4402",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "TICK-4402 SLA acknowledgment status",
        [CHUNK_TICK_ESCALATION],
        "medium",
        "TICK-4402",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Engineer assignment on ticket TICK-4402",
        [CHUNK_TICK_ESCALATION],
        "easy",
        "TICK-4402",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Ticket TICK-1011 memory allocation crash logs",
        [CHUNK_CRASH_DIAG],
        "medium",
        "TICK-1011",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "TICK-1011 PDF export HTTP 500 error mitigation",
        [CHUNK_CRASH_DIAG],
        "hard",
        "TICK-1011",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Container memory limit patch for TICK-1011",
        [CHUNK_CRASH_DIAG],
        "hard",
        "TICK-1011",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "High severity ticket TICK-8821 response window",
        [CHUNK_TICK_ESCALATION],
        "medium",
        "TICK-8821",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "TICK-3042 hardware replacement tracking",
        [CHUNK_TICK_ESCALATION],
        "medium",
        "TICK-3042",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Portal update status for ticket TICK-4402",
        [CHUNK_TICK_ESCALATION],
        "easy",
        "TICK-4402",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "TICK-1011 heap dump diagnostic review",
        [CHUNK_CRASH_DIAG],
        "hard",
        "TICK-1011",
    ),
    # SKU product identifiers (SKU-WIDGET-01, SKU-SENSOR-02, SKU-CABLE-03)
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-WIDGET-01 price and operating temperature",
        [CHUNK_SKU_WIDGET],
        "easy",
        "SKU-WIDGET-01",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-WIDGET-01 firmware verification SHA-256",
        [CHUNK_SKU_WIDGET],
        "hard",
        "SKU-WIDGET-01",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Dual power redundant specification for SKU-WIDGET-01",
        [CHUNK_SKU_WIDGET],
        "medium",
        "SKU-WIDGET-01",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-SENSOR-02 temperature tolerance and accuracy",
        [CHUNK_SKU_SENSOR],
        "easy",
        "SKU-SENSOR-02",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-SENSOR-02 RS-485 Modbus datasheet",
        [CHUNK_SKU_SENSOR],
        "medium",
        "SKU-SENSOR-02",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Unit price and warranty for SKU-SENSOR-02",
        [CHUNK_SKU_SENSOR],
        "easy",
        "SKU-SENSOR-02",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Optical isolation in SKU-SENSOR-02 telemetry",
        [CHUNK_SKU_SENSOR],
        "hard",
        "SKU-SENSOR-02",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-CABLE-03 IP67 connector cable length",
        [CHUNK_SKU_CABLE],
        "easy",
        "SKU-CABLE-03",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Industrial connector cable SKU-CABLE-03 price $25",
        [CHUNK_SKU_CABLE],
        "easy",
        "SKU-CABLE-03",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Compatibility between SKU-WIDGET-01 and SKU-SENSOR-02",
        [CHUNK_SKU_CABLE],
        "medium",
        "SKU-WIDGET-01",
    ),
    # Multi-Tenant Identifier Queries (Beta RMA, Gamma Freight IDs)
    IDQueryDef(
        BETA_ORG_ID,
        "Beta RMA-99128 authorization return number",
        [CHUNK_BETA_RETURN],
        "easy",
        "RMA-99128",
    ),
    IDQueryDef(
        BETA_ORG_ID,
        "Beta RMA-99128 restocking fee calculation",
        [CHUNK_BETA_RETURN],
        "medium",
        "RMA-99128",
    ),
    IDQueryDef(
        BETA_ORG_ID,
        "Beta support ticket SLA-402 response time",
        [CHUNK_BETA_SLA],
        "easy",
        "SLA-402",
    ),
    IDQueryDef(
        GAMMA_ORG_ID,
        "Gamma dispatch tracking number FRT-88201 hub update",
        [CHUNK_GAMMA_FREIGHT],
        "easy",
        "FRT-88201",
    ),
    IDQueryDef(
        GAMMA_ORG_ID,
        "Gamma lost freight claim FRT-88201 60 days",
        [CHUNK_GAMMA_FREIGHT],
        "medium",
        "FRT-88201",
    ),
    IDQueryDef(
        GAMMA_ORG_ID,
        "Class-9 hazardous placard compliance manifest",
        [CHUNK_GAMMA_HAZARDOUS],
        "medium",
        "Class-9",
    ),
    IDQueryDef(
        GAMMA_ORG_ID,
        "MSDS packaging inspection procedure Gamma",
        [CHUNK_GAMMA_HAZARDOUS],
        "hard",
        "MSDS",
    ),
    # Additional Identifier-bearing queries to reach 55
    IDQueryDef(
        DEMO_ORG_ID,
        "Refund policy claim citing invoice INV-2026-8891",
        [CHUNK_ACME_REFUND],
        "medium",
        "INV-2026-8891",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Order ORD-9901 tracking courier dispatched",
        [CHUNK_ACME_SHIPPING],
        "easy",
        "ORD-9901",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-WIDGET-01 replacement shipment courier",
        [CHUNK_ACME_SHIPPING],
        "medium",
        "SKU-WIDGET-01",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Ticket TICK-4402 1-hour priority response",
        [CHUNK_ACME_SLA],
        "medium",
        "TICK-4402",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "SKU-SENSOR-02 replacement under 12-month factory warranty",
        [CHUNK_SKU_SENSOR],
        "easy",
        "SKU-SENSOR-02",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Invoice INV-2026-1044 Net 30 commercial terms",
        [CHUNK_PAYMENT_TERMS],
        "medium",
        "INV-2026-1044",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "TICK-1011 memory allocation error exporter.py:142",
        [CHUNK_CRASH_DIAG],
        "hard",
        "TICK-1011",
    ),
    IDQueryDef(
        DEMO_ORG_ID,
        "Order ORD-8820 courier tracking 12-hour activation",
        [CHUNK_ORD_TRACKING],
        "easy",
        "ORD-8820",
    ),
]


def generate_queries() -> list[RetrievalDatasetItem]:
    """Generate 109 queries: 54 natural-language and 55 identifier-bearing."""
    queries: list[RetrievalDatasetItem] = []
    q_id = 0

    for spec in NL_SPECS:
        q_id += 1
        queries.append(
            RetrievalDatasetItem(
                query_id=f"ret_nl_{q_id:04d}",
                organization_id=spec.org_id,
                query_type=QueryType.NATURAL_LANGUAGE,
                query=spec.query,
                gold_chunk_ids=spec.chunk_ids,
                difficulty=spec.diff,
                target_entity=None,
                metadata={"source": "semantic_catalog"},
            )
        )

    for id_spec in ID_SPECS:
        q_id += 1
        queries.append(
            RetrievalDatasetItem(
                query_id=f"ret_id_{q_id:04d}",
                organization_id=id_spec.org_id,
                query_type=QueryType.IDENTIFIER_BEARING,
                query=id_spec.query,
                gold_chunk_ids=id_spec.chunk_ids,
                difficulty=id_spec.diff,
                target_entity=id_spec.entity,
                metadata={"source": "entity_catalog"},
            )
        )

    return queries


def save_queries(queries: list[RetrievalDatasetItem]) -> int:
    """Write retrieval query items to evaluation/datasets/retrieval/queries.jsonl."""
    RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RETRIEVAL_DIR / "queries.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for q in queries:
            f.write(q.model_dump_json() + "\n")
    return len(queries)


def main() -> None:
    """Build and save retrieval seed dataset."""
    queries = generate_queries()
    count = save_queries(queries)
    nl_count = sum(1 for q in queries if q.query_type == QueryType.NATURAL_LANGUAGE)
    id_count = sum(1 for q in queries if q.query_type == QueryType.IDENTIFIER_BEARING)
    print(f"Generated {count} retrieval queries: {nl_count} natural, {id_count} identifier.")


if __name__ == "__main__":
    main()

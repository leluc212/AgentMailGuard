"""Pure FetchPlan builder (R13.3, design.md §5.4, ADR-0008).

Requirements: R13.3. Typed IDs are always planned, the routed profile's context_policy gates
the full snapshot, INTENT_ENTITIES maps a few intents to entities, nothing planned is empty.
"""

from __future__ import annotations

from typing import cast

import pytest

from packages.business.plan import INTENT_ENTITIES, build_fetch_plan
from packages.domain.business import EntityRef, EntityType, FetchPlan
from packages.llm.profile import ContextPolicy

NON_BUSINESS = ContextPolicy.THREAD_PLUS_RAG.value
BUSINESS = ContextPolicy.THREAD_PLUS_RAG_PLUS_BUSINESS.value


def test_typed_ids_override_a_non_business_policy() -> None:
    plan = build_fetch_plan(
        subject="Quick question",
        body="Where is my order #82915?",
        context_policy=NON_BUSINESS,
        intent=None,
    )
    assert plan == FetchPlan(refs=(EntityRef(entity=EntityType.ORDER, reference="ORD-82915"),))
    assert not plan.is_empty


def test_business_policy_plans_the_orders_and_tickets_snapshot() -> None:
    plan = build_fetch_plan(
        subject="My account",
        body="Could you check my account? I think I was charged twice.",
        context_policy=BUSINESS,
        intent=None,
    )
    assert plan.refs == ()
    assert plan.snapshot == frozenset({EntityType.ORDER, EntityType.TICKET})


@pytest.mark.parametrize(
    "intent",
    ["invoice_inquiry", "receipt_lookup", "payment_failure", "refund_request", " Refund_Request "],
)
def test_mapped_intent_plans_only_its_entities(intent: str) -> None:
    plan = build_fetch_plan(
        subject="Help", body="I need help with this.", context_policy=NON_BUSINESS, intent=intent
    )
    assert plan.refs == ()
    assert plan.snapshot == frozenset({EntityType.ORDER})


def test_business_policy_wins_over_the_intent_map() -> None:
    plan = build_fetch_plan(
        subject="Invoice",
        body="Question about my bill.",
        context_policy=BUSINESS,
        intent="invoice_inquiry",
    )
    assert plan.snapshot == frozenset({EntityType.ORDER, EntityType.TICKET})


def test_nothing_planned_is_empty() -> None:
    plan = build_fetch_plan(
        subject="Thanks",
        body="Thank you, in order to close this out we are done.",
        context_policy=NON_BUSINESS,
        intent="thank_you",
    )
    assert plan.is_empty
    assert plan == FetchPlan()


def test_unmapped_intent_and_missing_intent_plan_no_snapshot() -> None:
    for intent in (None, "", "general_question"):
        plan = build_fetch_plan(
            subject="Hi", body="Hello there.", context_policy=NON_BUSINESS, intent=intent
        )
        assert plan.is_empty


def test_invoice_reference_is_planned_for_the_provider_to_mark_unsupported() -> None:
    plan = build_fetch_plan(
        subject="Discrepancy on Invoice INV-2026-8891",
        body="We were billed twice on invoice INV-2026-8891.",
        context_policy=NON_BUSINESS,
        intent=None,
    )
    assert plan.refs == (EntityRef(entity=EntityType.INVOICE, reference="INV-2026-8891"),)
    assert plan.snapshot == frozenset()


def test_subject_and_body_are_both_scanned_and_deduplicated() -> None:
    plan = build_fetch_plan(
        subject="Status update for Order ORD-9901 and Ticket TICK-4402",
        body="Could you update me on ticket TICK-4402 and the replacement for order ORD-9901?",
        context_policy=NON_BUSINESS,
        intent=None,
    )
    assert plan.refs == (
        EntityRef(entity=EntityType.ORDER, reference="ORD-9901"),
        EntityRef(entity=EntityType.TICKET, reference="TICK-4402"),
    )


def test_intent_entities_matches_design_and_is_read_only() -> None:
    assert dict(INTENT_ENTITIES) == {
        "invoice_inquiry": frozenset({EntityType.ORDER}),
        "receipt_lookup": frozenset({EntityType.ORDER}),
        "payment_failure": frozenset({EntityType.ORDER}),
        "refund_request": frozenset({EntityType.ORDER}),
    }
    with pytest.raises(TypeError):
        cast(dict[str, frozenset[EntityType]], INTENT_ENTITIES)["order_status"] = frozenset()

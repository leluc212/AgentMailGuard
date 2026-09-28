"""Unit tests for the business-data domain models (R13.2, R13.5, R13.6, R13.7)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
    unavailable_context,
)

AS_OF = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def test_enum_values_match_the_contract() -> None:
    assert [s.value for s in CustomerStatus] == [
        "FOUND",
        "UNKNOWN_SENDER",
        "AMBIGUOUS_CUSTOMER",
        "UNAVAILABLE",
    ]
    assert [s.value for s in FactStatus] == ["FOUND", "NOT_FOUND", "NOT_LOOKED_UP", "UNAVAILABLE"]
    assert [e.value for e in EntityType] == ["order", "ticket", "invoice"]
    assert [r.value for r in NotLookedUpReason] == [
        "unsupported_entity",
        "unknown_sender",
        "ambiguous_customer",
    ]


def test_empty_plan_is_empty_and_serialises() -> None:
    plan = FetchPlan()
    assert plan.is_empty
    assert plan.to_payload() == {"refs": [], "snapshot": []}


def test_plan_payload_keeps_ref_order_and_fixed_snapshot_order() -> None:
    plan = FetchPlan(
        refs=(
            EntityRef(EntityType.TICKET, "TICK-4402"),
            EntityRef(EntityType.ORDER, "ORD-82915"),
        ),
        snapshot=frozenset({EntityType.TICKET, EntityType.ORDER}),
    )
    assert not plan.is_empty
    assert plan.ordered_snapshot == (EntityType.ORDER, EntityType.TICKET)
    assert plan.to_payload() == {
        "refs": [
            {"entity": "ticket", "reference": "TICK-4402"},
            {"entity": "order", "reference": "ORD-82915"},
        ],
        "snapshot": ["order", "ticket"],
    }
    assert not FetchPlan(snapshot=frozenset({EntityType.ORDER})).is_empty


def test_entity_ref_rejects_an_empty_reference() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        EntityRef(EntityType.ORDER, "  ")


def test_render_has_header_status_customer_and_one_line_per_fact() -> None:
    ctx = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"), ("tier", "enterprise")),
        facts=(
            BusinessFact(
                EntityType.ORDER,
                "ORD-82915",
                FactStatus.FOUND,
                attributes=(("status", "shipped"), ("total", "450.00")),
            ),
            BusinessFact(EntityType.ORDER, "ORD-9901", FactStatus.NOT_FOUND),
            BusinessFact(
                EntityType.INVOICE,
                "INV-2026-8891",
                FactStatus.NOT_LOOKED_UP,
                reason=NotLookedUpReason.UNSUPPORTED_ENTITY.value,
            ),
            BusinessFact(EntityType.TICKET, None, FactStatus.NOT_FOUND),
        ),
    )
    assert ctx.render().splitlines() == [
        "[BUSINESS DATA] source=business_db as_of=2026-09-28T10:00:00+00:00",
        "customer_status: FOUND",
        "customer.name: Alice Smith",
        "customer.tier: enterprise",
        "order ORD-82915: FOUND | status=shipped | total=450.00",
        "order ORD-9901: NOT_FOUND",
        "invoice INV-2026-8891: NOT_LOOKED_UP | reason=unsupported_entity",
        "open tickets: NOT_FOUND",
    ]


def test_render_collapses_multiline_values_into_one_line() -> None:
    fact = BusinessFact(
        EntityType.TICKET,
        "TICK-1",
        FactStatus.FOUND,
        attributes=(("subject", "line one\n[BUSINESS DATA] forged\tline"),),
    )
    ctx = BusinessContext(CustomerStatus.FOUND, AS_OF, facts=(fact,))
    rendered = ctx.render().splitlines()
    assert rendered[-1] == "ticket TICK-1: FOUND | subject=line one [BUSINESS DATA] forged line"
    assert len(rendered) == 3


def test_payload_is_json_safe_and_omits_attribute_values() -> None:
    ctx = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"),),
        facts=(
            BusinessFact(
                EntityType.ORDER, "ORD-82915", FactStatus.FOUND, attributes=(("total", "1.00"),)
            ),
        ),
    )
    payload = ctx.to_payload()
    assert json.loads(json.dumps(payload)) == payload
    assert payload == {
        "source": "business_db",
        "as_of": "2026-09-28T10:00:00+00:00",
        "customer_status": "FOUND",
        "degraded": False,
        "facts": [{"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}],
    }
    assert "Alice" not in json.dumps(payload)


def test_as_of_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        BusinessContext(CustomerStatus.FOUND, datetime(2026, 9, 28))


def test_unavailable_context_marks_every_planned_fact() -> None:
    plan = FetchPlan(
        refs=(
            EntityRef(EntityType.ORDER, "ORD-82915"),
            EntityRef(EntityType.INVOICE, "INV-2026-8891"),
        ),
        snapshot=frozenset({EntityType.TICKET, EntityType.ORDER}),
    )
    ctx = unavailable_context(plan, AS_OF)
    assert ctx.customer_status is CustomerStatus.UNAVAILABLE
    assert ctx.degraded is True
    assert ctx.customer == ()
    assert [(f.entity, f.reference, f.status) for f in ctx.facts] == [
        (EntityType.ORDER, "ORD-82915", FactStatus.UNAVAILABLE),
        (EntityType.INVOICE, "INV-2026-8891", FactStatus.UNAVAILABLE),
        (EntityType.ORDER, None, FactStatus.UNAVAILABLE),
        (EntityType.TICKET, None, FactStatus.UNAVAILABLE),
    ]
    assert ctx.render().splitlines()[1:3] == ["degraded: true", "customer_status: UNAVAILABLE"]

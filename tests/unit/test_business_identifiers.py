"""Typed business-reference extractor (R13.3, design.md §5.4, ADR-0008).

Requirements: R13.3. The extractor keeps the entity type, normalises to the stored
`order_number` / `ticket_number` format and runs on every job.
"""

from __future__ import annotations

import pytest

from packages.business.identifiers import extract_entity_refs
from packages.domain.business import EntityRef, EntityType


def _order(reference: str) -> EntityRef:
    return EntityRef(entity=EntityType.ORDER, reference=reference)


def _ticket(reference: str) -> EntityRef:
    return EntityRef(entity=EntityType.TICKET, reference=reference)


def _invoice(reference: str) -> EntityRef:
    return EntityRef(entity=EntityType.INVOICE, reference=reference)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is the status of order 82915?", _order("ORD-82915")),
        ("Where is my order #82915?", _order("ORD-82915")),
        ("Where is my order#82915?", _order("ORD-82915")),
        ("Checking order no. 82915 please", _order("ORD-82915")),
        ("Order Number 82915 has not arrived", _order("ORD-82915")),
        ("Reference ORDER-58192 from the proposal", _order("ORD-58192")),
        ("lower-case ord-9901 works", _order("ORD-9901")),
        ("Status for ORD-9901", _order("ORD-9901")),
        ("Escalated as TICKET-4402", _ticket("TICK-4402")),
        ("See tick-4402", _ticket("TICK-4402")),
        ("My ticket number 4402 is still open", _ticket("TICK-4402")),
        ("Following up on ticket #4402.", _ticket("TICK-4402")),
        ("Discrepancy on Invoice INV-2026-8891", _invoice("INV-2026-8891")),
        ("proposal example inv-2026-01829", _invoice("INV-2026-01829")),
    ],
)
def test_each_accepted_form_is_typed_and_normalised(text: str, expected: EntityRef) -> None:
    assert extract_entity_refs(text) == (expected,)


@pytest.mark.parametrize(
    "text",
    [
        "We changed the config in order to fix the outage.",
        "I placed an order 2 days ago and nothing happened.",
        "This is ticket 3 of 5 in the batch.",
        "order 123 is too short to be an order number",
        "",
    ],
)
def test_prose_and_short_numbers_do_not_match(text: str) -> None:
    assert extract_entity_refs(text) == ()


def test_refs_are_deduplicated_in_order_of_first_appearance() -> None:
    text = (
        "TICK-4402 is about ORD-9901. To repeat: order 9901 and ticket #4402. "
        "Also invoice INV-2026-8891."
    )
    assert extract_entity_refs(text) == (
        _ticket("TICK-4402"),
        _order("ORD-9901"),
        _invoice("INV-2026-8891"),
    )


def test_fixture_subject_with_prefixed_refs_after_the_words() -> None:
    """Edward's fixture subject: the bare pattern must not fire on 'Order ORD-9901'."""
    subject = "Status update for Order ORD-9901 and Ticket TICK-4402"
    assert extract_entity_refs(subject) == (_order("ORD-9901"), _ticket("TICK-4402"))

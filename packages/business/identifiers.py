"""Typed business-reference extractor (R13.3, design.md §5.4, ADR-0008).

Separate from the retrieval query builder's untyped identifier regex, which stays as it is:
this extractor keeps the entity type and normalises each match to the stored number format
(`order_number` = ``ORD-<digits>``, `ticket_number` = ``TICK-<digits>``). It runs on every job,
independent of ``retrieval_required``.

Accepted forms, case-insensitive:
- prefixed: ``ORD-<digits>`` / ``ORDER-<digits>``, ``TICK-<digits>`` / ``TICKET-<digits>``,
  ``INV-<digits>[-<digits>...]``;
- bare: the word ``order`` or ``ticket``, optionally followed by ``#``, ``no.`` or ``number``,
  then at least 4 digits ("order 82915", "order #82915", "ticket number 4402").

"in order to", "an order 2 days ago" and "ticket 3 of 5" do not match.
"""

from __future__ import annotations

import re

from packages.domain.business import EntityRef, EntityType

ORDER_PREFIX = "ORD-"
TICKET_PREFIX = "TICK-"
INVOICE_PREFIX = "INV-"

_PREFIXED_ORDER = re.compile(r"\bORD(?:ER)?-(\d+)\b", re.IGNORECASE)
_PREFIXED_TICKET = re.compile(r"\bTICK(?:ET)?-(\d+)\b", re.IGNORECASE)
_INVOICE = re.compile(r"\bINV-(\d+(?:-\d+)*)\b", re.IGNORECASE)
_BARE = re.compile(
    r"\b(order|ticket)(?:\s+(?:no\.|number|#)\s*|\s*#\s*|\s+)(\d{4,})\b",
    re.IGNORECASE,
)


def extract_entity_refs(text: str) -> tuple[EntityRef, ...]:
    """Return typed, normalised references in order of first appearance, without duplicates."""
    found: list[tuple[int, EntityRef]] = []
    for match in _PREFIXED_ORDER.finditer(text):
        ref = EntityRef(entity=EntityType.ORDER, reference=f"{ORDER_PREFIX}{match.group(1)}")
        found.append((match.start(), ref))
    for match in _PREFIXED_TICKET.finditer(text):
        ref = EntityRef(entity=EntityType.TICKET, reference=f"{TICKET_PREFIX}{match.group(1)}")
        found.append((match.start(), ref))
    for match in _INVOICE.finditer(text):
        ref = EntityRef(entity=EntityType.INVOICE, reference=f"{INVOICE_PREFIX}{match.group(1)}")
        found.append((match.start(), ref))
    for match in _BARE.finditer(text):
        is_order = match.group(1).lower() == "order"
        entity = EntityType.ORDER if is_order else EntityType.TICKET
        prefix = ORDER_PREFIX if is_order else TICKET_PREFIX
        found.append(
            (match.start(), EntityRef(entity=entity, reference=f"{prefix}{match.group(2)}"))
        )

    found.sort(key=lambda item: item[0])
    seen: set[EntityRef] = set()
    refs: list[EntityRef] = []
    for _, ref in found:
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return tuple(refs)

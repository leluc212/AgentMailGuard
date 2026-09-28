"""Provider-neutral resolution and fact assembly shared by every implementation (R13.4, R13.6).

Each implementation supplies only a `CustomerLookups` object (its five scoped queries); the
rules that turn rows into statuses live here once, so the in-memory and PostgreSQL providers
cannot drift apart.

Requirements:
- R13.4: Sender → customer resolution first; every lookup scoped to the resolved customer.
- R13.6: A missing entity is an explicit NOT_FOUND fact, never an omission.
- specs/design.md §5.4 "Snapshot" and "Statuses"; docs/adr/0008-business-data-fetch-plan.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
)

Row = Mapping[str, Any]
Attributes = tuple[tuple[str, str], ...]


class CustomerLookups(Protocol):
    """The scoped queries an implementation must provide. Every one takes organization_id."""

    async def customers_by_email(self, organization_id: UUID, sender_email: str) -> Sequence[Row]:
        """Customers whose email equals `sender_email` ignoring case (at most 2 needed)."""
        ...

    async def order_by_number(
        self, organization_id: UUID, customer_id: UUID, order_number: str
    ) -> Row | None: ...

    async def ticket_by_number(
        self, organization_id: UUID, customer_id: UUID, ticket_number: str
    ) -> Row | None: ...

    async def recent_orders(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> Sequence[Row]:
        """Newest first: `placed_at DESC, order_number DESC`."""
        ...

    async def open_tickets(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> Sequence[Row]:
        """Status not closed/resolved, newest first: `opened_at DESC, ticket_number DESC`."""
        ...


CLOSED_TICKET_STATUSES: frozenset[str] = frozenset({"closed", "resolved"})
"""Ticket statuses the snapshot leaves out (design §5.4 "Snapshot")."""


def normalise_email(sender_email: str) -> str:
    """The comparison form of an address: surrounding whitespace removed, lower case."""
    return sender_email.strip().lower()


def _day(value: datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def _money(value: Any) -> str:
    return f"{Decimal(str(value)):.2f}"


def customer_attributes(row: Row) -> tuple[tuple[str, str], ...]:
    return (
        ("name", str(row["name"])),
        ("account_status", str(row.get("account_status") or "active")),
        ("tier", str(row.get("tier") or "standard")),
    )


def order_attributes(row: Row) -> tuple[tuple[str, str], ...]:
    attrs: list[tuple[str, str]] = [
        ("status", str(row["status"])),
        ("total", _money(row["total"])),
    ]
    if row.get("placed_at") is not None:
        attrs.append(("placed_at", _day(row["placed_at"])))
    if row.get("shipped_at") is not None:
        attrs.append(("shipped_at", _day(row["shipped_at"])))
    return tuple(attrs)


def ticket_attributes(row: Row) -> tuple[tuple[str, str], ...]:
    attrs: list[tuple[str, str]] = [
        ("status", str(row["status"])),
        ("priority", str(row["priority"])),
        ("subject", str(row["subject"])),
    ]
    if row.get("opened_at") is not None:
        attrs.append(("opened_at", _day(row["opened_at"])))
    return tuple(attrs)


def _unsupported(entity: EntityType, reference: str | None) -> BusinessFact:
    return BusinessFact(
        entity,
        reference,
        FactStatus.NOT_LOOKED_UP,
        reason=NotLookedUpReason.UNSUPPORTED_ENTITY.value,
    )


def _not_looked_up(plan: FetchPlan, reason: NotLookedUpReason) -> tuple[BusinessFact, ...]:
    """Every planned fact when resolution gave no single customer.

    Invoices keep `unsupported_entity`: they are never looked up, whoever the sender is.
    """
    facts: list[BusinessFact] = []
    for ref in plan.refs:
        if ref.entity is EntityType.INVOICE:
            facts.append(_unsupported(ref.entity, ref.reference))
        else:
            facts.append(
                BusinessFact(ref.entity, ref.reference, FactStatus.NOT_LOOKED_UP, reason.value)
            )
    for entity in plan.ordered_snapshot:
        if entity is EntityType.INVOICE:
            facts.append(_unsupported(entity, None))
        else:
            facts.append(BusinessFact(entity, None, FactStatus.NOT_LOOKED_UP, reason.value))
    return tuple(facts)


async def assemble_business_context(
    lookups: CustomerLookups,
    *,
    organization_id: UUID,
    sender_email: str,
    plan: FetchPlan,
    snapshot_orders: int,
    snapshot_tickets: int,
    as_of: datetime,
) -> BusinessContext:
    """Resolve the sender, then run the plan's lookups scoped to that one customer.

    Fact order: typed references in plan order, then snapshot orders, then snapshot
    tickets. A snapshot row already reported by a typed reference is not repeated. A
    snapshot kind with no rows yields one NOT_FOUND fact (reference None); a snapshot
    limit of 0 disables that kind.
    """
    customers = await lookups.customers_by_email(organization_id, normalise_email(sender_email))
    if len(customers) == 0:
        return BusinessContext(
            customer_status=CustomerStatus.UNKNOWN_SENDER,
            as_of=as_of,
            facts=_not_looked_up(plan, NotLookedUpReason.UNKNOWN_SENDER),
        )
    if len(customers) > 1:
        return BusinessContext(
            customer_status=CustomerStatus.AMBIGUOUS_CUSTOMER,
            as_of=as_of,
            facts=_not_looked_up(plan, NotLookedUpReason.AMBIGUOUS_CUSTOMER),
        )

    customer = customers[0]
    customer_id: UUID = customer["id"]
    facts: list[BusinessFact] = []
    typed: dict[EntityType, set[str]] = {entity: set() for entity in EntityType}

    for ref in plan.refs:
        typed[ref.entity].add(ref.reference)
        if ref.entity is EntityType.ORDER:
            row = await lookups.order_by_number(organization_id, customer_id, ref.reference)
            facts.append(
                BusinessFact(ref.entity, ref.reference, FactStatus.NOT_FOUND)
                if row is None
                else BusinessFact(
                    ref.entity, ref.reference, FactStatus.FOUND, attributes=order_attributes(row)
                )
            )
        elif ref.entity is EntityType.TICKET:
            row = await lookups.ticket_by_number(organization_id, customer_id, ref.reference)
            facts.append(
                BusinessFact(ref.entity, ref.reference, FactStatus.NOT_FOUND)
                if row is None
                else BusinessFact(
                    ref.entity, ref.reference, FactStatus.FOUND, attributes=ticket_attributes(row)
                )
            )
        else:
            facts.append(_unsupported(ref.entity, ref.reference))

    for entity in plan.ordered_snapshot:
        if entity is EntityType.ORDER and snapshot_orders > 0:
            rows = await lookups.recent_orders(organization_id, customer_id, snapshot_orders)
            facts.extend(_snapshot_facts(entity, rows, "order_number", order_attributes, typed))
        elif entity is EntityType.TICKET and snapshot_tickets > 0:
            rows = await lookups.open_tickets(organization_id, customer_id, snapshot_tickets)
            facts.extend(_snapshot_facts(entity, rows, "ticket_number", ticket_attributes, typed))
        elif entity is EntityType.INVOICE:
            facts.append(_unsupported(entity, None))

    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=as_of,
        customer=customer_attributes(customer),
        facts=tuple(facts),
    )


def _snapshot_facts(
    entity: EntityType,
    rows: Sequence[Row],
    number_key: str,
    attributes: Callable[[Row], Attributes],
    typed: dict[EntityType, set[str]],
) -> list[BusinessFact]:
    if not rows:
        return [BusinessFact(entity, None, FactStatus.NOT_FOUND)]
    return [
        BusinessFact(entity, str(row[number_key]), FactStatus.FOUND, attributes=attributes(row))
        for row in rows
        if str(row[number_key]) not in typed[entity]
    ]

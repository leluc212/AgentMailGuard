"""Business-data value objects: fetch plan, facts and the rendered context (R13).

Requirements:
- R13.2: Provider-neutral models, so a CRM/ERP adapter returns the same types.
- R13.4: Customer resolution is recorded once as `customer_status`.
- R13.5: Facts render as one labelled `[BUSINESS DATA]` block with source and as-of.
- R13.6: A missing entity is an explicit `NOT_FOUND` fact, never an omission.
- R13.7: `unavailable_context` marks every planned fact `UNAVAILABLE` and sets `degraded`.
- specs/design.md §5.4 "Business data (R13)"; docs/adr/0008-business-data-fetch-plan.md.
- CLAUDE.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class CustomerStatus(StrEnum):
    """Outcome of resolving the sender address to one customer record (R13.4)."""

    FOUND = "FOUND"
    UNKNOWN_SENDER = "UNKNOWN_SENDER"
    AMBIGUOUS_CUSTOMER = "AMBIGUOUS_CUSTOMER"
    UNAVAILABLE = "UNAVAILABLE"


class FactStatus(StrEnum):
    """Outcome of one planned lookup or snapshot entry (R13.6, R13.7)."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_LOOKED_UP = "NOT_LOOKED_UP"
    UNAVAILABLE = "UNAVAILABLE"


class EntityType(StrEnum):
    """Business entity kinds a plan can name. Definition order is the render order."""

    ORDER = "order"
    TICKET = "ticket"
    INVOICE = "invoice"


class NotLookedUpReason(StrEnum):
    """Why a planned fact was not looked up (design §5.4 "Statuses")."""

    UNSUPPORTED_ENTITY = "unsupported_entity"
    UNKNOWN_SENDER = "unknown_sender"
    AMBIGUOUS_CUSTOMER = "ambiguous_customer"


SNAPSHOT_LABELS: dict[EntityType, str] = {
    EntityType.ORDER: "recent orders",
    EntityType.TICKET: "open tickets",
    EntityType.INVOICE: "invoices",
}
"""Render key of a fact with no reference (a snapshot entry that found no rows)."""


def _one_line(value: str) -> str:
    """Collapse whitespace so a stored value can never open a new prompt line."""
    return " ".join(str(value).split())


@dataclass(frozen=True)
class EntityRef:
    """A typed reference extracted from the email, in stored form (e.g. `ORD-82915`)."""

    entity: EntityType
    reference: str

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("EntityRef.reference must not be empty")

    def to_payload(self) -> dict[str, str]:
        return {"entity": self.entity.value, "reference": self.reference}


@dataclass(frozen=True)
class FetchPlan:
    """What to look up for one job: typed references plus snapshot entity kinds."""

    refs: tuple[EntityRef, ...] = ()
    snapshot: frozenset[EntityType] = frozenset()

    @property
    def is_empty(self) -> bool:
        """True when nothing is planned: no provider call and no customer resolution."""
        return not self.refs and not self.snapshot

    @property
    def ordered_snapshot(self) -> tuple[EntityType, ...]:
        """Snapshot kinds in the fixed EntityType order, so output never depends on set order."""
        return tuple(entity for entity in EntityType if entity in self.snapshot)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form for the CONTEXT_READY payload (replay)."""
        return {
            "refs": [ref.to_payload() for ref in self.refs],
            "snapshot": [entity.value for entity in self.ordered_snapshot],
        }


@dataclass(frozen=True)
class BusinessFact:
    """One planned lookup or snapshot row with its status and ordered attributes."""

    entity: EntityType
    reference: str | None
    status: FactStatus
    reason: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def render_line(self) -> str:
        """One `key: value` line, e.g. `order ORD-82915: FOUND | status=shipped`."""
        key = (
            f"{self.entity.value} {_one_line(self.reference)}"
            if self.reference
            else SNAPSHOT_LABELS[self.entity]
        )
        parts = [self.status.value]
        if self.reason:
            parts.append(f"reason={self.reason}")
        parts.extend(f"{name}={_one_line(value)}" for name, value in self.attributes)
        return f"{key}: {' | '.join(parts)}"

    def to_payload(self) -> dict[str, Any]:
        """Statuses only: attribute values stay out of the persisted event payload."""
        return {
            "entity": self.entity.value,
            "reference": self.reference,
            "status": self.status.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BusinessContext:
    """Everything the business subsystem contributed to one job's context."""

    customer_status: CustomerStatus
    as_of: datetime
    customer: tuple[tuple[str, str], ...] = ()
    facts: tuple[BusinessFact, ...] = ()
    source: str = "business_db"
    degraded: bool = False

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("BusinessContext.as_of must be timezone-aware")

    def render(self) -> str:
        """The `[BUSINESS DATA]` block: header, customer_status, then one line per fact."""
        lines = [f"[BUSINESS DATA] source={self.source} as_of={self.as_of.isoformat()}"]
        if self.degraded:
            lines.append("degraded: true")
        lines.append(f"customer_status: {self.customer_status.value}")
        lines.extend(f"customer.{name}: {_one_line(value)}" for name, value in self.customer)
        lines.extend(fact.render_line() for fact in self.facts)
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form for the CONTEXT_READY payload; carries no customer attributes."""
        return {
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "customer_status": self.customer_status.value,
            "degraded": self.degraded,
            "facts": [fact.to_payload() for fact in self.facts],
        }


def unavailable_context(plan: FetchPlan, as_of: datetime) -> BusinessContext:
    """The degraded context: customer and every planned fact `UNAVAILABLE` (R13.7)."""
    facts = [BusinessFact(ref.entity, ref.reference, FactStatus.UNAVAILABLE) for ref in plan.refs]
    facts.extend(
        BusinessFact(entity, None, FactStatus.UNAVAILABLE) for entity in plan.ordered_snapshot
    )
    return BusinessContext(
        customer_status=CustomerStatus.UNAVAILABLE,
        as_of=as_of,
        facts=tuple(facts),
        degraded=True,
    )

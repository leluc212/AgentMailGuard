"""Pure FetchPlan builder: what business data to fetch, decided in code (R13.3, ADR-0008).

No I/O and no model call. Inputs are signals the pipeline already has (design.md §5.4):
1. typed IDs in subject + body are always planned (they override ``context_policy``);
2. snapshot: the routed profile's ``context_policy`` == ``thread_plus_rag_plus_business``
   plans orders + tickets; otherwise an intent in ``INTENT_ENTITIES`` plans only its entities;
3. nothing planned means an empty plan, so the caller makes no provider call.

``INV-`` references are planned like any typed ID; the provider records them as
``NOT_LOOKED_UP`` with reason ``unsupported_entity`` because there is no invoice table.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from packages.business.identifiers import extract_entity_refs
from packages.domain.business import EntityType, FetchPlan
from packages.llm.profile import ContextPolicy

_ORDERS_ONLY = frozenset({EntityType.ORDER})
_FULL_SNAPSHOT = frozenset({EntityType.ORDER, EntityType.TICKET})

INTENT_ENTITIES: Mapping[str, frozenset[EntityType]] = MappingProxyType(
    {
        "invoice_inquiry": _ORDERS_ONLY,
        "receipt_lookup": _ORDERS_ONLY,
        "payment_failure": _ORDERS_ONLY,
        "refund_request": _ORDERS_ONLY,
    }
)
"""Intents that name transactional entities without a typed ID (design.md §5.4)."""


def build_fetch_plan(
    *, subject: str, body: str, context_policy: str, intent: str | None
) -> FetchPlan:
    """Plan the lookups for one job from its text, profile policy and intent."""
    refs = extract_entity_refs(f"{subject}\n{body}")
    if context_policy == ContextPolicy.THREAD_PLUS_RAG_PLUS_BUSINESS:
        snapshot = _FULL_SNAPSHOT
    else:
        key = intent.strip().lower() if intent else ""
        snapshot = INTENT_ENTITIES.get(key, frozenset())
    return FetchPlan(refs=refs, snapshot=snapshot)

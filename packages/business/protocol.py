"""The replaceable business-data provider interface (R13.2).

Requirements:
- R13.2: One interface, so the local PostgreSQL implementation can later be replaced by a
  CRM/ERP adapter without touching the Context Builder.
- R13.4: The provider resolves the sender to a customer first and scopes every lookup to it.
- specs/design.md §5.4 "Business data (R13)"; docs/adr/0008-business-data-fetch-plan.md.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from packages.domain.business import BusinessContext, FetchPlan


@runtime_checkable
class BusinessDataProvider(Protocol):
    """Executes a FetchPlan for one sender inside one organization.

    Implementations resolve `sender_email` to a customer (case-insensitive, whole address,
    within `organization_id`) and run only fixed, parameterised lookups scoped to
    `(organization_id, customer_id)`. They raise on infrastructure errors; the caller
    (`fetch_business_context`, task 5.4) turns a timeout or error into
    `unavailable_context`.
    """

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext: ...

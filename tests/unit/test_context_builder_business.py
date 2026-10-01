"""Context Builder business-data wiring (R13.3, R13.7, design.md §5.4, ADR-0008).

Typed IDs are always planned; the routed profile's context_policy gates the snapshot; nothing
planned means no provider call; a timeout degrades instead of failing; the plan, statuses and
degradation flag go into the CONTEXT_READY payload.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.business.memory import InMemoryBusinessDataProvider
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.db.fixtures import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDERS,
    BUSINESS_TICKETS,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
)
from packages.db.job import InMemoryJobStore
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
)
from packages.domain.entities import (
    Classification,
    EmailAddress,
    Job,
    NormalizedMessage,
    ProcessingEvent,
)
from packages.domain.state_machine import JobState
from packages.llm.profile import AgentProfileRegistry

REGISTRY_PATH = "config/agent_profiles.yaml"
ALICE = "alice.smith@clientcorp.com"
ORDER_PLAN = FetchPlan(refs=(EntityRef(entity=EntityType.ORDER, reference="ORD-82915"),))


def _found_order() -> BusinessContext:
    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        customer=(("name", "Alice Smith"),),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-82915",
                status=FactStatus.FOUND,
                attributes=(("status", "shipped"),),
            ),
        ),
    )


class SpyProvider:
    def __init__(self, result: BusinessContext | None = None, *, delay_s: float = 0.0) -> None:
        self.result = result
        self.delay_s = delay_s
        self.calls: list[tuple[UUID, str, FetchPlan]] = []

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        self.calls.append((organization_id, sender_email, plan))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        assert self.result is not None
        return self.result


def _message(
    org_id: UUID,
    *,
    subject: str,
    body: str,
    sender: str = ALICE,
    sender_name: str | None = None,
    body_clean: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email=sender, name=sender_name),
        recipients=[EmailAddress(email="support@acme.com")],
        subject=subject,
        body_text=body,
        body_text_clean=body if body_clean is None else body_clean,
        received_at=datetime.now(UTC),
    )


async def _queued_job(store: InMemoryJobStore, msg: NormalizedMessage) -> Job:
    job = Job(
        id=uuid4(),
        organization_id=msg.organization_id,
        thread_id=msg.thread_id,
        message_id=msg.message_id,
        state=JobState.QUEUED,
        idempotency_key=f"job-{msg.message_id}",
    )
    await store.create_job(job)
    return job


async def _context_ready(store: InMemoryJobStore, job: Job) -> ProcessingEvent:
    events = await store.list_events_for_job(job.organization_id, job.id)
    return next(e for e in events if e.state_to == JobState.CONTEXT_READY.value)


def _builder(
    provider: SpyProvider | InMemoryBusinessDataProvider | None,
    store: InMemoryJobStore,
    *,
    timeout_ms: int = 500,
) -> ContextBuilder:
    return ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        business_data_provider=provider,
        job_store=store,
        profile_registry=AgentProfileRegistry.from_yaml(REGISTRY_PATH),
        business_timeout_ms=timeout_ms,
    )


async def test_typed_order_id_is_fetched_even_for_a_general_inquiry() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Quick question", body="What is the status of order 82915?")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="general_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == [(org_id, ALICE, ORDER_PLAN)]
    assert pkg.business_data is provider.result
    assert [name for name, _ in pkg.get_ordered_sections()][-1] == "business_data"

    ready = await _context_ready(store, job)
    assert ready.payload["business_plan"] == ORDER_PLAN.to_payload()
    assert ready.payload["customer_status"] == "FOUND"
    assert ready.payload["business_fact_statuses"] == [
        {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
    ]
    assert ready.payload["business_data_degraded"] is False
    assert ready.payload["retrieval_performed"] is False


async def test_billing_profile_plans_the_snapshot_without_ids() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="My account", body="I think I was charged twice.")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="billing", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == [
        (org_id, ALICE, FetchPlan(snapshot=frozenset({EntityType.ORDER, EntityType.TICKET})))
    ]


async def test_mapped_intent_plans_orders_under_a_non_business_profile() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Refund", body="Please refund my last purchase.")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="support", intent="refund_request", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == [(org_id, ALICE, FetchPlan(snapshot=frozenset({EntityType.ORDER})))]


async def test_no_classification_uses_the_default_profile_and_plans_nothing() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Hello", body="Just saying thanks for the help.")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(job, msg, None, thread_messages=[msg])

    assert provider.calls == []
    assert pkg.business_data is None
    assert "business_data" not in [name for name, _ in pkg.get_ordered_sections()]
    ready = await _context_ready(store, job)
    assert ready.payload["business_plan"] == FetchPlan().to_payload()
    assert ready.payload["customer_status"] is None
    assert ready.payload["business_fact_statuses"] == []
    assert ready.payload["business_data_degraded"] is False


async def test_provider_timeout_degrades_and_the_job_still_reaches_context_ready() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Order", body="Where is order #82915?")
    provider = SpyProvider(_found_order(), delay_s=1.0)
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store, timeout_ms=20).build_context(
        job,
        msg,
        Classification(category="general_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert pkg.business_data is not None
    assert pkg.business_data.customer_status == CustomerStatus.UNAVAILABLE
    assert pkg.business_data.degraded is True
    assert job.state == JobState.CONTEXT_READY
    ready = await _context_ready(store, job)
    assert ready.payload["business_data_degraded"] is True
    assert ready.payload["customer_status"] == "UNAVAILABLE"
    assert [row["status"] for row in ready.payload["business_fact_statuses"]] == ["UNAVAILABLE"]


async def test_without_a_provider_nothing_is_planned_or_fetched() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Invoice", body="Question on invoice INV-2026-001, order 82915.")
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(None, store).build_context(
        job,
        msg,
        Classification(category="billing", intent="invoice_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert pkg.business_data is None
    ready = await _context_ready(store, job)
    assert ready.payload["business_plan"] == FetchPlan().to_payload()
    assert ready.payload["business_data_degraded"] is False


async def test_invoice_reference_reaches_the_context_as_not_looked_up() -> None:
    """Bob's billing fixture email: INV- is planned and recorded, never NOT_FOUND (R13.6)."""
    fixture = next(f for f in FIXTURE_EMAILS if f.key == "billing_invoice")
    msg = _message(
        DEMO_ORG_ID,
        subject=fixture.subject,
        body=fixture.body_text,
        sender=fixture.sender_email,
    )
    provider = InMemoryBusinessDataProvider(
        customers=BUSINESS_CUSTOMERS, orders=BUSINESS_ORDERS, tickets=BUSINESS_TICKETS
    )
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="billing", retrieval_required=False),
        thread_messages=[msg],
    )

    assert pkg.business_data is not None
    assert pkg.business_data.customer_status == CustomerStatus.FOUND
    invoice = next(f for f in pkg.business_data.facts if f.entity == EntityType.INVOICE)
    assert invoice.reference == "INV-2026-8891"
    assert invoice.status == FactStatus.NOT_LOOKED_UP
    assert invoice.reason == NotLookedUpReason.UNSUPPORTED_ENTITY


async def test_sender_case_and_display_name_reach_the_provider_as_the_bare_address() -> None:
    """Review focus 1: the parser splits off the display name; the provider ignores case."""
    msg = _message(
        DEMO_ORG_ID,
        subject="Order status question",
        body="What is the status of order 82915?",
        sender="Alice.Smith@ClientCorp.COM",
        sender_name="Alice Smith",
    )
    classification = Classification(category="general_inquiry", retrieval_required=False)

    spy = SpyProvider(_found_order())
    spy_store = InMemoryJobStore()
    await _builder(spy, spy_store).build_context(
        await _queued_job(spy_store, msg), msg, classification, thread_messages=[msg]
    )
    assert [sender for _, sender, _ in spy.calls] == ["Alice.Smith@ClientCorp.COM"]

    provider = InMemoryBusinessDataProvider(
        customers=BUSINESS_CUSTOMERS, orders=BUSINESS_ORDERS, tickets=BUSINESS_TICKETS
    )
    store = InMemoryJobStore()
    pkg = await _builder(provider, store).build_context(
        await _queued_job(store, msg), msg, classification, thread_messages=[msg]
    )
    assert pkg.business_data is not None
    assert pkg.business_data.customer_status == CustomerStatus.FOUND
    order = next(f for f in pkg.business_data.facts if f.reference == "ORD-82915")
    assert order.status == FactStatus.FOUND
    assert dict(order.attributes)["status"] == "dispatched"  # seeded by Task 2


async def test_order_number_only_in_quoted_history_is_not_planned() -> None:
    """Review focus 5: the plan reads body_text_clean, which has the quoted history stripped."""
    org_id = uuid4()
    clean = "Thanks, that answers my question."
    msg = _message(
        org_id,
        subject="Re: Thanks",
        body=f"{clean}\n\nOn Mon, 21 Sep 2026, Alice wrote:\n> Where is order #77001?",
        body_clean=clean,
    )
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="general_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == []
    assert pkg.business_data is None

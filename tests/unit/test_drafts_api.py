"""Draft review API on in-memory stores (task 6.1, 6.2; R16.6, R16.7, R23.2, R23.6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import APISettings, BrokerSettings, CategoryRoutingSettings
from packages.db.review import BusinessDataView, BusinessFactView, InMemoryReviewStore
from packages.domain.dispatch import DispatchMode
from packages.domain.entities import EmailAddress, GeneratedDraft, Job, NormalizedMessage
from packages.domain.review import edit_distance
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import get_default_registry
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from services.api.main import create_app

CHUNK_ID = uuid4()
GENERATED_BODY = "Your order ORD-82915 was dispatched on 24 September."


@dataclass
class RecordingPublisher:
    """Stands in for MessagePublisher: records what approve publishes."""

    settings: BrokerSettings = field(default_factory=BrokerSettings)
    published: list[tuple[str, str, JobEnvelope]] = field(default_factory=list)
    fail: bool = False

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.published.append((exchange_name, routing_key, envelope))


@dataclass(frozen=True)
class Seeded:
    org_id: UUID
    mailbox_id: UUID
    draft: GeneratedDraft
    job: Job
    original: NormalizedMessage


async def _seed(
    store: InMemoryReviewStore,
    org_id: UUID,
    *,
    category: str = "billing",
    created_at: datetime | None = None,
    mailbox_id: UUID | None = None,
    job_state: JobState = JobState.DRAFTED,
) -> Seeded:
    mbx = mailbox_id or uuid4()
    thread_id = uuid4()
    original = NormalizedMessage(
        message_id=uuid4(),
        thread_id=thread_id,
        mailbox_id=mbx,
        organization_id=org_id,
        provider="fake",
        provider_message_id=f"prov-{uuid4().hex[:8]}",
        sender=EmailAddress("alice@customer.example", "Alice"),
        received_at=datetime.now(UTC),
        rfc822_message_id="orig-1@customer.example",
        subject="Where is order 82915?",
        body_text="What is the status of order 82915?",
    )
    job, _ = await store.jobs.create_job(
        Job(
            organization_id=org_id,
            message_id=original.message_id,
            thread_id=thread_id,
            state=job_state.value,
            idempotency_key=f"gen-{uuid4()}",
        )
    )
    draft = GeneratedDraft(
        organization_id=org_id,
        message_id=original.message_id,
        thread_id=thread_id,
        job_id=job.id,
        subject="Re: Where is order 82915?",
        body=GENERATED_BODY,
        citations=[
            {
                "citation_id": "DOC-7-01",
                "chunk_id": str(CHUNK_ID),
                "document_id": str(uuid4()),
                "external_id": "DOC-7-01",
            }
        ],
        created_at=created_at or datetime.now(UTC),
    )
    store.add_draft(
        draft,
        original=original,
        category=category,
        thread_summary="Alice asks where order 82915 is.",
        business_data=BusinessDataView(
            customer_status="FOUND",
            degraded=False,
            facts=(BusinessFactView(entity="order", reference="ORD-82915", status="FOUND"),),
        ),
    )
    return Seeded(org_id, mbx, draft, job, original)


@pytest.fixture
def store() -> InMemoryReviewStore:
    s = InMemoryReviewStore()
    s.add_chunk(CHUNK_ID, content="Orders ship within 2 business days.", heading_path=("Shipping",))
    return s


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def metrics() -> PipelineMetrics:
    return create_pipeline_metrics()


@pytest.fixture
def app(
    store: InMemoryReviewStore, publisher: RecordingPublisher, metrics: PipelineMetrics
) -> FastAPI:
    application = create_app(lifespan_enabled=False)
    application.state.review_store = store
    application.state.publisher = publisher
    application.state.metrics = metrics
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


def _h(org_id: UUID) -> dict[str, str]:
    return {"X-Organization-Id": str(org_id)}


def _decisions(metrics: PipelineMetrics, decision: str, category: str) -> float:
    value: float = metrics.draft_decisions_total.labels(
        decision=decision, category=category
    )._value.get()
    return value


async def test_list_filters_and_cursor_pagination(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    """R16.6 / R23.6: newest first, keyset cursor, filters, tenant scope."""
    org = uuid4()
    mbx = uuid4()
    base = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    a = await _seed(store, org, created_at=base, mailbox_id=mbx)
    b = await _seed(store, org, created_at=base + timedelta(minutes=1), mailbox_id=mbx)
    c = await _seed(store, org, category="support", created_at=base + timedelta(minutes=2))
    await _seed(store, uuid4())  # another tenant's draft never appears

    first = await client.get("/v1/drafts", params={"limit": 2}, headers=_h(org))
    assert first.status_code == 200
    page1 = first.json()
    assert [i["id"] for i in page1["items"]] == [str(c.draft.id), str(b.draft.id)]
    assert page1["limit"] == 2
    assert page1["next_cursor"]

    second = await client.get(
        "/v1/drafts", params={"limit": 2, "cursor": page1["next_cursor"]}, headers=_h(org)
    )
    page2 = second.json()
    assert [i["id"] for i in page2["items"]] == [str(a.draft.id)]
    assert page2["next_cursor"] is None

    billing = (
        await client.get("/v1/drafts", params={"category": "billing"}, headers=_h(org))
    ).json()
    assert {i["id"] for i in billing["items"]} == {str(a.draft.id), str(b.draft.id)}
    assert billing["items"][0]["category"] == "billing"
    assert billing["items"][0]["job_state"] == "DRAFTED"

    by_mailbox = (
        await client.get("/v1/drafts", params={"mailbox": str(mbx)}, headers=_h(org))
    ).json()
    assert {i["id"] for i in by_mailbox["items"]} == {str(a.draft.id), str(b.draft.id)}

    approved = (
        await client.get("/v1/drafts", params={"status": "approved"}, headers=_h(org))
    ).json()
    assert approved["items"] == []


async def test_invalid_cursor_and_status_are_rejected(client: AsyncClient) -> None:
    org = uuid4()
    bad_cursor = await client.get("/v1/drafts", params={"cursor": "nope"}, headers=_h(org))
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"
    bad_status = await client.get("/v1/drafts", params={"status": "sent"}, headers=_h(org))
    assert bad_status.status_code == 422


async def test_detail_carries_original_summary_chunks_and_business_facts(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    """6.1: the reviewer sees the email, the thread summary, cited chunks and business facts."""
    seeded = await _seed(store, uuid4())
    resp = await client.get(f"/v1/drafts/{seeded.draft.id}", headers=_h(seeded.org_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == GENERATED_BODY
    assert body["original"]["subject"] == "Where is order 82915?"
    assert body["original"]["sender_email"] == "alice@customer.example"
    assert body["thread_summary"] == "Alice asks where order 82915 is."
    assert body["cited_chunks"] == [
        {
            "citation_id": "DOC-7-01",
            "chunk_id": str(CHUNK_ID),
            "document_id": seeded.draft.citations[0]["document_id"],
            "external_id": "DOC-7-01",
            "content": "Orders ship within 2 business days.",
            "heading_path": ["Shipping"],
        }
    ]
    assert body["business_data"] == {
        "customer_status": "FOUND",
        "degraded": False,
        "facts": [{"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}],
    }
    assert body["feedback"] is None
    assert body["dispatch_mode"] == "create_draft"  # the UI tells the reviewer what approve does


async def test_detail_reports_the_dispatch_mode_from_the_categories_yaml(
    tmp_path: Path, store: InMemoryReviewStore
) -> None:
    """6.4 / 6.8: the API loads the same YAML as the dispatch-worker, so the reviewer sees the
    mode approve will really use; the process-default registry is not changed."""
    config = tmp_path / "categories.yaml"
    config.write_text(
        "categories:\n  - category: billing\n    dispatch_mode: send_reply\n",
        encoding="utf-8",
    )
    settings = APISettings(
        _env_file=None, routing=CategoryRoutingSettings(categories_config_path=str(config))
    )
    application = create_app(settings, lifespan_enabled=False)
    application.state.review_store = store
    seeded = await _seed(store, uuid4(), category="billing")

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        resp = await ac.get(f"/v1/drafts/{seeded.draft.id}", headers=_h(seeded.org_id))

    assert resp.status_code == 200
    assert resp.json()["dispatch_mode"] == "send_reply"
    assert get_default_registry().dispatch_mode_for("billing") is DispatchMode.CREATE_DRAFT


async def test_other_tenant_gets_404_everywhere(
    client: AsyncClient, store: InMemoryReviewStore, publisher: RecordingPublisher
) -> None:
    """R23.6: a draft id from another organization is invisible and cannot be decided."""
    seeded = await _seed(store, uuid4())
    other = _h(uuid4())
    draft_url = f"/v1/drafts/{seeded.draft.id}"
    assert (await client.get(draft_url, headers=other)).status_code == 404
    assert (await client.patch(draft_url, json={"body": "x"}, headers=other)).status_code == 404
    assert (await client.post(f"{draft_url}/approve", headers=other)).status_code == 404
    assert (await client.post(f"{draft_url}/reject", headers=other)).status_code == 404
    assert publisher.published == []


async def test_patch_edits_only_while_draft(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    edited = await client.patch(
        url, json={"body": "Order ORD-82915 shipped on 24 September."}, headers=_h(seeded.org_id)
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "draft"
    detail = (await client.get(url, headers=_h(seeded.org_id))).json()
    assert detail["body"] == "Order ORD-82915 shipped on 24 September."

    await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    late = await client.patch(url, json={"body": "too late"}, headers=_h(seeded.org_id))
    assert late.status_code == 409
    assert late.json()["code"] == "DRAFT_NOT_EDITABLE"


async def test_approve_unchanged_publishes_dispatch_and_counts_accepted(
    client: AsyncClient,
    store: InMemoryReviewStore,
    publisher: RecordingPublisher,
    metrics: PipelineMetrics,
) -> None:
    """6.1 / 6.2: approve commits, then publishes to email.dispatch; one accepted decision."""
    seeded = await _seed(store, uuid4())
    resp = await client.post(
        f"/v1/drafts/{seeded.draft.id}/approve",
        json={"reviewer": "demo", "rating": 5, "review_ms": 4200},
        headers=_h(seeded.org_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "accepted"
    assert body["edit_distance"] == 0
    assert body["draft_status"] == "approved"
    assert body["job_state"] == "DRAFTED"
    assert body["created"] is True
    assert body["published"] is True

    [(exchange, routing_key, envelope)] = publisher.published
    assert (exchange, routing_key) == ("email.dispatch", "email.dispatch")
    assert envelope.job_id == str(seeded.job.id)
    assert envelope.job_type == "dispatch"
    assert envelope.mailbox_id == str(seeded.mailbox_id)
    assert envelope.payload == {"draft_id": str(seeded.draft.id), "trigger": "approve"}
    assert envelope.idempotency_key == derive_idempotency_key(
        organization_id=seeded.org_id,
        mailbox_id=seeded.mailbox_id,
        provider_message_id=seeded.original.provider_message_id,
        operation_type="dispatch",
    )

    feedback = (
        await client.get(f"/v1/drafts/{seeded.draft.id}", headers=_h(seeded.org_id))
    ).json()["feedback"]
    assert feedback["decision"] == "accepted"
    assert feedback["review_ms"] == 4200
    assert feedback["rating"] == 5
    assert feedback["reviewer"] == "demo"
    assert _decisions(metrics, "accepted", "billing") == 1


async def test_edit_then_approve_records_edited_with_distance_from_generated_body(
    client: AsyncClient, store: InMemoryReviewStore, metrics: PipelineMetrics
) -> None:
    """6.2: two edits, then approve: distance is from the generated body, not the last edit."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    await client.patch(url, json={"body": "Draft one."}, headers=_h(seeded.org_id))
    final = "Order ORD-82915 shipped on 24 September. Tracking follows."
    await client.patch(url, json={"body": final}, headers=_h(seeded.org_id))

    body = (await client.post(f"{url}/approve", headers=_h(seeded.org_id))).json()
    assert body["decision"] == "edited"
    assert body["edit_distance"] == edit_distance(GENERATED_BODY, final)
    feedback = (await client.get(url, headers=_h(seeded.org_id))).json()["feedback"]
    assert feedback["edited_body"] == final
    assert _decisions(metrics, "edited", "billing") == 1
    assert _decisions(metrics, "accepted", "billing") == 0


async def test_repeated_approve_republishes_without_a_second_feedback_row(
    client: AsyncClient,
    store: InMemoryReviewStore,
    publisher: RecordingPublisher,
    metrics: PipelineMetrics,
) -> None:
    """6.1: a lost publish cannot strand an approved draft; feedback stays one row."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}/approve"
    first = (await client.post(url, headers=_h(seeded.org_id))).json()
    second = (await client.post(url, headers=_h(seeded.org_id))).json()

    assert second["created"] is False
    assert second["published"] is True
    assert second["feedback_id"] == first["feedback_id"]
    assert len(publisher.published) == 2
    assert len(store.feedback_rows(seeded.org_id)) == 1
    assert _decisions(metrics, "accepted", "billing") == 1


async def test_approve_after_completion_publishes_nothing(
    client: AsyncClient, store: InMemoryReviewStore, publisher: RecordingPublisher
) -> None:
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}/approve"
    await client.post(url, headers=_h(seeded.org_id))
    for state in (JobState.DISPATCHED, JobState.COMPLETED):
        await store.jobs.transition_job_state(seeded.org_id, seeded.job.id, state)

    again = (await client.post(url, headers=_h(seeded.org_id))).json()
    assert again["job_state"] == "COMPLETED"
    assert again["published"] is False
    assert len(publisher.published) == 1


async def test_publish_failure_keeps_the_approval_and_approve_again_republishes(
    client: AsyncClient, store: InMemoryReviewStore, publisher: RecordingPublisher
) -> None:
    """Approve commits before it publishes (design.md §5.8)."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    publisher.fail = True
    failed = await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    assert failed.status_code == 503
    assert failed.json()["code"] == "DISPATCH_PUBLISH_FAILED"
    assert (await client.get(url, headers=_h(seeded.org_id))).json()["status"] == "approved"

    publisher.fail = False
    retried = (await client.post(f"{url}/approve", headers=_h(seeded.org_id))).json()
    assert retried["published"] is True
    assert retried["created"] is False
    assert len(publisher.published) == 1
    assert len(store.feedback_rows(seeded.org_id)) == 1


async def test_missing_publisher_is_503_after_commit(
    app: FastAPI, client: AsyncClient, store: InMemoryReviewStore
) -> None:
    seeded = await _seed(store, uuid4())
    app.state.publisher = None
    resp = await client.post(f"/v1/drafts/{seeded.draft.id}/approve", headers=_h(seeded.org_id))
    assert resp.status_code == 503
    assert resp.json()["code"] == "PUBLISHER_UNAVAILABLE"
    assert len(store.feedback_rows(seeded.org_id)) == 1


async def test_reject_completes_the_job_without_dispatch(
    client: AsyncClient,
    store: InMemoryReviewStore,
    publisher: RecordingPublisher,
    metrics: PipelineMetrics,
) -> None:
    """6.1: reject is DRAFTED -> COMPLETED with no send; a repeated reject returns the first."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    first = (
        await client.post(f"{url}/reject", json={"review_ms": 900}, headers=_h(seeded.org_id))
    ).json()
    assert first["decision"] == "rejected"
    assert first["job_state"] == "COMPLETED"
    assert first["draft_status"] == "rejected"
    assert first["published"] is False

    again = (await client.post(f"{url}/reject", headers=_h(seeded.org_id))).json()
    assert again["feedback_id"] == first["feedback_id"]
    assert again["created"] is False

    approve = await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    assert approve.status_code == 409
    assert approve.json()["code"] == "DRAFT_ALREADY_REJECTED"
    assert publisher.published == []
    job = await store.jobs.get_job(seeded.org_id, seeded.job.id)
    assert job is not None and job.state == JobState.COMPLETED.value
    assert _decisions(metrics, "rejected", "billing") == 1


async def test_reject_after_approve_is_a_conflict(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    resp = await client.post(f"{url}/reject", headers=_h(seeded.org_id))
    assert resp.status_code == 409
    assert resp.json()["code"] == "DRAFT_ALREADY_APPROVED"


async def test_approve_requires_a_drafted_job(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    seeded = await _seed(store, uuid4(), job_state=JobState.GENERATING)
    resp = await client.post(f"/v1/drafts/{seeded.draft.id}/approve", headers=_h(seeded.org_id))
    assert resp.status_code == 409
    assert resp.json()["code"] == "JOB_NOT_DRAFTED"


async def test_decision_body_is_validated(client: AsyncClient, store: InMemoryReviewStore) -> None:
    seeded = await _seed(store, uuid4())
    resp = await client.post(
        f"/v1/drafts/{seeded.draft.id}/approve",
        json={"rating": 6, "review_ms": -1},
        headers=_h(seeded.org_id),
    )
    assert resp.status_code == 422

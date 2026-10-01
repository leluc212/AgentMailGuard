"""Review UI job timeline and knowledge upload over the real /v1 API (task 6.8).

Requirements: R23.5 (job timeline and current state per message from processing_event),
R23.7 (knowledge upload with per-document ingestion status), R23.6 (tenant header).
The frontend's /v1 client is wired in-process (httpx.ASGITransport) to services.api with
in-memory stores, so these tests exercise the real API contract without a network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from packages.core.settings import FrontendServiceSettings, FrontendSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.job import InMemoryJobStore
from packages.db.knowledge import InMemoryKnowledgeStore
from packages.db.message import InMemoryMessageStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage
from packages.domain.knowledge import KnowledgeDocument
from packages.domain.state_machine import JobState
from services.api.main import create_app as create_api_app
from services.frontend.main import create_app as create_frontend_app

ORG = UUID("33333333-3333-4333-8333-333333333333")
HX = {"HX-Request": "true"}
PIPELINE = (
    JobState.NORMALIZED,
    JobState.CLASSIFIED,
    JobState.QUEUED,
    JobState.CONTEXT_READY,
    JobState.GENERATING,
    JobState.DRAFTED,
)


def _api(**state: Any) -> FastAPI:
    app = create_api_app(lifespan_enabled=False)
    app.state.storage_client = FakeObjectStorageClient()
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


@asynccontextmanager
async def _ui(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    settings = FrontendServiceSettings(
        _env_file=None, frontend=FrontendSettings(api_base_url="http://api", organization_id=ORG)
    )
    ui_app = create_frontend_app(
        settings, transport=httpx.ASGITransport(app=api_app), configure_logging=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_app), base_url="http://ui"
    ) as client:
        yield client


async def _message_with_job(
    states: tuple[JobState, ...],
) -> tuple[UUID, InMemoryMessageStore, InMemoryJobStore]:
    messages, jobs = InMemoryMessageStore(), InMemoryJobStore()
    msg_id, thread_id = uuid4(), uuid4()
    await messages.insert_message(
        NormalizedMessage(
            message_id=msg_id,
            thread_id=thread_id,
            mailbox_id=uuid4(),
            organization_id=ORG,
            provider="fake",
            provider_message_id=f"prov-{msg_id.hex[:8]}",
            sender=EmailAddress("alice.smith@clientcorp.com", "Alice Smith"),
            received_at=datetime(2026, 9, 28, 9, 0, tzinfo=UTC),
            subject="Where is order ORD-82915?",
            body_text="Hello",
        )
    )
    job, _ = await jobs.create_job(
        Job(
            organization_id=ORG,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.RECEIVED.value,
            idempotency_key=f"timeline-{uuid4()}",
        )
    )
    for state in states:
        await jobs.transition_job_state(ORG, job.id, state)
    return msg_id, messages, jobs


async def test_timeline_shows_every_event_and_the_current_state_and_keeps_polling() -> None:
    msg_id, messages, jobs = await _message_with_job(PIPELINE)
    async with _ui(_api(message_store=messages, job_store=jobs)) as ui:
        response = await ui.get(f"/messages/{msg_id}/timeline")

    assert response.status_code == 200
    html = response.text
    assert "Where is order ORD-82915?" in html
    assert "Current state: <strong>DRAFTED</strong>" in html
    for state in ("RECEIVED", *(s.value for s in PIPELINE)):
        assert f"<td>{state}</td>" in html
    assert 'hx-trigger="every 5s"' in html


async def test_a_finished_job_timeline_stops_polling() -> None:
    msg_id, messages, jobs = await _message_with_job((*PIPELINE, JobState.COMPLETED))
    async with _ui(_api(message_store=messages, job_store=jobs)) as ui:
        response = await ui.get(f"/messages/{msg_id}/timeline")

    assert "Current state: <strong>COMPLETED</strong>" in response.text
    assert "every 5s" not in response.text


async def test_a_timeline_poll_returns_only_the_events_fragment() -> None:
    msg_id, messages, jobs = await _message_with_job(PIPELINE)
    async with _ui(_api(message_store=messages, job_store=jobs)) as ui:
        response = await ui.get(f"/messages/{msg_id}/timeline", headers=HX)

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="timeline-events"' in response.text


async def test_an_unknown_message_timeline_is_not_found() -> None:
    async with _ui(_api(message_store=InMemoryMessageStore(), job_store=InMemoryJobStore())) as ui:
        response = await ui.get(f"/messages/{uuid4()}/timeline")
    assert response.status_code == 404
    assert "not found" in response.text


async def test_upload_forwards_the_file_and_lists_it_with_its_status() -> None:
    store = InMemoryKnowledgeStore()
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    async with _ui(_api(knowledge_store=store, publisher=publisher)) as ui:
        response = await ui.post(
            "/knowledge",
            data={"title": "Refund policy", "category": "policy"},
            files={"file": ("refund.md", b"# Refunds\n\nWithin 14 days.", "text/markdown")},
            headers=HX,
        )

    assert response.status_code == 200
    assert "Upload accepted: Refund policy is pending." in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "<td>Refund policy</td>" in response.text
    docs, total = await store.list_documents(organization_id=ORG)
    assert total == 1
    assert docs[0].title == "Refund policy"
    assert docs[0].category == "policy"
    publisher.publish.assert_awaited_once()


async def test_an_empty_file_is_refused_without_calling_the_api() -> None:
    store = InMemoryKnowledgeStore()
    async with _ui(_api(knowledge_store=store, publisher=MagicMock())) as ui:
        response = await ui.post(
            "/knowledge", files={"file": ("empty.md", b"", "text/markdown")}, headers=HX
        )

    assert response.status_code == 200
    assert response.headers["HX-Reswap"] == "none"
    assert "Error: Choose a non-empty file to upload." in response.text
    assert (await store.list_documents(organization_id=ORG))[1] == 0


async def test_a_document_row_polls_until_ingestion_finishes_then_announces_it() -> None:
    store = InMemoryKnowledgeStore()
    doc = await store.insert_document(
        KnowledgeDocument(organization_id=ORG, title="Refund policy", status="embedding")
    )
    async with _ui(_api(knowledge_store=store)) as ui:
        running = await ui.get(f"/knowledge/documents/{doc.id}/status", headers=HX)
        await store.update_document_status(ORG, doc.id, "active")
        finished = await ui.get(f"/knowledge/documents/{doc.id}/status", headers=HX)

    assert 'hx-trigger="every 3s"' in running.text
    assert "embedding" in running.text
    assert "hx-swap-oob" not in running.text
    assert "every 3s" not in finished.text
    assert "Refund policy: active" in finished.text


async def test_the_knowledge_page_shows_failures_with_their_reason() -> None:
    store = InMemoryKnowledgeStore()
    doc = await store.insert_document(
        KnowledgeDocument(organization_id=ORG, title="Scanned contract", status="pending")
    )
    await store.update_document_status(ORG, doc.id, "failed", failure_reason="No text layer")
    async with _ui(_api(knowledge_store=store)) as ui:
        response = await ui.get("/knowledge")

    assert response.status_code == 200
    assert "Scanned contract" in response.text
    assert "failed" in response.text
    assert "No text layer" in response.text
    assert '<label for="file">' in response.text

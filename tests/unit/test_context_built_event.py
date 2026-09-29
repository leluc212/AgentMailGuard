"""The ai-worker records one ``context_built`` event per context build (R21, task 7.20).

The live benchmark's feeder reads the event to learn which chunks reached the model and whether
retrieval degraded, a reranker ran or a summary was written. Values the ContextPackage does not
expose are recorded as null (unknown), never guessed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from packages.broker.envelope import JobEnvelope
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.context.summarizer import ThreadSummarizer
from packages.core.settings import SummarizationSettings
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.db.thread_state import InMemoryThreadStateStore
from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
    ProcessingEvent,
)
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.router import ComplexityRouter
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.consumer import AIWorkerConsumer, context_built_payload
from services.ai_worker.drafting import DraftingService

CHUNK = {"chunk_id": "CHUNK-INV-1", "document_id": "DOC-BILLING"}


class DiagnosingBuilder(ContextBuilder):
    """A builder whose package carries diagnostics, as a later builder revision will."""

    annotations: dict[str, Any] = {}
    chunk_rerank_score: float | None = None

    async def build_context(self, *args: Any, **kwargs: Any) -> ContextPackage:
        package = await super().build_context(*args, **kwargs)
        vars(package).update(self.annotations)
        for chunk in package.retrieved_chunks:
            chunk.rerank_score = self.chunk_rerank_score
        return package


class BrokenEventStore(InMemoryJobStore):
    async def record_event(self, *args: Any, **kwargs: Any) -> ProcessingEvent:
        raise RuntimeError("database hiccup")


@dataclass
class Run:
    jobs: InMemoryJobStore
    job: Job
    events: list[ProcessingEvent]

    @property
    def built(self) -> list[ProcessingEvent]:
        return [e for e in self.events if e.event_type == "context_built"]


async def _run(
    *,
    thread_size: int = 1,
    retrieval_required: bool = True,
    summarizer: bool = True,
    summarizer_model: str | None = None,
    jobs: InMemoryJobStore | None = None,
    builder: type[ContextBuilder] = ContextBuilder,
) -> Run:
    jobs = jobs or InMemoryJobStore()
    message_store = InMemoryMessageStore()
    thread_states = InMemoryThreadStateStore()
    org_id, mailbox_id, thread_id = uuid4(), uuid4(), uuid4()
    start = datetime.now(UTC) - timedelta(hours=thread_size)
    last: NormalizedMessage | None = None
    for i in range(thread_size):
        last = NormalizedMessage(
            message_id=uuid4(),
            thread_id=thread_id,
            mailbox_id=mailbox_id,
            organization_id=org_id,
            provider="mock",
            provider_message_id=f"p-{i}-{uuid4().hex[:6]}",
            sender=EmailAddress(email="alice@example.com"),
            received_at=start + timedelta(hours=i),
            subject="Invoice question",
            body_text=f"Message {i}: payment terms for invoice INV-2026-001?",
            body_text_clean=f"Message {i}: payment terms for invoice INV-2026-001?",
        )
        await message_store.insert_message(last)
    assert last is not None
    job, _ = await jobs.create_job(
        Job(
            organization_id=org_id,
            message_id=last.message_id,
            thread_id=thread_id,
            state=JobState.QUEUED.value,
            idempotency_key=f"k-{uuid4()}",
        )
    )
    backend = FakeSearchBackend()
    backend.add_chunk(
        chunk_id=CHUNK["chunk_id"],
        document_id=CHUNK["document_id"],
        organization_id=org_id,
        content="Invoice payment terms and procedure for INV-2026-001.",
        category="billing",
    )
    provider = FakeLLMProvider()
    settings_kwargs: dict[str, Any] = {"min_messages_threshold": 4}
    if summarizer_model is not None:
        settings_kwargs["summarizer_model"] = summarizer_model
    summarization = SummarizationSettings(**settings_kwargs)
    consumer = AIWorkerConsumer(
        "email.billing.normal",
        job_store=jobs,
        message_store=message_store,
        context_builder=builder(
            thread_assembler=ThreadContextAssembler(
                settings=summarization,
                thread_state_store=thread_states,
                message_store=message_store,
            ),
            retriever=HybridRetriever(backend),
            job_store=jobs,
        ),
        router=ComplexityRouter(),
        drafting=DraftingService(
            generator=SinglePassGenerator(
                llm_provider=provider,
                profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
            ),
            job_store=jobs,
            persistence=InMemoryDraftPersistence(jobs, InMemoryDraftStore()),
            price_table={},
        ),
        summarizer=(
            ThreadSummarizer(llm=provider, store=thread_states, settings=summarization)
            if summarizer
            else None
        ),
    )
    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(org_id),
        message_id=last.provider_message_id,
        classification={
            "category": "billing",
            "intent": "invoice_inquiry",
            "retrieval_required": retrieval_required,
        },
    )

    await consumer.process_job(envelope, MagicMock())

    return Run(jobs, job, await jobs.list_events_for_job(org_id, job.id))


async def test_one_event_records_the_retrieved_chunks_and_a_skipped_summary() -> None:
    run = await _run(thread_size=1)

    (built,) = run.built
    assert built.state_from == built.state_to == JobState.CONTEXT_READY.value
    assert built.payload == {
        "retrieved": [{**CHUNK, "rank": 1, "rerank_score": None}],
        "retrieval_degraded": None,
        "retrieval_underfilled": None,
        "rerank_applied": None,
        "summary_triggered": False,
        "summary_model": None,
    }
    json.dumps(built.payload)  # what the Postgres store writes as jsonb


async def test_the_event_sits_between_context_ready_and_generating() -> None:
    run = await _run()

    assert [(e.event_type, e.state_to) for e in run.events] == [
        ("state_transition", "QUEUED"),
        ("state_transition", "CONTEXT_READY"),
        ("context_built", "CONTEXT_READY"),
        ("state_transition", "GENERATING"),
        ("state_transition", "DRAFTED"),
    ]


async def test_a_summary_names_the_model_that_wrote_it() -> None:
    run = await _run(thread_size=6, summarizer_model="qwen2.5:7b-instruct")

    (built,) = run.built
    assert built.payload["summary_triggered"] is True
    assert built.payload["summary_model"] == "qwen2.5:7b-instruct"


async def test_a_summary_on_the_fast_tier_names_the_tier_model() -> None:
    run = await _run(thread_size=6)

    (built,) = run.built
    assert built.payload["summary_triggered"] is True
    assert built.payload["summary_model"] == "fake-fast-model"


async def test_no_retrieval_means_no_retrieved_chunks() -> None:
    run = await _run(retrieval_required=False)

    (built,) = run.built
    assert built.payload["retrieved"] == []


async def test_without_a_summarizer_the_summary_fields_are_unknown() -> None:
    run = await _run(summarizer=False)

    (built,) = run.built
    assert built.payload["summary_triggered"] is None
    assert built.payload["summary_model"] is None


async def test_diagnostics_the_package_carries_are_recorded() -> None:
    class Diagnosed(DiagnosingBuilder):
        annotations = {
            "retrieval_degraded": False,
            "retrieval_underfilled": True,
            "rerank_applied": True,
        }
        chunk_rerank_score = 0.91

    run = await _run(builder=Diagnosed)

    (built,) = run.built
    assert built.payload["retrieved"] == [{**CHUNK, "rank": 1, "rerank_score": 0.91}]
    assert built.payload["retrieval_degraded"] is False
    assert built.payload["retrieval_underfilled"] is True
    assert built.payload["rerank_applied"] is True


async def test_a_failed_event_write_does_not_fail_the_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.ai_worker.consumer"):
        run = await _run(jobs=BrokenEventStore())

    stored = await run.jobs.get_job(run.job.organization_id, run.job.id)
    assert stored is not None and stored.state == JobState.DRAFTED.value
    assert run.built == []
    assert any("context_built" in record.getMessage() for record in caplog.records)


def test_payload_builder_reads_chunks_in_context_order() -> None:
    def chunk(name: str, score: float | None) -> Candidate:
        return Candidate(
            chunk_id=f"c-{name}", document_id=f"d-{name}", content="x", rerank_score=score
        )

    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="p",
        sender=EmailAddress(email="a@example.com"),
        received_at=datetime.now(UTC),
    )
    context = ContextPackage(
        agent_instructions="a",
        category_instructions="b",
        current_message=message,
        retrieved_chunks=[chunk("x", 0.9), chunk("y", None)],
    )

    payload = context_built_payload(context, None)

    assert payload["retrieved"] == [
        {"chunk_id": "c-x", "document_id": "d-x", "rank": 1, "rerank_score": 0.9},
        {"chunk_id": "c-y", "document_id": "d-y", "rank": 2, "rerank_score": None},
    ]

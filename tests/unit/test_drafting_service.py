"""DraftingService: CONTEXT_READY -> GENERATING -> DRAFTED with one generation (R18.1, R16.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm import (
    AgentProfileRegistry,
    FakeLLMProvider,
    SinglePassGenerator,
    UnvalidatedDraftError,
)
from services.ai_worker.drafting import DraftingService

PRICES = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
REPLY = {
    "action": "reply",
    "draft": "Hello Alice, open Settings and choose Reset Password.",
    "confidence": 0.95,
    "knowledge_chunks": ["DOC-125-08"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


class _CountingResponder:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.calls = 0
        self._responses = list(responses) or [REPLY]

    def __call__(self, messages: Any, schema: Any, tier: Any) -> dict[str, Any]:
        self.calls += 1
        return dict(self._responses[min(self.calls, len(self._responses)) - 1])


def _context(org_id: object) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=org_id,  # type: ignore[arg-type]
        provider="mock",
        provider_message_id="prov-1",
        sender=EmailAddress(email="alice@example.com", name="Alice"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=[
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Open Settings and choose Reset Password.",
                external_id="DOC-125-08",
            )
        ],
    )


async def _service(
    state: JobState, responder: _CountingResponder
) -> tuple[DraftingService, InMemoryJobStore, Job]:
    jobs = InMemoryJobStore()
    job, _ = await jobs.create_job(
        Job(organization_id=uuid4(), state=state.value, idempotency_key=f"k-{uuid4()}")
    )
    service = DraftingService(
        generator=SinglePassGenerator(
            llm_provider=FakeLLMProvider(responder=responder),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=jobs,
        persistence=InMemoryDraftPersistence(jobs, InMemoryDraftStore()),
        price_table=PRICES,
    )
    return service, jobs, job


async def test_context_ready_job_is_drafted_with_one_generation() -> None:
    responder = _CountingResponder()
    service, jobs, job = await _service(JobState.CONTEXT_READY, responder)

    outcome = await service.draft(job, _context(job.organization_id), category="technical_support")

    assert responder.calls == 1
    assert outcome.created is True
    assert outcome.job.state == JobState.DRAFTED.value
    assert outcome.draft.body == REPLY["draft"]
    assert outcome.draft.citation_mismatch is False
    assert outcome.draft.cost_estimate is not None
    transitions = [
        (e.state_from, e.state_to)
        for e in await jobs.list_events_for_job(job.organization_id, job.id)
    ]
    assert transitions[-2:] == [("CONTEXT_READY", "GENERATING"), ("GENERATING", "DRAFTED")]


async def test_generating_job_from_a_retry_is_drafted() -> None:
    """A redelivery after RETRY_PENDING -> GENERATING recovery continues where it left off."""
    responder = _CountingResponder()
    service, _, job = await _service(JobState.GENERATING, responder)

    outcome = await service.draft(job, _context(job.organization_id))

    assert responder.calls == 1
    assert outcome.job.state == JobState.DRAFTED.value


async def test_redelivered_drafted_job_skips_generation() -> None:
    """Review Focus 1: no second billed call and no second draft after redelivery."""
    responder = _CountingResponder()
    service, _, job = await _service(JobState.CONTEXT_READY, responder)
    context = _context(job.organization_id)
    first = await service.draft(job, context)

    second = await service.draft(job, context)

    assert responder.calls == 1
    assert second.created is False
    assert second.draft.id == first.draft.id


async def test_job_in_the_wrong_state_is_refused_before_any_call() -> None:
    responder = _CountingResponder()
    service, _, job = await _service(JobState.QUEUED, responder)

    with pytest.raises(IllegalStateTransitionError):
        await service.draft(job, _context(job.organization_id))

    assert responder.calls == 0


async def test_invalid_output_persists_nothing_and_leaves_job_generating() -> None:
    """R16.3: a payload that fails validation twice never becomes a draft."""
    bad = {"action": "reply"}
    responder = _CountingResponder(bad, bad)
    service, jobs, job = await _service(JobState.CONTEXT_READY, responder)

    with pytest.raises(UnvalidatedDraftError):
        await service.draft(job, _context(job.organization_id))

    stored = await jobs.get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.GENERATING.value
    persistence = service.persistence
    assert await persistence.find_draft_for_job(job.organization_id, job.id) is None

"""A persisted AI draft counts once toward emails_generated_total and its cost (R21.4, R21.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.protocol import CallProvenance, LLMResult
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from services.ai_worker.drafting import UNKNOWN_CATEGORY, DraftingService

REPLY = {
    "action": "reply",
    "draft": "Hello Alice, open Settings and choose Reset Password.",
    "confidence": 0.95,
    "knowledge_chunks": ["DOC-125-08"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
PRICED = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}


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
        agent_instructions="a",
        category_instructions="c",
        current_message=message,
        retrieved_chunks=[
            Candidate(chunk_id="c1", document_id="d1", content="Reset.", external_id="DOC-125-08")
        ],
    )


async def _service(
    metrics: object, prices: dict[str, ModelPricing], provider: FakeLLMProvider | None = None
) -> tuple[DraftingService, Job]:
    jobs = InMemoryJobStore()
    job, _ = await jobs.create_job(
        Job(
            organization_id=uuid4(),
            state=JobState.CONTEXT_READY.value,
            idempotency_key=f"k-{uuid4()}",
        )
    )
    service = DraftingService(
        generator=SinglePassGenerator(
            llm_provider=provider or FakeLLMProvider(default_response=REPLY),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=jobs,
        persistence=InMemoryDraftPersistence(jobs, InMemoryDraftStore()),
        price_table=prices,
        metrics=metrics,  # type: ignore[arg-type]
    )
    return service, job


def _emails(m: PipelineMetrics, org: object, category: str, tier: str) -> float | None:
    return m.registry.get_sample_value(
        "emails_generated_total",
        {"organization": str(org), "category": category, "model_tier": tier},
    )


async def test_created_draft_counts_the_email_and_its_cost() -> None:
    m = create_pipeline_metrics()
    service, job = await _service(m, PRICED)

    outcome = await service.draft(job, _context(job.organization_id), category="support")

    tier = outcome.draft.model_tier or ""
    assert _emails(m, job.organization_id, "support", tier) == 1
    assert m.registry.get_sample_value(
        "generated_draft_cost_total", {"category": "support", "model_tier": tier}
    ) == pytest.approx(outcome.draft.cost_estimate)


async def test_redelivery_does_not_count_the_email_twice() -> None:
    """Review Focus 5."""
    m = create_pipeline_metrics()
    service, job = await _service(m, PRICED)
    context = _context(job.organization_id)
    first = await service.draft(job, context, category="support")

    second = await service.draft(job, context, category="support")

    assert second.created is False
    assert _emails(m, job.organization_id, "support", first.draft.model_tier or "") == 1


async def test_unpriced_draft_counts_email_but_not_cost() -> None:
    """Review Focus 3."""
    m = create_pipeline_metrics()
    service, job = await _service(m, {})

    outcome = await service.draft(job, _context(job.organization_id), category="billing")

    tier = outcome.draft.model_tier or ""
    assert outcome.draft.cost_estimate is None
    assert _emails(m, job.organization_id, "billing", tier) == 1
    assert (
        m.registry.get_sample_value(
            "generated_draft_cost_total", {"category": "billing", "model_tier": tier}
        )
        is None
    )


async def test_missing_category_uses_the_unknown_label() -> None:
    m = create_pipeline_metrics()
    service, job = await _service(m, PRICED)

    outcome = await service.draft(job, _context(job.organization_id))

    assert _emails(m, job.organization_id, UNKNOWN_CATEGORY, outcome.draft.model_tier or "") == 1


async def test_broken_metrics_do_not_block_the_draft() -> None:
    """Review Focus 4."""
    service, job = await _service(object(), PRICED)

    outcome = await service.draft(job, _context(job.organization_id), category="support")

    assert outcome.created is True


async def test_created_draft_writes_one_draft_persisted_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DoD #5 (R21.3): the persistence step emits a structured log line; redelivery does not."""
    import logging

    caplog.set_level(logging.INFO, logger="services.ai_worker.drafting")
    service, job = await _service(create_pipeline_metrics(), PRICED)
    context = _context(job.organization_id)

    first = await service.draft(job, context, category="support")
    await service.draft(job, context, category="support")

    lines = [r for r in caplog.records if r.getMessage() == "draft_persisted"]
    assert len(lines) == 1
    fields = lines[0].__dict__["fields"]
    assert fields["draft_id"] == str(first.draft.id)
    assert fields["job_id"] == str(job.id)
    assert fields["category"] == "support"
    assert fields["model_tier"] == first.draft.model_tier
    assert fields["cost_estimate"] == first.draft.cost_estimate
    assert fields["citation_mismatch"] is first.draft.citation_mismatch
    assert "body" not in fields


class _RoutedFake(FakeLLMProvider):
    """A fake that reports, as an OpenRouter-routed client does, who served each call."""

    async def generate(self, **kwargs: Any) -> LLMResult:
        result = await super().generate(**kwargs)
        result.provenance = CallProvenance(
            requested_model="meta-llama/llama-3.1-8b-instruct",
            served_provider="CoreWeave",
            attempt=1,
            generation_id="gen-1",
            cost=0.000004,
        )
        return result


async def test_the_draft_persisted_line_names_the_provider_that_served_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OpenRouter route (work package R4): the ai-worker keeps no provenance column, so the
    structured log is where a C0 draft's served provider is recorded."""
    import logging

    caplog.set_level(logging.INFO, logger="services.ai_worker.drafting")
    service, job = await _service(
        create_pipeline_metrics(), PRICED, _RoutedFake(default_response=REPLY)
    )

    await service.draft(job, _context(job.organization_id), category="support")

    (line,) = [r for r in caplog.records if r.getMessage() == "draft_persisted"]
    (call,) = line.__dict__["fields"]["llm_provenance"]
    assert call["served_provider"] == "CoreWeave" and call["generation_id"] == "gen-1"


async def test_an_unrouted_draft_line_has_no_provenance_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="services.ai_worker.drafting")
    service, job = await _service(create_pipeline_metrics(), PRICED)

    await service.draft(job, _context(job.organization_id), category="support")

    (line,) = [r for r in caplog.records if r.getMessage() == "draft_persisted"]
    assert "llm_provenance" not in line.__dict__["fields"]

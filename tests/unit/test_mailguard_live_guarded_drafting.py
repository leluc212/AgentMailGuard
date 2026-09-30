"""GuardedDraftingService: the ai-worker's drafting step with AgentMailGuard around its one
generation call (task 7.20; ADR-0011; R22.12, R18.1, R16.4, R14.9, R19.3).

The real guard pipeline and rag-email's real generator run on fake providers, with the
in-memory job and draft stores. Skipped when mailguard is not importable (CI).
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry

pytest.importorskip("mailguard")

from mailguard.llm.protocol import LLMResponseError as GuardLLMResponseError  # noqa: E402

from evaluation.mailguard_bench.guard_build import GuardBuild, build_guard  # noqa: E402
from evaluation.mailguard_bench.guarded_reply import guard_marks_fallbacks  # noqa: E402
from evaluation.mailguard_bench.live.guarded_drafting import (  # noqa: E402
    AUDIT_SCHEMA,
    GUARD_MODEL_NAME,
    GuardedDraftingService,
)
from evaluation.mailguard_bench.resilience import RateLimitedError  # noqa: E402
from packages.broker.envelope import JobEnvelope  # noqa: E402
from packages.context.assembly import ThreadContextAssembler  # noqa: E402
from packages.context.builder import ContextBuilder  # noqa: E402
from packages.core.settings import ModelPricing, SummarizationSettings  # noqa: E402
from packages.db.draft import InMemoryDraftStore  # noqa: E402
from packages.db.draft_persistence import InMemoryDraftPersistence  # noqa: E402
from packages.db.job import InMemoryJobStore  # noqa: E402
from packages.db.message import InMemoryMessageStore  # noqa: E402
from packages.db.thread_state import InMemoryThreadStateStore  # noqa: E402
from packages.domain.entities import (  # noqa: E402
    Candidate,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import IllegalStateTransitionError, JobState  # noqa: E402
from packages.llm import (  # noqa: E402
    AgentProfileRegistry,
    FakeLLMProvider,
    SinglePassGenerator,
    UnvalidatedDraftError,
)
from packages.llm.budget import CallBudgetTracker, CallKind  # noqa: E402
from packages.llm.drafts import UnpersistableDraftError  # noqa: E402
from packages.llm.protocol import ModelTier  # noqa: E402
from packages.llm.router import ComplexityRouter  # noqa: E402
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics  # noqa: E402
from packages.retrieval.models import Candidate as RetrievedCandidate  # noqa: E402
from packages.retrieval.models import RetrievalQuery  # noqa: E402
from packages.retrieval.retriever import RetrievalResult  # noqa: E402
from services.ai_worker.consumer import AIWorkerConsumer  # noqa: E402
from services.ai_worker.drafting import DraftingOutcome, DraftingService  # noqa: E402
from tests.unit.test_mailguard_bench_guarded_reply import (  # noqa: E402
    BENIGN,
    INJECTION,
    REPLY,
    guard_fake,
)

PRICES = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
SENDER = "alice@example.com"
# The guard reads the model's own citation: an id that no retrieved chunk carries.
UNKNOWN_CITATION = {**REPLY, "knowledge_chunks": ["DOC-404"]}
# The customer's own address reaches the draft, which L4 counts as the injected goal's payload.
ECHOES_ADDRESS = {**REPLY, "draft": f"Hello Alice, we will email the steps to {SENDER} today."}
LEAKS_AN_ADDRESS = {**REPLY, "draft": "Please write to billing@vendor.example for invoices."}


def _context(
    org_id: UUID,
    *,
    body: str = BENIGN,
    rerank_score: float | None = None,
    thread_summary: str | None = None,
) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=org_id,
        provider="mock",
        provider_message_id="prov-1",
        sender=EmailAddress(email=SENDER, name="Alice"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text=body,
        body_text_clean=body,
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        thread_summary=thread_summary,
        retrieved_chunks=[
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Open Settings and choose Reset Password.",
                external_id="DOC-125-08",
                rerank_score=rerank_score,
            )
        ],
    )


@dataclass
class Rig:
    service: GuardedDraftingService
    jobs: InMemoryJobStore
    drafts: InMemoryDraftStore
    job: Job
    fake: FakeLLMProvider
    guard: GuardBuild
    audit_path: Path

    def context(self, **kwargs: Any) -> ContextPackage:
        return _context(UUID(str(self.job.organization_id)), **kwargs)

    def audit_lines(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        text = self.audit_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def guard_calls(self) -> int:
        return len(self.guard.guard_llm.inner.calls)

    async def transitions(self) -> list[tuple[str | None, str]]:
        events = await self.jobs.list_events_for_job(self.job.organization_id, self.job.id)
        return [(e.state_from, e.state_to) for e in events]


# What the triage gate records on a job's QUEUED transition (services/triage_worker/gate.py).
GATE_PAYLOAD: dict[str, Any] = {
    "category": "support",
    "intent": "password_reset",
    "priority": "normal",
    "retrieval_required": True,
    "workflow_hint": "ai",
    "confidence": 0.9,
    "decided_by": "llm",
}
# The states a job passes on its way from the gate's QUEUED to the one a test wants.
AFTER_QUEUED = {
    JobState.QUEUED: [],
    JobState.CONTEXT_READY: [JobState.CONTEXT_READY],
    JobState.GENERATING: [JobState.CONTEXT_READY, JobState.GENERATING],
}


async def _rig(
    tmp_path: Path,
    *,
    preset: str = "C3",
    reply: dict[str, Any] = REPLY,
    state: JobState = JobState.CONTEXT_READY,
    metrics: PipelineMetrics | None = None,
    message: NormalizedMessage | None = None,
    queued_payload: dict[str, Any] | None = None,
) -> Rig:
    full = preset == "C3"  # the live v2 C3 runs every guard LLM stage
    guard = build_guard(
        preset,
        model_name="fake",
        audit_log_path=tmp_path / "raw" / "guard_l5.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
        l3b_llm=full,
        l4_llm=full,
    )
    guard.guard_llm.inner = guard_fake()  # the stages hold the CountingProvider, not inner
    fake = FakeLLMProvider(default_response=reply)
    jobs, drafts = InMemoryJobStore(), InMemoryDraftStore()
    routed = queued_payload is not None  # the job went through the gate: its history has a QUEUED
    job, _ = await jobs.create_job(
        Job(
            organization_id=message.organization_id if message else uuid4(),
            message_id=message.message_id if message else None,
            thread_id=message.thread_id if message else None,
            state=(JobState.QUEUED if routed else state).value,
            idempotency_key=f"k-{uuid4()}",
        ),
        initial_event_payload=queued_payload,
    )
    for target in AFTER_QUEUED[state] if routed else []:
        job, _ = await jobs.transition_job_state(job.organization_id, job.id, target)
    audit_path = tmp_path / "raw" / f"audit__{preset}.jsonl"
    service = GuardedDraftingService(
        generator=SinglePassGenerator(
            llm_provider=fake,
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=jobs,
        persistence=InMemoryDraftPersistence(jobs, drafts),
        price_table=PRICES,
        metrics=metrics,
        guard=guard,
        audit_path=audit_path,
    )
    return Rig(
        service=service,
        jobs=jobs,
        drafts=drafts,
        job=job,
        fake=fake,
        guard=guard,
        audit_path=audit_path,
    )


def _decision(line: dict[str, Any]) -> dict[str, Any]:
    decision: dict[str, Any] = line["report"]["decision"]
    return decision


def _reason(line: dict[str, Any]) -> str:
    """The escalation_reason a guard decision must leave on the draft (the L5 decision's words)."""
    decision = _decision(line)
    return f"agentmailguard:{decision['action']}:{decision['matched_rule_id']}"


def _spy_on_pipeline_run(rig: Rig) -> dict[str, Any]:
    """The keyword arguments the guard's pipeline is called with (filled when the job runs)."""
    seen: dict[str, Any] = {}
    run = rig.guard.pipeline.run

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return await run(*args, **kwargs)

    rig.guard.pipeline.run = spy  # the pipeline is typed Any: no ignore needed
    return seen


async def _stored_draft(rig: Rig) -> Any:
    return await rig.service.persistence.find_draft_for_job(rig.job.organization_id, rig.job.id)


# ------------------------------------------------------------------ the interface


def test_the_public_interface_is_the_drafting_services() -> None:
    assert issubclass(GuardedDraftingService, DraftingService)
    assert inspect.signature(GuardedDraftingService.draft) == inspect.signature(
        DraftingService.draft
    )
    base = inspect.signature(DraftingService.__init__).parameters
    guarded = inspect.signature(GuardedDraftingService.__init__).parameters
    for name, parameter in base.items():  # build_consumers passes exactly these keywords
        assert guarded[name].kind == parameter.kind
        assert guarded[name].default == parameter.default
    for name in set(guarded) - set(base):  # what the guard needs comes in by keyword only
        assert guarded[name].kind is inspect.Parameter.KEYWORD_ONLY


async def test_the_state_transitions_are_the_drafting_services(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    plain_jobs = InMemoryJobStore()
    plain_job, _ = await plain_jobs.create_job(
        Job(
            organization_id=uuid4(),
            state=JobState.CONTEXT_READY.value,
            idempotency_key=f"k-{uuid4()}",
        )
    )
    plain = DraftingService(
        generator=SinglePassGenerator(
            llm_provider=FakeLLMProvider(default_response=REPLY),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=plain_jobs,
        persistence=InMemoryDraftPersistence(plain_jobs, InMemoryDraftStore()),
        price_table=PRICES,
    )
    kwargs: dict[str, Any] = {
        "category": "support",
        "escalated_tier": ModelTier.STRONG,
        "escalation_reason": "low_confidence",
    }

    await rig.service.draft(rig.job, rig.context(), **kwargs)
    await plain.draft(plain_job, _context(UUID(str(plain_job.organization_id))), **kwargs)

    async def generating_payload(jobs: InMemoryJobStore, job: Job) -> Any:
        events = await jobs.list_events_for_job(job.organization_id, job.id)
        return next(e.payload for e in events if e.state_to == JobState.GENERATING.value)

    assert await generating_payload(rig.jobs, rig.job) == await generating_payload(
        plain_jobs, plain_job
    )
    assert (await rig.transitions())[-2:] == [
        (JobState.CONTEXT_READY.value, JobState.GENERATING.value),
        (JobState.GENERATING.value, JobState.DRAFTED.value),
    ]


# ------------------------------------------------------------------ a clean job


async def test_a_benign_job_is_drafted_with_the_guards_template_and_one_generation_call(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    context = rig.context()

    outcome = await rig.service.draft(rig.job, context, category="support")

    assert isinstance(outcome, DraftingOutcome)
    assert len(rig.fake.recorded_calls) == 1
    sent = rig.fake.recorded_calls[0]["messages"]
    assert [m.role for m in sent] == ["system", "user"]  # the guard's L3 prompt, not the profile's
    assert "[TASK]" in sent[1].content
    assert outcome.created is True
    assert outcome.job.state == JobState.DRAFTED.value
    draft = outcome.draft
    assert (draft.action, draft.body) == ("reply", REPLY["draft"])
    assert draft.subject == "Re: Password reset"
    assert draft.escalation_reason == "none"
    assert draft.model_name == "fake-fast-model"
    assert draft.input_tokens > 0 and draft.output_tokens > 0
    assert draft.citation_mismatch is False
    assert draft.job_id == rig.job.id and draft.organization_id == rig.job.organization_id
    assert await rig.transitions() == [
        (None, JobState.CONTEXT_READY.value),
        (JobState.CONTEXT_READY.value, JobState.GENERATING.value),
        (JobState.GENERATING.value, JobState.DRAFTED.value),
    ]


async def test_the_audit_line_carries_the_v1_fields_of_a_guarded_row(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    context = rig.context(rerank_score=0.42)
    message = context.current_message

    outcome = await rig.service.draft(rig.job, context, category="support")

    (line,) = rig.audit_lines()
    assert line["schema"] == AUDIT_SCHEMA
    assert line["message_id"] == str(message.message_id)
    assert line["organization_id"] == str(message.organization_id)
    assert line["job_id"] == str(rig.job.id)
    assert line["draft_id"] == str(outcome.draft.id)
    assert line["config"] == "C3"
    assert (line["final_body"], line["final_action"]) == (REPLY["draft"], "reply")
    assert (line["blocked_inbound"], line["blocked_outbound"]) == (False, False)
    assert line["generation"]["called"] is True
    assert line["generation"]["calls"] == 1
    assert line["generation"]["reply_v1"] == REPLY
    assert line["guard_llm"]["calls"] >= 1
    assert set(line["timings_ms"]) == {"guarded_total", "generation", "guard"}
    assert line["report"]["inbound_decision"]["action"] == "draft_only"
    assert _decision(line)["action"] == "draft_only"
    assert line["draft"]["body_after_guard"] == REPLY["draft"]
    assert line["system_instructions"] == context.agent_instructions
    assert line["retrieved"] == [
        {"chunk_id": "chunk-1", "document_id": "doc-1", "rank": 1, "rerank_score": 0.42}
    ]
    assert line["guard_errors"] == []
    assert isinstance(line["ts"], str)


async def test_c0t_runs_the_template_with_no_layer_and_no_guard_call(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, preset="C0T")

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert rig.guard_calls() == 0
    assert outcome.draft.body == REPLY["draft"]
    assert outcome.draft.escalation_reason == "none"
    (line,) = rig.audit_lines()
    assert line["config"] == "C0T" and line["prompt_mode"] == "none"
    assert line["report"]["decision"] is None


async def test_the_l4_redacted_body_is_what_gets_persisted(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, reply=LEAKS_AN_ADDRESS)

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert outcome.draft.body == "Please write to [REDACTED:pii.email] for invoices."
    assert outcome.draft.action == "reply"
    (line,) = rig.audit_lines()
    assert line["final_body"] == outcome.draft.body
    assert line["draft"]["redacted_by_l4"] is True
    assert line["generation"]["reply_v1"]["draft"] == LEAKS_AN_ADDRESS["draft"]  # kept as said


# ------------------------------------------------------------------ what the guard stops


async def test_a_blocked_inbound_email_becomes_an_escalate_draft_and_no_model_call(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)

    outcome = await rig.service.draft(rig.job, rig.context(body=INJECTION), category="support")

    assert rig.fake.recorded_calls == []  # no generation, so no tokens spent on an attack
    draft = outcome.draft
    assert draft.action == "escalate" and draft.body == ""
    assert draft.model_name == GUARD_MODEL_NAME == "agentmailguard"
    assert (draft.input_tokens, draft.output_tokens) == (0, 0)
    assert draft.citations == [] and draft.citation_mismatch is False
    assert draft.cost_estimate == 0.0
    assert draft.subject == "Re: Password reset"
    (line,) = rig.audit_lines()
    assert line["blocked_inbound"] is True and line["blocked_outbound"] is False
    assert _decision(line)["action"] in ("block", "quarantine")
    assert draft.escalation_reason == _reason(line)
    assert re.fullmatch(r"agentmailguard:(block|quarantine):[\w-]+", draft.escalation_reason)
    assert (line["final_body"], line["final_action"]) == ("", "escalate")
    assert line["generation"]["called"] is False
    assert outcome.job.state == JobState.DRAFTED.value  # the job still ends DRAFTED


async def test_a_blocked_reply_becomes_the_same_escalate_draft(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, reply=ECHOES_ADDRESS)

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert len(rig.fake.recorded_calls) == 1  # the model spoke; the guard stopped the reply
    draft = outcome.draft
    assert draft.action == "escalate" and draft.body == ""
    assert draft.model_name == "agentmailguard"
    assert (draft.input_tokens, draft.output_tokens) == (0, 0)
    (line,) = rig.audit_lines()
    assert line["blocked_inbound"] is False and line["blocked_outbound"] is True
    assert _decision(line)["action"] in ("block", "quarantine")
    assert draft.escalation_reason == _reason(line)
    assert line["generation"]["called"] is True  # what the model said stays in the audit line
    assert line["generation"]["reply_v1"] == ECHOES_ADDRESS
    assert (line["final_body"], line["final_action"]) == ("", "escalate")


async def test_human_approval_keeps_the_generated_draft_and_names_the_rule(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, reply=UNKNOWN_CITATION)

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    (line,) = rig.audit_lines()
    assert _decision(line)["action"] == "human_approval"
    draft = outcome.draft
    assert draft.escalation_reason == _reason(line)
    assert (draft.action, draft.body) == ("reply", UNKNOWN_CITATION["draft"])  # kept
    assert draft.model_name == "fake-fast-model"
    assert draft.citation_mismatch is True
    assert line["blocked_inbound"] is False and line["blocked_outbound"] is False
    assert (line["final_body"], line["final_action"]) == (draft.body, "reply")


# ------------------------------------------------------------------ the ai-worker's own inputs


async def test_the_routers_tier_and_reason_reach_the_generation_call(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)

    outcome = await rig.service.draft(
        rig.job,
        rig.context(),
        category="support",
        escalated_tier=ModelTier.STRONG,
        escalation_reason="low_confidence",
    )

    assert rig.fake.recorded_calls[0]["tier"] == ModelTier.STRONG
    assert outcome.draft.model_name == "fake-strong-model"
    assert outcome.draft.model_tier == "strong"
    assert outcome.draft.escalation_reason == "low_confidence"  # not a guard decision: unchanged


async def test_the_jobs_budget_tracker_counts_the_one_generation(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    tracker = CallBudgetTracker()

    await rig.service.draft(rig.job, rig.context(), category="support", budget_tracker=tracker)

    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1  # the guard's own stage calls are not the job's budget


async def test_l3b_gets_the_retrieval_query_the_context_builder_builds(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    seen = _spy_on_pipeline_run(rig)

    await rig.service.draft(
        rig.job,
        rig.context(thread_summary="Customer asked about a reset"),
        category="support",
    )

    assert seen["query"] == (
        f"Thread summary: Customer asked about a reset. Subject: Password reset. Body: {BENIGN}"
    )
    assert seen["category"] == "support"
    assert seen["system_instructions"] == "You are an enterprise AI assistant."


async def test_l3b_gets_the_intent_triage_recorded_in_its_query(tmp_path: Path) -> None:
    # R12.1: the query is built from the email, the thread summary AND the classification
    # intent. draft() is not told the intent, so it reads what the gate recorded on the job.
    rig = await _rig(tmp_path, queued_payload=GATE_PAYLOAD)
    seen = _spy_on_pipeline_run(rig)

    await rig.service.draft(
        rig.job, rig.context(thread_summary="Customer asked about a reset"), category="support"
    )

    assert seen["query"] == (
        "Intent: password_reset. Thread summary: Customer asked about a reset. "
        f"Subject: Password reset. Body: {BENIGN}"
    )


async def test_a_job_triage_gave_no_intent_gets_a_query_without_one(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, queued_payload={**GATE_PAYLOAD, "intent": None})
    seen = _spy_on_pipeline_run(rig)

    await rig.service.draft(rig.job, rig.context(), category="support")

    assert seen["query"] == f"Subject: Password reset. Body: {BENIGN}"


async def test_the_audit_line_records_the_query_l3b_was_given(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, queued_payload=GATE_PAYLOAD)
    seen = _spy_on_pipeline_run(rig)

    await rig.service.draft(rig.job, rig.context(), category="support")

    (line,) = rig.audit_lines()
    assert line["retrieval_query"] == seen["query"]
    assert line["retrieval_query"].startswith("Intent: password_reset. ")


async def test_a_job_the_gate_never_queued_is_drafted_and_the_gap_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await _rig(tmp_path)  # created in CONTEXT_READY: no QUEUED transition in its history
    seen = _spy_on_pipeline_run(rig)

    with caplog.at_level(logging.WARNING):
        outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert outcome.job.state == JobState.DRAFTED.value
    assert seen["query"] == f"Subject: Password reset. Body: {BENIGN}"  # nothing to recover
    assert any("no triage intent" in record.getMessage() for record in caplog.records)


async def test_a_job_without_a_category_uses_the_default_profile(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)

    outcome = await rig.service.draft(rig.job, rig.context())

    assert outcome.draft.body == REPLY["draft"]
    payload = next(
        e.payload
        for e in await rig.jobs.list_events_for_job(rig.job.organization_id, rig.job.id)
        if e.state_to == JobState.GENERATING.value
    )
    assert payload["category"] is None


# ------------------------------------------------------------------ DraftingService's guarantees


async def test_a_redelivered_drafted_job_skips_the_guard_and_the_model(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    context = rig.context()
    first = await rig.service.draft(rig.job, context, category="support")
    guard_calls = rig.guard_calls()

    second = await rig.service.draft(rig.job, context, category="support")

    assert second.created is False and second.draft.id == first.draft.id
    assert len(rig.fake.recorded_calls) == 1
    assert rig.guard_calls() == guard_calls
    assert len(rig.audit_lines()) == 1


async def test_a_job_in_the_wrong_state_is_refused_before_any_call(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, state=JobState.QUEUED)

    with pytest.raises(IllegalStateTransitionError):
        await rig.service.draft(rig.job, rig.context(), category="support")

    assert rig.fake.recorded_calls == [] and rig.guard_calls() == 0
    assert rig.audit_lines() == []


async def test_a_job_that_was_recovered_into_generating_is_drafted(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, state=JobState.GENERATING)

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert outcome.job.state == JobState.DRAFTED.value
    assert await rig.transitions() == [
        (None, JobState.GENERATING.value),
        (JobState.GENERATING.value, JobState.DRAFTED.value),
    ]


async def test_an_unknown_job_is_a_key_error(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    ghost = replace(rig.job, id=uuid4())

    with pytest.raises(KeyError):
        await rig.service.draft(ghost, rig.context(), category="support")

    assert rig.fake.recorded_calls == []


async def test_invalid_model_output_persists_nothing_and_leaves_the_job_generating(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, reply={"action": "reply"})  # fails reply.v1 validation twice

    with pytest.raises(UnvalidatedDraftError):
        await rig.service.draft(rig.job, rig.context(), category="support")

    stored = await rig.jobs.get_job(rig.job.organization_id, rig.job.id)
    assert stored is not None and stored.state == JobState.GENERATING.value
    assert await _stored_draft(rig) is None
    assert rig.audit_lines() == []


async def test_a_rate_limited_guard_stage_fails_the_job_for_the_retry_ladder(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    rig.guard.guard_llm.inner.set_error(GuardLLMResponseError("OpenAI HTTP 429: quota"))

    with pytest.raises(RateLimitedError):
        await rig.service.draft(rig.job, rig.context(), category="support")

    stored = await rig.jobs.get_job(rig.job.organization_id, rig.job.id)
    assert stored is not None and stored.state == JobState.GENERATING.value
    assert await _stored_draft(rig) is None
    assert rig.audit_lines() == []


@pytest.mark.skipif(
    guard_marks_fallbacks(), reason="the installed guard marks failed AI steps (llm_fallback)"
)
async def test_a_guard_layer_error_is_flagged_in_the_audit_line_never_hidden(
    tmp_path: Path,
) -> None:
    # v1 turns this into an error row (a weaker guard is never a defence); the worker cannot
    # fail the job (the retry ladder waits 30 s to 30 min), so it records the errors for the
    # feeder to turn into that same error row.
    rig = await _rig(tmp_path)
    rig.guard.guard_llm.inner.set_error(GuardLLMResponseError("OpenAI HTTP 500: upstream"))

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert outcome.job.state == JobState.DRAFTED.value
    (line,) = rig.audit_lines()
    assert any(error.startswith("guard_llm:") for error in line["guard_errors"])
    assert line["guard_llm"]["calls"] >= 1


@pytest.mark.skipif(
    not guard_marks_fallbacks(), reason="the installed guard does not mark failed AI steps"
)
async def test_failed_guard_ai_steps_are_audited_per_layer_and_the_job_is_drafted(
    tmp_path: Path,
) -> None:
    # Amendment 1, C.1 generalised (ADR-0012 decision 4): every AI stage that fell back is in the
    # audit line with its reason; none of them is a guard error, so the case is scored normally.
    rig = await _rig(tmp_path)
    rig.guard.guard_llm.inner.set_error(GuardLLMResponseError("OpenAI HTTP 500: upstream"))

    outcome = await rig.service.draft(rig.job, rig.context(), category="support")

    assert outcome.job.state == JobState.DRAFTED.value
    (line,) = rig.audit_lines()
    assert line["guard_errors"] == []
    # L1's judge, L3b's and L4's run only when their cheap stages escalate; L2's always runs.
    assert [f["layer"] for f in line["guard_fallbacks"]] == ["l2_intent_extractor"]
    assert {f["reason"] for f in line["guard_fallbacks"]} == {"error"}
    assert all("HTTP 500" in f["error"] for f in line["guard_fallbacks"])
    assert line["l2_llm_schema_fallback"] is False  # a transport error, not an answer


@pytest.mark.skipif(
    not guard_marks_fallbacks(), reason="the installed guard does not mark failed AI steps"
)
async def test_the_draft_persisted_log_counts_the_guard_fallbacks_next_to_the_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # CLAUDE.md 3.5: a processing step's structured log carries what it recorded. A fallback is
    # scored, not an error, so it needs its own count beside guard_errors.
    rig = await _rig(tmp_path)
    rig.guard.guard_llm.inner.set_error(GuardLLMResponseError("OpenAI HTTP 500: upstream"))

    with caplog.at_level(logging.INFO):
        await rig.service.draft(rig.job, rig.context(), category="support")

    (record,) = [r for r in caplog.records if r.getMessage() == "draft_persisted"]
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["guard_fallbacks"] == 1
    assert fields["guard_errors"] == 0


@pytest.mark.skipif(
    not guard_marks_fallbacks(), reason="the installed guard does not mark failed AI steps"
)
async def test_an_l2_answer_without_the_schema_sets_l2_llm_schema_fallback_in_the_audit(
    tmp_path: Path,
) -> None:
    from mailguard.llm.fake import FakeLLMProvider as GuardFakeLLM

    rig = await _rig(tmp_path)
    rig.guard.guard_llm.inner = GuardFakeLLM(
        default_response={"raw_text": "Sure, happy to help."}, model_name="fake:fake"
    )

    await rig.service.draft(rig.job, rig.context(), category="support")

    (line,) = rig.audit_lines()
    by_layer = {f["layer"]: f["reason"] for f in line["guard_fallbacks"]}
    assert by_layer["l2_intent_extractor"] == "non_json"
    assert line["l2_llm_schema_fallback"] is True
    assert line["guard_errors"] == []


@pytest.mark.skipif(
    not guard_marks_fallbacks(), reason="the installed guard does not mark failed AI steps"
)
async def test_a_healthy_guarded_job_audits_an_empty_fallback_list(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)

    await rig.service.draft(rig.job, rig.context(), category="support")

    (line,) = rig.audit_lines()
    assert line["guard_fallbacks"] == [] and line["l2_llm_schema_fallback"] is False


async def test_a_persisted_generated_draft_counts_once_and_a_guard_escalation_not_at_all(
    tmp_path: Path,
) -> None:
    metrics = create_pipeline_metrics(registry=CollectorRegistry())

    def emails(org: object) -> float:
        """emails_generated_total of one organization, whatever its category and tier labels."""
        return sum(
            sample.value
            for family in metrics.registry.collect()
            for sample in family.samples
            if sample.name == "emails_generated_total"
            and sample.labels.get("organization") == str(org)
        )

    drafted = await _rig(tmp_path / "drafted", metrics=metrics)
    await drafted.service.draft(drafted.job, drafted.context(), category="support")
    blocked = await _rig(tmp_path / "blocked", metrics=metrics)
    await blocked.service.draft(blocked.job, blocked.context(body=INJECTION), category="support")

    assert emails(drafted.job.organization_id) == 1.0
    assert emails(blocked.job.organization_id) == 0.0  # nothing was generated


async def test_the_audit_line_is_written_before_the_draft_is_committed(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    persistence = rig.service.persistence
    seen: list[int] = []
    persist = persistence.persist_drafted

    async def persist_after_checking(draft: Any) -> Any:
        seen.append(len(rig.audit_lines()))  # what a crash right here would leave behind
        return await persist(draft)

    persistence.persist_drafted = persist_after_checking  # type: ignore[method-assign]

    await rig.service.draft(rig.job, rig.context(), category="support")

    assert seen == [1]


async def test_an_escalate_draft_needs_a_thread_like_any_draft(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, state=JobState.GENERATING)  # skip the CONTEXT_READY transition
    context = rig.context(body=INJECTION)
    context.current_message = replace(context.current_message, thread_id="")

    with pytest.raises(UnpersistableDraftError, match="no thread"):
        await rig.service.draft(rig.job, context, category="support")

    assert rig.audit_lines() == []  # nothing was recorded for a draft that cannot exist


# ------------------------------------------------------------------ inside the real consumer


@pytest.mark.parametrize(
    ("body", "action", "generations"),
    [(BENIGN, "reply", 1), (INJECTION, "escalate", 0)],
)
async def test_the_ai_worker_consumer_runs_a_queued_job_through_the_guard(
    tmp_path: Path, body: str, action: str, generations: int
) -> None:
    """The consumer's own path: envelope -> message -> ContextBuilder -> router -> guarded draft."""
    message = _context(uuid4(), body=body).current_message
    rig = await _rig(tmp_path, state=JobState.QUEUED, message=message)
    messages = InMemoryMessageStore()
    await messages.insert_message(message)
    consumer = AIWorkerConsumer(
        "email.support.normal",
        job_store=rig.jobs,
        message_store=messages,
        context_builder=ContextBuilder(
            thread_assembler=ThreadContextAssembler(
                settings=SummarizationSettings(),
                thread_state_store=InMemoryThreadStateStore(),
                message_store=messages,
            ),
            job_store=rig.jobs,
        ),
        router=ComplexityRouter(),
        drafting=rig.service,
    )
    envelope = JobEnvelope(
        job_id=str(rig.job.id),
        idempotency_key=f"gen-{rig.job.id}",
        job_type="generate_reply",
        organization_id=str(message.organization_id),
        message_id=message.provider_message_id,
        classification={"category": "support", "retrieval_required": False, "confidence": 0.9},
    )

    await consumer.process_job(envelope, MagicMock())

    stored = await rig.jobs.get_job(rig.job.organization_id, rig.job.id)
    assert stored is not None and stored.state == JobState.DRAFTED.value
    (persisted,) = await rig.drafts.list_drafts_for_job(rig.job.id, rig.job.organization_id)
    assert persisted.action == action
    assert len(rig.fake.recorded_calls) == generations
    (line,) = rig.audit_lines()
    assert line["config"] == "C3" and line["job_id"] == str(rig.job.id)
    assert (line["final_action"], line["message_id"]) == (action, str(message.message_id))


async def test_l3b_gets_exactly_the_query_retrieval_ran_with(tmp_path: Path) -> None:
    """Through the consumer, with the intent set and retrieval required: one query, two readers.

    The ContextBuilder builds the retrieval query from the full triage classification; the
    guard's L3b compares the retrieved chunks with a query of its own. They must be the same
    text, or the echo check judges chunks against a question nobody asked.
    """
    message = _context(uuid4()).current_message
    rig = await _rig(tmp_path, state=JobState.QUEUED, message=message, queued_payload=GATE_PAYLOAD)
    messages = InMemoryMessageStore()
    await messages.insert_message(message)
    asked: list[RetrievalQuery] = []

    class RecordingRetriever:
        async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
            asked.append(query)
            return RetrievalResult(
                candidates=[
                    RetrievedCandidate(
                        chunk_id="chunk-1",
                        document_id="doc-1",
                        content="Open Settings and choose Reset Password.",
                        metadata={"external_id": "DOC-125-08"},
                    )
                ]
            )

    consumer = AIWorkerConsumer(
        "email.support.normal",
        job_store=rig.jobs,
        message_store=messages,
        context_builder=ContextBuilder(
            thread_assembler=ThreadContextAssembler(
                settings=SummarizationSettings(),
                thread_state_store=InMemoryThreadStateStore(),
                message_store=messages,
            ),
            retriever=RecordingRetriever(),  # type: ignore[arg-type]
            job_store=rig.jobs,
        ),
        router=ComplexityRouter(),
        drafting=rig.service,
    )
    seen = _spy_on_pipeline_run(rig)
    envelope = JobEnvelope(
        job_id=str(rig.job.id),
        idempotency_key=f"gen-{rig.job.id}",
        job_type="generate_reply",
        organization_id=str(message.organization_id),
        message_id=message.provider_message_id,
        classification=dict(GATE_PAYLOAD),
    )

    await consumer.process_job(envelope, MagicMock())

    (query,) = asked  # retrieval ran once, and with the intent
    assert query.semantic_text.startswith("Intent: password_reset. Subject: Password reset.")
    assert seen["query"] == query.semantic_text
    (line,) = rig.audit_lines()
    assert line["retrieval_query"] == query.semantic_text
    assert [chunk["chunk_id"] for chunk in line["retrieved"]] == ["chunk-1"]

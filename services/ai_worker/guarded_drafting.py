"""AgentMailGuard around the ai-worker's one generation call, for C3 jobs (task 9.7).

ADR-0018 decision 6; R25.5, R25.6; ADR-0010 (only the guard's public API).

    LiveGuardedDraftingService.draft(job, context, category=...)
       guard_mode C0 (or GUARD__ENABLED=false) ─▶ DraftingService.draft, unchanged
       guard_mode C3:
         CONTEXT_READY ─▶ GENERATING
         MailGuardPipeline(GuardConfig.preset("C3")).run(email, chunks, draft_factory, ...)
            inbound_guard → inbound_gate (pass 1) → doc_scanner → inbound_gate (pass 2)
               block / quarantine ⇒ no model call
            channel isolation → prompt ── ONE generation call ── output_guard
         ├─ blocked        ⇒ escalated draft, body "", model "agentmailguard", 0 tokens
         ├─ human_approval ⇒ the generated draft, reason agentmailguard:human_approval:<rule>
         └─ otherwise      ⇒ the generated draft, its body as the output guard left it
         persist_drafted(draft, guard_report=...)  ─▶ GENERATING ─▶ DRAFTED, one transaction

The outcome mapping is the benchmark's (``evaluation/mailguard_bench/live/guarded_drafting.py``,
``guard_outcome``); a parity test keeps the two equal. The benchmark code and the guard pin are not
changed. rag-email adds no rule, classifier, sanitiser or policy: every decision is the guard's.

Judges (owner decisions C13, C14): a ``fake`` job builds no judge (the guard's cheap stages
only); a ``live`` job passes rag-email's live ``LLMProvider`` to the pipeline's judge arguments
through ``JudgeAdapter`` (the two LLM protocols are the same). Judge calls are counted in the guard
report and ``guard_llm_calls_total``, outside the job's ``CallBudgetTracker`` (ADR-0018 decision
6, C7). The guard's decision is display only: it changes neither ``dispatch_mode`` nor review.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packages.core.run_context import current_guard_mode, current_model_profile
from packages.core.settings import GuardSettings, ModelPricing
from packages.db.draft_persistence import DraftPersistence
from packages.db.guard_report import GuardReportRecord
from packages.db.job import JobStore
from packages.domain.entities import ContextPackage, GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.budget import CallBudgetTracker
from packages.llm.drafts import (
    NO_ESCALATION,
    UnpersistableDraftError,
    build_generated_draft,
    reply_subject,
)
from packages.llm.generator import GenerationResult, SinglePassGenerator
from packages.llm.protocol import ChatMessage, LLMProvider, ModelTier
from packages.llm.reply_format import render_reply_format_rules
from services.ai_worker.drafting import DraftingOutcome, DraftingService

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)

GUARD_MODEL_NAME = "agentmailguard"
"""``model_name`` of a draft the guard stopped; ``escalation_reason`` starts with it too."""
ESCALATE_ACTION = "escalate"
HUMAN_APPROVAL = "human_approval"
GUARDED_MODE = "C3"


def guard_outcome(record: Mapping[str, Any]) -> tuple[str, str | None]:
    """The guard's outcome and the draft's ``escalation_reason`` (the benchmark's mapping).

    ``record`` holds ``blocked_inbound``, ``blocked_outbound``, ``inbound_action``,
    ``final_action`` and ``rule``. Returns ``("escalated", "agentmailguard:<action>:<rule>")``
    when the email or the reply was blocked, ``("human_approval", ...)`` when a reviewer must
    sign the draft off, else ``("drafted", None)``.
    """
    rule = record.get("rule") or "unknown"
    if record["blocked_inbound"] or record["blocked_outbound"]:
        action = record["inbound_action"] if record["blocked_inbound"] else record["final_action"]
        return "escalated", f"{GUARD_MODEL_NAME}:{action}:{rule}"
    if record.get("final_action") == HUMAN_APPROVAL:
        return HUMAN_APPROVAL, f"{GUARD_MODEL_NAME}:{HUMAN_APPROVAL}:{rule}"
    return "drafted", None


def guarded_system_instructions(agent_instructions: str | None) -> str:
    """The trusted system text of the guarded prompt: the profile's, then the reply format."""
    rules = "Reply format rules:\n" + render_reply_format_rules(start=1)
    base = (agent_instructions or "").strip()
    return f"{base}\n\n{rules}" if base else rules


def _value(obj: Any) -> str | None:
    if obj is None:
        return None
    return str(getattr(obj, "value", obj))


class JudgeAdapter:
    """rag-email's ``LLMProvider`` behind AgentMailGuard's protocol, counting its calls."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def generate(
        self,
        *,
        messages: list[Any],
        schema: dict[str, Any] | None = None,
        tier: Any = None,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> Any:
        from mailguard.llm.protocol import LLMResult as GuardResult
        from mailguard.llm.protocol import ModelTier as GuardTier

        self.calls += 1
        result = await self._provider.generate(
            messages=[ChatMessage(role=str(m.role), content=str(m.content)) for m in messages],
            schema=schema,
            tier=ModelTier(str(_value(tier) or ModelTier.FAST)),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        return GuardResult(
            content=result.content,
            model=result.model,
            tier=GuardTier(str(result.tier)),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            raw_finish_reason=result.raw_finish_reason,
            raw_response=result.raw_response,
        )


def build_pipeline(settings: GuardSettings, judge: JudgeAdapter | None) -> Any:
    """The guard's C3 preset; judges only when ``judge`` is given (a live job).

    Raises:
        ImportError: If AgentMailGuard is not installed (the image installs it, ADR-0018).
    """
    from mailguard.config.settings import MailGuardSettings
    from mailguard.pipeline import GuardConfig, MailGuardPipeline

    guard_settings = MailGuardSettings(_env_file=None)
    guard_settings.inbound_guard.ml_model_path = settings.l1_model_path
    guard_settings.policy.audit_log_path = settings.audit_log_path
    if judge is None:
        return MailGuardPipeline(guard_settings, GuardConfig.preset(GUARDED_MODE))
    # A live C3 job runs every judge of each layer it runs, as the benchmark's C3 does.
    guard_settings.doc_scanner.llm_enabled = True
    guard_settings.doc_scanner.set_judge_llm_enabled = True
    guard_settings.output_guard.llm_enabled = True
    return MailGuardPipeline(
        guard_settings,
        GuardConfig.preset(GUARDED_MODE),
        inbound_judge=judge,
        inbound_extractor=judge,
        doc_judge=judge,
        doc_set_judge=judge,
        output_judge=judge,
    )


def l1_classifier_available(settings: GuardSettings) -> bool:
    """Whether the private L1 classifier is mounted where the guard looks for it."""
    return Path(settings.l1_model_path).is_file()


class LiveGuardedDraftingService(DraftingService):
    """DraftingService that runs C3 jobs through AgentMailGuard (C0 jobs are untouched)."""

    def __init__(
        self,
        *,
        generator: SinglePassGenerator,
        job_store: JobStore,
        persistence: DraftPersistence,
        price_table: Mapping[str, ModelPricing],
        metrics: PipelineMetrics | None = None,
        guard_settings: GuardSettings,
        live_judge_provider: LLMProvider | None = None,
        max_tokens: int = 1000,
    ) -> None:
        super().__init__(
            generator=generator,
            job_store=job_store,
            persistence=persistence,
            price_table=price_table,
            metrics=metrics,
        )
        self.guard_settings = guard_settings
        self.live_judge_provider = live_judge_provider
        self.max_tokens = max_tokens

    def job_guard_mode(self) -> str:
        """The job's guard mode; C0 whenever the guard is not enabled."""
        if not self.guard_settings.enabled:
            return "C0"
        return current_guard_mode() or self.guard_settings.default_mode

    async def draft(
        self,
        job: Job,
        context: ContextPackage,
        *,
        category: str | None = None,
        budget_tracker: CallBudgetTracker | None = None,
        escalated_tier: ModelTier | str | None = None,
        escalation_reason: str | None = None,
    ) -> DraftingOutcome:
        """Draft a C0 job as DraftingService does; guard, generate and persist a C3 job."""
        if self.job_guard_mode() != GUARDED_MODE:
            return await super().draft(
                job,
                context,
                category=category,
                budget_tracker=budget_tracker,
                escalated_tier=escalated_tier,
                escalation_reason=escalation_reason,
            )

        current = await self.job_store.get_job(job.organization_id, job.id)
        if current is None:
            raise KeyError(f"Job {job.id} not found for organization {job.organization_id}")
        if current.state == JobState.DRAFTED.value:
            existing = await self.persistence.find_draft_for_job(
                current.organization_id, current.id
            )
            if existing is not None:
                return DraftingOutcome(draft=existing, job=current, created=False)
        if current.state == JobState.CONTEXT_READY.value:
            message = context.current_message
            current, _ = await self.job_store.transition_job_state(
                organization_id=current.organization_id,
                job_id=current.id,
                target_state=JobState.GENERATING,
                payload={
                    "category": category,
                    "escalated_tier": str(escalated_tier) if escalated_tier else None,
                    "escalation_reason": escalation_reason or NO_ESCALATION,
                    "guard_mode": GUARDED_MODE,
                },
                message_id=message.message_id,
                thread_id=message.thread_id,
            )
        elif current.state != JobState.GENERATING.value:
            raise IllegalStateTransitionError(current.state, JobState.GENERATING)

        profile = current_model_profile() or (
            "live" if self.live_judge_provider is not None else "fake"
        )
        judge = (
            JudgeAdapter(self.live_judge_provider)
            if profile == "live" and self.live_judge_provider is not None
            else None
        )
        pipeline = build_pipeline(self.guard_settings, judge)
        generations: list[GenerationResult] = []
        generation_ms = 0

        async def draft_factory(messages: Sequence[Any]) -> dict[str, Any]:
            nonlocal generation_ms
            if generations:
                raise RuntimeError("the guard asked for a second generation call in one job")
            started = time.perf_counter()
            try:
                result = await self.generator.generate_from_messages(
                    [ChatMessage(role=str(m.role), content=str(m.content)) for m in messages],
                    context=context,
                    category=category,
                    budget_tracker=budget_tracker,
                    escalated_tier=escalated_tier,
                    escalation_reason=escalation_reason,
                    max_tokens=self.max_tokens,
                )
            finally:
                generation_ms += int((time.perf_counter() - started) * 1000)
            generations.append(result)
            return dict(result.content)

        from mailguard.integration.adapters import (
            chunks_from_context,
            guarded_email_from_context,
            recent_messages_from_context,
            report_json,
        )

        started = time.perf_counter()
        report, guarded, _bundle = await pipeline.run(
            guarded_email_from_context(context),
            chunks_from_context(context),
            draft_factory,
            system_instructions=guarded_system_instructions(context.agent_instructions),
            category_instructions=context.category_instructions,
            thread_summary=context.thread_summary,
            recent_messages=recent_messages_from_context(context),
            business_data=None,
            category=category,
        )
        total_ms = int((time.perf_counter() - started) * 1000)

        record = self._record(report, pipeline)
        outcome_kind, reason = guard_outcome(record)
        if outcome_kind == "escalated":
            draft = self._escalation_draft(current, context, reason)
        else:
            if not generations:
                raise RuntimeError(
                    f"job {job.id}: the guard let a reply through without generating"
                )
            body = guarded.body if guarded is not None else None
            if body is None:
                raise RuntimeError(f"job {job.id}: the guard returned no draft body")
            draft = build_generated_draft(
                generations[-1], context, job_id=current.id, price_table=self.price_table
            )
            draft = dataclasses.replace(
                draft, body=str(body), escalation_reason=reason or draft.escalation_reason
            )

        guard_calls = judge.calls if judge is not None else 0
        report_row = GuardReportRecord(
            organization_id=current.organization_id,
            job_id=current.id,
            guard_mode=GUARDED_MODE,
            model_profile=profile,
            schema_version=int(getattr(report, "schema_version", 3)),
            inbound_action=record["inbound_action"],
            final_action=record["final_action"],
            report={
                **json.loads(report_json(report)),
                "outcome": outcome_kind,
                "rule": record["rule"],
                "blocked_inbound": record["blocked_inbound"],
                "blocked_outbound": record["blocked_outbound"],
                "generation_called": bool(generations),
                "judge_tokens": {
                    "input": judge.input_tokens if judge else 0,
                    "output": judge.output_tokens if judge else 0,
                },
                "l1_classifier_loaded": l1_classifier_available(self.guard_settings),
            },
            guard_llm_calls=guard_calls,
            timings_ms={
                "guarded_total": total_ms,
                "generation": generation_ms,
                "guard": max(0, total_ms - generation_ms),
            },
        )
        outcome = await self.persistence.persist_drafted(draft, guard_report=report_row)
        if outcome.created:
            if outcome_kind != "escalated":
                self._count_generated(outcome.draft, category)
            self._count_guard_calls(profile, guard_calls)
            logger.info(
                "guarded_draft_persisted",
                extra={
                    "fields": {
                        "draft_id": str(outcome.draft.id),
                        "job_id": str(outcome.job.id),
                        "guard_mode": GUARDED_MODE,
                        "model_profile": profile,
                        "guard_outcome": outcome_kind,
                        "inbound_action": record["inbound_action"],
                        "final_action": record["final_action"],
                        "rule": record["rule"],
                        "guard_llm_calls": guard_calls,
                        "timings_ms": report_row.timings_ms,
                    }
                },
            )
        return DraftingOutcome(draft=outcome.draft, job=outcome.job, created=outcome.created)

    @staticmethod
    def _record(report: Any, pipeline: Any) -> dict[str, Any]:
        """The facts ``guard_outcome`` reads, as the benchmark's summary derives them."""
        inbound = report.inbound_decision
        final = report.decision
        blocked_inbound = bool(pipeline.blocked(inbound))
        blocked_outbound = bool(
            final is not None and final is not inbound and pipeline.blocked(final)
        )
        return {
            "blocked_inbound": blocked_inbound,
            "blocked_outbound": blocked_outbound,
            "inbound_action": _value(inbound.action) if inbound is not None else None,
            "final_action": _value(final.action) if final is not None else None,
            "rule": final.matched_rule_id if final is not None else None,
        }

    def _escalation_draft(
        self, job: Job, context: ContextPackage, reason: str | None
    ) -> GeneratedDraft:
        """The draft of a job the guard stopped: no body, no tokens, no model call."""
        message = context.current_message
        if not str(message.thread_id or "").strip():
            raise UnpersistableDraftError(
                f"Message {message.message_id} has no thread; generated_draft.thread_id is required"
            )
        return GeneratedDraft(
            organization_id=message.organization_id,
            job_id=job.id,
            message_id=message.message_id,
            thread_id=message.thread_id,
            action=ESCALATE_ACTION,
            subject=reply_subject(message.subject),
            body="",
            model_name=GUARD_MODEL_NAME,
            model_tier=GUARD_MODEL_NAME,
            escalation_reason=reason,
            input_tokens=0,
            output_tokens=0,
            cost_estimate=0.0,
            status="draft",
        )

    def _count_guard_calls(self, profile: str, calls: int) -> None:
        metrics: Any = self.metrics
        if metrics is None or calls <= 0:
            return
        try:
            metrics.guard_llm_calls_total.labels(model_profile=profile).inc(calls)
        except Exception:
            logger.warning("guard_llm_calls_total emission failed", exc_info=True)

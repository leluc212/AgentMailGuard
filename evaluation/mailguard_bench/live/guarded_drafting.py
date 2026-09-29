"""GuardedDraftingService: the ai-worker's drafting step with AgentMailGuard around its one
generation call (task 7.20; ADR-0011; R22.12, R18.1, R16.4).

    AIWorkerConsumer ─ ContextPackage ─▶ GuardedDraftingService.draft(job, context, category=…)
       CONTEXT_READY ─▶ GENERATING                      (state machine, as DraftingService)
       GuardedCaseExecutor.execute                      (guarded_reply.py, unchanged)
          L1 → L2 → L5 inbound ── block/quarantine ⇒ no model call
          L3b → L3 prompt ── ONE reply.v1 call ── L4 → L5 outbound
       ├─ blocked         ⇒ escalate draft (body "", model agentmailguard, 0 tokens)
       ├─ human_approval  ⇒ the generated draft, reason agentmailguard:human_approval:<rule>
       └─ otherwise       ⇒ the generated draft, its body as L4 left it
       audit line ─▶ <run_dir>/raw/audit__<config>.jsonl      (before the commit below)
       persist_drafted ─▶ GENERATING ─▶ DRAFTED              (one transaction, as DraftingService)

The class has DraftingService's public interface, so ``build_consumers(...,
drafting_factory=...)`` swaps it in and everything before it (summary, retrieval, rerank, routing)
stays the ai-worker's. What the guard decides is AgentMailGuard's (ADR-0010); this module only
turns its outcome into the draft rag-email persists. The guard's own LLM calls (L1 judge, L2, L3b,
L4) are not part of the job's CallBudgetTracker; the audit line counts them in ``guard_llm``.

Audit line, one JSON object per job, flat: ``message_id``, ``organization_id``, ``job_id``,
``draft_id``, ``config``, every field of a v1 guarded row's ``result`` (guarded_reply.py:
``blocked_inbound``, ``blocked_outbound``, ``report``, ``generation``, ``guard_llm``,
``timings_ms``, ``draft``, ``system_instructions``, ...), ``final_body`` and ``final_action`` (the
persisted draft's body and action, as the scorer's flat record names them), ``decision_action``
(the L5 decision's action, which v1 called ``final_action``), ``escalation_reason``,
``retrieved`` (the context's chunks, as the ``context_built`` event lists them) and
``guard_errors``. A job that is retried appends a line per attempt; the last line of a message wins.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from evaluation.mailguard_bench.case_adapter import CaseEmail, EvalCase, PreparedCase
from evaluation.mailguard_bench.guard_build import GuardBuild
from evaluation.mailguard_bench.guarded_reply import CaseExecution, GuardedCaseExecutor
from evaluation.mailguard_bench.results import ResultStore
from packages.core.settings import ModelPricing
from packages.db.draft_persistence import DraftPersistence
from packages.db.job import JobStore
from packages.domain.entities import Classification, ContextPackage, GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.budget import CallBudgetTracker
from packages.llm.drafts import (
    NO_ESCALATION,
    UnpersistableDraftError,
    build_generated_draft,
    reply_subject,
)
from packages.llm.generator import GenerationResult, SinglePassGenerator
from packages.llm.profile import AgentProfile
from packages.llm.protocol import ChatMessage, ModelTier
from packages.retrieval.query_builder import RetrievalQueryBuilder
from services.ai_worker.drafting import DraftingOutcome, DraftingService

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)

AUDIT_SCHEMA = "mailguard-guard-audit.v1"
GUARD_MODEL_NAME = "agentmailguard"
"""``model_name`` of a draft the guard stopped; ``escalation_reason`` starts with it too."""
ESCALATE_ACTION = "escalate"
HUMAN_APPROVAL = "human_approval"
"""``PolicyAction.HUMAN_APPROVAL``'s value (mailguard is imported lazily, so not the enum)."""
LIVE_JOB_KIND = "live"
"""``EvalCase.kind`` of a job the ai-worker hands over: it is no benchmark case."""


class GuardOutcome(StrEnum):
    """What AgentMailGuard decided about one job, in the terms of the draft that results."""

    ESCALATED = "escalated"  # the inbound email or the reply was blocked or quarantined
    HUMAN_APPROVAL = "human_approval"  # the draft stands, a reviewer must sign it off
    DRAFTED = "drafted"  # draft_only, auto_send, or no layer active (C0T)


def guard_outcome(record: Mapping[str, Any]) -> tuple[GuardOutcome, str | None]:
    """The guard's outcome for one job and the ``escalation_reason`` it leaves on the draft.

    Args:
        record: ``CaseExecution.record`` (guarded_reply.py); only ``blocked_inbound``,
            ``blocked_outbound``, ``inbound_action``, ``final_action`` and ``rule`` are read.

    Returns:
        ``(ESCALATED, "agentmailguard:<action>:<rule_id>")`` when the inbound email or the reply
        was blocked, ``(HUMAN_APPROVAL, "agentmailguard:human_approval:<rule_id>")`` when the
        final decision needs a reviewer, and ``(DRAFTED, None)`` otherwise.
    """
    rule = record.get("rule") or "unknown"
    if record["blocked_inbound"] or record["blocked_outbound"]:
        action = record["inbound_action"] if record["blocked_inbound"] else record["final_action"]
        return GuardOutcome.ESCALATED, f"{GUARD_MODEL_NAME}:{action}:{rule}"
    if record.get("final_action") == HUMAN_APPROVAL:
        return GuardOutcome.HUMAN_APPROVAL, f"{GUARD_MODEL_NAME}:{HUMAN_APPROVAL}:{rule}"
    return GuardOutcome.DRAFTED, None


class _JobGenerator(SinglePassGenerator):
    """The service's generator with this job's budget, tier and escalation on its one call.

    GuardedCaseExecutor calls ``generate_from_messages(messages, context=, category=,
    max_tokens=)`` and knows nothing of what the ai-worker passes to ``draft``, so it is added
    here. The call goes to the wrapped generator (the instance ``build_consumers`` made) and its
    result is kept, because the executor's record holds only a summary of it.
    """

    def __init__(
        self,
        base: SinglePassGenerator,
        *,
        budget_tracker: CallBudgetTracker | None,
        escalated_tier: ModelTier | str | None,
        escalation_reason: str | None,
    ) -> None:
        super().__init__(
            llm_provider=base.llm_provider,
            profile_registry=base.profile_registry,
            metrics=base.metrics,
            price_table=base.price_table,
        )
        self._base = base
        self._budget_tracker = budget_tracker
        self._escalated_tier = escalated_tier
        self._escalation_reason = escalation_reason
        self.results: list[GenerationResult] = []

    async def generate_from_messages(
        self,
        messages: Sequence[ChatMessage],
        *,
        context: ContextPackage,
        category: str | None = None,
        profile: AgentProfile | None = None,
        budget_tracker: CallBudgetTracker | None = None,
        escalated_tier: ModelTier | str | None = None,
        escalation_reason: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> GenerationResult:
        result = await self._base.generate_from_messages(
            messages,
            context=context,
            category=category,
            profile=profile,
            budget_tracker=budget_tracker if budget_tracker is not None else self._budget_tracker,
            escalated_tier=escalated_tier if escalated_tier is not None else self._escalated_tier,
            escalation_reason=(
                escalation_reason if escalation_reason is not None else self._escalation_reason
            ),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.results.append(result)
        return result


class GuardedDraftingService(DraftingService):
    """Moves a context-ready job through AgentMailGuard and one generation to a persisted draft.

    Takes DraftingService's keyword arguments plus the guard (a ``GuardBuild``, whose
    ``config`` names the audit lines), the audit file and, optionally, the generation token limit
    and the query builder the L3b echo check should see (default: the ContextBuilder's own).
    """

    def __init__(
        self,
        *,
        generator: SinglePassGenerator,
        job_store: JobStore,
        persistence: DraftPersistence,
        price_table: Mapping[str, ModelPricing],
        metrics: PipelineMetrics | None = None,
        guard: GuardBuild,
        audit_path: Path,
        max_tokens: int = 1000,
        query_builder: RetrievalQueryBuilder | None = None,
    ) -> None:
        super().__init__(
            generator=generator,
            job_store=job_store,
            persistence=persistence,
            price_table=price_table,
            metrics=metrics,
        )
        self.guard = guard
        self.audit_path = audit_path
        self.max_tokens = max_tokens
        self.query_builder = query_builder or RetrievalQueryBuilder()
        self._audit = ResultStore(audit_path)

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
        """Guard, generate and persist the job's draft, or return the one already persisted.

        Raises:
            KeyError: If the job does not exist in its organization.
            IllegalStateTransitionError: If the job is neither CONTEXT_READY, GENERATING
                nor already DRAFTED. Raised before the guard or any model is called.
            UnpersistableDraftError: If the draft cannot be persisted.
            RateLimitedError: If a guard LLM stage hit HTTP 429. Nothing is persisted, the job
                stays GENERATING and the AI-worker consumer's retry ladder takes it. Any other
                guard-layer failure is kept in the audit line's ``guard_errors``: the retry
                ladder waits 30 s to 30 min, and v1 made those cases error rows (a weaker guard is
                never a defence), so the feeder turns a non-empty ``guard_errors`` into one.
            LLMError: Any generation failure, including ``UnvalidatedDraftError``; nothing is
                persisted and the job stays GENERATING, as for DraftingService.
        """
        current = await self.job_store.get_job(job.organization_id, job.id)
        if current is None:
            raise KeyError(f"Job {job.id} not found for organization {job.organization_id}")

        if current.state == JobState.DRAFTED.value:
            existing = await self.persistence.find_draft_for_job(
                current.organization_id, current.id
            )
            if existing is not None:
                logger.info(
                    "Job %s already drafted as %s; skipping the guard and generation",
                    job.id,
                    existing.id,
                )
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
                },
                message_id=message.message_id,
                thread_id=message.thread_id,
            )
        elif current.state != JobState.GENERATING.value:
            raise IllegalStateTransitionError(current.state, JobState.GENERATING)

        generator = _JobGenerator(
            self.generator,
            budget_tracker=budget_tracker,
            escalated_tier=escalated_tier,
            escalation_reason=escalation_reason,
        )
        executor = GuardedCaseExecutor(
            pipeline=self.guard.pipeline,
            generator=generator,
            guard_llm=self.guard.guard_llm,
            max_tokens=self.max_tokens,
        )
        execution = await executor.execute(self._prepared_case(current, context, category))

        outcome_kind, reason = guard_outcome(execution.record)
        if outcome_kind is GuardOutcome.ESCALATED:
            draft = self._escalation_draft(current, context, reason)
        else:
            draft = self._generated_draft(current, context, generator, execution, reason)
        # The line goes down first: a crash before the commit leaves a line for a retry to
        # supersede, never a DRAFTED job without its guard facts.
        self._audit.append(self._audit_line(current, context, draft, execution))

        outcome = await self.persistence.persist_drafted(draft)
        if outcome.created:
            if outcome_kind is not GuardOutcome.ESCALATED:
                self._count_generated(outcome.draft, category)
            logger.info(
                "draft_persisted",
                extra={
                    "fields": {
                        "draft_id": str(outcome.draft.id),
                        "job_id": str(outcome.job.id),
                        "category": category or "unknown",
                        "model_tier": outcome.draft.model_tier,
                        "escalation_reason": outcome.draft.escalation_reason,
                        "input_tokens": outcome.draft.input_tokens,
                        "output_tokens": outcome.draft.output_tokens,
                        "cost_estimate": outcome.draft.cost_estimate,
                        "citation_mismatch": outcome.draft.citation_mismatch,
                        "guard_config": self.guard.config,
                        "guard_outcome": str(outcome_kind),
                        "guard_errors": len(execution.guard_errors),
                    }
                },
            )
        return DraftingOutcome(draft=outcome.draft, job=outcome.job, created=outcome.created)

    def _prepared_case(
        self, job: Job, context: ContextPackage, category: str | None
    ) -> PreparedCase:
        """The job as the PreparedCase GuardedCaseExecutor reads.

        The executor reads the context, ``classification.category`` and ``retrieval_query``,
        nothing else. A live job has no benchmark case, ingested documents or retrieval map, so
        those are empty. The query is rebuilt with the ContextBuilder's own builder from what
        ``draft`` receives (the intent is not passed in, and v1 cases had none); it is what L3b's
        query-echo check compares the retrieved chunks with. An empty category resolves the
        default profile, as ``None`` does.
        """
        message = context.current_message
        classification = Classification(category=category or "")
        query = self.query_builder.build(
            message=message,
            classification=classification,
            thread_summary=context.thread_summary,
        )
        case = EvalCase(
            case_id=str(job.id),
            kind=LIVE_JOB_KIND,
            source="ai_worker",
            technique=None,
            vector="email",
            category=classification.category,
            email=CaseEmail(
                sender_email=message.sender.email,
                sender_name=message.sender.name or "",
                subject=message.subject,
                body_text=message.body_text_clean or message.body_text,
            ),
            kb_docs=(),
            kb_query="",
            goal={},
            attacker={},
            expected_keywords=(),
            meta={},
        )
        return PreparedCase(
            case=case,
            organization_id=UUID(str(message.organization_id)),
            message=message,
            classification=classification,
            context=context,
            retrieval_query=query.semantic_text,
            ingested={},
            retrieved=(),
            context_ms=0,
        )

    def _escalation_draft(
        self, job: Job, context: ContextPackage, reason: str | None
    ) -> GeneratedDraft:
        """The draft of a job the guard stopped: no body, no tokens, no model."""
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

    def _generated_draft(
        self,
        job: Job,
        context: ContextPackage,
        generator: _JobGenerator,
        execution: CaseExecution,
        reason: str | None,
    ) -> GeneratedDraft:
        """The generated draft, with the body L4 left and the guard's reason when it has one."""
        if not generator.results:
            raise RuntimeError(f"job {job.id}: the guard let a reply through without generating")
        body = execution.record["draft"]["body_after_guard"]
        if body is None:
            raise RuntimeError(f"job {job.id}: the guard returned no draft body")
        draft = build_generated_draft(
            generator.results[-1], context, job_id=job.id, price_table=self.price_table
        )
        return dataclasses.replace(
            draft, body=str(body), escalation_reason=reason or draft.escalation_reason
        )

    def _audit_line(
        self, job: Job, context: ContextPackage, draft: GeneratedDraft, execution: CaseExecution
    ) -> dict[str, Any]:
        message = context.current_message
        record = dict(execution.record)
        decision_action = record.pop("final_action")  # v1's name for the L5 decision's action
        return {
            "schema": AUDIT_SCHEMA,
            "ts": datetime.now(UTC).isoformat(),
            "message_id": str(message.message_id),
            "organization_id": str(message.organization_id),
            "job_id": str(job.id),
            "draft_id": str(draft.id),
            "config": self.guard.config,
            **record,
            "decision_action": decision_action,
            "final_body": draft.body,
            "final_action": draft.action,
            "escalation_reason": draft.escalation_reason,
            "retrieved": [
                {
                    "chunk_id": str(chunk.chunk_id),
                    "document_id": str(chunk.document_id),
                    "rank": rank,
                    "rerank_score": chunk.rerank_score,
                }
                for rank, chunk in enumerate(context.retrieved_chunks, start=1)
            ],
            "guard_errors": list(execution.guard_errors),
        }

"""Early-Exit Gate and cost optimization governor for email triage (R6.5, R6.6, R6.12–R6.15).

Implements the three mutually exclusive economic routing outcomes:
1. Early exit (reply_required == false) -> Job transitions to COMPLETED. Zero retrieval,
   zero rerank, zero generation (R6.5). (~45% of inbound mail)
2. Deterministic template reply (workflow_hint == 'template') -> Approved template rendered
   and persisted; Job transitions directly to DRAFTED with zero retrieval and zero generation
   (R6.12, R6.13). If no template matches (category, intent), falls back to workflow_hint='ai'
   so replies are never blocked (R6.14). (~20% of inbound mail)
3. Actionable AI generation (workflow_hint == 'ai') -> Job transitions to QUEUED (R6.6, R7.1).
   Selective hybrid RAG is performed only if retrieval_required == true. (~35% of inbound mail)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from packages.db.draft import DraftStore
from packages.db.job import JobStore
from packages.domain.entities import (
    Classification,
    GeneratedDraft,
    Job,
    NormalizedMessage,
    ProcessingEvent,
)
from packages.domain.rules import EmailContext
from packages.domain.state_machine import (
    IllegalStateTransitionError,
    JobState,
    transition_job,
)
from packages.domain.templates import (
    TemplateDefinition,
    TemplateRegistry,
    TemplateRenderResult,
)
from packages.observability.funnel import FunnelOutcome, RAGMode, record_funnel_outcome
from packages.observability.metrics import PipelineMetrics, get_metrics

logger = logging.getLogger(__name__)


class GateAction(StrEnum):
    """Routing action decided by the early-exit gate (R6.5, R6.6, R6.12–R6.15)."""

    EARLY_EXIT = "early_exit"
    TEMPLATE_REPLY = "template_reply"
    PROCEED_NO_RAG = "proceed_no_rag"
    PROCEED_RAG = "proceed_rag"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of early-exit gate evaluation detailing state transition and permitted work."""

    action: GateAction
    job: Job
    event: ProcessingEvent
    classification: Classification
    reply_required: bool
    retrieval_required: bool
    workflow_hint: str
    should_embed: bool
    should_retrieve: bool
    should_rerank: bool
    should_generate: bool
    reason: str
    rendered_draft: GeneratedDraft | None = None
    template_result: TemplateRenderResult | None = None


@runtime_checkable
class DownstreamPipelineHooks(Protocol):
    """Protocol for downstream RAG and generation components to verify zero-call assertions."""

    async def embed_query(self, text: str) -> list[float]:
        """Generate query vector embedding."""
        ...

    async def retrieve_knowledge(self, query: str, filters: dict[str, Any]) -> list[Any]:
        """Execute hybrid lexical and semantic knowledge retrieval."""
        ...

    async def rerank_candidates(self, query: str, candidates: list[Any]) -> list[Any]:
        """Execute neural candidate reranking."""
        ...

    async def generate_reply(self, context: Any) -> str:
        """Execute LLM response draft generation."""
        ...


class GatedPipelineRunner:
    """Downstream pipeline coordinator enforcing selective AI execution (R6.5, R6.6, R6.13)."""

    def __init__(self, hooks: DownstreamPipelineHooks) -> None:
        self.hooks = hooks

    async def run_pipeline(
        self,
        decision: GateDecision,
        query: str = "",
        filters: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Execute permitted pipeline stages based strictly on the GateDecision."""
        if decision.action == GateAction.EARLY_EXIT:
            # Zero AI work: strictly prohibited per R6.5
            return {
                "status": "completed_early_exit",
                "embedding_performed": False,
                "retrieval_performed": False,
                "rerank_performed": False,
                "generation_performed": False,
                "reply": None,
            }

        if decision.action == GateAction.TEMPLATE_REPLY:
            # Zero AI work: deterministic template reply rendered directly to DRAFTED (R6.13)
            reply_body = (
                decision.rendered_draft.body
                if decision.rendered_draft
                else (decision.template_result.body if decision.template_result else None)
            )
            return {
                "status": "drafted_template_reply",
                "embedding_performed": False,
                "retrieval_performed": False,
                "rerank_performed": False,
                "generation_performed": False,
                "reply": reply_body,
            }

        retrieved_chunks: list[Any] = []
        if decision.should_retrieve:
            # Execute embedding and retrieval only if permitted
            if decision.should_embed:
                await self.hooks.embed_query(query)
            retrieved_chunks = await self.hooks.retrieve_knowledge(query, filters or {})
            if decision.should_rerank and retrieved_chunks:
                retrieved_chunks = await self.hooks.rerank_candidates(query, retrieved_chunks)

        reply: str | None = None
        if decision.should_generate:
            generation_ctx = context or {"chunks": retrieved_chunks}
            reply = await self.hooks.generate_reply(generation_ctx)

        return {
            "status": "processed",
            "embedding_performed": decision.should_embed,
            "retrieval_performed": decision.should_retrieve,
            "rerank_performed": decision.should_rerank,
            "generation_performed": decision.should_generate,
            "reply": reply,
            "chunks_count": len(retrieved_chunks),
        }


def _extract_ids_from_message(
    job: Job,
    message: NormalizedMessage | EmailContext | dict[str, Any] | None,
) -> tuple[UUID | str, UUID | str]:
    """Extract or fallback message_id and thread_id."""
    msg_id: UUID | str = (
        getattr(message, "message_id", None)
        or (message.get("message_id") if isinstance(message, dict) else None)
        or job.message_id
        or uuid4()
    )

    th_id: UUID | str = (
        getattr(message, "thread_id", None)
        or (message.get("thread_id") if isinstance(message, dict) else None)
        or job.thread_id
        or uuid4()
    )

    return msg_id, th_id


class EarlyExitGate:
    """Early-Exit Gate implementing the primary cost governor of the system (R6.5–R6.15).

    Three mutually exclusive outcomes:
    1. reply_required == false -> Transition to COMPLETED. Zero retrieval, zero generation (R6.5).
    2. workflow_hint == 'template' -> Render template and transition to DRAFTED.
       Zero retrieval, zero generation (R6.12, R6.13). Fall back to 'ai' if missing (R6.14).
    3. workflow_hint == 'ai' -> Transition to QUEUED. Downstream AI generation (R6.6, R7.1).
    """

    def __init__(
        self,
        job_store: JobStore | None = None,
        template_registry: TemplateRegistry | None = None,
        draft_store: DraftStore | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self.job_store = job_store
        self.template_registry = template_registry
        self.draft_store = draft_store
        self.metrics = metrics or get_metrics()

    def _record_metrics(
        self,
        action: GateAction,
        org_id: UUID | str,
        category: str,
        reason: str,
        template_id: str | None = None,
    ) -> None:
        """Record funnel accounting and specific gate metrics (R6.10, R6.15, R21.4)."""
        org_str = str(org_id)
        if action == GateAction.EARLY_EXIT:
            record_funnel_outcome(
                self.metrics,
                organization=org_str,
                category=category,
                outcome=FunnelOutcome.EARLY_EXIT,
                rag_mode=RAGMode.NONE,
            )
            self.metrics.emails_early_exit_total.labels(
                organization=org_str,
                category=category,
                reason=reason,
            ).inc()
        elif action == GateAction.TEMPLATE_REPLY:
            record_funnel_outcome(
                self.metrics,
                organization=org_str,
                category=category,
                outcome=FunnelOutcome.TEMPLATE,
                rag_mode=RAGMode.NONE,
            )
            self.metrics.emails_templated_total.labels(
                organization=org_str,
                template_id=template_id or "unknown",
            ).inc()
        elif action == GateAction.PROCEED_RAG:
            record_funnel_outcome(
                self.metrics,
                organization=org_str,
                category=category,
                outcome=FunnelOutcome.AI_GENERATION,
                rag_mode=RAGMode.RAG,
            )
        elif action == GateAction.PROCEED_NO_RAG:
            record_funnel_outcome(
                self.metrics,
                organization=org_str,
                category=category,
                outcome=FunnelOutcome.AI_GENERATION,
                rag_mode=RAGMode.NO_RAG,
            )

    def evaluate_decision(
        self,
        job: Job,
        classification: Classification,
        trace_id: str | None = None,
        message: NormalizedMessage | EmailContext | dict[str, Any] | None = None,
        business_data: dict[str, Any] | None = None,
    ) -> GateDecision:
        """Pure in-memory state transition and gate decision evaluation.

        Args:
            job: Current job entity. Must be at NORMALIZED or CLASSIFIED state.
            classification: Classification outcome from triage cascade.
            trace_id: Optional distributed trace identifier.
            message: Optional normalized message or context for template variable substitution.
            business_data: Optional business data dictionary for variable substitution.

        Returns:
            GateDecision containing transitioned job, emitted event, and execution flags.
        """
        active_trace_id = trace_id or job.trace_id

        # 1. Progress job to CLASSIFIED if currently at NORMALIZED
        current_job = job
        if current_job.state == JobState.NORMALIZED:
            current_job, _ = transition_job(
                current_job,
                JobState.CLASSIFIED,
                payload={
                    "category": classification.category,
                    "confidence": classification.confidence,
                    "decided_by": classification.decided_by,
                },
                trace_id=active_trace_id,
            )

        if current_job.state != JobState.CLASSIFIED:
            raise IllegalStateTransitionError(
                current_job.state,
                JobState.COMPLETED if not classification.reply_required else JobState.QUEUED,
            )

        # Outcome 1: Early Exit -> straight to COMPLETED (R6.5)
        if not classification.reply_required or classification.workflow_hint == "none":
            action = GateAction.EARLY_EXIT
            reason = "no_reply_required"
            payload = {
                "early_exit": True,
                "reason": reason,
                "category": classification.category,
                "intent": classification.intent,
                "confidence": classification.confidence,
                "decided_by": classification.decided_by,
            }
            final_job, event = transition_job(
                current_job,
                JobState.COMPLETED,
                payload=payload,
                trace_id=active_trace_id,
            )
            self._record_metrics(
                action=action,
                org_id=job.organization_id,
                category=classification.category,
                reason=reason,
            )
            return GateDecision(
                action=action,
                job=final_job,
                event=event,
                classification=classification,
                reply_required=False,
                retrieval_required=False,
                workflow_hint="none",
                should_embed=False,
                should_retrieve=False,
                should_rerank=False,
                should_generate=False,
                reason=reason,
            )

        # Outcome 2: Deterministic template reply (R6.12, R6.13, R6.14)
        matched_template: TemplateDefinition | None = None
        if classification.workflow_hint == "template" and self.template_registry:
            matched_template = self.template_registry.find_template(
                classification.category,
                classification.intent,
            )

        if (
            classification.workflow_hint == "template"
            and self.template_registry is not None
            and matched_template is not None
        ):
            action = GateAction.TEMPLATE_REPLY
            reason = "deterministic_template_reply"
            rendered_result = self.template_registry.render(
                matched_template,
                message=message or {},
                business_data=business_data or {},
            )

            msg_id, th_id = _extract_ids_from_message(job, message)
            draft = GeneratedDraft(
                id=uuid4(),
                organization_id=job.organization_id,
                job_id=job.id,
                message_id=msg_id,
                thread_id=th_id,
                action="reply",
                subject=rendered_result.subject,
                body=rendered_result.body,
                confidence=classification.confidence,
                citations=[],
                citation_mismatch=False,
                model_name="template",
                model_tier="template",
                escalation_reason=None,
                prompt_version=f"{matched_template.id}:{matched_template.version}",
                input_tokens=0,
                output_tokens=0,
                cost_estimate=0.0,
                status="draft",
            )

            payload = {
                "template_reply": True,
                "template_id": matched_template.id,
                "template_version": matched_template.version,
                "category": classification.category,
                "intent": classification.intent,
                "confidence": classification.confidence,
                "decided_by": classification.decided_by,
                "draft_id": str(draft.id),
            }

            final_job, event = transition_job(
                current_job,
                JobState.DRAFTED,
                payload=payload,
                trace_id=active_trace_id,
            )
            self._record_metrics(
                action=action,
                org_id=job.organization_id,
                category=classification.category,
                reason=reason,
                template_id=matched_template.id,
            )

            return GateDecision(
                action=action,
                job=final_job,
                event=event,
                classification=classification,
                reply_required=True,
                retrieval_required=False,
                workflow_hint="template",
                should_embed=False,
                should_retrieve=False,
                should_rerank=False,
                should_generate=False,
                reason=reason,
                rendered_draft=draft,
                template_result=rendered_result,
            )

        # Outcome 3: Actionable mail with AI Generation (or template fallback to 'ai')
        # If workflow_hint was 'template' but no template matched, fall back to 'ai' (R6.14)
        effective_workflow_hint = "ai"
        effective_cls = classification
        if classification.workflow_hint == "template" and matched_template is None:
            logger.info(
                "No template matched for (%s, %s). Falling back to workflow_hint='ai' (R6.14).",
                classification.category,
                classification.intent,
            )
            effective_cls = replace(classification, workflow_hint="ai")

        target_state = JobState.QUEUED
        if not effective_cls.retrieval_required:
            action = GateAction.PROCEED_NO_RAG
            reason = "retrieval_not_required"
            should_retrieve = False
            should_embed = False
            should_rerank = False
        else:
            action = GateAction.PROCEED_RAG
            reason = "retrieval_required"
            should_retrieve = True
            should_embed = True
            should_rerank = True

        should_generate = True

        payload = {
            "category": effective_cls.category,
            "intent": effective_cls.intent,
            "priority": effective_cls.priority,
            "retrieval_required": effective_cls.retrieval_required,
            "workflow_hint": effective_workflow_hint,
            "confidence": effective_cls.confidence,
            "decided_by": effective_cls.decided_by,
        }

        final_job, event = transition_job(
            current_job,
            target_state,
            payload=payload,
            trace_id=active_trace_id,
        )
        self._record_metrics(
            action=action,
            org_id=job.organization_id,
            category=effective_cls.category,
            reason=reason,
        )

        return GateDecision(
            action=action,
            job=final_job,
            event=event,
            classification=effective_cls,
            reply_required=True,
            retrieval_required=effective_cls.retrieval_required,
            workflow_hint=effective_workflow_hint,
            should_embed=should_embed,
            should_retrieve=should_retrieve,
            should_rerank=should_rerank,
            should_generate=should_generate,
            reason=reason,
        )

    async def evaluate_and_persist(
        self,
        job: Job,
        classification: Classification,
        job_store: JobStore | None = None,
        trace_id: str | None = None,
        message: NormalizedMessage | EmailContext | dict[str, Any] | None = None,
        business_data: dict[str, Any] | None = None,
        draft_store: DraftStore | None = None,
    ) -> GateDecision:
        """Evaluate gate and persist state transition, event, and draft atomically.

        Args:
            job: Current job entity.
            classification: Classification outcome.
            job_store: Optional job store override (defaults to instance job_store).
            trace_id: Optional trace ID.
            message: Optional normalized message or context for template variable substitution.
            business_data: Optional business data dictionary for variable substitution.
            draft_store: Optional draft store override (defaults to instance draft_store).

        Returns:
            GateDecision with updated job, persisted event, and persisted draft (if template).
        """
        store = job_store or self.job_store
        if not store:
            raise ValueError("A valid JobStore must be provided for evaluate_and_persist")

        active_draft_store = draft_store or self.draft_store
        active_trace_id = trace_id or job.trace_id

        # 1. Progress to CLASSIFIED in DB if currently NORMALIZED
        current_job = job
        if current_job.state == JobState.NORMALIZED:
            current_job, _ = await store.transition_job_state(
                organization_id=current_job.organization_id,
                job_id=current_job.id,
                target_state=JobState.CLASSIFIED,
                payload={
                    "category": classification.category,
                    "confidence": classification.confidence,
                    "decided_by": classification.decided_by,
                    "trace_id": active_trace_id,
                },
                message_id=current_job.message_id,
                thread_id=current_job.thread_id,
            )

        if current_job.state != JobState.CLASSIFIED:
            raise IllegalStateTransitionError(
                current_job.state,
                JobState.COMPLETED if not classification.reply_required else JobState.QUEUED,
            )

        # Outcome 1: Early Exit
        if not classification.reply_required or classification.workflow_hint == "none":
            action = GateAction.EARLY_EXIT
            reason = "no_reply_required"
            payload = {
                "early_exit": True,
                "reason": reason,
                "category": classification.category,
                "intent": classification.intent,
                "confidence": classification.confidence,
                "decided_by": classification.decided_by,
                "trace_id": active_trace_id,
            }
            final_job, event = await store.transition_job_state(
                organization_id=current_job.organization_id,
                job_id=current_job.id,
                target_state=JobState.COMPLETED,
                payload=payload,
                result_ref={
                    "early_exit": True,
                    "category": classification.category,
                    "confidence": classification.confidence,
                    "decided_by": classification.decided_by,
                },
                message_id=current_job.message_id,
                thread_id=current_job.thread_id,
            )
            self._record_metrics(
                action=action,
                org_id=job.organization_id,
                category=classification.category,
                reason=reason,
            )
            return GateDecision(
                action=action,
                job=final_job,
                event=event,
                classification=classification,
                reply_required=False,
                retrieval_required=False,
                workflow_hint="none",
                should_embed=False,
                should_retrieve=False,
                should_rerank=False,
                should_generate=False,
                reason=reason,
            )

        # Outcome 2: Deterministic template reply
        matched_template: TemplateDefinition | None = None
        if classification.workflow_hint == "template" and self.template_registry:
            matched_template = self.template_registry.find_template(
                classification.category,
                classification.intent,
            )

        if (
            classification.workflow_hint == "template"
            and self.template_registry is not None
            and matched_template is not None
        ):
            action = GateAction.TEMPLATE_REPLY
            reason = "deterministic_template_reply"
            rendered_result = self.template_registry.render(
                matched_template,
                message=message or {},
                business_data=business_data or {},
            )

            msg_id, th_id = _extract_ids_from_message(job, message)
            draft = GeneratedDraft(
                id=uuid4(),
                organization_id=job.organization_id,
                job_id=job.id,
                message_id=msg_id,
                thread_id=th_id,
                action="reply",
                subject=rendered_result.subject,
                body=rendered_result.body,
                confidence=classification.confidence,
                citations=[],
                citation_mismatch=False,
                model_name="template",
                model_tier="template",
                escalation_reason=None,
                prompt_version=f"{matched_template.id}:{matched_template.version}",
                input_tokens=0,
                output_tokens=0,
                cost_estimate=0.0,
                status="draft",
            )

            # Persist draft to store if available
            persisted_draft = draft
            if active_draft_store is not None:
                persisted_draft = await active_draft_store.create_draft(draft)

            payload = {
                "template_reply": True,
                "template_id": matched_template.id,
                "template_version": matched_template.version,
                "category": classification.category,
                "intent": classification.intent,
                "confidence": classification.confidence,
                "decided_by": classification.decided_by,
                "draft_id": str(persisted_draft.id),
                "trace_id": active_trace_id,
            }

            final_job, event = await store.transition_job_state(
                organization_id=current_job.organization_id,
                job_id=current_job.id,
                target_state=JobState.DRAFTED,
                payload=payload,
                result_ref={
                    "draft_id": str(persisted_draft.id),
                    "template_id": matched_template.id,
                },
                message_id=current_job.message_id,
                thread_id=current_job.thread_id,
            )
            self._record_metrics(
                action=action,
                org_id=job.organization_id,
                category=classification.category,
                reason=reason,
                template_id=matched_template.id,
            )

            return GateDecision(
                action=action,
                job=final_job,
                event=event,
                classification=classification,
                reply_required=True,
                retrieval_required=False,
                workflow_hint="template",
                should_embed=False,
                should_retrieve=False,
                should_rerank=False,
                should_generate=False,
                reason=reason,
                rendered_draft=persisted_draft,
                template_result=rendered_result,
            )

        # Outcome 3: Actionable mail with AI Generation (or fallback)
        effective_workflow_hint = "ai"
        effective_cls = classification
        if classification.workflow_hint == "template" and matched_template is None:
            logger.info(
                "No template matched for (%s, %s). Falling back to workflow_hint='ai' (R6.14).",
                classification.category,
                classification.intent,
            )
            effective_cls = replace(classification, workflow_hint="ai")

        target_state = JobState.QUEUED
        if not effective_cls.retrieval_required:
            action = GateAction.PROCEED_NO_RAG
            reason = "retrieval_not_required"
            should_retrieve = False
            should_embed = False
            should_rerank = False
        else:
            action = GateAction.PROCEED_RAG
            reason = "retrieval_required"
            should_retrieve = True
            should_embed = True
            should_rerank = True

        should_generate = True

        payload = {
            "category": effective_cls.category,
            "intent": effective_cls.intent,
            "priority": effective_cls.priority,
            "retrieval_required": effective_cls.retrieval_required,
            "workflow_hint": effective_workflow_hint,
            "confidence": effective_cls.confidence,
            "decided_by": effective_cls.decided_by,
        }

        final_job, event = await store.transition_job_state(
            organization_id=current_job.organization_id,
            job_id=current_job.id,
            target_state=target_state,
            payload=payload,
            message_id=current_job.message_id,
            thread_id=current_job.thread_id,
        )
        self._record_metrics(
            action=action,
            org_id=job.organization_id,
            category=effective_cls.category,
            reason=reason,
        )

        return GateDecision(
            action=action,
            job=final_job,
            event=event,
            classification=effective_cls,
            reply_required=True,
            retrieval_required=effective_cls.retrieval_required,
            workflow_hint=effective_workflow_hint,
            should_embed=should_embed,
            should_retrieve=should_retrieve,
            should_rerank=should_rerank,
            should_generate=should_generate,
            reason=reason,
        )

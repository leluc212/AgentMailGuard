"""Early-exit gate and selective AI routing engine (R6.5, R6.6, design.md §5.3, §8).

Requirements:
- R6.5: IF reply_required = false, THEN THE SYSTEM SHALL transition the job
  directly to COMPLETED and SHALL NOT perform embedding, retrieval, reranking, or generation.
- R6.6: IF retrieval_required = false, THEN THE SYSTEM SHALL skip the hybrid RAG call
  and build context from thread and business data only.
- R18.1–R18.5: Atomic state machine transitions and audit event generation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from packages.db.job import JobStore
from packages.domain.entities import Classification, Job, ProcessingEvent
from packages.domain.state_machine import (
    IllegalStateTransitionError,
    JobState,
    transition_job,
)

logger = logging.getLogger(__name__)


class GateAction(StrEnum):
    """Routing action decided by the early-exit gate."""

    EARLY_EXIT = "early_exit"
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
    """Downstream pipeline coordinator enforcing selective AI execution (R6.5, R6.6)."""

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


class EarlyExitGate:
    """Early-Exit Gate implementing the primary cost governor of the system (R6.5, R6.6).

    Rules:
    1. reply_required == false -> Transition directly to COMPLETED. Zero retrieval, zero generation.
    2. retrieval_required == false -> Transition to QUEUED. Skip hybrid RAG; context is thread+biz.
    3. retrieval_required == true -> Transition to QUEUED. Full hybrid RAG context assembly.
    """

    def __init__(self, job_store: JobStore | None = None) -> None:
        self.job_store = job_store

    def evaluate_decision(
        self,
        job: Job,
        classification: Classification,
        trace_id: str | None = None,
    ) -> GateDecision:
        """Pure in-memory state transition and gate decision evaluation.

        Args:
            job: Current job entity. Must be at NORMALIZED or CLASSIFIED state.
            classification: Classification outcome from triage cascade.
            trace_id: Optional distributed trace identifier.

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

        # 2. Gate Decision branch
        if not classification.reply_required:
            # Case 1: Early Exit -> straight to COMPLETED (R6.5)
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
            return GateDecision(
                action=action,
                job=final_job,
                event=event,
                classification=classification,
                reply_required=False,
                retrieval_required=False,
                workflow_hint=classification.workflow_hint,
                should_embed=False,
                should_retrieve=False,
                should_rerank=False,
                should_generate=False,
                reason=reason,
            )

        # Case 2 & 3: Actionable mail -> transition to QUEUED
        target_state = JobState.QUEUED
        if not classification.retrieval_required:
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

        should_generate = classification.workflow_hint != "none"

        payload = {
            "category": classification.category,
            "intent": classification.intent,
            "priority": classification.priority,
            "retrieval_required": classification.retrieval_required,
            "workflow_hint": classification.workflow_hint,
            "confidence": classification.confidence,
            "decided_by": classification.decided_by,
        }

        final_job, event = transition_job(
            current_job,
            target_state,
            payload=payload,
            trace_id=active_trace_id,
        )

        return GateDecision(
            action=action,
            job=final_job,
            event=event,
            classification=classification,
            reply_required=True,
            retrieval_required=classification.retrieval_required,
            workflow_hint=classification.workflow_hint,
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
    ) -> GateDecision:
        """Evaluate gate and persist state transition and event atomically to JobStore.

        Args:
            job: Current job entity.
            classification: Classification outcome.
            job_store: Optional job store override (defaults to instance job_store).
            trace_id: Optional trace ID.

        Returns:
            GateDecision with updated job and persisted event.
        """
        store = job_store or self.job_store
        if not store:
            raise ValueError("A valid JobStore must be provided for evaluate_and_persist")

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

        # 2. Gate Decision & DB Transition
        if not classification.reply_required:
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
            return GateDecision(
                action=action,
                job=final_job,
                event=event,
                classification=classification,
                reply_required=False,
                retrieval_required=False,
                workflow_hint=classification.workflow_hint,
                should_embed=False,
                should_retrieve=False,
                should_rerank=False,
                should_generate=False,
                reason=reason,
            )

        # Actionable path
        target_state = JobState.QUEUED
        if not classification.retrieval_required:
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

        should_generate = classification.workflow_hint != "none"

        payload = {
            "category": classification.category,
            "intent": classification.intent,
            "priority": classification.priority,
            "retrieval_required": classification.retrieval_required,
            "workflow_hint": classification.workflow_hint,
            "confidence": classification.confidence,
            "decided_by": classification.decided_by,
        }

        final_job, event = await store.transition_job_state(
            organization_id=current_job.organization_id,
            job_id=current_job.id,
            target_state=target_state,
            payload=payload,
            message_id=current_job.message_id,
            thread_id=current_job.thread_id,
        )

        return GateDecision(
            action=action,
            job=final_job,
            event=event,
            classification=classification,
            reply_required=True,
            retrieval_required=classification.retrieval_required,
            workflow_hint=classification.workflow_hint,
            should_embed=should_embed,
            should_retrieve=should_retrieve,
            should_rerank=should_rerank,
            should_generate=should_generate,
            reason=reason,
        )

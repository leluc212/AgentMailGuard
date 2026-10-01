"""AI-worker consumer core (task 4.13a): lane queue -> context -> tier -> draft.

Per job: load job (early exit if already drafted, R19.3) -> load message -> classification
from the envelope snapshot (R7.3) -> ContextBuilder (QUEUED -> CONTEXT_READY) -> a
``context_built`` diagnostics event (R21) -> ComplexityRouter -> DraftingService
(CONTEXT_READY -> GENERATING -> DRAFTED). Failures are routed by ``failure_policy``;
BaseConsumer owns ack/nack, the retry ladder and the DLQ.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError
from packages.broker.envelope import JobEnvelope
from packages.context.builder import ContextBuilder
from packages.context.summarizer import SummarizationResult, ThreadSummarizer
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import JobStore
from packages.db.message import MessageStore
from packages.domain.entities import Classification, ContextPackage
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.router import ComplexityRouter, EscalationReason
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.drafting import DraftingService
from services.ai_worker.failure_policy import (
    DRAFTED_OR_LATER,
    Disposition,
    classify_generation_failure,
)

logger = logging.getLogger(__name__)

DEFAULT_CATEGORY = "general_inquiry"
CONTEXT_BUILT_EVENT = "context_built"
"""``processing_event.event_type`` of the diagnostics recorded after a job's context is built."""
_CLASSIFICATION_FIELDS = frozenset(f.name for f in dataclasses.fields(Classification))


def classification_from_snapshot(snapshot: dict[str, Any]) -> Classification:
    """Rebuild the triage classification carried in the job envelope (R7.3)."""
    values = {k: v for k, v in snapshot.items() if k in _CLASSIFICATION_FIELDS}
    values.setdefault("category", DEFAULT_CATEGORY)
    return Classification(**values)


def context_built_payload(
    context: ContextPackage, summary: SummarizationResult | None
) -> dict[str, Any]:
    """Diagnostics of one context build: the payload of a ``context_built`` event (R21).

    ``rank`` is a chunk's 1-based position in the context handed to the model. A value nothing
    could tell is None (unknown), never guessed: ``retrieval_degraded``, ``retrieval_underfilled``
    and ``rerank_applied`` are the ContextPackage's own fields, None when no retrieval ran, and
    the ``summary_*`` values are None without a summarizer. ``retrieval_vector_error`` is why the
    vector branch failed (the query embedding or the ANN search), None when it did not.
    """
    return {
        "retrieved": [
            {
                "chunk_id": str(chunk.chunk_id),
                "document_id": str(chunk.document_id),
                "rank": rank,
                "rerank_score": chunk.rerank_score,
            }
            for rank, chunk in enumerate(context.retrieved_chunks, start=1)
        ],
        "retrieval_degraded": context.retrieval_degraded,
        "retrieval_underfilled": context.retrieval_underfilled,
        "retrieval_vector_error": context.retrieval_vector_error,
        "rerank_applied": context.rerank_applied,
        "summary_triggered": None if summary is None else summary.summarized,
        "summary_model": None if summary is None else summary.model,
    }


class AIWorkerConsumer(BaseConsumer):
    """Consumes one lane queue ``email.<category>.<priority>`` and drafts each job."""

    def __init__(
        self,
        queue_name: str,
        *,
        job_store: JobStore,
        message_store: MessageStore,
        context_builder: ContextBuilder,
        router: ComplexityRouter,
        drafting: DraftingService,
        summarizer: ThreadSummarizer | None = None,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        super().__init__(
            queue_name=queue_name,
            broker_settings=broker_settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=job_store,
            metrics=metrics,
        )
        self.jobs = job_store
        self.messages = message_store
        self.context_builder = context_builder
        self.router = router
        self.drafting = drafting
        self.summarizer = summarizer

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        """Draft one job; route any failure to retry, dead-letter or drop."""
        try:
            await self._generate(envelope)
        except Exception as exc:
            job_state = (
                await self._current_state(envelope)
                if isinstance(exc, IllegalStateTransitionError)
                else None
            )
            decision = classify_generation_failure(exc, job_state=job_state)
            if decision.disposition is Disposition.ACK_DROP:
                logger.info("Delivery for job %s dropped: %s", envelope.job_id, decision.reason)
                return
            if decision.disposition is Disposition.DEAD_LETTER:
                if isinstance(exc, FatalError):
                    raise
                raise FatalError(decision.reason) from exc
            raise

    async def _generate(self, envelope: JobEnvelope) -> None:
        org_id = envelope.organization_id
        try:
            job_id = UUID(str(envelope.job_id))
        except ValueError as err:
            raise FatalError(f"Envelope job_id {envelope.job_id!r} is not a UUID") from err
        job = await self.jobs.get_job(org_id, job_id)
        if job is None:
            raise FatalError(f"Job {job_id} not found for organization {org_id}")
        if job.state in DRAFTED_OR_LATER:
            logger.info("Job %s already %s; acknowledging without work", job_id, job.state)
            return
        if job.message_id is None:
            raise FatalError(f"Job {job_id} has no message_id")
        message = await self.messages.get_message(org_id, job.message_id)
        if message is None:
            raise FatalError(f"Message {job.message_id} not found for job {job_id}")

        classification = classification_from_snapshot(envelope.classification)
        thread_messages = await self.messages.get_messages_by_thread(org_id, message.thread_id)
        thread_state = None
        summary: SummarizationResult | None = None
        if self.summarizer is not None:
            # Threshold-triggered (R8.2, R8.3): below the threshold this makes no model call.
            summary = await self.summarizer.summarize_thread(
                org_id, message.thread_id, thread_messages
            )
            thread_state = summary.thread_state
        context = await self.context_builder.build_context(
            job,
            message,
            classification,
            thread_messages=thread_messages,
            thread_state=thread_state,
        )
        await self._record_context_built(org_id, job.id, context, summary)
        escalations = await self._escalations_performed(org_id, job.id)
        decision = self.router.route(context, classification, escalations)
        await self.drafting.draft(
            job,
            context,
            category=classification.category,
            escalated_tier=decision.tier if decision.is_escalated else None,
            escalation_reason=str(decision.escalation_reason) if decision.is_escalated else None,
        )

    async def _record_context_built(
        self,
        org_id: UUID | str,
        job_id: UUID | str,
        context: ContextPackage,
        summary: SummarizationResult | None,
    ) -> None:
        """Record the context build's diagnostics on the job's timeline (R21). Never raises.

        Diagnostics only: the draft does not depend on this row, so a failed write is logged and
        the job goes on. A redelivered job builds its context again and records another event;
        readers take the latest.
        """
        payload = context_built_payload(context, summary)
        try:
            await self.jobs.record_event(org_id, job_id, CONTEXT_BUILT_EVENT, payload)
        except Exception:
            logger.warning("context_built event for job %s was not recorded", job_id, exc_info=True)
            return
        logger.info(
            CONTEXT_BUILT_EVENT,
            extra={
                "fields": {
                    "job_id": str(job_id),
                    "retrieved_chunks": len(payload["retrieved"]),
                    "retrieval_degraded": payload["retrieval_degraded"],
                    "rerank_applied": payload["rerank_applied"],
                    "summary_triggered": payload["summary_triggered"],
                }
            },
        )

    async def _escalations_performed(self, org_id: UUID | str, job_id: UUID | str) -> int:
        """Escalations earlier deliveries of this job made (R15.5 cap input).

        Every delivery that reaches generation records its routing on the GENERATING
        transition; a forced single tier is a mode, not an escalation, so it is not counted.
        """
        not_counted = {None, EscalationReason.NONE.value, EscalationReason.SINGLE_TIER_FORCED.value}
        events = await self.jobs.list_events_for_job(org_id, job_id)
        return sum(
            1
            for event in events
            if event.state_to == JobState.GENERATING.value
            and (event.payload or {}).get("escalation_reason") not in not_counted
        )

    async def _current_state(self, envelope: JobEnvelope) -> str | None:
        try:
            job = await self.jobs.get_job(envelope.organization_id, UUID(str(envelope.job_id)))
        except Exception:
            return None
        return job.state if job is not None else None

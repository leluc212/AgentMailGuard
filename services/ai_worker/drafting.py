"""Drafting service: generate one reply for a context-ready job and persist it once.

Requirements: R18.1 (CONTEXT_READY -> GENERATING -> DRAFTED), R16.4 (persist the
draft), R14.9 (exactly one generation per job), R19.3 / R19.7 (a redelivered job that
is already drafted is not generated again), R16.3 (generation errors propagate to the
retry/DLQ path and persist nothing).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from packages.core.settings import ModelPricing
from packages.db.draft_persistence import DraftPersistence
from packages.db.job import JobStore
from packages.domain.entities import ContextPackage, GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.budget import CallBudgetTracker
from packages.llm.drafts import NO_ESCALATION, build_generated_draft
from packages.llm.generator import SinglePassGenerator
from packages.llm.protocol import ModelTier

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

UNKNOWN_CATEGORY = "unknown"
"""Metric label for drafts generated without a classification category."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftingOutcome:
    """The job's draft; ``created`` is False when an earlier delivery already drafted it."""

    draft: GeneratedDraft
    job: Job
    created: bool


class DraftingService:
    """Moves a context-ready job through generation to a persisted draft."""

    def __init__(
        self,
        *,
        generator: SinglePassGenerator,
        job_store: JobStore,
        persistence: DraftPersistence,
        price_table: Mapping[str, ModelPricing],
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self.generator = generator
        self.job_store = job_store
        self.persistence = persistence
        self.price_table = price_table
        self.metrics = metrics

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
        """Generate and persist the job's draft, or return the one already persisted.

        Raises:
            KeyError: If the job does not exist in its organization.
            IllegalStateTransitionError: If the job is neither CONTEXT_READY, GENERATING
                nor already DRAFTED. Raised before any model call is billed.
            UnpersistableDraftError: If the generation result cannot be persisted.
            LLMError: Any generation failure, including ``UnvalidatedDraftError``; the
                job stays GENERATING for the retry ladder and nothing is persisted.
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
                    "Job %s already drafted as %s; skipping generation", job.id, existing.id
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

        result = await self.generator.generate_draft(
            context,
            category=category,
            budget_tracker=budget_tracker,
            escalated_tier=escalated_tier,
            escalation_reason=escalation_reason,
        )
        draft = build_generated_draft(
            result, context, job_id=current.id, price_table=self.price_table
        )
        outcome = await self.persistence.persist_drafted(draft)
        if outcome.created:
            self._count_generated(outcome.draft, category)
            logger.info(
                "draft_persisted",
                extra={
                    "fields": {
                        "draft_id": str(outcome.draft.id),
                        "job_id": str(outcome.job.id),
                        "category": category or UNKNOWN_CATEGORY,
                        "model_tier": outcome.draft.model_tier,
                        "escalation_reason": outcome.draft.escalation_reason,
                        "input_tokens": outcome.draft.input_tokens,
                        "output_tokens": outcome.draft.output_tokens,
                        "cost_estimate": outcome.draft.cost_estimate,
                        "citation_mismatch": outcome.draft.citation_mismatch,
                    }
                },
            )
        return DraftingOutcome(draft=outcome.draft, job=outcome.job, created=outcome.created)

    def _count_generated(self, draft: GeneratedDraft, category: str | None) -> None:
        """Count one generated email and its cost (R21.4, R21.6). Never raises."""
        metrics: Any = self.metrics
        if metrics is None:
            return
        label = category or UNKNOWN_CATEGORY
        tier = draft.model_tier or "unknown"
        try:
            metrics.emails_generated_total.labels(
                organization=str(draft.organization_id), category=label, model_tier=tier
            ).inc()
            if draft.cost_estimate is not None:
                metrics.generated_draft_cost_total.labels(category=label, model_tier=tier).inc(
                    draft.cost_estimate
                )
        except Exception:
            logger.warning("Generated-email metrics emission failed", exc_info=True)

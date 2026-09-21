"""Cascading triage orchestration engine (R6.1, R6.2, R6.7, R6.9, R6.11, design.md §5.3).

Chains Stage 1 (Deterministic Rules) -> Stage 2 (Lightweight ML) -> Stage 3 (Small LLM)
with strict short-circuiting on confidence thresholds, dynamic per-tenant/per-category
threshold resolution, durable PostgreSQL persistence, and review-flagged safe defaults.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.db.classification import ClassificationStore
from packages.domain.entities import Classification, NormalizedMessage
from packages.domain.rules import EmailContext
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.llm_classifier import LLMTriageClassifier
from services.triage_worker.rules import HotReloadableRuleEngine
from services.triage_worker.thresholds import ThresholdManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageExecutionRecord:
    """Telemetry record for a single stage evaluation in the cascade (R6.7)."""

    stage: str  # 'rule' | 'ml' | 'llm' | 'default'
    evaluated: bool
    accepted: bool
    confidence: float | None
    threshold: float
    category: str | None
    latency_ms: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize record to dictionary."""
        return {
            "stage": self.stage,
            "evaluated": self.evaluated,
            "accepted": self.accepted,
            "confidence": self.confidence,
            "threshold": self.threshold,
            "category": self.category,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class CascadeResult:
    """Comprehensive output of the cascading triage evaluation."""

    classification: Classification
    stages_executed: list[StageExecutionRecord]
    total_latency_ms: int
    decided_stage: str  # 'rule' | 'ml' | 'llm' | 'default'
    persisted_id: UUID | None = None


class CascadingTriageEngine:
    """Unified 3-stage cascading triage engine adhering to design.md §5.3."""

    def __init__(
        self,
        rule_engine: HotReloadableRuleEngine | None = None,
        ml_classifier: MLClassifier | None = None,
        llm_classifier: LLMTriageClassifier | None = None,
        threshold_manager: ThresholdManager | None = None,
        classification_store: ClassificationStore | None = None,
    ) -> None:
        self.rule_engine = rule_engine or HotReloadableRuleEngine()
        self.ml_classifier = ml_classifier
        self.llm_classifier = llm_classifier or LLMTriageClassifier()
        self.threshold_manager = threshold_manager or ThresholdManager()
        self.classification_store = classification_store

    def _coerce_context(
        self,
        context: EmailContext | NormalizedMessage | dict[str, Any],
    ) -> tuple[EmailContext, UUID | str | None, UUID | str | None]:
        """Extract EmailContext, organization_id, and message_id from polymorphic input."""
        org_id: UUID | str | None = None
        msg_id: UUID | str | None = None

        if isinstance(context, NormalizedMessage):
            ctx = EmailContext.from_message(context)
            org_id = context.organization_id
            msg_id = context.message_id
        elif isinstance(context, dict):
            ctx = EmailContext.from_dict(context)
            org_id = context.get("organization_id")
            msg_id = context.get("message_id")
        elif isinstance(context, EmailContext):
            ctx = context
        else:
            raise TypeError(f"Unsupported context type for cascading triage: {type(context)}")

        return ctx, org_id, msg_id

    async def triage(
        self,
        context: EmailContext | NormalizedMessage | dict[str, Any],
        organization_id: UUID | str | None = None,
        message_id: UUID | str | None = None,
        persist: bool = False,
    ) -> CascadeResult:
        """Execute the 3-stage triage cascade with early exit on confidence thresholds (R6.1, R6.2).

        Args:
            context: EmailContext, NormalizedMessage, or dict.
            organization_id: Optional tenant ID override (defaults to message.organization_id).
            message_id: Optional message ID override (defaults to message.message_id).
            persist: If True and classification_store is configured, persist result to DB (R6.7).

        Returns:
            CascadeResult containing final Classification and per-stage telemetry.
        """
        start_time = time.perf_counter()
        ctx, ctx_org_id, ctx_msg_id = self._coerce_context(context)
        effective_org_id = organization_id or ctx_org_id
        effective_msg_id = message_id or ctx_msg_id

        stage_records: list[StageExecutionRecord] = []
        final_classification: Classification | None = None

        # =========================================================================
        # Stage 1: Deterministic Rules Engine (~1ms)
        # =========================================================================
        stage1_start = time.perf_counter()
        rule_threshold = self.threshold_manager.get_threshold(
            stage="rule",
            organization_id=effective_org_id,
        )

        try:
            rule_match = self.rule_engine.evaluate(ctx)
            stage1_latency = max(1, int((time.perf_counter() - stage1_start) * 1000))

            if rule_match is not None:
                # Per-category threshold resolution if category is known
                rule_threshold = self.threshold_manager.get_threshold(
                    stage="rule",
                    category=rule_match.category,
                    organization_id=effective_org_id,
                )

                if rule_match.confidence >= rule_threshold:
                    # Accepted at Stage 1! Early exit (R6.2)
                    final_classification = rule_match
                    stage_records.append(
                        StageExecutionRecord(
                            stage="rule",
                            evaluated=True,
                            accepted=True,
                            confidence=rule_match.confidence,
                            threshold=rule_threshold,
                            category=rule_match.category,
                            latency_ms=stage1_latency,
                        )
                    )
                else:
                    # Matched rule but below threshold, proceed to Stage 2
                    stage_records.append(
                        StageExecutionRecord(
                            stage="rule",
                            evaluated=True,
                            accepted=False,
                            confidence=rule_match.confidence,
                            threshold=rule_threshold,
                            category=rule_match.category,
                            latency_ms=stage1_latency,
                        )
                    )
            else:
                # Rule engine abstained
                stage_records.append(
                    StageExecutionRecord(
                        stage="rule",
                        evaluated=True,
                        accepted=False,
                        confidence=None,
                        threshold=rule_threshold,
                        category=None,
                        latency_ms=stage1_latency,
                    )
                )
        except Exception as exc:
            stage1_latency = max(1, int((time.perf_counter() - stage1_start) * 1000))
            logger.warning("Stage 1 Rule evaluation error: %s", exc)
            stage_records.append(
                StageExecutionRecord(
                    stage="rule",
                    evaluated=True,
                    accepted=False,
                    confidence=None,
                    threshold=rule_threshold,
                    category=None,
                    latency_ms=stage1_latency,
                    error=str(exc),
                )
            )

        # =========================================================================
        # Stage 2: Lightweight ML Classifier (~20-50ms)
        # =========================================================================
        if final_classification is None and self.ml_classifier is not None:
            stage2_start = time.perf_counter()
            ml_threshold = self.threshold_manager.get_threshold(
                stage="ml",
                organization_id=effective_org_id,
            )

            try:
                ml_res = self.ml_classifier.classify(ctx)
                stage2_latency = max(1, int((time.perf_counter() - stage2_start) * 1000))

                ml_threshold = self.threshold_manager.get_threshold(
                    stage="ml",
                    category=ml_res.category,
                    organization_id=effective_org_id,
                )

                if ml_res.confidence >= ml_threshold:
                    # Accepted at Stage 2! Early exit (R6.2)
                    final_classification = ml_res
                    stage_records.append(
                        StageExecutionRecord(
                            stage="ml",
                            evaluated=True,
                            accepted=True,
                            confidence=ml_res.confidence,
                            threshold=ml_threshold,
                            category=ml_res.category,
                            latency_ms=stage2_latency,
                        )
                    )
                else:
                    # ML confidence below threshold, advance to Stage 3
                    stage_records.append(
                        StageExecutionRecord(
                            stage="ml",
                            evaluated=True,
                            accepted=False,
                            confidence=ml_res.confidence,
                            threshold=ml_threshold,
                            category=ml_res.category,
                            latency_ms=stage2_latency,
                        )
                    )
            except Exception as exc:
                stage2_latency = max(1, int((time.perf_counter() - stage2_start) * 1000))
                logger.warning("Stage 2 ML evaluation error: %s", exc)
                stage_records.append(
                    StageExecutionRecord(
                        stage="ml",
                        evaluated=True,
                        accepted=False,
                        confidence=None,
                        threshold=ml_threshold,
                        category=None,
                        latency_ms=stage2_latency,
                        error=str(exc),
                    )
                )

        # =========================================================================
        # Stage 3: Small-LLM Fallback Classifier (~200-300ms)
        # =========================================================================
        if final_classification is None and self.llm_classifier is not None:
            stage3_start = time.perf_counter()
            llm_threshold = self.threshold_manager.get_threshold(
                stage="llm",
                organization_id=effective_org_id,
            )

            try:
                llm_res = await self.llm_classifier.classify(ctx)
                stage3_latency = max(1, int((time.perf_counter() - stage3_start) * 1000))

                llm_threshold = self.threshold_manager.get_threshold(
                    stage="llm",
                    category=llm_res.category,
                    organization_id=effective_org_id,
                )

                if llm_res.confidence >= llm_threshold:
                    # Accepted at Stage 3!
                    final_classification = llm_res
                    stage_records.append(
                        StageExecutionRecord(
                            stage="llm",
                            evaluated=True,
                            accepted=True,
                            confidence=llm_res.confidence,
                            threshold=llm_threshold,
                            category=llm_res.category,
                            latency_ms=stage3_latency,
                        )
                    )
                else:
                    stage_records.append(
                        StageExecutionRecord(
                            stage="llm",
                            evaluated=True,
                            accepted=False,
                            confidence=llm_res.confidence,
                            threshold=llm_threshold,
                            category=llm_res.category,
                            latency_ms=stage3_latency,
                        )
                    )
            except Exception as exc:
                stage3_latency = max(1, int((time.perf_counter() - stage3_start) * 1000))
                logger.warning("Stage 3 LLM evaluation error: %s", exc)
                stage_records.append(
                    StageExecutionRecord(
                        stage="llm",
                        evaluated=True,
                        accepted=False,
                        confidence=None,
                        threshold=llm_threshold,
                        category=None,
                        latency_ms=stage3_latency,
                        error=str(exc),
                    )
                )

        # =========================================================================
        # Safe Default Fallback (R6.11)
        # =========================================================================
        total_latency = max(1, int((time.perf_counter() - start_time) * 1000))

        if final_classification is None:
            logger.warning(
                "All triage stages failed or abstained for message %s. Emitting safe default.",
                effective_msg_id,
            )
            raw_fallback: dict[str, Any] = {
                "review_flag": True,
                "stages_attempted": [r.to_dict() for r in stage_records],
                "workflow_hint": "ai",
            }
            final_classification = Classification(
                category="general_inquiry",
                intent="unclassified_fallback",
                priority="normal",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=True,
                confidence=0.0,
                decided_by="default",
                latency_ms=total_latency,
                model=None,
                raw=raw_fallback,
            )
            stage_records.append(
                StageExecutionRecord(
                    stage="default",
                    evaluated=True,
                    accepted=True,
                    confidence=0.0,
                    threshold=0.0,
                    category="general_inquiry",
                    latency_ms=total_latency,
                )
            )

        decided_stage = final_classification.decided_by

        # =========================================================================
        # Persistence (R6.7)
        # =========================================================================
        persisted_id: UUID | None = None
        if persist and self.classification_store is not None:
            if effective_org_id and effective_msg_id:
                try:
                    persisted_row = await self.classification_store.save_classification(
                        organization_id=effective_org_id,
                        message_id=effective_msg_id,
                        classification=final_classification,
                    )
                    persisted_id = persisted_row.id
                except Exception as exc:
                    logger.error(
                        "Failed to persist classification result for message %s: %s",
                        effective_msg_id,
                        exc,
                    )
            else:
                logger.debug("Skipping persistence: missing organization_id or message_id")

        return CascadeResult(
            classification=final_classification,
            stages_executed=stage_records,
            total_latency_ms=total_latency,
            decided_stage=decided_stage,
            persisted_id=persisted_id,
        )

    def triage_sync(
        self,
        context: EmailContext | NormalizedMessage | dict[str, Any],
        organization_id: UUID | str | None = None,
        message_id: UUID | str | None = None,
        persist: bool = False,
    ) -> CascadeResult:
        """Synchronous wrapper for triage(), safe for sync workers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    self.triage(
                        context=context,
                        organization_id=organization_id,
                        message_id=message_id,
                        persist=persist,
                    ),
                )
                return future.result()
        else:
            return asyncio.run(
                self.triage(
                    context=context,
                    organization_id=organization_id,
                    message_id=message_id,
                    persist=persist,
                )
            )

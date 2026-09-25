"""Single-pass draft synthesis generator and orchestration (R14.3, R14.4, R14.6, R15.5).

Implements single-pass orchestration replacing multi-agent planner/critic/writer pipelines
with a single prompt-templated generation call enforcing strict budget tracking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from packages.domain.entities import ContextPackage
from packages.llm.budget import (
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallKind,
)
from packages.llm.profile import AgentProfile, AgentProfileRegistry
from packages.llm.protocol import ChatMessage, LLMProvider, LLMResult, ModelTier
from packages.llm.validation import (
    DraftReplyPayload,
    DraftValidationError,
    UnvalidatedDraftError,
    build_repair_messages,
    validate_draft_payload,
)

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerationResult:
    """Structured generation output and metadata from SinglePassGenerator."""

    content: dict[str, Any]
    profile: AgentProfile
    prompt_version: str
    model: str
    tier: ModelTier
    escalation_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    budget_tracker: CallBudgetTracker = field(default_factory=CallBudgetTracker)
    is_repaired: bool = False
    repair_attempts: int = 0
    validated_payload: DraftReplyPayload | None = None


class SinglePassGenerator:
    """Orchestrates single-pass email draft generation with strict call budgeting (R14.3, R14.4).

    Replaces multi-agent planning/criticism loops with a single structured LLM call
    configured via declarative AgentProfiles (R14.1). Enforces exact-1 generation
    invariant per job (R14.9, R15.5).
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        profile_registry: AgentProfileRegistry,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.profile_registry = profile_registry
        self.metrics = metrics

    async def generate_draft(
        self,
        context: ContextPackage,
        *,
        category: str | None = None,
        profile: AgentProfile | None = None,
        budget_tracker: CallBudgetTracker | None = None,
        escalated_tier: ModelTier | str | None = None,
        escalation_reason: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> GenerationResult:
        """Synthesize draft reply in a single model invocation (R14.3, R14.4, R14.6, R15.5).

        Args:
            context: Assembled ContextPackage for the current message and thread.
            category: Optional classification category to resolve AgentProfile.
            profile: Optional explicit AgentProfile overriding category resolution.
            budget_tracker: Optional CallBudgetTracker for the job lifecycle.
            escalated_tier: Optional escalated ModelTier replacing default profile tier (R15.5).
            escalation_reason: Optional diagnostic justification for model tier escalation.
            temperature: Sampling temperature for generation.
            max_tokens: Maximum completion tokens.

        Returns:
            GenerationResult containing validated content, profile metadata, and usage metrics.

        Raises:
            CallBudgetExceededError: If invocation breaches call ceiling or per-kind limits.
            CallBudgetViolationError: If required generation call was omitted.
            UnvalidatedDraftError: If the payload still fails schema validation after one
                repair retry, or the repair budget is already spent. The job fails into the
                retry/DLQ path and no unvalidated draft is ever returned (R16.3).
            LLMError: If underlying model provider fails.
        """
        # 1. Resolve profile
        resolved_profile = profile or self.profile_registry.resolve_profile(category)

        # 2. Resolve model tier (escalation replaces generation tier, never adds a call - R15.5)
        raw_tier = escalated_tier or resolved_profile.model_tier
        effective_tier = raw_tier if isinstance(raw_tier, ModelTier) else ModelTier(raw_tier)

        # 3. Render prompt text
        prompt_text = self.profile_registry.render_prompt(resolved_profile, context)

        # 4. Build chat message
        messages = [ChatMessage(role="user", content=prompt_text)]

        # 5. Fetch output schema
        schema = self.profile_registry.get_schema(resolved_profile)

        # 6 & 7. Track budget and wrap with BudgetedLLMProvider
        if budget_tracker is not None:
            tracker = budget_tracker
        elif isinstance(self.llm_provider, BudgetedLLMProvider):
            tracker = self.llm_provider.tracker
        else:
            tracker = CallBudgetTracker()

        effective_metrics = self.metrics or getattr(self.llm_provider, "metrics", None)
        if isinstance(self.llm_provider, BudgetedLLMProvider):
            if self.llm_provider.tracker is tracker:
                budgeted = self.llm_provider
            else:
                budgeted = BudgetedLLMProvider(
                    self.llm_provider.provider,
                    tracker=tracker,
                    metrics=effective_metrics,
                )
        else:
            budgeted = BudgetedLLMProvider(
                self.llm_provider,
                tracker=tracker,
                metrics=effective_metrics,
            )

        # 8. Execute generation
        result = await budgeted.generate(
            messages=messages,
            schema=schema,
            tier=effective_tier,
            max_tokens=max_tokens,
            temperature=temperature,
            call_kind=CallKind.GENERATE,
        )

        # 9. Assert generation budget (exactly 1 generation call per job, total <= 4)
        tracker.assert_generation_budget(require_generation=True)

        # 10. Validate the structured output, repairing at most once (R16.2, R16.3)
        attempts: list[LLMResult] = [result]
        try:
            validated_payload = validate_draft_payload(result.content, schema)
            repair_attempts = 0
        except DraftValidationError as initial_error:
            self._record_validation_failure("initial")
            repair_result = await self._repair_draft(
                budgeted=budgeted,
                tracker=tracker,
                messages=messages,
                invalid_content=result.content,
                validation_error=str(initial_error),
                schema=schema,
                tier=effective_tier,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            attempts.append(repair_result)
            repair_attempts = 1
            try:
                validated_payload = validate_draft_payload(repair_result.content, schema)
            except DraftValidationError as repair_error:
                self._record_validation_failure("repair")
                self._record_repair_outcome("failed")
                raise UnvalidatedDraftError(
                    "Draft failed schema validation after one repair retry; "
                    f"failing job into the retry/DLQ path without persisting: {repair_error}"
                ) from repair_error
            self._record_repair_outcome("succeeded")

        # A repair replaces the rejected output; its cost is added to the job, not swapped in.
        final_result = attempts[-1]
        total_input_tokens = sum(attempt.input_tokens for attempt in attempts)
        total_output_tokens = sum(attempt.output_tokens for attempt in attempts)
        total_latency_ms = sum(attempt.latency_ms for attempt in attempts)

        # 11. Record metrics if configured
        if self.metrics:
            tier_label = (
                str(effective_tier.value)
                if hasattr(effective_tier, "value")
                else str(effective_tier)
            )
            self._count_call_directly(budgeted, "generate", result.model)
            if hasattr(self.metrics, "generation_latency_ms"):
                try:
                    self.metrics.generation_latency_ms.labels(
                        model=final_result.model, tier=tier_label
                    ).observe(total_latency_ms)
                except (ValueError, TypeError, AttributeError):
                    self.metrics.generation_latency_ms.observe(total_latency_ms)
            if hasattr(self.metrics, "input_tokens_total"):
                self.metrics.input_tokens_total.labels(
                    model=final_result.model, tier=tier_label
                ).inc(total_input_tokens)
            if hasattr(self.metrics, "output_tokens_total"):
                self.metrics.output_tokens_total.labels(
                    model=final_result.model, tier=tier_label
                ).inc(total_output_tokens)
            if budget_tracker is None:
                tracker.export_metrics(self.metrics)

        # 12. Return GenerationResult
        return GenerationResult(
            content=final_result.content,
            profile=resolved_profile,
            prompt_version=resolved_profile.prompt_version,
            model=final_result.model,
            tier=effective_tier,
            escalation_reason=escalation_reason,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            latency_ms=total_latency_ms,
            budget_tracker=tracker,
            is_repaired=repair_attempts > 0,
            repair_attempts=repair_attempts,
            validated_payload=validated_payload,
        )

    async def _repair_draft(
        self,
        *,
        budgeted: BudgetedLLMProvider,
        tracker: CallBudgetTracker,
        messages: list[ChatMessage],
        invalid_content: Any,
        validation_error: str,
        schema: dict[str, Any] | None,
        tier: ModelTier,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        """Re-ask the model for a schema-conformant payload, once, within budget (R16.3, R14.9).

        Raises:
            UnvalidatedDraftError: If the job has no repair call left to spend. The draft
                stays unvalidated and is never returned, so it can never be persisted.
        """
        try:
            tracker.check_can_call(CallKind.REPAIR)
        except CallBudgetExceededError as budget_error:
            self._record_repair_outcome("failed")
            raise UnvalidatedDraftError(
                "Draft failed schema validation and the repair budget is already spent; "
                f"failing job into the retry/DLQ path without persisting: {budget_error}"
            ) from budget_error

        logger.warning(
            "draft_schema_validation_failed_repairing",
            extra={"validation_error": validation_error, "job_id": tracker.job_id},
        )
        repair_messages = build_repair_messages(
            messages,
            invalid_content,
            validation_error,
            schema,
        )
        repair_result = await budgeted.generate(
            messages=repair_messages,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            call_kind=CallKind.REPAIR,
        )
        self._count_call_directly(budgeted, "repair", repair_result.model)
        return repair_result

    def _count_call_directly(
        self,
        budgeted: BudgetedLLMProvider,
        kind: str,
        model: str,
    ) -> None:
        """Increment llm_calls_total only when the budgeted provider is not already doing it."""
        metrics = self.metrics
        if metrics is None or not hasattr(metrics, "llm_calls_total"):
            return
        if budgeted.metrics is metrics:
            return
        metrics.llm_calls_total.labels(kind=kind, model=model).inc()

    def _record_validation_failure(self, stage: str) -> None:
        """Count a draft schema validation failure by pipeline stage (R16.2, R21.4)."""
        metrics = self.metrics
        if metrics is None or not hasattr(metrics, "draft_validation_failures_total"):
            return
        metrics.draft_validation_failures_total.labels(stage=stage).inc()

    def _record_repair_outcome(self, status: str) -> None:
        """Count the outcome of a schema repair retry (R16.3, R21.4)."""
        metrics = self.metrics
        if metrics is None or not hasattr(metrics, "draft_repairs_total"):
            return
        metrics.draft_repairs_total.labels(status=status).inc()

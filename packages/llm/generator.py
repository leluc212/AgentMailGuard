"""Single-pass draft synthesis generator and orchestration (R14.3, R14.4, R14.6, R15.5).

Implements single-pass orchestration replacing multi-agent planner/critic/writer pipelines
with a single prompt-templated generation call enforcing strict budget tracking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from packages.domain.entities import ContextPackage
from packages.llm.budget import BudgetedLLMProvider, CallBudgetTracker, CallKind
from packages.llm.profile import AgentProfile, AgentProfileRegistry
from packages.llm.protocol import ChatMessage, LLMProvider, ModelTier

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
        escalated_tier: ModelTier | None = None,
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

        if isinstance(self.llm_provider, BudgetedLLMProvider):
            if self.llm_provider.tracker is tracker:
                budgeted = self.llm_provider
            else:
                budgeted = BudgetedLLMProvider(self.llm_provider.provider, tracker=tracker)
        else:
            budgeted = BudgetedLLMProvider(self.llm_provider, tracker=tracker)

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

        # 10. Record metrics if configured
        if self.metrics:
            tier_label = (
                str(effective_tier.value)
                if hasattr(effective_tier, "value")
                else str(effective_tier)
            )
            if hasattr(self.metrics, "llm_calls_total"):
                self.metrics.llm_calls_total.labels(kind="generate", model=result.model).inc()
            if hasattr(self.metrics, "generation_latency_ms"):
                try:
                    self.metrics.generation_latency_ms.labels(
                        model=result.model, tier=tier_label
                    ).observe(result.latency_ms)
                except (ValueError, TypeError, AttributeError):
                    self.metrics.generation_latency_ms.observe(result.latency_ms)
            if hasattr(self.metrics, "input_tokens_total"):
                self.metrics.input_tokens_total.labels(model=result.model, tier=tier_label).inc(
                    result.input_tokens
                )
            if hasattr(self.metrics, "output_tokens_total"):
                self.metrics.output_tokens_total.labels(model=result.model, tier=tier_label).inc(
                    result.output_tokens
                )
            tracker.export_metrics(self.metrics)

        # 11. Return GenerationResult
        return GenerationResult(
            content=result.content,
            profile=resolved_profile,
            prompt_version=resolved_profile.prompt_version,
            model=result.model,
            tier=effective_tier,
            escalation_reason=escalation_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            budget_tracker=tracker,
        )

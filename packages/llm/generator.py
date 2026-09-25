"""Single-pass draft synthesis generator and orchestration (R14.3, R14.4, R14.6, R15.5).

Implements single-pass orchestration replacing multi-agent planner/critic/writer pipelines
with a single prompt-templated generation call enforcing strict budget tracking.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from packages.domain.entities import ContextPackage
from packages.llm.budget import (
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallKind,
)
from packages.llm.citations import CitationVerdict, verify_citations
from packages.llm.profile import AgentProfile, AgentProfileRegistry
from packages.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResult,
    LLMSchemaValidationError,
    ModelTier,
)
from packages.llm.validation import (
    DraftReplyPayload,
    DraftValidationError,
    UnvalidatedDraftError,
    assert_schema_matches_contract,
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
    citation_verdict: CitationVerdict | None = None

    @property
    def citation_mismatch(self) -> bool:
        """True when the draft cited a chunk absent from its context (R16.5)."""
        return self.citation_verdict is not None and self.citation_verdict.mismatch


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
            DraftSchemaContractError: If the resolved profile's output schema declares
                constraints this module cannot enforce. A deployment error, not a model
                mistake: raised before any call is billed and never repaired.
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

        # 5. Fetch output schema and refuse one this module cannot actually enforce
        schema = self.profile_registry.get_schema(resolved_profile)
        assert_schema_matches_contract(schema)

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

        # 8. Execute generation. A provider that cannot parse a structured payload at all
        #    raises LLMSchemaValidationError itself; that is a validation failure too, and it
        #    is the one a repair retry most often fixes, so it must not escape here (R16.2).
        attempts: list[LLMResult] = []
        generate_model = "unknown"
        parse_failure: LLMSchemaValidationError | None = None
        generate_already_counted = False
        try:
            result = await budgeted.generate(
                messages=messages,
                schema=schema,
                tier=effective_tier,
                max_tokens=max_tokens,
                temperature=temperature,
                call_kind=CallKind.GENERATE,
            )
        except (DraftValidationError, UnvalidatedDraftError):
            # Defensive: these are our own subclasses of LLMSchemaValidationError and no
            # provider raises them, but re-raising keeps the clause below from ever treating
            # a validation verdict as a provider parse failure and repairing it twice.
            raise
        except LLMSchemaValidationError as exc:
            parse_failure = exc
            # The model was invoked and billed even though its output was unusable, so the
            # budget must see the call the provider never got to record (R14.9). The
            # provider also aborted before incrementing llm_calls_total, and it is the only
            # thing that normally does, so this call must be counted here or the generate
            # series silently undercounts exactly the failed generations (R14.10).
            tracker.record_call(CallKind.GENERATE, tier=str(effective_tier))
            self._force_count_call(CallKind.GENERATE.value, generate_model)
            generate_already_counted = True
        else:
            attempts.append(result)
            generate_model = result.model

        # 9. Assert generation budget (exactly 1 generation call per job, total <= 4)
        tracker.assert_generation_budget(require_generation=True)

        try:
            # 10. Validate the structured output, repairing at most once (R16.2, R16.3)
            validated_payload, repair_attempts = await self._validate_with_repair(
                budgeted=budgeted,
                tracker=tracker,
                messages=messages,
                schema=schema,
                tier=effective_tier,
                max_tokens=max_tokens,
                temperature=temperature,
                attempts=attempts,
                parse_failure=parse_failure,
            )
        finally:
            # 11. Record metrics on every exit. A job that burned calls and then failed is
            #     precisely the job the per-job budget histogram exists to expose (R14.10).
            self._emit_generation_metrics(
                budgeted=budgeted,
                attempts=attempts,
                tier=effective_tier,
                generate_model=generate_model,
                tracker=tracker,
                export_budget=budget_tracker is None,
                count_generate=not generate_already_counted,
            )

        # Verify citation grounding (R16.5). A mismatch flags the draft; it never fails the
        # job, which is R16.3's job. The label uses the resolved profile when no category was
        # supplied, so it stays bounded by config/agent_profiles.yaml either way.
        citation_verdict = verify_citations(validated_payload.knowledge_chunks, context)
        if citation_verdict.mismatch:
            logger.warning(
                "draft_citation_mismatch",
                extra={
                    "job_id": tracker.job_id,
                    "mismatched_citations": citation_verdict.mismatched,
                    "supplied_chunks": citation_verdict.supplied_count,
                },
            )
        self._record_citation_verdict(
            citation_verdict,
            category=category or resolved_profile.profile,
        )

        total_input, total_output, total_latency = self._totals(attempts)
        final_model = attempts[-1].model if attempts else generate_model

        # 12. Return GenerationResult carrying the validated payload, so a caller that
        #     persists `content` persists exactly what was validated (R16.2).
        return GenerationResult(
            content=validated_payload.model_dump(),
            profile=resolved_profile,
            prompt_version=resolved_profile.prompt_version,
            model=final_model,
            tier=effective_tier,
            escalation_reason=escalation_reason,
            input_tokens=total_input,
            output_tokens=total_output,
            latency_ms=total_latency,
            budget_tracker=tracker,
            is_repaired=repair_attempts > 0,
            repair_attempts=repair_attempts,
            validated_payload=validated_payload,
            citation_verdict=citation_verdict,
        )

    async def _validate_with_repair(
        self,
        *,
        budgeted: BudgetedLLMProvider,
        tracker: CallBudgetTracker,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None,
        tier: ModelTier,
        max_tokens: int,
        temperature: float,
        attempts: list[LLMResult],
        parse_failure: LLMSchemaValidationError | None,
    ) -> tuple[DraftReplyPayload, int]:
        """Validate the generated payload, spending the job's one repair retry if needed.

        Appends any repair attempt to `attempts` so the caller can account for its cost.

        Raises:
            UnvalidatedDraftError: If validation still fails after the repair retry, or the
                repair budget is already spent. No unvalidated payload is ever returned.
        """
        if parse_failure is None:
            try:
                return validate_draft_payload(attempts[-1].content, schema), 0
            except DraftValidationError as initial_error:
                invalid_content: Any = attempts[-1].content
                validation_error = str(initial_error)
        else:
            # The provider could not parse its own response; repair from the raw text.
            invalid_content = parse_failure.raw_content or str(parse_failure)
            validation_error = str(parse_failure)

        self._record_validation_failure("initial")
        try:
            repair_result = await self._repair_draft(
                budgeted=budgeted,
                tracker=tracker,
                messages=messages,
                invalid_content=invalid_content,
                validation_error=validation_error,
                schema=schema,
                tier=tier,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except (DraftValidationError, UnvalidatedDraftError):
            raise
        except LLMSchemaValidationError as repair_parse_error:
            # The repair response was itself unparseable — the common double failure, since
            # an endpoint that ignores the schema directive ignores it twice and a token
            # truncation recurs at the same limit. It is a validation failure like any other,
            # so it fails as UnvalidatedDraftError rather than leaking the provider's type.
            # _repair_draft already counted the failed repair; only the stage is missing.
            self._record_validation_failure("repair")
            raise UnvalidatedDraftError(
                "Repair retry returned an unparseable payload; failing job into the "
                f"retry/DLQ path without persisting: {repair_parse_error}"
            ) from repair_parse_error

        attempts.append(repair_result)
        try:
            payload = validate_draft_payload(repair_result.content, schema)
        except DraftValidationError as repair_error:
            self._record_validation_failure("repair")
            self._record_repair_outcome("failed")
            raise UnvalidatedDraftError(
                "Draft failed schema validation after one repair retry; "
                f"failing job into the retry/DLQ path without persisting: {repair_error}"
            ) from repair_error
        self._record_repair_outcome("succeeded")
        return payload, 1

    @staticmethod
    def _totals(attempts: list[LLMResult]) -> tuple[int, int, int]:
        """Sum tokens and latency across every model attempt the job paid for."""
        return (
            sum(attempt.input_tokens for attempt in attempts),
            sum(attempt.output_tokens for attempt in attempts),
            sum(attempt.latency_ms for attempt in attempts),
        )

    def _emit_generation_metrics(
        self,
        *,
        budgeted: BudgetedLLMProvider,
        attempts: list[LLMResult],
        tier: ModelTier,
        generate_model: str,
        tracker: CallBudgetTracker,
        export_budget: bool,
        count_generate: bool = True,
    ) -> None:
        """Emit generation cost and latency metrics, on the success and failure paths alike.

        Never raises. This runs in a `finally`, so an exception escaping here would replace
        the exception that actually failed the job — a caller would then retry for the wrong
        reason and lose the signal that the draft was never valid.
        """
        if self.metrics is None:
            return

        tier_label = str(tier.value) if hasattr(tier, "value") else str(tier)
        total_input, total_output, total_latency = self._totals(attempts)
        model_label = attempts[-1].model if attempts else generate_model

        # Each emission is guarded on its own: one misconfigured collector must not cost the
        # job every metric after it, least of all the per-job budget export (R14.10).
        if count_generate:
            self._guarded(lambda: self._count_call_directly(budgeted, "generate", generate_model))

        # Only observe cost and latency for attempts that actually returned. A call the
        # provider aborted has unknown timings, and observing a fabricated 0 ms would drag
        # the latency percentiles down — making failures improve the SLO.
        if attempts:
            if hasattr(self.metrics, "generation_latency_ms"):
                self._guarded(lambda: self._observe_latency(model_label, tier_label, total_latency))
            if hasattr(self.metrics, "input_tokens_total"):
                self._guarded(
                    lambda: self.metrics.input_tokens_total.labels(  # type: ignore[union-attr]
                        model=model_label, tier=tier_label
                    ).inc(total_input)
                )
            if hasattr(self.metrics, "output_tokens_total"):
                self._guarded(
                    lambda: self.metrics.output_tokens_total.labels(  # type: ignore[union-attr]
                        model=model_label, tier=tier_label
                    ).inc(total_output)
                )
        if export_budget:
            self._guarded(lambda: tracker.export_metrics(self.metrics))

    def _observe_latency(self, model_label: str, tier_label: str, total_latency: int) -> None:
        """Observe generation latency, falling back to an unlabelled histogram."""
        assert self.metrics is not None
        try:
            self.metrics.generation_latency_ms.labels(model=model_label, tier=tier_label).observe(
                total_latency
            )
        except (ValueError, TypeError, AttributeError):
            self.metrics.generation_latency_ms.observe(total_latency)

    def _guarded(self, emit: Callable[[], Any]) -> None:
        """Run one metric emission; never raise.

        `_emit_generation_metrics` runs inside a `finally`, so an exception escaping any
        emission would replace the exception that actually failed the job.
        """
        try:
            emit()
        except Exception:
            logger.exception("generation_metric_emission_failed")

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
        try:
            repair_result = await budgeted.generate(
                messages=repair_messages,
                schema=schema,
                tier=tier,
                max_tokens=max_tokens,
                temperature=temperature,
                call_kind=CallKind.REPAIR,
            )
        except LLMSchemaValidationError:
            # The model produced a completion, it just wasn't parseable — so this call was
            # billed and must appear in the budget and the call counter, exactly as the
            # generate leg does. check_can_call(REPAIR) passed above, so this cannot exceed.
            tracker.record_call(CallKind.REPAIR, tier=str(tier))
            self._force_count_call(CallKind.REPAIR.value, "unknown")
            self._record_repair_outcome("failed")
            raise
        except Exception:
            # A transport error or timeout is not evidence that anything was generated, so
            # it is not recorded as a billed call — but the repair did fail, and an operator
            # counting failed repairs needs those too.
            self._record_repair_outcome("failed")
            raise
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

    def _force_count_call(self, kind: str, model: str) -> None:
        """Increment llm_calls_total for a call the provider aborted before recording."""
        metrics = self.metrics
        if metrics is None or not hasattr(metrics, "llm_calls_total"):
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

    def _record_citation_verdict(self, verdict: CitationVerdict, category: str) -> None:
        """Count the grounding check for this draft (R16.5, R21.4).

        Every schema-valid draft increments the verified counter, including one that cited
        nothing, so the denominator is "drafts that could have cited" and the exported ratio
        is the share of drafts carrying at least one ungrounded citation.
        """
        metrics = self.metrics
        if metrics is None:
            return
        if hasattr(metrics, "citations_verified_total"):
            metrics.citations_verified_total.labels(category=category).inc()
        if verdict.mismatch and hasattr(metrics, "citation_mismatches_total"):
            metrics.citation_mismatches_total.labels(category=category).inc()

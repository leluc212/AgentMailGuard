"""Complexity router data structures, action heuristics, and ComplexityRouter engine.

References: R15.1-R15.6, design.md §5.7.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from packages.core.settings import ComplexityRouterSettings, LLMTiersSettings
from packages.knowledge.token_counter import TokenCounter
from packages.llm.protocol import ModelTier

if TYPE_CHECKING:
    from packages.domain.entities import Candidate, Classification, ContextPackage
    from packages.llm.profile import AgentProfile


class EscalationReason(StrEnum):
    """Categorical reasons for escalating to a higher capability model tier (R15.3, R15.6)."""

    NONE = "none"
    LOW_CLASSIFICATION_CONFIDENCE = "low_classification_confidence"
    COMPLEX_THREAD = "complex_thread"
    INSUFFICIENT_RETRIEVAL_EVIDENCE = "insufficient_retrieval_evidence"
    MULTIPLE_REQUESTED_ACTIONS = "multiple_requested_actions"
    OVERSIZED_CONTEXT = "oversized_context"
    SINGLE_TIER_FORCED = "single_tier_forced"


@dataclass(frozen=True)
class RoutingDecision:
    """Immutable outcome of complexity routing for model selection (R15.4, design.md §5.7)."""

    tier: ModelTier
    model: str = ""
    is_escalated: bool = False
    escalation_reason: EscalationReason = EscalationReason.NONE
    details: dict[str, Any] = field(default_factory=dict)


_POLITE_CLOSINGS_PATTERN = re.compile(
    r"\b(have\s+a\s+(?:great|good|nice|wonderful)|thank\s+you|thanks|best\s+regards|warm\s+regards|kind\s+regards|sincerely)\b",
    re.IGNORECASE,
)

_TRANSITION_PATTERN = re.compile(
    r"\b(additionally|also|furthermore|secondly|in\s+addition|as\s+well\s+as)\b",
    re.IGNORECASE,
)

_ACTION_VERBS_PATTERN = re.compile(
    r"\b(send|call|update|reset|cancel|refund|delete|provide|check|confirm|review|verify|resend|forward|attach|fix|help|contact|notify|process|issue|change|remove|add)\b",
    re.IGNORECASE,
)

_DIRECTIVE_WORDS_PATTERN = re.compile(
    r"\b(please|kindly|could\s+you|would\s+you|can\s+you)\b",
    re.IGNORECASE,
)

_LEADING_ACTION_VERB_PATTERN = re.compile(
    rf"^\s*(?:{_ACTION_VERBS_PATTERN.pattern})",
    re.IGNORECASE,
)

_LIST_ITEM_LINE_PATTERN = re.compile(
    r"^\s*(?:(?:[1-9]|\d{2})[\.\)]|[\-\*\•])\s+.*$",
    re.MULTILINE,
)

_LIST_HEADER_PATTERN = re.compile(r"(?i)\bplease(\s+take\s+care\s+of)?:\s*")


def _normalize_inline_lists(text: str) -> str:
    """Normalize inline bulleted or numbered items following a colon into distinct lines."""
    if re.search(r"[:;]\s*[\-\*\•]\s+", text):
        text = re.sub(r"[:;]\s*([\-\*\•])\s+", r":\n\1 ", text)
        text = re.sub(r"(?<=\S)\s+([\-\*\•])\s+", r"\n\1 ", text)

    if re.search(r"[:;]\s*(?:[1-9]|\d{2})[\.\)]\s+", text):
        text = re.sub(r"[:;]\s*((?:[1-9]|\d{2})[\.\)])\s+", r":\n\1 ", text)
        text = re.sub(r"(?<=\S)\s+((?:[1-9]|\d{2})[\.\)])\s+", r"\n\1 ", text)

    return text


def count_requested_actions(text: str) -> int:
    """Count discrete requested actions or directives in email text (R15.3).

    Uses a heuristic combining question marks, numbered/bullet list items, and transition
    directives to estimate how many discrete tasks the email asks the agent to perform.

    Args:
        text: Input email body text.

    Returns:
        Non-negative integer count of requested actions.
    """
    if not text or not text.strip():
        return 0

    normalized_text = _normalize_inline_lists(text)

    # 1. Count list item lines (numbered or bullets)
    list_items = _LIST_ITEM_LINE_PATTERN.findall(normalized_text)
    list_items_count = len(list_items)

    # Strip list item lines completely to avoid leaking verbs/directives into prose analysis
    cleaned_text = _LIST_ITEM_LINE_PATTERN.sub("", normalized_text)
    cleaned_text = _LIST_HEADER_PATTERN.sub("", cleaned_text)

    # 2. Split non-list text into sentences/clauses
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", cleaned_text) if s.strip()]

    non_list_actions = 0
    for s in sentences:
        # Count questions
        q_count = len(re.findall(r"\?+", s))
        if q_count > 0:
            non_list_actions += q_count
            continue

        # Strip polite closings / gratitude before checking for directives/verbs
        s_clean = _POLITE_CLOSINGS_PATTERN.sub("", s).strip()
        if not s_clean:
            continue

        has_directive = bool(_DIRECTIVE_WORDS_PATTERN.search(s_clean))
        has_transition = bool(_TRANSITION_PATTERN.search(s_clean))
        has_action_verb = bool(_ACTION_VERBS_PATTERN.search(s_clean))
        has_leading_action_verb = bool(_LEADING_ACTION_VERB_PATTERN.match(s_clean))

        if ((has_directive or has_transition) and has_action_verb) or has_leading_action_verb:
            non_list_actions += 1

    total = list_items_count + non_list_actions
    return max(0, total)


class ComplexityRouter:
    """Evaluates context package and triage classification to route model tier (R15.1-R15.6).

    Implements a model cascade strategy directing ~90% routine traffic to fast/routine models,
    while deterministically escalating complex requests to high-capability models based on
    5 configurable threshold triggers (R15.1, R15.2, R15.3).
    """

    def __init__(
        self,
        settings: ComplexityRouterSettings | None = None,
        token_counter: TokenCounter | None = None,
        metrics: Any | None = None,
        *,
        tiers_settings: LLMTiersSettings | None = None,
    ) -> None:
        self.settings = settings or ComplexityRouterSettings()
        self.tiers_settings = tiers_settings
        self.token_counter = token_counter or TokenCounter()
        self.metrics = metrics

    def _resolve_tier(self, tier_name: str | ModelTier) -> ModelTier:
        """Map tier name string or enum to ModelTier."""
        if isinstance(tier_name, ModelTier):
            return tier_name
        normalized = str(tier_name).strip().lower()
        if normalized == "routine":
            return ModelTier.ROUTINE
        if normalized == "high_capability":
            return ModelTier.HIGH_CAPABILITY
        if normalized == "fast":
            return ModelTier.FAST
        if normalized == "strong":
            return ModelTier.STRONG
        if normalized == "fallback":
            return ModelTier.FALLBACK
        try:
            return ModelTier(normalized)
        except ValueError:
            return ModelTier.ROUTINE

    def _resolve_model(self, tier: ModelTier) -> str:
        """Look up concrete model name from tiers_settings."""
        if self.tiers_settings is not None:
            if tier in (ModelTier.ROUTINE, ModelTier.FAST):
                model = getattr(self.tiers_settings, "routine_model", None) or getattr(
                    self.tiers_settings, "fast_model", ""
                )
                return str(model or "")
            if tier in (ModelTier.HIGH_CAPABILITY, ModelTier.STRONG):
                model = getattr(self.tiers_settings, "high_capability_model", None) or getattr(
                    self.tiers_settings, "strong_model", ""
                )
                return str(model or "")
            if tier == ModelTier.FALLBACK:
                return str(getattr(self.tiers_settings, "fallback_model", "") or "")
        return ""

    @staticmethod
    def _extract_chunk_score(chunk: Candidate) -> float:
        """Extract primary relevance score from retrieved chunk candidate."""
        if hasattr(chunk, "rerank_score") and chunk.rerank_score is not None:
            return float(chunk.rerank_score)
        if hasattr(chunk, "fused_score") and chunk.fused_score is not None:
            return float(chunk.fused_score)
        if hasattr(chunk, "vector_score") and chunk.vector_score is not None:
            return float(chunk.vector_score)
        if hasattr(chunk, "lexical_score") and chunk.lexical_score is not None:
            return float(chunk.lexical_score)
        metadata = getattr(chunk, "metadata", None)
        if isinstance(metadata, dict):
            raw_score = metadata.get("score", 0.0)
            try:
                return float(raw_score)
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    def _record_escalation_metric(self, reason: EscalationReason, tier: ModelTier) -> None:
        """Record Prometheus escalation counter metric if instrument is available."""
        if self.metrics is not None and hasattr(self.metrics, "model_escalations_total"):
            with contextlib.suppress(Exception):
                self.metrics.model_escalations_total.labels(
                    reason=reason.value, tier=tier.value
                ).inc()

    def route(
        self,
        context: ContextPackage,
        classification: Classification | None = None,
        escalations_performed: int = 0,
        *,
        profile: AgentProfile | None = None,
    ) -> RoutingDecision:
        """Route incoming context package to appropriate model capability tier (R15.1-R15.6).

        Args:
            context: Assembled generation context package.
            classification: Optional classification output from triage engine.
            profile: Optional agent profile specializing response.
            escalations_performed: Count of escalations already performed for this job.

        Returns:
            RoutingDecision containing selected tier, model identifier, and diagnostic details.
        """
        # 1. Disabled router -> routine tier, no escalation
        if not self.settings.enabled:
            target_tier = self._resolve_tier(self.settings.default_tier)
            return RoutingDecision(
                tier=target_tier,
                model=self._resolve_model(target_tier),
                is_escalated=False,
                escalation_reason=EscalationReason.NONE,
                details={"router_enabled": False},
            )

        # 2. Forced single tier (H4 ablation comparison - R15.6)
        if self.settings.force_single_tier:
            target_tier = self._resolve_tier(self.settings.single_tier_override)
            self._record_escalation_metric(EscalationReason.SINGLE_TIER_FORCED, target_tier)
            return RoutingDecision(
                tier=target_tier,
                model=self._resolve_model(target_tier),
                is_escalated=True,
                escalation_reason=EscalationReason.SINGLE_TIER_FORCED,
                details={"forced": True},
            )

        # 3. Max escalations cap per job (R15.5)
        if escalations_performed >= self.settings.max_escalations_per_job:
            target_tier = self._resolve_tier(self.settings.default_tier)
            return RoutingDecision(
                tier=target_tier,
                model=self._resolve_model(target_tier),
                is_escalated=False,
                escalation_reason=EscalationReason.NONE,
                details={
                    "escalation_suppressed": True,
                    "escalations_performed": escalations_performed,
                },
            )

        # 4. Evaluate triggers in deterministic order (R15.3)
        escalated_tier = self._resolve_tier(self.settings.escalated_tier)

        # Trigger 1: Low classification confidence
        if (
            classification is not None
            and classification.confidence < self.settings.confidence_threshold
        ):
            reason = EscalationReason.LOW_CLASSIFICATION_CONFIDENCE
            self._record_escalation_metric(reason, escalated_tier)
            return RoutingDecision(
                tier=escalated_tier,
                model=self._resolve_model(escalated_tier),
                is_escalated=True,
                escalation_reason=reason,
                details={
                    "trigger": "low_classification_confidence",
                    "confidence": classification.confidence,
                    "threshold": self.settings.confidence_threshold,
                },
            )

        # Trigger 2: Complex thread (message count or token volume)
        message_count = len(context.recent_messages)
        thread_tokens = self.token_counter.count_tokens(context.thread_summary or "")
        for m in context.recent_messages:
            body = getattr(m, "body_text", "") or getattr(m, "body_text_clean", "") or ""
            if body:
                thread_tokens += self.token_counter.count_tokens(body)

        if (
            message_count >= self.settings.thread_messages_threshold
            or thread_tokens >= self.settings.thread_tokens_threshold
        ):
            reason = EscalationReason.COMPLEX_THREAD
            self._record_escalation_metric(reason, escalated_tier)
            return RoutingDecision(
                tier=escalated_tier,
                model=self._resolve_model(escalated_tier),
                is_escalated=True,
                escalation_reason=reason,
                details={
                    "trigger": "complex_thread",
                    "message_count": message_count,
                    "messages_threshold": self.settings.thread_messages_threshold,
                    "thread_tokens": thread_tokens,
                    "tokens_threshold": self.settings.thread_tokens_threshold,
                },
            )

        # Trigger 3: Insufficient retrieval evidence
        if classification is not None and classification.retrieval_required:
            if len(context.retrieved_chunks) == 0:
                reason = EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE
                self._record_escalation_metric(reason, escalated_tier)
                return RoutingDecision(
                    tier=escalated_tier,
                    model=self._resolve_model(escalated_tier),
                    is_escalated=True,
                    escalation_reason=reason,
                    details={
                        "trigger": "insufficient_retrieval_evidence",
                        "retrieved_chunks_count": 0,
                        "min_required": self.settings.min_retrieved_chunks,
                    },
                )

            qualifying_chunks = sum(
                1
                for c in context.retrieved_chunks
                if self._extract_chunk_score(c) >= self.settings.min_relevance_score
            )
            if qualifying_chunks < self.settings.min_retrieved_chunks:
                reason = EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE
                self._record_escalation_metric(reason, escalated_tier)
                return RoutingDecision(
                    tier=escalated_tier,
                    model=self._resolve_model(escalated_tier),
                    is_escalated=True,
                    escalation_reason=reason,
                    details={
                        "trigger": "insufficient_retrieval_evidence",
                        "qualifying_chunks": qualifying_chunks,
                        "min_required": self.settings.min_retrieved_chunks,
                        "min_relevance_score": self.settings.min_relevance_score,
                    },
                )

        # Trigger 4: Multiple requested actions
        curr_msg = context.current_message
        body = (
            getattr(curr_msg, "body_text_clean", "")
            or getattr(curr_msg, "body_text", "")
            or getattr(curr_msg, "body_html", "")
            or ""
        )
        actions = count_requested_actions(body)
        if actions >= self.settings.multiple_actions_threshold:
            reason = EscalationReason.MULTIPLE_REQUESTED_ACTIONS
            self._record_escalation_metric(reason, escalated_tier)
            return RoutingDecision(
                tier=escalated_tier,
                model=self._resolve_model(escalated_tier),
                is_escalated=True,
                escalation_reason=reason,
                details={
                    "trigger": "multiple_requested_actions",
                    "actions_count": actions,
                    "threshold": self.settings.multiple_actions_threshold,
                },
            )

        # Trigger 5: Oversized context
        if hasattr(context, "get_ordered_sections"):
            sections = context.get_ordered_sections()
            total_context_tokens = sum(
                self.token_counter.count_tokens(sec_text) for _, sec_text in sections if sec_text
            )
        else:
            total_context_tokens = 0

        if total_context_tokens > self.settings.context_tokens_threshold:
            reason = EscalationReason.OVERSIZED_CONTEXT
            self._record_escalation_metric(reason, escalated_tier)
            return RoutingDecision(
                tier=escalated_tier,
                model=self._resolve_model(escalated_tier),
                is_escalated=True,
                escalation_reason=reason,
                details={
                    "trigger": "oversized_context",
                    "total_context_tokens": total_context_tokens,
                    "threshold": self.settings.context_tokens_threshold,
                },
            )

        # 6. Default route (R15.2)
        default_tier = self._resolve_tier(self.settings.default_tier)
        return RoutingDecision(
            tier=default_tier,
            model=self._resolve_model(default_tier),
            is_escalated=False,
            escalation_reason=EscalationReason.NONE,
            details={"default": True},
        )

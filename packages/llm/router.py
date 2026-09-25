"""Complexity router data structures and action heuristics (R15.3, R15.4, R15.6, design.md §5.7)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from packages.llm.protocol import ModelTier


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

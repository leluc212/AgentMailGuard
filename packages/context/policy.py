"""Conversation summarization policy and threshold evaluation (R8.2, R8.3, R8.4, design.md §5.4).

Evaluates whether an email conversation thread should be summarized or supplied verbatim.
Enforces:
- R8.2: Short threads below thresholds are supplied verbatim without summarization.
- R8.3: Threshold-triggered summarization based on message count or estimated context tokens.
- R8.4: Avoiding regeneration on every message via summarized_through_message_id tracking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from packages.core.settings import SummarizationSettings
from packages.domain.entities import NormalizedMessage, ThreadState
from packages.knowledge.token_counter import TokenCounter

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


THREAD_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": "Short canonical topic or title of the email thread.",
        },
        "current_intent": {
            "type": "string",
            "description": "The customer's active intent or goal in the thread.",
        },
        "summary": {
            "type": "string",
            "description": (
                "Concise summary of conversational progress, key facts, and current status."
            ),
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Unresolved questions, pending action items, or requests for clarification."
            ),
        },
        "resolved_items": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Key points, facts, or questions that have been resolved or agreed upon."
            ),
        },
    },
    "required": ["topic", "current_intent", "summary", "open_questions", "resolved_items"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SummarizationDecision:
    """Decision outcome determining whether summarization should execute."""

    should_summarize: bool
    reason: str
    message_count: int
    estimated_tokens: int
    messages_to_summarize: list[NormalizedMessage] = field(default_factory=list)
    latest_message_id: UUID | None = None


class SummarizationPolicy:
    """Evaluates thread summarization policy according to configured thresholds (R8.2–R8.4)."""

    def __init__(
        self,
        settings: SummarizationSettings,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.settings = settings
        self.token_counter = token_counter or TokenCounter()

    def estimate_context_tokens(self, messages: list[NormalizedMessage]) -> int:
        """Estimate the token footprint of messages in a thread."""
        total = 0
        for msg in messages:
            content = msg.body_text_clean or msg.body_text or msg.snippet
            header = f"From: {msg.sender.email}\nSubject: {msg.subject}\n"
            total += self.token_counter.count_tokens(f"{header}{content}\n")
        return total

    def evaluate(
        self,
        messages: list[NormalizedMessage],
        current_state: ThreadState | None = None,
    ) -> SummarizationDecision:
        """Evaluate if thread needs summarization based on thresholds and state.

        Returns SummarizationDecision with should_summarize and diagnostic reason.
        """
        if not messages:
            return SummarizationDecision(
                should_summarize=False,
                reason="no_messages",
                message_count=0,
                estimated_tokens=0,
                messages_to_summarize=[],
                latest_message_id=None,
            )

        # Order chronologically
        sorted_messages = sorted(messages, key=lambda m: m.received_at)
        latest_msg_id = _to_uuid(sorted_messages[-1].message_id)
        count = len(sorted_messages)
        est_tokens = self.estimate_context_tokens(sorted_messages)

        count_triggered = count > self.settings.min_messages_threshold
        tokens_triggered = est_tokens > self.settings.context_token_threshold

        # R8.2: Short thread below thresholds -> supply verbatim without summarization
        if not (count_triggered or tokens_triggered):
            return SummarizationDecision(
                should_summarize=False,
                reason="below_threshold",
                message_count=count,
                estimated_tokens=est_tokens,
                messages_to_summarize=sorted_messages,
                latest_message_id=latest_msg_id,
            )

        # R8.4: Do not regenerate if already summarized through latest message
        if (
            current_state is not None
            and current_state.summary
            and current_state.summarized_through_message_id == latest_msg_id
        ):
            return SummarizationDecision(
                should_summarize=False,
                reason="already_summarized",
                message_count=count,
                estimated_tokens=est_tokens,
                messages_to_summarize=sorted_messages,
                latest_message_id=latest_msg_id,
            )

        # R8.4 / design §5.4 LAG: an existing summary is refreshed only once more than
        # `resummarize_lag_messages` messages arrived after it; the verbatim window covers
        # the newer ones until then. An unknown summarized-through id forces a refresh.
        if current_state is not None and current_state.summary:
            ids = [_to_uuid(m.message_id) for m in sorted_messages]
            through = current_state.summarized_through_message_id
            if through is not None and through in ids:
                newer = len(ids) - 1 - ids.index(through)
                if newer <= self.settings.resummarize_lag_messages:
                    return SummarizationDecision(
                        should_summarize=False,
                        reason="within_lag",
                        message_count=count,
                        estimated_tokens=est_tokens,
                        messages_to_summarize=sorted_messages,
                        latest_message_id=latest_msg_id,
                    )

        # R8.3: Threshold exceeded and new messages exist -> trigger summarization
        reason = (
            "message_count_threshold_exceeded" if count_triggered else "token_threshold_exceeded"
        )

        return SummarizationDecision(
            should_summarize=True,
            reason=reason,
            message_count=count,
            estimated_tokens=est_tokens,
            messages_to_summarize=sorted_messages,
            latest_message_id=latest_msg_id,
        )

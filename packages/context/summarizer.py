"""Thread summarizer orchestrating LLM summarization and state persistence (R8.2–R8.4).

Ensures:
- R8.2: Short threads below thresholds trigger zero LLM calls.
- R8.3: Threshold-triggered summarization extracts topic, intent, summary, q[], res[],
  on the configured summarizer model, or the FAST tier when none is configured.
- R8.4: Saves summarized_through_message_id to avoid regenerating summary on every message.
- R8.6: Thread state versioning and persistence via ThreadStateStore.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.context.policy import (
    THREAD_SUMMARY_SCHEMA,
    SummarizationDecision,
    SummarizationPolicy,
)
from packages.core.settings import SummarizationSettings
from packages.db.thread_state import ThreadStateStore
from packages.domain.entities import NormalizedMessage, ThreadState
from packages.knowledge.token_counter import TokenCounter
from packages.llm.protocol import ChatMessage, LLMProvider, ModelTier

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class SummarizationResult:
    """Outcome of thread summarization execution.

    ``model`` names the model the provider reports it used; it is None when nothing was
    summarized.
    """

    summarized: bool
    thread_state: ThreadState | None
    verbatim_messages: list[NormalizedMessage]
    decision: SummarizationDecision
    model: str | None = None


class ThreadSummarizer:
    """Orchestrates conversation summarization policy, LLM invocation, and state persistence."""

    def __init__(
        self,
        llm: LLMProvider,
        store: ThreadStateStore,
        settings: SummarizationSettings,
        policy: SummarizationPolicy | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.settings = settings
        # None (unset or blank in the environment) leaves the FAST tier model in charge (R8.3)
        self.model = settings.summarizer_model
        self.token_counter = token_counter or TokenCounter()
        self.policy = policy or SummarizationPolicy(settings, self.token_counter)

    def _format_conversation_for_summary(
        self,
        messages: list[NormalizedMessage],
        current_state: ThreadState | None = None,
    ) -> list[ChatMessage]:
        """Build chat messages for LLM summarization prompt."""
        system_prompt = (
            "You are an enterprise email conversation summarizer. Analyze the provided "
            "email thread and extract structured conversation state. Be concise and factual.\n"
            "Return: topic, current_intent, summary, open_questions (unresolved inquiries), "
            "and resolved_items (agreed facts or answered questions)."
        )

        blocks: list[str] = []
        if current_state and current_state.summary:
            blocks.append(f"PREVIOUS SUMMARY:\n{current_state.summary}\n")
            if current_state.resolved_items:
                blocks.append(f"PREVIOUS RESOLVED: {', '.join(current_state.resolved_items)}\n")
            if current_state.open_questions:
                blocks.append(f"PREVIOUS OPEN: {', '.join(current_state.open_questions)}\n")

        blocks.append("CONVERSATION MESSAGES:")
        for idx, msg in enumerate(messages, start=1):
            sender = msg.sender.email if msg.sender else "unknown"
            body = msg.body_text_clean or msg.body_text or msg.snippet
            blocks.append(f"--- Message {idx} from {sender} at {msg.received_at} ---\n{body}")

        user_content = "\n\n".join(blocks)

        return [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_content),
        ]

    async def summarize_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
        messages: list[NormalizedMessage],
        current_state: ThreadState | None = None,
    ) -> SummarizationResult:
        """Evaluate summarization policy and conditionally generate/persist thread state.

        Asserts in tests that a short thread triggers zero summarization calls (R8.2).
        """
        org_u = _to_uuid(organization_id)
        thread_u = _to_uuid(thread_id)

        # 1. Fetch current state if not passed
        if current_state is None:
            current_state = await self.store.get(org_u, thread_u)

        # 2. Evaluate policy
        decision = self.policy.evaluate(messages=messages, current_state=current_state)

        # 3. If policy says do not summarize -> return verbatim messages with 0 LLM calls
        if not decision.should_summarize:
            return SummarizationResult(
                summarized=False,
                thread_state=current_state,
                verbatim_messages=messages,
                decision=decision,
            )

        # 4. Generate structured summary on the configured model, else the fast LLM tier (R8.3)
        prompt_messages = self._format_conversation_for_summary(
            messages=decision.messages_to_summarize,
            current_state=current_state,
        )

        params: dict[str, Any] = {"model": self.model} if self.model else {}
        llm_result = await self.llm.generate(
            messages=prompt_messages,
            schema=THREAD_SUMMARY_SCHEMA,
            tier=ModelTier.FAST,
            temperature=0.0,
            **params,
        )

        payload = llm_result.content
        summary_text = str(payload.get("summary", ""))
        topic_text = str(payload.get("topic", ""))
        intent_text = str(payload.get("current_intent", ""))
        open_q = [str(q) for q in payload.get("open_questions", [])]
        resolved = [str(r) for r in payload.get("resolved_items", [])]
        token_est = self.token_counter.count_tokens(summary_text)

        # 5. Update or create thread state with summarized_through_message_id (R8.4)
        if current_state is None:
            new_state = ThreadState(
                thread_id=thread_u,
                organization_id=org_u,
                topic=topic_text,
                current_intent=intent_text,
                summary=summary_text,
                open_questions=open_q,
                resolved_items=resolved,
                summarized_through_message_id=decision.latest_message_id,
                token_estimate=token_est,
                version=1,
            )
        else:
            new_state = copy.deepcopy(current_state)
            new_state.topic = topic_text
            new_state.current_intent = intent_text
            new_state.summary = summary_text
            new_state.open_questions = open_q
            new_state.resolved_items = resolved
            new_state.summarized_through_message_id = decision.latest_message_id
            new_state.token_estimate = token_est

        # 6. Persist to store under optimistic concurrency
        saved_state = await self.store.save(new_state)

        return SummarizationResult(
            summarized=True,
            thread_state=saved_state,
            verbatim_messages=messages,
            decision=decision,
            model=llm_result.model,
        )

"""Thread context assembly and token compression accounting (R8.5, R8.7, R8.8, design.md §5.4).

Implements:
- R8.5: Assemble context as summary + latest N messages + current email when summary exists.
- R8.2: Short threads below thresholds supply all messages verbatim.
- R8.7 / H3: Pre- vs post-compression token calculation and telemetry recording for hypothesis H3.
- R8.8: Inbound email isolation within thread subsystem, kept out of knowledge corpus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from packages.core.settings import SummarizationSettings
from packages.domain.entities import NormalizedMessage, ThreadState
from packages.knowledge.token_counter import TokenCounter

if TYPE_CHECKING:
    from packages.db.message import MessageStore
    from packages.db.thread_state import ThreadStateStore
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _format_single_message(msg: NormalizedMessage, index: int | None = None) -> str:
    """Format a single message header and body for context representation."""
    sender_str = msg.sender.email if msg.sender else "unknown"
    if index is not None:
        prefix = f"--- Message {index} from {sender_str} at {msg.received_at} ---"
    else:
        prefix = f"From: {sender_str}\nSubject: {msg.subject}\nReceived: {msg.received_at}"
    body = msg.body_text_clean or msg.body_text or msg.snippet or ""
    return f"{prefix}\n{body}"


def _format_summary_block(
    summary: str,
    topic: str | None = None,
    current_intent: str | None = None,
    open_questions: list[str] | None = None,
    resolved_items: list[str] | None = None,
) -> str:
    """Build structured summary text representation."""
    lines = [f"Summary: {summary}"]
    if topic:
        lines.append(f"Topic: {topic}")
    if current_intent:
        lines.append(f"Current Intent: {current_intent}")
    if resolved_items:
        lines.append(f"Resolved: {', '.join(resolved_items)}")
    if open_questions:
        lines.append(f"Open Questions: {', '.join(open_questions)}")
    return "\n".join(lines)


@dataclass(frozen=True)
class AssembledThreadContext:
    """Assembled conversation context ready for prompt packaging (R8.5, R8.7)."""

    thread_id: UUID
    organization_id: UUID
    current_message: NormalizedMessage
    summary: str | None = None
    topic: str | None = None
    current_intent: str | None = None
    open_questions: list[str] = field(default_factory=list)
    resolved_items: list[str] = field(default_factory=list)
    recent_messages: list[NormalizedMessage] = field(default_factory=list)
    has_summary: bool = False
    pre_compression_tokens: int = 0
    post_compression_tokens: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 1.0

    def format_for_prompt(self) -> str:
        """Format thread context into structured plaintext for model prompt injection."""
        blocks: list[str] = []

        if self.has_summary and self.summary:
            summary_header = "=== THREAD SUMMARY ==="
            summary_content = _format_summary_block(
                summary=self.summary,
                topic=self.topic,
                current_intent=self.current_intent,
                open_questions=self.open_questions,
                resolved_items=self.resolved_items,
            )
            blocks.append(f"{summary_header}\n{summary_content}")

        if self.recent_messages:
            section_title = (
                "=== RECENT MESSAGES ===" if self.has_summary else "=== CONVERSATION HISTORY ==="
            )
            msg_texts = [
                _format_single_message(m, index=idx)
                for idx, m in enumerate(self.recent_messages, start=1)
            ]
            blocks.append(f"{section_title}\n" + "\n\n".join(msg_texts))

        curr_content = _format_single_message(self.current_message)
        blocks.append(f"=== CURRENT EMAIL ===\n{curr_content}")

        return "\n\n".join(blocks)

    def get_sections(self) -> list[tuple[str, str]]:
        """Return ordered sections matching ContextPackage prompt requirements (design.md §5.4)."""
        sections: list[tuple[str, str]] = []

        if self.has_summary and self.summary:
            summary_content = _format_summary_block(
                summary=self.summary,
                topic=self.topic,
                current_intent=self.current_intent,
                open_questions=self.open_questions,
                resolved_items=self.resolved_items,
            )
            sections.append(("thread_summary", summary_content))

        if self.recent_messages:
            msg_texts = [
                _format_single_message(m, index=idx)
                for idx, m in enumerate(self.recent_messages, start=1)
            ]
            sections.append(("recent_thread_messages", "\n---\n".join(msg_texts)))

        curr_body = self.current_message.body_text_clean or self.current_message.body_text
        sections.append(("current_email", curr_body))

        return sections

    def to_context_package_args(self) -> dict[str, Any]:
        """Convert into kwargs compatible with ContextPackage constructor (R14.8)."""
        return {
            "current_message": self.current_message,
            "thread_summary": self.summary,
            "recent_messages": self.recent_messages,
        }


class ThreadContextAssembler:
    """Assembles email thread context combining summary, recent messages, and current email (R8.5).

    Computes pre- and post-compression token counts, tokens saved for H3 (R8.7),
    and enforces strict isolation of inbound email from the knowledge corpus (R8.8).
    """

    def __init__(
        self,
        settings: SummarizationSettings | None = None,
        token_counter: TokenCounter | None = None,
        metrics: PipelineMetrics | None = None,
        thread_state_store: ThreadStateStore | None = None,
        message_store: MessageStore | None = None,
    ) -> None:
        self.settings = settings or SummarizationSettings()
        self.token_counter = token_counter or TokenCounter()
        self.metrics = metrics
        self.thread_state_store = thread_state_store
        self.message_store = message_store

    async def assemble(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
        current_message: NormalizedMessage,
        thread_messages: list[NormalizedMessage] | None = None,
        thread_state: ThreadState | None = None,
        keep_latest_messages: int | None = None,
    ) -> AssembledThreadContext:
        """Assemble thread context based on available state and messages.

        Parameters
        ----------
        organization_id : UUID | str
            Tenant scope.
        thread_id : UUID | str
            Thread ID being assembled.
        current_message : NormalizedMessage
            The inbound email being replied to.
        thread_messages : list[NormalizedMessage] | None
            Chronological thread messages. If None, fetched via message_store.
        thread_state : ThreadState | None
            Current ThreadState entity. If None, fetched via thread_state_store.
        keep_latest_messages : int | None
            Override for N latest messages kept alongside summary. Defaults to settings.

        Returns
        -------
        AssembledThreadContext
            Assembled context with pre/post token counts and tokens saved.
        """
        org_u = _to_uuid(organization_id)
        thread_u = _to_uuid(thread_id)
        curr_id = _to_uuid(current_message.message_id)
        n_keep = (
            keep_latest_messages
            if keep_latest_messages is not None
            else self.settings.keep_latest_messages
        )

        # 1. Resolve thread messages
        if thread_messages is None:
            if self.message_store is not None:
                all_msgs = await self.message_store.get_messages_by_thread(org_u, thread_u)
            else:
                all_msgs = []
        else:
            all_msgs = thread_messages

        # Filter out the current message to determine historical context
        historical_messages = [m for m in all_msgs if _to_uuid(m.message_id) != curr_id]
        historical_messages.sort(key=lambda m: m.received_at)

        # 2. Resolve thread state
        if thread_state is None and self.thread_state_store is not None:
            thread_state = await self.thread_state_store.get(org_u, thread_u)

        # 3. Check if summary exists
        has_summary = bool(
            thread_state is not None and thread_state.summary and thread_state.summary.strip()
        )

        # 4. Formulate recent messages and summary attributes (R8.5)
        if has_summary and thread_state is not None:
            summary_val = thread_state.summary
            topic_val = thread_state.topic
            intent_val = thread_state.current_intent
            open_q = list(thread_state.open_questions or [])
            resolved_i = list(thread_state.resolved_items or [])
            recent_messages = historical_messages[-n_keep:] if n_keep > 0 else []
        else:
            summary_val = None
            topic_val = None
            intent_val = None
            open_q = []
            resolved_i = []
            recent_messages = list(historical_messages)

        # 5. Token accounting for hypothesis H3 (R8.7)
        # Pre-compression: full uncompressed thread (all history + current)
        uncompressed_parts = [
            _format_single_message(m, index=idx)
            for idx, m in enumerate(historical_messages, start=1)
        ]
        uncompressed_parts.append(_format_single_message(current_message))
        pre_compression_text = "\n\n".join(uncompressed_parts)
        pre_tokens = self.token_counter.count_tokens(pre_compression_text)

        # Post-compression: assembled context
        if has_summary and summary_val:
            summary_text = _format_summary_block(
                summary=summary_val,
                topic=topic_val,
                current_intent=intent_val,
                open_questions=open_q,
                resolved_items=resolved_i,
            )
            compressed_parts = [summary_text]
            compressed_parts.extend(
                _format_single_message(m, index=idx)
                for idx, m in enumerate(recent_messages, start=1)
            )
            compressed_parts.append(_format_single_message(current_message))
            post_compression_text = "\n\n".join(compressed_parts)
            post_tokens = self.token_counter.count_tokens(post_compression_text)
            tokens_saved = max(0, pre_tokens - post_tokens)
            compression_ratio = round(post_tokens / pre_tokens, 4) if pre_tokens > 0 else 1.0
        else:
            post_tokens = pre_tokens
            tokens_saved = 0
            compression_ratio = 1.0

        # 6. Record metric if enabled (R8.7, R21.4)
        if self.metrics is not None and tokens_saved > 0:
            self.metrics.tokens_saved_total.labels(organization=str(org_u)).inc(tokens_saved)

        return AssembledThreadContext(
            thread_id=thread_u,
            organization_id=org_u,
            current_message=current_message,
            summary=summary_val,
            topic=topic_val,
            current_intent=intent_val,
            open_questions=open_q,
            resolved_items=resolved_i,
            recent_messages=recent_messages,
            has_summary=has_summary,
            pre_compression_tokens=pre_tokens,
            post_compression_tokens=post_tokens,
            tokens_saved=tokens_saved,
            compression_ratio=compression_ratio,
        )

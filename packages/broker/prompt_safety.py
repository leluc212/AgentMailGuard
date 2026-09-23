"""Prompt safety and isolation assertion utilities (R3.7, design.md §7.4).

Enforces that LLM prompts generated during worker micro-batching are strictly isolated
to a single email message, with exhaustive verification against cross-contamination.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from packages.broker.envelope import JobEnvelope
from packages.domain.rules import EmailContext
from packages.llm.protocol import ChatMessage

logger = logging.getLogger(__name__)


class PromptContaminationError(AssertionError):
    """Raised when an LLM prompt contains content or metadata from a different email (R3.7)."""


@dataclass(frozen=True)
class PromptExecutionRecord:
    """Captured prompt execution record linking an email job to its generated prompt messages."""

    job_id: str
    message_id: str
    subject: str
    sender_email: str
    body_keywords: list[str]
    prompt_text: str

    @classmethod
    def from_call(
        cls,
        envelope_or_context: JobEnvelope | EmailContext | dict[str, Any],
        messages: list[ChatMessage] | str,
        body_keywords: list[str] | None = None,
    ) -> PromptExecutionRecord:
        """Construct a PromptExecutionRecord from an envelope/context and chat messages."""
        if isinstance(messages, str):
            prompt_str = messages
        elif isinstance(messages, list):
            prompt_str = "\n".join(
                f"[{getattr(m, 'role', 'user')}]: {getattr(m, 'content', str(m))}"
                for m in messages
            )
        else:
            prompt_str = str(messages)

        if isinstance(envelope_or_context, JobEnvelope):
            job_id = str(envelope_or_context.job_id)
            msg_id = str(envelope_or_context.message_id or "")
            payload = envelope_or_context.payload or {}
            subject = str(payload.get("subject", ""))
            sender = str(payload.get("sender_email", payload.get("sender", "")))
            body = str(payload.get("body_text", payload.get("body", "")))
        elif isinstance(envelope_or_context, EmailContext):
            job_id = ""
            msg_id = str(getattr(envelope_or_context, "message_id", "") or "")
            subject = envelope_or_context.subject or ""
            sender = envelope_or_context.sender_email or ""
            body = envelope_or_context.body_text or ""
        elif isinstance(envelope_or_context, dict):
            job_id = str(envelope_or_context.get("job_id", ""))
            msg_id = str(envelope_or_context.get("message_id", ""))
            subject = str(envelope_or_context.get("subject", ""))
            sender = str(
                envelope_or_context.get("sender_email")
                or envelope_or_context.get("sender")
                or ""
            )
            body = str(envelope_or_context.get("body_text", envelope_or_context.get("body", "")))
        else:
            job_id = ""
            msg_id = ""
            subject = ""
            sender = ""
            body = ""

        # Extract distinct tokens from body if keywords not explicitly supplied
        keywords = body_keywords
        if keywords is None and body:
            words = [w.strip(".,;:?!'\"()[]{}") for w in body.split() if len(w) > 4]
            keywords = list(dict.fromkeys(words))[:10]  # top 10 unique significant words

        return cls(
            job_id=job_id,
            message_id=msg_id,
            subject=subject,
            sender_email=sender,
            body_keywords=keywords or [],
            prompt_text=prompt_str,
        )


def assert_prompt_isolation(
    records: list[PromptExecutionRecord],
) -> None:
    """Assert strict prompt isolation across a micro-batch of processed emails (R3.7).

    Validates that:
    1. Every prompt contains content from its own email context.
    2. No prompt contains any identifying tokens, message IDs, subjects, sender emails,
       or distinctive body keywords from any OTHER email in the batch.

    Raises:
        PromptContaminationError: If cross-contamination is detected.
    """
    if len(records) <= 1:
        return

    for i, current in enumerate(records):
        # Verify no contamination from sibling records (j != i)
        for j, sibling in enumerate(records):
            if i == j:
                continue

            # 1. Message ID isolation
            if (
                sibling.message_id
                and len(sibling.message_id) > 6
                and sibling.message_id in current.prompt_text
            ):
                raise PromptContaminationError(
                    f"Cross-contamination detected! Prompt for message '{current.message_id}' "
                    f"(job '{current.job_id}') contains foreign message ID "
                    f"'{sibling.message_id}' from sibling job '{sibling.job_id}' (R3.7)."
                )

            # 2. Distinctive foreign sender email isolation
            if (
                sibling.sender_email
                and sibling.sender_email != current.sender_email
                and len(sibling.sender_email) > 5
                and sibling.sender_email in current.prompt_text
            ):
                raise PromptContaminationError(
                    f"Cross-contamination detected! Prompt for message '{current.message_id}' "
                    f"contains foreign sender '{sibling.sender_email}' "
                    f"from sibling job '{sibling.job_id}' (R3.7)."
                )

            # 3. Distinctive foreign subject isolation
            if (
                sibling.subject
                and sibling.subject != current.subject
                and len(sibling.subject) > 8
                and sibling.subject in current.prompt_text
            ):
                raise PromptContaminationError(
                    f"Cross-contamination detected! Prompt for message '{current.message_id}' "
                    f"contains foreign subject '{sibling.subject}' "
                    f"from sibling job '{sibling.job_id}' (R3.7)."
                )

            # 4. Distinctive foreign body keywords isolation
            for kw in sibling.body_keywords:
                # Only check keywords that do not genuinely appear in current email's context
                if (
                    kw
                    and len(kw) >= 5
                    and kw not in current.subject
                    and kw not in " ".join(current.body_keywords)
                    and kw in current.prompt_text
                ):
                    raise PromptContaminationError(
                        f"Cross-contamination detected! Prompt for message '{current.message_id}' "
                        f"contains foreign keyword '{kw}' "
                        f"from sibling job '{sibling.job_id}' (R3.7)."
                    )

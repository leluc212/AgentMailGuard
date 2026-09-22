"""Small-LLM fallback classifier for Stage 3 email triage (R6.1, R6.3, design.md §5.3).

Invoked when deterministic rules (Stage 1) and lightweight ML (Stage 2) both abstain
or fall below their confidence cutoffs. Uses structured LLM generation with schema
enforcement over the fast routine model tier (gpt-4o-mini).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from packages.domain.entities import Classification, NormalizedMessage
from packages.domain.rules import EmailContext
from packages.domain.taxonomy import (
    CANONICAL_CATEGORIES as CANONICAL_CATEGORIES,
)
from packages.domain.taxonomy import (
    is_valid_category,
    normalize_category,
)
from packages.llm.client import HttpLLMProvider
from packages.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMSchemaValidationError,
    ModelTier,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert enterprise email triage classifier.
Analyze the inbound email and return structured JSON adhering to the schema.

Supported categories:
- support: Technical bugs, crashes, incidents, or feature troubleshooting.
- sales: Demo requests, pricing inquiries, enterprise licensing, partnerships.
- billing: Invoices, payment receipts, charges, refunds, payment method updates.
- administration: Account setup, user provisioning, password resets, permissions.
- scheduling: Meeting requests, calendar invitations, scheduling availability.
- general_inquiry: General questions about the company, services, or unclassified.
- automated_notification: System alerts, newsletters, CI/CD reports, noreply updates.
- acknowledgement: Standalone thank-you emails, receipt/delivery confirmations.
- no_response: Spam, unsolicited promotional outreach, or unaddressed digests.

Routing guidelines:
1. reply_required: False for automated_notification, no_response, thank-yous.
   True for actionable customer requests.
2. workflow_hint: 'none' if reply_required is False.
   'template' for deterministic receipt confirmations or meeting acks.
   'ai' for complex drafting requiring RAG.
3. retrieval_required: True if domain knowledge retrieval is necessary.
   False for automated or routine acknowledgements.
4. priority: 'urgent' (production outages, urgent security/payment deadlines),
   'high' (severe delays, urgent requests), 'normal' (standard), 'low' (automated).
5. confidence: Float between 0.0 and 1.0 reflecting classification certainty.
"""


class LLMTriageOutput(BaseModel):
    """Structured JSON schema for Stage 3 LLM email classification."""

    category: str = Field(description="One of the 9 canonical email categories")
    intent: str = Field(description="Fine-grained user intent, e.g. password_reset, demo_request")
    priority: Literal["urgent", "high", "normal", "low"] = Field(
        default="normal",
        description="Business priority level based on urgency cues",
    )
    reply_required: bool = Field(
        description="Whether this message requires an outbound email response"
    )
    workflow_hint: Literal["ai", "template", "none"] = Field(
        description="Downstream routing action: ai, template, or none"
    )
    retrieval_required: bool = Field(
        description="Whether enterprise RAG retrieval is required for drafting"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model self-assessed confidence score between 0.0 and 1.0",
    )
    reasoning: str | None = Field(
        default=None,
        description="Brief justification for the classification decision",
    )

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        """Normalize category name and enforce canonical category vocabulary (R6.4)."""
        cleaned = normalize_category(v)
        if not is_valid_category(cleaned):
            valid_cats = sorted(CANONICAL_CATEGORIES)
            raise ValueError(f"Category '{v}' not recognized; must be one of {valid_cats}")
        return cleaned


def prepare_triage_prompt(ctx: EmailContext) -> list[ChatMessage]:
    """Construct concise prompt messages for Stage 3 LLM classification."""
    body_text = ctx.body_text_clean or ctx.body_text
    truncated_body = body_text[:2000].strip()

    header_notes = []
    if ctx.headers.get("auto-submitted"):
        header_notes.append(f"Auto-Submitted: {ctx.headers['auto-submitted']}")
    if ctx.headers.get("list-unsubscribe"):
        header_notes.append("List-Unsubscribe: present")

    user_content_lines = [
        f"Subject: {ctx.subject}",
        f"Sender: {ctx.sender_email}",
    ]
    if header_notes:
        user_content_lines.append("Headers: " + ", ".join(header_notes))
    user_content_lines.append("")
    user_content_lines.append("Body:")
    user_content_lines.append(truncated_body or "(Empty body)")

    return [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content="\n".join(user_content_lines)),
    ]


class LLMTriageClassifier:
    """Stage 3 LLM triage classifier implementing fallback classification."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        tier: ModelTier = ModelTier.FAST,
        fallback_on_error: bool = False,
        max_tokens: int = 250,
        temperature: float = 0.0,
    ) -> None:
        self._provider = provider or HttpLLMProvider()
        self._tier = tier
        self._fallback_on_error = fallback_on_error
        self._max_tokens = max_tokens
        self._temperature = temperature

    @property
    def provider(self) -> LLMProvider:
        """Active LLMProvider instance."""
        return self._provider

    @property
    def tier(self) -> ModelTier:
        """Active model tier."""
        return self._tier

    @staticmethod
    def safe_default(
        error_message: str | None = None,
        latency_ms: int = 0,
    ) -> Classification:
        """Return R6.11 safe default review classification when triage fails."""
        raw: dict[str, Any] = {"review_flag": True}
        if error_message:
            raw["error"] = error_message
        return Classification(
            category="general_inquiry",
            intent="unclassified_fallback",
            priority="normal",
            reply_required=True,
            workflow_hint="ai",
            retrieval_required=True,
            confidence=0.0,
            decided_by="default",
            latency_ms=latency_ms,
            model=None,
            raw=raw,
        )

    async def classify(
        self,
        context: EmailContext | NormalizedMessage | dict[str, Any],
    ) -> Classification:
        """Classify an email context using the structured LLM provider.

        Args:
            context: EmailContext, NormalizedMessage, or raw dictionary.

        Returns:
            Classification entity adhering to design.md §5.3.

        Raises:
            LLMError: If LLM call fails and fallback_on_error is False.
        """
        start_time = time.perf_counter()

        if isinstance(context, NormalizedMessage):
            ctx = EmailContext.from_message(context)
        elif isinstance(context, dict):
            ctx = EmailContext.from_dict(context)
        elif isinstance(context, EmailContext):
            ctx = context
        else:
            raise TypeError(f"Unsupported context type for classification: {type(context)}")

        messages = prepare_triage_prompt(ctx)
        schema = LLMTriageOutput.model_json_schema()

        try:
            result = await self._provider.generate(
                messages=messages,
                schema=schema,
                tier=self._tier,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            parsed = LLMTriageOutput.model_validate(result.content)
            elapsed_ms = max(result.latency_ms, int((time.perf_counter() - start_time) * 1000))

            return Classification(
                category=parsed.category,
                intent=parsed.intent,
                priority=parsed.priority,
                reply_required=parsed.reply_required,
                workflow_hint=parsed.workflow_hint,
                retrieval_required=parsed.retrieval_required,
                confidence=parsed.confidence,
                decided_by="llm",
                latency_ms=elapsed_ms,
                model=result.model,
                raw={
                    "reasoning": parsed.reasoning,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "tier": str(result.tier),
                    "finish_reason": result.raw_finish_reason,
                },
            )

        except Exception as exc:
            elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
            logger.warning("Stage 3 LLM triage classification failed: %s", exc)
            if self._fallback_on_error:
                return self.safe_default(error_message=str(exc), latency_ms=elapsed_ms)
            if isinstance(exc, (LLMError, ValueError)):
                raise
            raise LLMSchemaValidationError(f"Triage output validation failed: {exc}") from exc

    def classify_sync(
        self,
        context: EmailContext | NormalizedMessage | dict[str, Any],
    ) -> Classification:
        """Synchronous wrapper for classify(), safe for sync workers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, self.classify(context))
                return future.result()
        else:
            return asyncio.run(self.classify(context))

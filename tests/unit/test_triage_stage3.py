"""Unit tests for Stage 3 Small-LLM Fallback Triage Classifier (R6.1, R6.3, design.md §5.3).

Verifies structured Pydantic schema validation, prompt formatting, domain entity
generation, multi-type context coercion, sync/async wrappers, and safe default fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.domain.entities import EmailAddress, NormalizedMessage
from packages.domain.rules import EmailContext
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import LLMTimeoutError, ModelTier
from services.triage_worker.llm_classifier import (
    CANONICAL_CATEGORIES,
    LLMTriageClassifier,
    LLMTriageOutput,
    prepare_triage_prompt,
)


@pytest.fixture
def sample_context() -> EmailContext:
    """Fixture producing a standard ambiguous inbound email context."""
    return EmailContext(
        sender_email="alex.smith@example.com",
        sender_name="Alex Smith",
        subject="Question regarding service degradation",
        body_text="We experienced intermittent timeouts on the EU gateway today. Please advise.",
        headers={"message-id": "<msg-001@example.com>"},
    )


@pytest.fixture
def sample_message() -> NormalizedMessage:
    """Fixture producing a valid NormalizedMessage domain entity."""
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="gmail",
        provider_message_id="gm-msg-12345",
        sender=EmailAddress(email="maria.garcia@clientcorp.com", name="Maria Garcia"),
        received_at=datetime.now(UTC),
        subject="Need invoice clarification for INV-2026-0042",
        body_text="Could you break down the professional services fee on our latest invoice?",
    )


class TestLLMTriageOutputSchema:
    """Validate Pydantic schema rules and constraints for Stage 3 outputs."""

    def test_all_canonical_categories_accepted(self) -> None:
        """Verify each of the 9 canonical categories validates successfully."""
        for category in CANONICAL_CATEGORIES:
            output = LLMTriageOutput(
                category=category,
                intent="general_inquiry",
                priority="normal",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=True,
                confidence=0.88,
            )
            assert output.category == category

    def test_category_synonym_normalization(self) -> None:
        """Verify technical_support is automatically mapped to support."""
        output = LLMTriageOutput(
            category="technical_support",
            intent="bug_report",
            priority="high",
            reply_required=True,
            workflow_hint="ai",
            retrieval_required=True,
            confidence=0.92,
        )
        assert output.category == "support"

    def test_unsupported_category_rejected(self) -> None:
        """Verify invalid category strings raise a validation error."""
        with pytest.raises(ValidationError, match="not recognized"):
            LLMTriageOutput(
                category="random_unknown_category",
                intent="test",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=True,
                confidence=0.5,
            )

    def test_confidence_bounds_enforced(self) -> None:
        """Verify confidence outside [0.0, 1.0] raises a validation error."""
        with pytest.raises(ValidationError):
            LLMTriageOutput(
                category="support",
                intent="test",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=True,
                confidence=1.5,
            )
        with pytest.raises(ValidationError):
            LLMTriageOutput(
                category="support",
                intent="test",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=True,
                confidence=-0.1,
            )


class TestPromptPreparation:
    """Validate prompt construction and message assembly."""

    def test_prompt_content_and_headers(self, sample_context: EmailContext) -> None:
        """Verify prompt formatting with subject, sender, and header flags."""
        sample_context.headers["auto-submitted"] = "auto-generated"
        sample_context.headers["list-unsubscribe"] = "<mailto:unsub@example.com>"

        messages = prepare_triage_prompt(sample_context)
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert "enterprise email triage classifier" in messages[0].content

        assert messages[1].role == "user"
        user_content = messages[1].content
        assert "Subject: Question regarding service degradation" in user_content
        assert "Sender: alex.smith@example.com" in user_content
        assert "Auto-Submitted: auto-generated" in user_content
        assert "List-Unsubscribe: present" in user_content
        assert "intermittent timeouts on the EU gateway" in user_content

    def test_prompt_truncation_of_large_body(self) -> None:
        """Verify massive email bodies are cleanly truncated to 2000 characters."""
        long_body = "x" * 10000
        ctx = EmailContext(
            sender_email="test@example.com",
            subject="Huge email",
            body_text=long_body,
        )
        messages = prepare_triage_prompt(ctx)
        user_body = messages[1].content
        # 2000 chars truncated + header prefix
        assert len(user_body) < 2200


class TestLLMTriageClassifier:
    """Validate LLMTriageClassifier end-to-end inference and contracts."""

    @pytest.mark.asyncio
    async def test_successful_classification(self, sample_context: EmailContext) -> None:
        """Verify successful structured classification returning Classification entity."""
        fake_response = {
            "category": "support",
            "intent": "service_incident",
            "priority": "high",
            "reply_required": True,
            "workflow_hint": "ai",
            "retrieval_required": True,
            "confidence": 0.94,
            "reasoning": "Customer reports intermittent gateway timeouts requiring investigation.",
        }
        provider = FakeLLMProvider(default_response=fake_response)
        classifier = LLMTriageClassifier(provider=provider, tier=ModelTier.FAST)

        classification = await classifier.classify(sample_context)

        assert classification.category == "support"
        assert classification.intent == "service_incident"
        assert classification.priority == "high"
        assert classification.reply_required is True
        assert classification.workflow_hint == "ai"
        assert classification.retrieval_required is True
        assert classification.confidence == 0.94
        assert classification.decided_by == "llm"
        assert classification.model == "fake-fast-model"
        assert classification.latency_ms >= 1
        assert classification.raw["reasoning"] == fake_response["reasoning"]
        assert classification.raw["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_input_coercion_types(
        self,
        sample_context: EmailContext,
        sample_message: NormalizedMessage,
    ) -> None:
        """Verify classifier handles EmailContext, NormalizedMessage, and raw dict."""
        provider = FakeLLMProvider()
        classifier = LLMTriageClassifier(provider=provider)

        c1 = await classifier.classify(sample_context)
        c2 = await classifier.classify(sample_message)
        c3 = await classifier.classify({"subject": "Dict email", "body_text": "hello"})

        assert c1.decided_by == "llm"
        assert c2.decided_by == "llm"
        assert c3.decided_by == "llm"

        with pytest.raises(TypeError, match="Unsupported context type"):
            await classifier.classify(12345)  # type: ignore[arg-type]

    def test_classify_sync(self, sample_context: EmailContext) -> None:
        """Verify synchronous classify_sync() wrapper works properly."""
        provider = FakeLLMProvider()
        classifier = LLMTriageClassifier(provider=provider)

        result = classifier.classify_sync(sample_context)
        assert result.decided_by == "llm"
        assert result.category == "support"

    @pytest.mark.asyncio
    async def test_error_propagation_when_fallback_disabled(
        self,
        sample_context: EmailContext,
    ) -> None:
        """Verify exceptions propagate when fallback_on_error is False."""
        provider = FakeLLMProvider()
        provider.set_error(LLMTimeoutError("Stage 3 timed out"))
        classifier = LLMTriageClassifier(provider=provider, fallback_on_error=False)

        with pytest.raises(LLMTimeoutError, match="Stage 3 timed out"):
            await classifier.classify(sample_context)

    @pytest.mark.asyncio
    async def test_safe_default_when_fallback_enabled(
        self,
        sample_context: EmailContext,
    ) -> None:
        """Verify R6.11 safe default review classification when fallback_on_error is True."""
        provider = FakeLLMProvider()
        provider.set_error(LLMTimeoutError("Stage 3 timed out"))
        classifier = LLMTriageClassifier(provider=provider, fallback_on_error=True)

        result = await classifier.classify(sample_context)

        assert result.category == "general_inquiry"
        assert result.intent == "unclassified_fallback"
        assert result.priority == "normal"
        assert result.reply_required is True
        assert result.workflow_hint == "ai"
        assert result.retrieval_required is True
        assert result.confidence == 0.0
        assert result.decided_by == "default"
        assert result.raw.get("review_flag") is True
        assert "Stage 3 timed out" in result.raw.get("error", "")

    def test_safe_default_static_helper(self) -> None:
        """Verify direct invocation of LLMTriageClassifier.safe_default() (R6.11)."""
        fallback = LLMTriageClassifier.safe_default(error_message="All 3 stages failed")
        assert fallback.category == "general_inquiry"
        assert fallback.priority == "normal"
        assert fallback.reply_required is True
        assert fallback.confidence == 0.0
        assert fallback.decided_by == "default"
        assert fallback.raw["review_flag"] is True
        assert fallback.raw["error"] == "All 3 stages failed"

"""Unit tests for CascadingTriageEngine and ThresholdManager (R6.1, R6.2, R6.7, R6.9, R6.11).

Verifies:
1. Strict short-circuiting: Stage 1 stops before ML/LLM; Stage 2 stops before LLM (R6.2).
2. Stage 3 fallback invocation when Stage 1 and Stage 2 abstain or miss thresholds.
3. Safe default assignment with review_flag=True when all stages fail (R6.11).
4. Hierarchical threshold resolution across org and category dimensions (R6.9).
5. Audit trail and persistence to ClassificationStore (R6.7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from packages.core.settings import TriageSettings
from packages.db.classification import InMemoryClassificationStore
from packages.domain.entities import (
    Classification,
    EmailAddress,
    NormalizedMessage,
)
from packages.domain.rules import Rule
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import LLMTimeoutError, ModelTier
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.llm_classifier import LLMTriageClassifier
from services.triage_worker.rules import HotReloadableRuleEngine
from services.triage_worker.thresholds import ThresholdManager


@pytest.fixture
def sample_message() -> NormalizedMessage:
    """Fixture producing a standard inbound email message."""
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="gmail",
        provider_message_id="msg-cascade-101",
        sender=EmailAddress(email="billing@clientcorp.com", name="Client Billing"),
        received_at=datetime.now(UTC),
        subject="Invoice inquiry for INV-2026-9999",
        body_text="Please find our payment inquiry for invoice INV-2026-9999.",
        headers={"auto-submitted": "no"},
    )


class TestThresholdResolutionHierarchy:
    """Validate hierarchical threshold resolution and runtime tuning (R6.9)."""

    def test_global_defaults(self) -> None:
        """Assert global defaults match TriageSettings."""
        settings = TriageSettings(
            rule_confidence_threshold=0.95,
            ml_confidence_threshold=0.80,
            llm_confidence_threshold=0.70,
        )
        tm = ThresholdManager(settings=settings)

        assert tm.get_threshold("rule") == 0.95
        assert tm.get_threshold("ml") == 0.80
        assert tm.get_threshold("llm") == 0.70

    def test_global_category_override(self) -> None:
        """Assert global category override takes precedence over global default."""
        tm = ThresholdManager()
        tm.set_global_category_threshold(category="billing", stage="ml", threshold=0.90)

        # General ML threshold is still 0.80
        assert tm.get_threshold("ml", category="support") == 0.80
        # Billing ML threshold is 0.90
        assert tm.get_threshold("ml", category="billing") == 0.90

    def test_organization_default_override(self) -> None:
        """Assert organization default takes precedence over global defaults."""
        tm = ThresholdManager()
        org_id = uuid4()
        tm.set_organization_threshold(organization_id=org_id, stage="ml", threshold=0.85)

        # Other orgs get default 0.80
        assert tm.get_threshold("ml") == 0.80
        # Configured org gets 0.85
        assert tm.get_threshold("ml", organization_id=org_id) == 0.85

    def test_organization_category_precedence(self) -> None:
        """Assert Org+Category override has the highest precedence (R6.9)."""
        tm = ThresholdManager()
        org_id = uuid4()

        # Set global category override
        tm.set_global_category_threshold("support", "ml", 0.82)
        # Set org default override
        tm.set_organization_threshold(org_id, "ml", 0.84)
        # Set org category override
        tm.set_organization_category_threshold(org_id, "support", "ml", 0.88)

        # Org + Category = 0.88
        assert tm.get_threshold("ml", category="support", organization_id=org_id) == 0.88
        # Org + other category = org default 0.84
        assert tm.get_threshold("ml", category="billing", organization_id=org_id) == 0.84
        # Other org + support = global category 0.82
        assert tm.get_threshold("ml", category="support") == 0.82

    def test_load_from_organization_settings_dict(self) -> None:
        """Assert dynamic loading from organization settings JSONB without restart."""
        tm = ThresholdManager()
        org_id = uuid4()
        config_payload = {
            "triage": {
                "rule_confidence_threshold": 0.98,
                "ml": 0.86,
                "category_thresholds": {
                    "billing": {"ml": 0.92, "llm": 0.80},
                },
            }
        }
        tm.load_organization_settings(org_id, config_payload)

        assert tm.get_threshold("rule", organization_id=org_id) == 0.98
        assert tm.get_threshold("ml", organization_id=org_id) == 0.86
        assert tm.get_threshold("ml", category="billing", organization_id=org_id) == 0.92
        assert tm.get_threshold("llm", category="billing", organization_id=org_id) == 0.80


class TestCascadeShortCircuiting:
    """Validate strict short-circuiting at each stage (R6.2)."""

    @pytest.mark.asyncio
    async def test_stage1_rule_short_circuits_ml_and_llm(
        self,
        sample_message: NormalizedMessage,
    ) -> None:
        """When a rule matches with conf >= threshold, ML and LLM must NOT be called."""
        rule = Rule.from_dict(
            {
                "id": "auto-sub",
                "when": {"header.auto-submitted": {"equals": "no"}},
                "then": {
                    "category": "automated_notification",
                    "confidence": 0.99,
                    "reply_required": False,
                    "workflow_hint": "none",
                    "retrieval_required": False,
                },
            }
        )
        rule_engine = HotReloadableRuleEngine(initial_rules=[rule])

        # Mock ML and LLM classifiers to assert zero calls
        mock_ml = MagicMock(spec=MLClassifier)
        mock_fake_llm = FakeLLMProvider()
        llm_classifier = LLMTriageClassifier(provider=mock_fake_llm)

        engine = CascadingTriageEngine(
            rule_engine=rule_engine,
            ml_classifier=mock_ml,
            llm_classifier=llm_classifier,
        )

        result = await engine.triage(sample_message)

        assert result.decided_stage == "rule"
        assert result.classification.category == "automated_notification"
        assert result.classification.confidence == 0.99
        assert result.classification.decided_by == "rule"

        # Invariant: ML and LLM were never invoked (R6.2)
        assert mock_ml.classify.call_count == 0
        assert len(mock_fake_llm.recorded_calls) == 0

        # Verify stage records
        assert len(result.stages_executed) == 1
        assert result.stages_executed[0].stage == "rule"
        assert result.stages_executed[0].accepted is True

    @pytest.mark.asyncio
    async def test_stage1_low_confidence_advances_to_stage2(
        self,
        sample_message: NormalizedMessage,
    ) -> None:
        """When a rule matches but confidence < threshold, cascade advances to ML."""
        rule = Rule.from_dict(
            {
                "id": "low-conf-rule",
                "when": {"header.auto-submitted": {"equals": "no"}},
                "then": {"category": "support", "confidence": 0.70},
            }
        )
        rule_engine = HotReloadableRuleEngine(initial_rules=[rule])

        # Mock ML returning high confidence
        mock_ml = MagicMock(spec=MLClassifier)
        mock_ml.classify.return_value = Classification(
            category="billing",
            confidence=0.91,
            priority="normal",
            reply_required=True,
            workflow_hint="ai",
            retrieval_required=True,
            decided_by="ml",
            latency_ms=10,
        )

        mock_fake_llm = FakeLLMProvider()
        llm_classifier = LLMTriageClassifier(provider=mock_fake_llm)

        engine = CascadingTriageEngine(
            rule_engine=rule_engine,
            ml_classifier=mock_ml,
            llm_classifier=llm_classifier,
        )

        result = await engine.triage(sample_message)

        assert result.decided_stage == "ml"
        assert result.classification.category == "billing"
        assert result.classification.confidence == 0.91
        assert result.classification.decided_by == "ml"

        # ML was called, LLM was NOT called
        assert mock_ml.classify.call_count == 1
        assert len(mock_fake_llm.recorded_calls) == 0

        # Stage records: rule rejected, ml accepted
        assert len(result.stages_executed) == 2
        assert result.stages_executed[0].stage == "rule"
        assert result.stages_executed[0].accepted is False
        assert result.stages_executed[1].stage == "ml"
        assert result.stages_executed[1].accepted is True

    @pytest.mark.asyncio
    async def test_stage2_low_confidence_advances_to_stage3_llm(
        self,
        sample_message: NormalizedMessage,
    ) -> None:
        """When rules abstain and ML confidence < 0.80, cascade invokes Stage 3 LLM."""
        rule_engine = HotReloadableRuleEngine(initial_rules=[])  # abstains

        # Mock ML with low confidence (0.65 < 0.80)
        mock_ml = MagicMock(spec=MLClassifier)
        mock_ml.classify.return_value = Classification(
            category="general_inquiry",
            confidence=0.65,
            decided_by="ml",
        )

        fake_llm = FakeLLMProvider(
            default_response={
                "category": "billing",
                "intent": "invoice_query",
                "priority": "normal",
                "reply_required": True,
                "workflow_hint": "ai",
                "retrieval_required": True,
                "confidence": 0.92,
                "reasoning": "Inquiry about invoice INV-2026-9999.",
            }
        )
        llm_classifier = LLMTriageClassifier(provider=fake_llm, tier=ModelTier.FAST)

        engine = CascadingTriageEngine(
            rule_engine=rule_engine,
            ml_classifier=mock_ml,
            llm_classifier=llm_classifier,
        )

        result = await engine.triage(sample_message)

        assert result.decided_stage == "llm"
        assert result.classification.category == "billing"
        assert result.classification.confidence == 0.92
        assert result.classification.decided_by == "llm"

        # Both ML and LLM were called
        assert mock_ml.classify.call_count == 1
        assert len(fake_llm.recorded_calls) == 1

        # Stage records: rule abstained, ml rejected, llm accepted
        assert len(result.stages_executed) == 3
        assert result.stages_executed[0].stage == "rule"
        assert result.stages_executed[0].accepted is False
        assert result.stages_executed[1].stage == "ml"
        assert result.stages_executed[1].accepted is False
        assert result.stages_executed[2].stage == "llm"
        assert result.stages_executed[2].accepted is True


class TestSafeDefaultAndPersistence:
    """Validate R6.11 safe default review fallback and R6.7 DB persistence."""

    @pytest.mark.asyncio
    async def test_all_stages_fail_assigns_safe_default(
        self,
        sample_message: NormalizedMessage,
    ) -> None:
        """When all 3 stages fail or return low confidence, safe default is assigned (R6.11)."""
        rule_engine = HotReloadableRuleEngine(initial_rules=[])  # abstains

        mock_ml = MagicMock(spec=MLClassifier)
        mock_ml.classify.return_value = Classification(
            category="support",
            confidence=0.50,  # below 0.80
            decided_by="ml",
        )

        fake_llm = FakeLLMProvider()
        fake_llm.set_error(LLMTimeoutError("Stage 3 LLM gateway timed out"))
        llm_classifier = LLMTriageClassifier(provider=fake_llm, fallback_on_error=False)

        engine = CascadingTriageEngine(
            rule_engine=rule_engine,
            ml_classifier=mock_ml,
            llm_classifier=llm_classifier,
        )

        result = await engine.triage(sample_message)

        # R6.11 assertions
        assert result.decided_stage == "default"
        assert result.classification.category == "general_inquiry"
        assert result.classification.priority == "normal"
        assert result.classification.reply_required is True
        assert result.classification.confidence == 0.0
        assert result.classification.decided_by == "default"
        assert result.classification.raw.get("review_flag") is True

        # Check that stages_attempted telemetry was captured
        stages_attempted = result.classification.raw.get("stages_attempted", [])
        assert len(stages_attempted) == 3  # rule, ml, llm

    @pytest.mark.asyncio
    async def test_persistence_to_classification_store(
        self,
        sample_message: NormalizedMessage,
    ) -> None:
        """When persist=True, classification is durably stored in ClassificationStore (R6.7)."""
        rule = Rule.from_dict(
            {
                "id": "r1",
                "when": {"header.auto-submitted": {"equals": "no"}},
                "then": {"category": "support", "confidence": 0.99},
            }
        )
        rule_engine = HotReloadableRuleEngine(initial_rules=[rule])
        store = InMemoryClassificationStore()

        engine = CascadingTriageEngine(
            rule_engine=rule_engine,
            classification_store=store,
        )

        result = await engine.triage(
            sample_message,
            persist=True,
        )

        assert result.persisted_id is not None

        # Verify stored record
        saved = await store.get_latest_classification_by_message(
            organization_id=sample_message.organization_id,
            message_id=sample_message.message_id,
        )
        assert saved is not None
        assert saved.id == result.persisted_id
        assert saved.category == "support"
        assert saved.decided_by == "rule"
        assert saved.confidence == 0.99
        assert saved.organization_id == sample_message.organization_id

    def test_sync_triage_wrapper(self, sample_message: NormalizedMessage) -> None:
        """Verify synchronous triage_sync wrapper operates properly."""
        rule = Rule.from_dict(
            {
                "id": "sync-r",
                "when": {"header.auto-submitted": {"equals": "no"}},
                "then": {"category": "sales", "confidence": 0.98},
            }
        )
        engine = CascadingTriageEngine(rule_engine=HotReloadableRuleEngine(initial_rules=[rule]))

        res = engine.triage_sync(sample_message)
        assert res.decided_stage == "rule"
        assert res.classification.category == "sales"
        assert res.total_latency_ms >= 0

    def test_unsupported_context_type_raises_type_error(self) -> None:
        """Verify passing an unsupported type raises TypeError."""
        engine = CascadingTriageEngine()
        with pytest.raises(TypeError, match="Unsupported context type"):
            engine.triage_sync(99999)  # type: ignore[arg-type]

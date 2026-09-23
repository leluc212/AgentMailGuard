"""Unit tests for category-aware routing, lane resolution, and dynamic queue configuration.

Fulfills requirements:
- R7.1: Publish actionable emails to email.<category>.<priority>.
- R7.2: Define normal and priority lanes with independent consumer scaling.
- R7.4: Add new categories by configuration, creating queues at startup without code changes.
- R7.6: Log startup warning naming any category queue with no configured consumer.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from packages.broker.envelope import JobEnvelope
from packages.broker.routing import (
    CANONICAL_LANES,
    HIGH_PRIORITY_LEVELS,
    format_routing_key,
    is_queue_consumed,
    load_categories_from_yaml,
    prepare_route_envelope,
    resolve_priority_lane,
)
from packages.core.settings import CategoryRoutingSettings, WorkerConcurrencySettings
from packages.domain.entities import Classification
from packages.domain.taxonomy import TaxonomyRegistry


class TestPriorityLaneResolution:
    """Validate priority mapping to routing lanes per R7.2 and design.md §7.1."""

    def test_canonical_lanes_defined(self) -> None:
        """System defines at minimum 'normal' and 'priority' lanes (R7.2)."""
        assert "normal" in CANONICAL_LANES
        assert "priority" in CANONICAL_LANES

    @pytest.mark.parametrize(
        "priority,expected_lane",
        [
            ("urgent", "priority"),
            ("high", "priority"),
            ("priority", "priority"),
            ("critical", "priority"),
            ("URGENT", "priority"),
            (" HIGH ", "priority"),
            ("normal", "normal"),
            ("low", "normal"),
            ("standard", "normal"),
            ("", "normal"),
            (None, "normal"),
        ],
    )
    def test_resolve_priority_lane(self, priority: str | None, expected_lane: str) -> None:
        """Urgency levels resolve to priority lane, standard levels resolve to normal lane."""
        assert resolve_priority_lane(priority) == expected_lane

    def test_high_priority_levels_membership(self) -> None:
        """Verify high priority levels set."""
        for level in ["urgent", "high", "priority", "critical"]:
            assert level in HIGH_PRIORITY_LEVELS


class TestRoutingKeyFormatting:
    """Validate topic routing key generation per R7.1."""

    def test_format_routing_key_canonical_categories(self) -> None:
        """Assert routing key format email.<category>.<lane> for canonical categories."""
        assert format_routing_key("billing", "urgent") == "email.billing.priority"
        assert format_routing_key("billing", "normal") == "email.billing.normal"
        assert format_routing_key("support", "high") == "email.support.priority"
        assert format_routing_key("support", "low") == "email.support.normal"
        assert format_routing_key("sales", "priority") == "email.sales.priority"
        assert format_routing_key("general_inquiry", "normal") == "email.general_inquiry.normal"

    def test_format_routing_key_alias_normalization(self) -> None:
        """Category aliases normalize to canonical forms in routing keys."""
        assert format_routing_key("technical_support", "urgent") == "email.support.priority"
        assert format_routing_key("invoice", "normal") == "email.billing.normal"
        assert format_routing_key("calendar", "high") == "email.scheduling.priority"
        assert format_routing_key("lead", "normal") == "email.sales.normal"

    def test_format_routing_key_default_priority(self) -> None:
        """When priority is omitted or None, defaults to normal lane."""
        assert format_routing_key("billing") == "email.billing.normal"
        assert format_routing_key("support", None) == "email.support.normal"


class TestUnconsumedQueueDetection:
    """Validate detection of queues without configured consumers per R7.6."""

    def test_exact_match(self) -> None:
        """Configured exact queue matches are recognized as consumed."""
        consumers = ["email.support.normal", "email.billing.priority"]
        assert is_queue_consumed("email.support.normal", consumers) is True
        assert is_queue_consumed("email.billing.priority", consumers) is True
        assert is_queue_consumed("email.sales.normal", consumers) is False

    def test_wildcard_pattern_match(self) -> None:
        """Glob wildcards match category or priority dimensions."""
        # Wildcard on category
        consumers_priority = ["email.*.priority"]
        assert is_queue_consumed("email.support.priority", consumers_priority) is True
        assert is_queue_consumed("email.billing.priority", consumers_priority) is True
        assert is_queue_consumed("email.support.normal", consumers_priority) is False

        # Wildcard on lane
        consumers_support = ["email.support.*"]
        assert is_queue_consumed("email.support.normal", consumers_support) is True
        assert is_queue_consumed("email.support.priority", consumers_support) is True
        assert is_queue_consumed("email.billing.normal", consumers_support) is False

    def test_empty_patterns_handled(self) -> None:
        """Empty or whitespace-only patterns do not trigger false matches."""
        consumers = ["", "  "]
        assert is_queue_consumed("email.support.normal", consumers) is False


class TestRouteEnvelopePreparation:
    """Validate JobEnvelope preparation carrying classification snapshots per R7.1 and R7.3."""

    def test_prepare_route_envelope_from_classification_entity(self) -> None:
        """JobEnvelope updated with generate_reply and carries classification snapshot."""
        org_id = str(uuid4())
        msg_id = str(uuid4())
        thread_id = str(uuid4())

        envelope = JobEnvelope(
            idempotency_key="org:mbx:msg-1:generate",
            job_type="triage",
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            attempt=2,
        )

        cls = Classification(
            category="billing",
            intent="invoice_inquiry",
            priority="urgent",
            reply_required=True,
            retrieval_required=True,
            workflow_hint="ai",
            confidence=0.95,
            decided_by="rule",
        )

        routing_key, routed_envelope = prepare_route_envelope(envelope, cls)

        # 1. Routing key
        assert routing_key == "email.billing.priority"

        # 2. Envelope fields
        assert routed_envelope.job_type == "generate_reply"
        assert routed_envelope.attempt == 0
        assert routed_envelope.organization_id == org_id
        assert routed_envelope.message_id == msg_id
        assert routed_envelope.thread_id == thread_id

        # 3. Snapshot preservation (R7.3)
        assert routed_envelope.category == "billing"
        assert routed_envelope.priority == "urgent"
        assert routed_envelope.reply_required is True
        assert routed_envelope.retrieval_required is True
        assert routed_envelope.workflow_hint == "ai"
        assert routed_envelope.classification["intent"] == "invoice_inquiry"
        assert routed_envelope.classification["confidence"] == 0.95

    def test_prepare_route_envelope_from_dict(self) -> None:
        """prepare_route_envelope handles dict classification payload."""
        envelope = JobEnvelope(
            idempotency_key="org:mbx:msg-2:generate",
            job_type="triage",
            organization_id=str(uuid4()),
        )
        cls_dict = {
            "category": "technical_support",
            "priority": "normal",
            "reply_required": True,
            "retrieval_required": True,
            "workflow_hint": "ai",
        }

        routing_key, routed_envelope = prepare_route_envelope(envelope, cls_dict)
        assert routing_key == "email.support.normal"
        assert routed_envelope.job_type == "generate_reply"
        assert routed_envelope.category == "support"


class TestDeclarativeCategoryConfiguration:
    """Validate declarative YAML category configuration and dynamic registration (R7.4)."""

    def test_load_categories_from_default_yaml(self) -> None:
        """Verify loading the canonical config/categories.yaml file."""
        config_path = Path("config/categories.yaml")
        assert config_path.is_file()

        registry = TaxonomyRegistry()
        loaded = load_categories_from_yaml(config_path, registry=registry)
        assert len(loaded) >= 9

        # Verify all canonical categories are registered
        for cat in [
            "support",
            "sales",
            "billing",
            "administration",
            "scheduling",
            "general_inquiry",
            "automated_notification",
            "acknowledgement",
            "no_response",
        ]:
            defn = registry.get(cat)
            assert defn is not None
            assert defn.category == cat

    def test_add_new_custom_category_via_yaml(self) -> None:
        """Adding a new category in YAML registers it without code change (R7.4)."""
        with TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "custom_categories.yaml"
            yaml_path.write_text(
                """
categories:
  - category: partnerships
    description: "Co-marketing, joint ventures, and strategic alliances."
    default_reply_required: true
    default_retrieval_required: true
    default_workflow_hint: ai
    default_priority: normal
    intents:
      - co_marketing
      - integration_partner
    aliases:
      - partner
      - alliance
                """,
                encoding="utf-8",
            )

            registry = TaxonomyRegistry()
            assert not registry.is_valid("partnerships")

            loaded = load_categories_from_yaml(yaml_path, registry=registry)
            assert len(loaded) == 1
            assert loaded[0].category == "partnerships"

            # Verify registry recognises the new category and alias
            assert registry.is_valid("partnerships")
            assert registry.is_valid("partner")
            assert registry.normalize("partner") == "partnerships"
            defn = registry.get("partnerships")
            assert defn is not None
            assert "co_marketing" in defn.intents

    def test_load_categories_missing_file_handled_gracefully(self) -> None:
        """Non-existent file logs a warning and returns an empty list without error."""
        registry = TaxonomyRegistry()
        loaded = load_categories_from_yaml("non_existent_categories.yaml", registry=registry)
        assert loaded == []


class TestConsumerScalingConfiguration:
    """Validate independent consumer scaling settings for lanes per R7.2."""

    def test_worker_concurrency_lane_defaults(self) -> None:
        """WorkerConcurrencySettings defines independent scaling for normal and priority lanes."""
        concurrency = WorkerConcurrencySettings()
        assert concurrency.ai_worker_normal_concurrency == 4
        assert concurrency.ai_worker_priority_concurrency == 8
        assert concurrency.ai_worker_normal_prefetch == 10
        assert concurrency.ai_worker_priority_prefetch == 5

    def test_category_routing_settings_defaults(self) -> None:
        """CategoryRoutingSettings defines default priority lanes and configured consumers."""
        routing_cfg = CategoryRoutingSettings()
        assert routing_cfg.priority_lanes == ["normal", "priority"]
        assert len(routing_cfg.configured_consumers) > 0
        assert "email.support.normal" in routing_cfg.configured_consumers
        assert "email.support.priority" in routing_cfg.configured_consumers
        assert "email.billing.priority" in routing_cfg.configured_consumers

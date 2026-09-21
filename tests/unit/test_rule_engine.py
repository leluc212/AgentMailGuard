"""Comprehensive unit tests for the declarative Stage 1 Rule Engine (R6.1, R6.8, R24.3).

Tests:
1. Field-level predicates and operators (headers, sender, subject, body, attachments).
2. Composite condition operators (any, all, not).
3. Rule actions and Classification output contracts (R6.3).
4. Dynamic hot-reloading with mtime detection and error isolation.
5. Regression suite over RFC 822 fixture emails in tests/fixtures/triage/.
6. Sub-2ms execution latency benchmark.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

from packages.domain.entities import NormalizedMessage
from packages.domain.rules import (
    EmailContext,
    FieldPredicate,
    RuleEngine,
    parse_condition,
)
from services.email_worker.normalizer import EmailNormalizer, NormalizationContext
from services.triage_worker.rules import (
    HotReloadableRuleEngine,
    load_rules_from_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "triage"
CONFIG_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "triage_rules.yaml"


def test_field_predicates_and_operators() -> None:
    """Test atomic field predicates (exists, equals, contains, starts_with, ends_with, matches)."""
    context = EmailContext(
        sender_email="billing@vendor.com",
        sender_name="Vendor Accounts",
        subject="Invoice INV-2026-10294 Due Soon",
        body_text_clean="Please review invoice INV-2026-10294. Balance due: $450.",
        headers={
            "auto-submitted": "auto-generated",
            "list-unsubscribe": "<mailto:unsub@vendor.com>",
        },
        attachments_count=2,
        attachments_filenames=("invoice.pdf", "receipt.png"),
    )

    # Header existence and equality
    p_header_exists = FieldPredicate(field_path="header.Auto-Submitted", exists=True)
    assert p_header_exists.evaluate(context) is True

    p_header_not_exists = FieldPredicate(field_path="header.X-Nonexistent", exists=True)
    assert p_header_not_exists.evaluate(context) is False

    p_header_equals = FieldPredicate(field_path="header.Auto-Submitted", equals="auto-generated")
    assert p_header_equals.evaluate(context) is True

    # Sender regex and equality
    p_sender_matches = FieldPredicate(field_path="sender.email", regex_pattern=r"^billing@")
    assert p_sender_matches.evaluate(context) is True

    p_sender_wrong = FieldPredicate(field_path="sender.email", regex_pattern=r"^support@")
    assert p_sender_wrong.evaluate(context) is False

    # Subject regex
    p_subject_inv = FieldPredicate(field_path="subject", regex_pattern=r"\bINV-\d{4}-\d{5}\b")
    assert p_subject_inv.evaluate(context) is True

    # Body contains and regex
    p_body_contains = FieldPredicate(field_path="body", contains="balance due")
    assert p_body_contains.evaluate(context) is True

    # Attachments
    p_att_count = FieldPredicate(field_path="has_attachments", exists=True)
    assert p_att_count.evaluate(context) is True

    p_att_filename = FieldPredicate(field_path="attachments.filenames", contains="invoice.pdf")
    assert p_att_filename.evaluate(context) is True


def test_composite_conditions_any_all_not() -> None:
    """Test composite boolean conditions (any, all, not)."""
    context = EmailContext(
        sender_email="marketing@newsletter.com",
        subject="Weekly Updates and Insights",
        headers={"list-unsubscribe": "<https://unsub.newsletter.com>"},
    )

    # 'any' condition: one matches, one does not
    cond_any = parse_condition({
        "any": [
            {"header.auto-submitted": {"exists": True}},
            {"header.list-unsubscribe": {"exists": True}},
        ]
    })
    assert cond_any.evaluate(context) is True

    # 'all' condition: one matches, one does not -> False
    cond_all_fail = parse_condition({
        "all": [
            {"header.auto-submitted": {"exists": True}},
            {"header.list-unsubscribe": {"exists": True}},
        ]
    })
    assert cond_all_fail.evaluate(context) is False

    # 'all' condition: both match -> True
    cond_all_pass = parse_condition({
        "all": [
            {"header.list-unsubscribe": {"exists": True}},
            {"sender.email": {"contains": "newsletter"}},
        ]
    })
    assert cond_all_pass.evaluate(context) is True

    # 'not' condition: negates matching predicate
    cond_not = parse_condition({
        "not": {"header.auto-submitted": {"exists": True}}
    })
    assert cond_not.evaluate(context) is True


def test_rule_action_and_classification_contract() -> None:
    """Test that a matching rule generates a Classification entity matching R6.3."""
    rule_dict = {
        "id": "test-urgent-billing",
        "description": "Urgent collection alert",
        "when": {
            "all": [
                {"body.matches": r"\b(overdue|collection)\b"},
                {"sender.email": {"contains": "finance"}},
            ]
        },
        "then": {
            "category": "billing",
            "intent": "overdue_notice",
            "priority": "urgent",
            "reply_required": True,
            "workflow_hint": "ai",
            "retrieval_required": True,
            "confidence": 0.98,
        },
    }
    engine = RuleEngine.from_dict({"rules": [rule_dict]})

    matching_context = EmailContext(
        sender_email="finance@vendor.com",
        body_text_clean="Your payment is past due and account is in collection status.",
    )
    result = engine.evaluate(matching_context)
    assert result is not None
    assert result.category == "billing"
    assert result.intent == "overdue_notice"
    assert result.priority == "urgent"
    assert result.reply_required is True
    assert result.workflow_hint == "ai"
    assert result.retrieval_required is True
    assert result.confidence == 0.98
    assert result.decided_by == "rule"
    assert result.raw.get("rule_id") == "test-urgent-billing"
    assert result.latency_ms >= 0

    # Non-matching context returns None (fall-through to Stage 2)
    non_matching = EmailContext(
        sender_email="support@vendor.com",
        body_text_clean="Everything is working normally.",
    )
    assert engine.evaluate(non_matching) is None


def test_hot_reload_mechanism_and_error_isolation(tmp_path: Path) -> None:
    """Test dynamic file mtime detection, reloading, and error isolation on malformed edits."""
    rules_file = tmp_path / "test_rules.yaml"

    initial_yaml = """
rules:
  - id: rule-v1
    when:
      subject: {equals: "v1 subject"}
    then:
      category: support
      confidence: 0.90
"""
    rules_file.write_text(initial_yaml, encoding="utf-8")

    engine = HotReloadableRuleEngine(rules_path=rules_file, auto_reload=True)
    assert engine.rules_count == 1

    msg_v1 = EmailContext(subject="v1 subject")
    res1 = engine.evaluate(msg_v1)
    assert res1 is not None
    assert res1.category == "support"
    assert res1.raw.get("rule_id") == "rule-v1"

    # Step 1: Update rule file on disk (bump mtime)
    time.sleep(0.05)
    updated_yaml = """
rules:
  - id: rule-v2
    when:
      subject: {equals: "v2 subject"}
    then:
      category: sales
      confidence: 0.95
"""
    rules_file.write_text(updated_yaml, encoding="utf-8")
    os.utime(rules_file, (time.time() + 1, time.time() + 1))

    # Evaluate triggers automatic reload
    msg_v2 = EmailContext(subject="v2 subject")
    res2 = engine.evaluate(msg_v2)
    assert res2 is not None
    assert res2.category == "sales"
    assert res2.raw.get("rule_id") == "rule-v2"

    # Old v1 rule is no longer active
    assert engine.evaluate(msg_v1) is None

    # Step 2: Write malformed YAML with syntax error
    time.sleep(0.05)
    malformed_yaml = """
rules:
  - id: broken-rule
    when: [unclosed bracket
"""
    rules_file.write_text(malformed_yaml, encoding="utf-8")
    os.utime(rules_file, (time.time() + 2, time.time() + 2))

    # Evaluate should catch error and keep rule-v2 intact without crashing
    res_after_error = engine.evaluate(msg_v2)
    assert res_after_error is not None
    assert res_after_error.category == "sales"
    assert res_after_error.raw.get("rule_id") == "rule-v2"


def test_fixture_emails_regression_suite() -> None:
    """Execute all 9 fixture emails against the authoritative config/triage_rules.yaml."""
    assert CONFIG_RULES_PATH.exists(), f"Missing config file: {CONFIG_RULES_PATH}"
    assert FIXTURES_DIR.exists(), f"Missing fixtures dir: {FIXTURES_DIR}"

    engine = HotReloadableRuleEngine(rules_path=CONFIG_RULES_PATH, auto_reload=False)
    assert engine.rules_count >= 10

    normalizer = EmailNormalizer()

    def normalize_fixture(filename: str) -> NormalizedMessage:
        path = FIXTURES_DIR / filename
        assert path.exists(), f"Fixture file not found: {filename}"
        raw_bytes = path.read_bytes()
        ctx = NormalizationContext(
            organization_id=uuid4(),
            mailbox_id=uuid4(),
            message_id=uuid4(),
            provider="gmail",
            provider_message_id=filename,
        )
        return normalizer.normalize(raw_bytes, ctx).message

    # 1. Auto-Submitted
    msg_01 = normalize_fixture("01_auto_submitted.eml")
    res_01 = engine.evaluate(msg_01)
    assert res_01 is not None
    assert res_01.category == "automated_notification"
    assert res_01.intent == "auto_submitted"
    assert res_01.reply_required is False
    assert res_01.workflow_hint == "none"
    assert res_01.confidence >= 0.95

    # 2. Newsletter / List-Unsubscribe
    msg_02 = normalize_fixture("02_newsletter.eml")
    res_02 = engine.evaluate(msg_02)
    assert res_02 is not None
    assert res_02.category == "automated_notification"
    assert res_02.intent == "newsletter"
    assert res_02.reply_required is False
    assert res_02.confidence >= 0.95

    # 3. No-reply sender
    msg_03 = normalize_fixture("03_no_reply.eml")
    res_03 = engine.evaluate(msg_03)
    assert res_03 is not None
    assert res_03.category == "automated_notification"
    assert res_03.intent == "system_notification"
    assert res_03.reply_required is False
    assert res_03.confidence >= 0.95

    # 4. Out of office
    msg_04 = normalize_fixture("04_out_of_office.eml")
    res_04 = engine.evaluate(msg_04)
    assert res_04 is not None
    assert res_04.category == "automated_notification"
    assert res_04.intent == "out_of_office"
    assert res_04.reply_required is False
    assert res_04.confidence >= 0.95

    # 5. Delivery status notification (bounce)
    msg_05 = normalize_fixture("05_delivery_status_notification.eml")
    res_05 = engine.evaluate(msg_05)
    assert res_05 is not None
    assert res_05.category == "automated_notification"
    assert res_05.intent == "bounce"
    assert res_05.reply_required is False
    assert res_05.confidence >= 0.95

    # 6. Invoice inquiry (INV-YYYY-NNNNN pattern)
    msg_06 = normalize_fixture("06_invoice_inquiry.eml")
    res_06 = engine.evaluate(msg_06)
    assert res_06 is not None
    assert res_06.category == "billing"
    assert res_06.intent == "invoice_inquiry"
    assert res_06.reply_required is True
    assert res_06.retrieval_required is True
    assert res_06.confidence >= 0.95

    # 7. Urgent billing overdue notice
    msg_07 = normalize_fixture("07_urgent_billing.eml")
    res_07 = engine.evaluate(msg_07)
    assert res_07 is not None
    assert res_07.category == "billing"
    assert res_07.intent == "overdue_payment"
    assert res_07.priority == "urgent"
    assert res_07.reply_required is True
    assert res_07.confidence >= 0.95

    # 8. Calendar meeting invite
    msg_08 = normalize_fixture("08_calendar_invite.eml")
    res_08 = engine.evaluate(msg_08)
    assert res_08 is not None
    assert res_08.category == "scheduling"
    assert res_08.intent == "calendar_event"
    assert res_08.reply_required is False
    assert res_08.confidence >= 0.95

    # 9. Actionable support inquiry (must fall through with None)
    msg_09 = normalize_fixture("09_actionable_support.eml")
    res_09 = engine.evaluate(msg_09)
    assert res_09 is None, (
        "Actionable customer inquiry should NOT be matched by triage stage 1 rules; "
        "it must fall through to stage 2 ML classifier"
    )


def test_rule_engine_evaluation_latency_sub_2ms() -> None:
    """Assert rule engine evaluates in < 2ms per email (~1ms target in specs/design.md §5.3)."""
    engine = load_rules_from_file(CONFIG_RULES_PATH)
    sample_context = EmailContext(
        sender_email="someone@external-client.com",
        sender_name="John Doe",
        subject="Important discussion regarding renewal terms",
        body_text_clean="Hello, let's schedule time next week to review our partnership contract.",
    )

    iterations = 200
    start = time.perf_counter()
    for _ in range(iterations):
        engine.evaluate(sample_context)
    total_elapsed = time.perf_counter() - start
    avg_ms = (total_elapsed / iterations) * 1000

    # Rule evaluation should be well under 2 milliseconds
    assert avg_ms < 2.0, f"Average evaluation took {avg_ms:.3f}ms, expected < 2.0ms"

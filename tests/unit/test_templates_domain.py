"""Unit tests for pure domain templates and variable substitution engine (R6.12, R6.13, R6.14)."""

import pytest

from packages.domain.entities import EmailAddress, NormalizedMessage
from packages.domain.rules import EmailContext
from packages.domain.templates import (
    TemplateDefinition,
    TemplateRegistry,
    build_template_context,
    substitute_variables,
)


def test_template_definition_from_dict_success() -> None:
    data = {
        "id": "ack_v1",
        "category": "acknowledgement",
        "intent": "receipt_confirmation",
        "subject": "Re: {{ subject }}",
        "body": "Thank you for contacting us.",
        "version": "v1.2",
    }
    tmpl = TemplateDefinition.from_dict(data)
    assert tmpl.id == "ack_v1"
    assert tmpl.category == "acknowledgement"
    assert tmpl.intent == "receipt_confirmation"
    assert tmpl.subject == "Re: {{ subject }}"
    assert tmpl.body == "Thank you for contacting us."
    assert tmpl.version == "v1.2"
    assert tmpl.is_active is True


def test_template_definition_from_match_spec() -> None:
    data = {
        "match": {"category": "Scheduling", "intent": "Meeting_Accepted"},
        "body": "Meeting confirmed.",
    }
    tmpl = TemplateDefinition.from_dict(data)
    assert tmpl.category == "scheduling"
    assert tmpl.intent == "meeting_accepted"
    assert tmpl.subject == "Re: {{ subject }}"
    assert tmpl.body == "Meeting confirmed."


def test_template_definition_missing_fields_raises() -> None:
    with pytest.raises(ValueError, match="category"):
        TemplateDefinition.from_dict({"intent": "receipt_confirmation"})

    with pytest.raises(ValueError, match="intent"):
        TemplateDefinition.from_dict({"category": "acknowledgement"})


def test_variable_substitution_flat_and_nested() -> None:
    context = {
        "subject": "Billing Question",
        "sender": {"name": "Alice Smith", "email": "alice@example.com"},
        "sender_name": "Alice Smith",
        "order_id": "ORD-12345",
        "business_data": {"tier": "enterprise"},
    }

    tpl = (
        "Hello {{ sender.name }}, we received your email '{{ subject }}' regarding "
        "order {{ order_id }} (Tier: {{ tier }})."
    )
    rendered, used = substitute_variables(tpl, context)

    assert "Hello Alice Smith" in rendered
    assert "Billing Question" in rendered
    assert "ORD-12345" in rendered
    assert "enterprise" in rendered
    assert used["sender.name"] == "Alice Smith"
    assert used["subject"] == "Billing Question"
    assert used["order_id"] == "ORD-12345"
    assert used["tier"] == "enterprise"


def test_variable_substitution_missing_var_fallback() -> None:
    context = {"subject": "Test"}
    tpl = "Subject: {{ subject }}, Note: {{ missing_field }}!"
    rendered, used = substitute_variables(tpl, context, fallback_empty=True)

    assert rendered == "Subject: Test, Note: !"
    assert used["missing_field"] == ""


def test_build_template_context_from_normalized_message() -> None:
    from datetime import UTC, datetime

    msg = NormalizedMessage(
        message_id="msg-101",
        organization_id="org-1",
        mailbox_id="mb-1",
        thread_id="th-1",
        provider="fake",
        provider_message_id="p-101",
        rfc822_message_id="<msg101@example.com>",
        sender=EmailAddress(name="Bob Jones", email="bob@example.com"),
        received_at=datetime.now(UTC),
        recipients=[EmailAddress(name="Support", email="support@company.com")],
        cc=[],
        subject="Request for quote",
        subject_normalized="Request for quote",
        body_text="Can I get pricing?",
        body_text_clean="Can I get pricing?",
        direction="inbound",
    )
    biz = {"customer_id": "CUST-999", "tier": "gold"}
    ctx = build_template_context(msg, business_data=biz)

    assert ctx["subject"] == "Request for quote"
    assert ctx["sender_name"] == "Bob Jones"
    assert ctx["sender_email"] == "bob@example.com"
    assert ctx["sender"]["name"] == "Bob Jones"
    assert ctx["customer_id"] == "CUST-999"
    assert ctx["business_data"]["tier"] == "gold"
    assert ctx["message_id"] == "<msg101@example.com>"


def test_build_template_context_from_email_context() -> None:
    e_ctx = EmailContext(
        sender_email="carol@example.com",
        sender_name="Carol",
        recipients=("team@company.com",),
        cc=(),
        subject="Sync meeting",
        subject_normalized="Sync meeting",
        body_text="Confirming our sync.",
        body_text_clean="Confirming our sync.",
        headers={"content-type": "text/plain"},
        attachments_count=0,
        attachments_filenames=(),
    )
    ctx = build_template_context(e_ctx)
    assert ctx["subject"] == "Sync meeting"
    assert ctx["sender_name"] == "Carol"
    assert ctx["sender_email"] == "carol@example.com"


def test_template_registry_registration_and_lookup() -> None:
    registry = TemplateRegistry()
    t1 = TemplateDefinition(
        id="ack_receipt",
        category="acknowledgement",
        intent="receipt_confirmation",
        subject="Re: {{ subject }}",
        body="Thank you {{ sender_name }}.",
    )
    registry.register(t1)

    assert len(registry.templates) == 1
    # Match exact
    found = registry.find_template("acknowledgement", "receipt_confirmation")
    assert found is not None
    assert found.id == "ack_receipt"

    # Match case-insensitive and with whitespace
    found_ci = registry.find_template(" ACKNOWLEDGEMENT ", " RECEIPT_CONFIRMATION ")
    assert found_ci is not None
    assert found_ci.id == "ack_receipt"

    # Non-match returns None
    assert registry.find_template("billing", "invoice_inquiry") is None
    assert registry.find_template("acknowledgement", "other_intent") is None


def test_template_registry_render() -> None:
    registry = TemplateRegistry()
    t1 = TemplateDefinition(
        id="ack_receipt",
        category="acknowledgement",
        intent="receipt_confirmation",
        subject="Re: {{ subject }}",
        body=(
            "Dear {{ sender_name }},\n\n"
            "We received message {{ message_id }}.\n"
            "Ref: {{ order_id }}"
        ),
    )
    registry.register(t1)

    msg_data = {
        "subject": "Help Needed",
        "sender": {"name": "Dave", "email": "dave@example.com"},
        "id": "msg-555",
    }
    biz_data = {"order_id": "ORD-777"}

    result = registry.render(t1, message=msg_data, business_data=biz_data)

    assert result.template_id == "ack_receipt"
    assert result.subject == "Re: Help Needed"
    assert "Dear Dave," in result.body
    assert "message msg-555" in result.body
    assert "Ref: ORD-777" in result.body
    assert result.variables_used["sender_name"] == "Dave"
    assert result.variables_used["order_id"] == "ORD-777"


def test_template_registry_from_dict_and_json() -> None:
    data = {
        "templates": [
            {
                "id": "t1",
                "match": {"category": "scheduling", "intent": "meeting_accepted"},
                "subject": "Accepted: {{ subject }}",
                "body": "Meeting confirmed for {{ sender_name }}.",
            }
        ]
    }
    registry = TemplateRegistry.from_dict(data)
    assert len(registry.templates) == 1
    tmpl = registry.find_template("scheduling", "meeting_accepted")
    assert tmpl is not None
    assert tmpl.subject == "Accepted: {{ subject }}"

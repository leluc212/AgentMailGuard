"""scripts/phase6_gate.py checks what the Phase 6 gate claims (6.10; R17.1–R17.7)."""

from __future__ import annotations

import importlib.util
import sys
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from packages.domain.taxonomy import TaxonomyRegistry

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ORIGINAL_ID = "cust-123@customer.example.com"
REPLY_ID = "reply-9@rag-email.local"
SUBJECT = "Overdue payment failure on account [gate-abc123]"


def _load_gate() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the gate imports its siblings stack_smoke, connect_gmail
    spec = importlib.util.spec_from_file_location("phase6_gate", SCRIPTS / "phase6_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves the module through sys.modules (importlib's documented recipe).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _registry(mode: str) -> TaxonomyRegistry:
    registry = TaxonomyRegistry()
    registry.register_from_dict(
        {
            "category": "billing",
            "description": "Billing",
            "default_reply_required": True,
            "default_retrieval_required": True,
            "default_workflow_hint": "ai",
            "dispatch_mode": mode,
        }
    )
    return registry


def _mime(msg_id: str, subject: str, in_reply_to: str | None, references: str | None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "demo@gmail.com"
    msg["To"] = "casey@customer.example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{msg_id}>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content("body")
    return msg.as_bytes()


def _snapshot(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "job_state": "COMPLETED",
        "outbound_rows": 1,
        "thread_messages": 2,
        "transitions": 9,
        "feedback_rows": 1,
    }
    values.update(overrides)
    return gate.ReplaySnapshot(**values)


def test_gate_subject_hits_the_urgent_billing_rule_and_carries_the_token() -> None:
    subject = gate.gate_subject("abc123")
    assert "[gate-abc123]" in subject
    assert "payment failure" in subject.lower() and "account" in subject.lower()


def test_dispatch_mode_send_reply_is_accepted() -> None:
    gate.check_dispatch_mode(_registry("send_reply"), "billing")


def test_dispatch_mode_create_draft_names_the_file_to_edit() -> None:
    with pytest.raises(gate.SmokeFailure, match="config/categories.yaml"):
        gate.check_dispatch_mode(_registry("create_draft"), "billing")


def test_unknown_category_is_refused() -> None:
    with pytest.raises(gate.SmokeFailure, match="support"):
        gate.check_dispatch_mode(_registry("send_reply"), "support")


def test_api_dispatch_mode_must_say_send_reply() -> None:
    """The review UI shows the API's field; a stale API would tell the reviewer 'draft'."""
    gate.check_api_dispatch_mode({"dispatch_mode": "send_reply"})
    with pytest.raises(gate.SmokeFailure, match="make up"):
        gate.check_api_dispatch_mode({"dispatch_mode": "create_draft"})
    with pytest.raises(gate.SmokeFailure, match="dispatch_mode"):
        gate.check_api_dispatch_mode({})


def test_dispatch_queue_needs_a_consumer() -> None:
    gate.check_dispatch_consumer({"email.dispatch": 1}, "email.dispatch")
    with pytest.raises(gate.SmokeFailure, match="email.dispatch"):
        gate.check_dispatch_consumer({"email.dispatch": 0}, "email.dispatch")
    with pytest.raises(gate.SmokeFailure, match="email.dispatch"):
        gate.check_dispatch_consumer({}, "email.dispatch")


def test_listed_draft_is_found_by_id() -> None:
    page = {"items": [{"id": "d-1"}, {"id": "d-2"}], "next_cursor": None}
    assert gate.find_listed_draft(page, "d-2") == {"id": "d-2"}
    with pytest.raises(gate.SmokeFailure, match="d-3"):
        gate.find_listed_draft(page, "d-3")


def test_outbound_row_must_reply_to_the_original() -> None:
    good = {
        "rfc822_message_id": REPLY_ID,
        "in_reply_to": ORIGINAL_ID,
        "subject": f"Re: {SUBJECT}",
        "provider_message_id": "gm-sent-1",
    }
    gate.check_outbound_row(good, original_rfc822_id=ORIGINAL_ID, original_subject=SUBJECT)
    for key, bad in [
        ("rfc822_message_id", None),
        ("in_reply_to", "other@x"),
        ("subject", f"Re: Re: {SUBJECT}"),
    ]:
        with pytest.raises(gate.SmokeFailure, match=key):
            gate.check_outbound_row(
                {**good, key: bad}, original_rfc822_id=ORIGINAL_ID, original_subject=SUBJECT
            )


def test_gmail_thread_holds_our_threaded_reply() -> None:
    messages = [
        ("gm-orig", _mime(ORIGINAL_ID, SUBJECT, None, None)),
        ("gm-sent-1", _mime(REPLY_ID, f"Re: {SUBJECT}", f"<{ORIGINAL_ID}>", f"<{ORIGINAL_ID}>")),
    ]
    gate.check_gmail_thread(
        messages,
        sent_provider_id="gm-sent-1",
        reply_rfc822_id=REPLY_ID,
        original_rfc822_id=ORIGINAL_ID,
        original_subject=SUBJECT,
    )


def test_gmail_thread_without_our_message_fails() -> None:
    with pytest.raises(gate.SmokeFailure, match="gm-sent-1"):
        gate.check_gmail_thread(
            [("gm-orig", _mime(ORIGINAL_ID, SUBJECT, None, None))],
            sent_provider_id="gm-sent-1",
            reply_rfc822_id=REPLY_ID,
            original_rfc822_id=ORIGINAL_ID,
            original_subject=SUBJECT,
        )


def test_gmail_replacing_our_message_id_fails_with_the_reason() -> None:
    """Research §8 Q1: if Gmail rewrites Message-ID, our stored id cannot thread replies."""
    messages = [
        (
            "gm-sent-1",
            _mime("gmail-made@mail.gmail.com", f"Re: {SUBJECT}", f"<{ORIGINAL_ID}>", None),
        )
    ]
    with pytest.raises(gate.SmokeFailure, match="Message-ID"):
        gate.check_gmail_thread(
            messages,
            sent_provider_id="gm-sent-1",
            reply_rfc822_id=REPLY_ID,
            original_rfc822_id=ORIGINAL_ID,
            original_subject=SUBJECT,
        )


def test_gmail_reply_must_carry_in_reply_to() -> None:
    messages = [("gm-sent-1", _mime(REPLY_ID, f"Re: {SUBJECT}", None, None))]
    with pytest.raises(gate.SmokeFailure, match="In-Reply-To"):
        gate.check_gmail_thread(
            messages,
            sent_provider_id="gm-sent-1",
            reply_rfc822_id=REPLY_ID,
            original_rfc822_id=ORIGINAL_ID,
            original_subject=SUBJECT,
        )


def test_replay_that_changes_nothing_passes() -> None:
    gate.check_replay(_snapshot(), _snapshot())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_state", "DISPATCHED"),
        ("outbound_rows", 2),
        ("thread_messages", 3),
        ("transitions", 10),
        ("feedback_rows", 2),
    ],
)
def test_replay_that_changes_anything_fails(field: str, value: Any) -> None:
    with pytest.raises(gate.SmokeFailure, match=field):
        gate.check_replay(_snapshot(), _snapshot(**{field: value}))

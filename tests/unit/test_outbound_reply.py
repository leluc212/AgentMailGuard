"""Pure outbound reply construction (R17.2, design.md §5.8 "Outbound reply", task 6.3)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.dispatch import (
    MissingProviderThreadError,
    MissingRecipientError,
    build_outbound_reply,
    build_reply_message_id,
    reply_subject,
)
from packages.domain.entities import EmailAddress, GeneratedDraft, NormalizedMessage

DRAFT_ID = UUID("12345678-1234-5678-1234-567812345678")
DOMAIN = "acme.example"
OUR_MESSAGE_ID = "<dispatch-12345678123456781234567812345678@acme.example>"


def _original(**overrides: Any) -> NormalizedMessage:
    values: dict[str, Any] = {
        "message_id": uuid4(),
        "thread_id": uuid4(),
        "mailbox_id": uuid4(),
        "organization_id": uuid4(),
        "provider": "fake",
        "provider_message_id": "prov-msg-1",
        "sender": EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith"),
        "received_at": datetime(2026, 9, 28, 9, 30, tzinfo=UTC),
        "rfc822_message_id": "orig-1@clientcorp.com",
        "references_ids": ["root-0@clientcorp.com"],
        "subject": "Order status question",
        "body_text": "Where is my order ORD-82915?\n\nThanks,\nAlice",
    }
    values.update(overrides)
    return NormalizedMessage(**values)


def _draft(draft_id: UUID = DRAFT_ID) -> GeneratedDraft:
    return GeneratedDraft(
        id=draft_id,
        subject="Your order",
        body="Hello Alice,\nYour order ORD-82915 has been dispatched.",
    )


def _build(original: NormalizedMessage | None = None, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "draft": _draft(),
        "original": original or _original(),
        "provider_thread_id": "th-provider-77",
        "message_id_domain": DOMAIN,
    }
    params.update(kwargs)
    return build_outbound_reply(**params)


def test_reply_threads_on_the_original() -> None:
    """R17.2: In-Reply-To = original Message-ID; References = its References + its Message-ID."""
    original = _original()
    reply = _build(original)

    assert reply.in_reply_to == "<orig-1@clientcorp.com>"
    assert reply.references == ["<root-0@clientcorp.com>", "<orig-1@clientcorp.com>"]
    assert reply.thread_id == "th-provider-77"
    assert reply.message_id == OUR_MESSAGE_ID
    assert reply.reply_to_provider_message_id == "prov-msg-1"
    assert reply.draft_id == str(DRAFT_ID)
    assert reply.mailbox_id == original.mailbox_id
    assert reply.organization_id == original.organization_id
    assert reply.cc == []


def test_reply_uses_the_provider_thread_id_never_our_uuid() -> None:
    original = _original()
    reply = _build(original, provider_thread_id="  18c2f0a9d1e4b7aa ")
    assert reply.thread_id == "18c2f0a9d1e4b7aa"
    assert reply.thread_id != str(original.thread_id)


@pytest.mark.parametrize("provider_thread_id", [None, "", "   "])
def test_missing_provider_thread_id_is_permanent(provider_thread_id: str | None) -> None:
    """Migration 0003 allows a NULL provider thread id; dispatch must dead-letter (6.3)."""
    original = _original()
    with pytest.raises(MissingProviderThreadError) as exc_info:
        _build(original, provider_thread_id=provider_thread_id)
    assert isinstance(exc_info.value, ValueError)
    assert exc_info.value.draft_id == str(DRAFT_ID)
    assert exc_info.value.thread_id == str(original.thread_id)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Order status question", "Re: Order status question"),
        ("Re: Order status question", "Re: Order status question"),
        ("RE: Order status question", "Re: Order status question"),
        ("re:Order status question", "Re: Order status question"),
        ("Re: RE: re: Order status question", "Re: Order status question"),
        ("  Re :  Order status question  ", "Re: Order status question"),
        ("Regarding the invoice", "Re: Regarding the invoice"),
        ("Reorder request", "Re: Reorder request"),
        ("", "Re:"),
    ],
)
def test_subject_has_exactly_one_re_prefix(subject: str, expected: str) -> None:
    assert reply_subject(subject) == expected
    assert _build(_original(subject=subject)).subject == expected


def test_draft_subject_is_not_used() -> None:
    """The provider keeps the reply in the thread only when the subject matches the original."""
    assert _build().subject == "Re: Order status question"


def test_original_without_references() -> None:
    reply = _build(_original(references_ids=[]))
    assert reply.in_reply_to == "<orig-1@clientcorp.com>"
    assert reply.references == ["<orig-1@clientcorp.com>"]


def test_original_without_message_id() -> None:
    """Review focus 1: no Message-ID on the original still builds a threaded reply."""
    reply = _build(_original(rfc822_message_id=None))
    assert reply.in_reply_to is None
    assert reply.references == ["<root-0@clientcorp.com>"]
    assert reply.thread_id == "th-provider-77"
    assert reply.subject.startswith("Re: ")
    assert reply.message_id == OUR_MESSAGE_ID


def test_references_are_deduplicated_bracketed_and_end_with_the_parent() -> None:
    original = _original(
        references_ids=[
            "root-0@clientcorp.com",
            "<mid-1@clientcorp.com>",
            "root-0@clientcorp.com",
            "orig-1@clientcorp.com",
            "  ",
        ]
    )
    reply = _build(original)
    assert reply.references == [
        "<root-0@clientcorp.com>",
        "<mid-1@clientcorp.com>",
        "<orig-1@clientcorp.com>",
    ]


def test_quoted_original_sits_below_the_reply() -> None:
    reply = _build()
    assert reply.body_text == (
        "Hello Alice,\nYour order ORD-82915 has been dispatched.\n"
        "\n"
        "On 2026-09-28 09:30 UTC, Alice Smith <alice.smith@clientcorp.com> wrote:\n"
        "> Where is my order ORD-82915?\n"
        ">\n"
        "> Thanks,\n"
        "> Alice\n"
    )


def test_quote_header_is_in_utc_whatever_the_original_zone() -> None:
    plus_seven = timezone(timedelta(hours=7))
    reply = _build(_original(received_at=datetime(2026, 9, 28, 16, 30, tzinfo=plus_seven)))
    assert "On 2026-09-28 09:30 UTC, Alice Smith" in reply.body_text


def test_empty_original_body_still_quotes_a_marker() -> None:
    reply = _build(_original(body_text=""))
    assert reply.body_text.endswith("wrote:\n>\n")


def test_sender_is_the_default_recipient() -> None:
    reply = _build()
    assert reply.to == [EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith")]


def test_reply_goes_to_the_sender_even_with_a_reply_to_header() -> None:
    """Reply-To is not persisted on the dispatch path, so the builder does not read it."""
    reply = _build(_original(headers={"reply-to": "billing@clientcorp.com"}))
    assert reply.to == [EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith")]


def test_no_recipient_address_is_permanent() -> None:
    with pytest.raises(MissingRecipientError) as exc_info:
        _build(_original(sender=EmailAddress(email="  ")))
    assert isinstance(exc_info.value, ValueError)
    assert exc_info.value.draft_id == str(DRAFT_ID)


def test_message_id_is_deterministic_per_draft() -> None:
    """design.md §5.8 step 4: a retry rebuilds the same Message-ID so the sent copy is findable."""
    first = _build()
    second = _build()
    other = _build(draft=_draft(uuid4()))
    assert first.message_id == second.message_id == OUR_MESSAGE_ID
    assert other.message_id != first.message_id
    assert build_reply_message_id(str(DRAFT_ID), "@acme.example") == OUR_MESSAGE_ID


@pytest.mark.parametrize("domain", ["", "   ", "bad domain", "a@b.example", "<acme.example>"])
def test_invalid_message_id_domain_is_rejected(domain: str) -> None:
    with pytest.raises(ValueError, match="message_id_domain"):
        build_reply_message_id(DRAFT_ID, domain)


def test_building_a_reply_does_not_mutate_the_inputs() -> None:
    original = _original()
    draft = _draft()
    before = (copy.deepcopy(original), copy.deepcopy(draft))
    build_outbound_reply(
        draft=draft,
        original=original,
        provider_thread_id="th-provider-77",
        message_id_domain=DOMAIN,
    )
    assert (original, draft) == before


def test_outbound_reply_carries_the_original_provider_message_id() -> None:
    """6.3a: Graph createReply needs the original email's provider id on the reply."""
    original = NormalizedMessage(
        message_id="00000000-0000-0000-0000-00000000a001",
        thread_id="00000000-0000-0000-0000-00000000b001",
        mailbox_id="00000000-0000-0000-0000-00000000c001",
        organization_id="00000000-0000-0000-0000-00000000d001",
        provider="graph",
        provider_message_id="AAMkAGI2-orig-001",
        sender=EmailAddress(email="customer@example.com"),
        received_at=datetime(2026, 9, 28, tzinfo=UTC),
        rfc822_message_id="orig-001@example.com",
        subject="Order question",
    )
    draft = GeneratedDraft(body="Your order shipped.", organization_id=original.organization_id)
    reply = build_outbound_reply(
        draft=draft,
        original=original,
        provider_thread_id="conv-001",
        message_id_domain="mail.example.com",
    )
    assert reply.reply_to_provider_message_id == "AAMkAGI2-orig-001"

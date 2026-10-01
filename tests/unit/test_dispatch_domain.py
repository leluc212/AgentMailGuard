"""Phase 6 dispatch domain types and entity changes (R17.1, R17.2, R19.2; ADR-0009)."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import packages.domain as domain
from packages.domain.dispatch import DispatchMode, ProviderDraftStatus
from packages.domain.entities import EmailAddress, GeneratedDraft, OutboundReply


def test_dispatch_mode_values() -> None:
    """R17.1: exactly two dispatch modes, spelled as config/categories.yaml spells them."""
    assert [m.value for m in DispatchMode] == ["create_draft", "send_reply"]
    assert DispatchMode("send_reply") is DispatchMode.SEND_REPLY


def test_provider_draft_status_values() -> None:
    """design.md §5.8 step 4: what get_draft_status reports after an ambiguous send."""
    assert [s.value for s in ProviderDraftStatus] == ["DRAFT", "SENT", "MISSING"]


def test_dispatch_types_are_exported_from_the_domain_package() -> None:
    assert domain.DispatchMode is DispatchMode
    assert domain.ProviderDraftStatus is ProviderDraftStatus
    assert {"DispatchMode", "ProviderDraftStatus"} <= set(domain.__all__)


def test_outbound_reply_thread_field_is_the_provider_thread_id_string() -> None:
    """6.3: the thread field carries the provider thread id as a string, never our UUID."""
    annotations = {f.name: f.type for f in dataclasses.fields(OutboundReply)}
    assert annotations["thread_id"] == "str"
    assert annotations["message_id"] == "str | None"
    assert annotations["reply_to_provider_message_id"] == "str | None"


def test_outbound_reply_new_fields() -> None:
    reply = OutboundReply(
        thread_id="18c2f0a9d1e4b7aa",
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        to=[EmailAddress(email="customer@example.com")],
        body_text="Hello",
        message_id="<dispatch-1@acme.example>",
        reply_to_provider_message_id="prov-msg-1",
    )
    assert reply.thread_id == "18c2f0a9d1e4b7aa"
    assert reply.message_id == "<dispatch-1@acme.example>"
    assert reply.reply_to_provider_message_id == "prov-msg-1"

    bare = OutboundReply(
        thread_id="th-1",
        mailbox_id="mbx-1",
        organization_id="org-1",
        to=[EmailAddress(email="customer@example.com")],
        body_text="Hello",
    )
    assert bare.message_id is None
    assert bare.reply_to_provider_message_id is None


def test_generated_draft_dispatch_handle_defaults_to_none() -> None:
    """ADR-0009: a fresh draft has no provider draft and no claimed dispatch key."""
    draft = GeneratedDraft(body="Hello")
    assert draft.provider_draft_id is None
    assert draft.provider_draft_message_id is None
    assert draft.dispatch_idempotency_key is None

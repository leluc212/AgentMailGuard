"""Pure construction of the outbound reply for a reviewed draft (R17.2, design.md §5.8).

Requirements:
- R17.2: correct threading headers (In-Reply-To, References) and the provider thread id.
- design.md §5.8 "Outbound reply": exactly one "Re: " before the original subject, a new
  Message-ID of our own (MIME-built replies only), the provider thread id (never our UUID),
  and the quoted original below the reply.
- ADR-0009: a null provider thread id dead-letters the dispatch.

Stored ids (email_message.rfc822_message_id, in_reply_to, references_ids) carry no angle
brackets (services/email_worker/parser.py strips them). Every id on the returned
OutboundReply is wrapped in <...>, ready for MIME headers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from packages.domain.entities import (
    EmailAddress,
    GeneratedDraft,
    NormalizedMessage,
    OutboundReply,
)

_REPLY_PREFIX = re.compile(r"^\s*(?:re\s*:\s*)+", re.IGNORECASE)
_DOMAIN_FORBIDDEN = re.compile(r"[<>@\s]")


class MissingProviderThreadError(ValueError):
    """The original's thread has no provider thread id, so the reply cannot join it.

    Permanent: the dispatch dead-letters (design.md §5.8 errors, ADR-0009).
    """

    def __init__(self, *, draft_id: str, thread_id: str) -> None:
        self.draft_id = draft_id
        self.thread_id = thread_id
        super().__init__(
            f"Draft {draft_id}: email_thread {thread_id} has no provider thread id; "
            "the reply cannot be threaded"
        )


class MissingRecipientError(ValueError):
    """The original's sender has no address to reply to. Permanent."""

    def __init__(self, *, draft_id: str) -> None:
        self.draft_id = draft_id
        super().__init__(f"Draft {draft_id}: the original email has no address to reply to")


def reply_subject(original_subject: str) -> str:
    """Return the original subject with exactly one leading ``"Re: "`` (R17.2)."""
    base = _REPLY_PREFIX.sub("", original_subject or "").strip()
    return f"Re: {base}" if base else "Re:"


def build_reply_message_id(draft_id: UUID | str, message_id_domain: str) -> str:
    """Return the deterministic Message-ID ``<dispatch-{draft id hex}@{domain}>``.

    Deterministic per draft, so a redelivered dispatch rebuilds the same header and the
    sent copy can be found by it after an ambiguous send (design.md §5.8 step 4).
    """
    domain = message_id_domain.strip().lstrip("@")
    if not domain or _DOMAIN_FORBIDDEN.search(domain):
        raise ValueError(f"Invalid message_id_domain {message_id_domain!r}")
    return f"<dispatch-{UUID(str(draft_id)).hex}@{domain}>"


def _clean_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("<>").strip()
    return cleaned or None


def _bracket(value: str) -> str:
    return f"<{value}>"


def _references(original: NormalizedMessage, parent_id: str | None) -> list[str]:
    ordered: list[str] = []
    for ref in original.references_ids:
        cleaned = _clean_id(ref)
        if cleaned and cleaned != parent_id and cleaned not in ordered:
            ordered.append(cleaned)
    if parent_id:
        ordered.append(parent_id)
    return [_bracket(ref) for ref in ordered]


def _recipients(original: NormalizedMessage, draft_id: str) -> list[EmailAddress]:
    # Reply-To is not persisted (email_message has no headers column), so the reply goes to
    # the sender; reading original.headers here would work only in unit tests.
    if original.sender.email.strip():
        return [original.sender]
    raise MissingRecipientError(draft_id=draft_id)


def _quote(original: NormalizedMessage) -> str:
    received: datetime = original.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    when = received.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    text = (original.body_text or "").replace("\r\n", "\n").rstrip("\n")
    lines = text.split("\n") if text else [""]
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return f"On {when}, {original.sender} wrote:\n{quoted}\n"


def build_outbound_reply(
    *,
    draft: GeneratedDraft,
    original: NormalizedMessage,
    provider_thread_id: str | None,
    message_id_domain: str,
) -> OutboundReply:
    """Build the reply to ``original`` carrying ``draft``'s body (R17.2, design.md §5.8).

    Raises:
        MissingProviderThreadError: ``provider_thread_id`` is None or blank.
        MissingRecipientError: the sender has no address.
        ValueError: ``message_id_domain`` is not a bare domain.
    """
    draft_id = str(draft.id)
    thread = (provider_thread_id or "").strip()
    if not thread:
        raise MissingProviderThreadError(draft_id=draft_id, thread_id=str(original.thread_id))

    parent_id = _clean_id(original.rfc822_message_id)
    return OutboundReply(
        thread_id=thread,
        mailbox_id=original.mailbox_id,
        organization_id=original.organization_id,
        to=_recipients(original, draft_id),
        body_text=f"{draft.body.rstrip()}\n\n{_quote(original)}",
        subject=reply_subject(original.subject),
        in_reply_to=_bracket(parent_id) if parent_id else None,
        references=_references(original, parent_id),
        draft_id=draft_id,
        message_id=build_reply_message_id(draft.id, message_id_domain),
        reply_to_provider_message_id=original.provider_message_id,
    )

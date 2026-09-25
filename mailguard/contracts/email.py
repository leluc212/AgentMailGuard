"""Input contracts: the email, retrieved chunks, and the draft under inspection.

``GuardedEmail.from_any`` accepts the core system's ``EmailContext``,
``NormalizedMessage`` or a plain dict *without importing the core package*
(duck typing keeps the security subsystem independently deployable).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first present attribute/key among ``names``."""
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
        if not isinstance(obj, dict) and hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _email_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("email") or "")
    return str(getattr(value, "email", value))


class GuardedEmail(BaseModel):
    """Provider-neutral inbound email as seen by the guard layers."""

    message_id: str = ""
    thread_id: str = ""
    organization_id: str = ""
    mailbox_id: str = ""
    sender_email: str = ""
    sender_name: str = ""
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_text_clean: str = Field(default="", description="Quoted history & signature removed")
    body_html: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list, description="Attachment filenames")
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    category: str | None = Field(default=None, description="Triage category if already known")

    @property
    def text(self) -> str:
        """The text the LLM would actually see (clean body preferred)."""
        return self.body_text_clean or self.body_text

    @property
    def full_text(self) -> str:
        """Subject + sender + complete body (scanners look here; injections hide in quotes)."""
        body = self.body_text or self.body_text_clean
        return f"Subject: {self.subject}\nFrom: {self.sender_name} <{self.sender_email}>\n\n{body}"

    @classmethod
    def from_any(cls, obj: Any) -> GuardedEmail:
        """Build from core ``EmailContext`` / ``NormalizedMessage`` / dict / GuardedEmail."""
        if isinstance(obj, GuardedEmail):
            return obj
        sender = _get(obj, "sender")
        sender_email = _get(obj, "sender_email") or _email_str(sender)
        sender_name = _get(obj, "sender_name")
        if not sender_name and sender is not None and not isinstance(sender, str):
            sender_name = _get(sender, "name", default="")
        headers_raw = _get(obj, "headers", default={}) or {}
        headers = {str(k).lower(): str(v) for k, v in dict(headers_raw).items()}
        attachments_raw = _get(obj, "attachments_filenames", "attachments", default=[]) or []
        attachments = [
            a if isinstance(a, str) else str(_get(a, "filename", default=a))
            for a in attachments_raw
        ]
        received = _get(obj, "received_at")
        return cls(
            message_id=str(_get(obj, "message_id", default="") or ""),
            thread_id=str(_get(obj, "thread_id", default="") or ""),
            organization_id=str(_get(obj, "organization_id", default="") or ""),
            mailbox_id=str(_get(obj, "mailbox_id", default="") or ""),
            sender_email=str(sender_email or "").lower(),
            sender_name=str(sender_name or ""),
            recipients=[_email_str(r).lower() for r in (_get(obj, "recipients", default=[]) or [])],
            cc=[_email_str(c).lower() for c in (_get(obj, "cc", default=[]) or [])],
            subject=str(_get(obj, "subject", default="") or ""),
            body_text=str(_get(obj, "body_text", "body", default="") or ""),
            body_text_clean=str(_get(obj, "body_text_clean", default="") or ""),
            body_html=_get(obj, "body_html"),
            headers=headers,
            attachments=attachments,
            received_at=received if isinstance(received, datetime) else datetime.now(UTC),
            category=_get(obj, "category"),
        )


class RetrievedChunk(BaseModel):
    """A knowledge chunk returned by hybrid RAG (mirrors core ``Candidate`` by duck typing)."""

    chunk_id: str
    document_id: str = ""
    content: str
    external_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    rerank_score: float | None = None

    @property
    def citation_id(self) -> str:
        return self.external_id or self.chunk_id

    @classmethod
    def from_any(cls, obj: Any) -> RetrievedChunk:
        if isinstance(obj, RetrievedChunk):
            return obj
        return cls(
            chunk_id=str(_get(obj, "chunk_id", "id", default="")),
            document_id=str(_get(obj, "document_id", default="") or ""),
            content=str(_get(obj, "content", "text", default="") or ""),
            external_id=_get(obj, "external_id"),
            metadata=dict(_get(obj, "metadata", default={}) or {}),
            rerank_score=_get(obj, "rerank_score"),
        )


class DraftCandidate(BaseModel):
    """The LLM-generated draft under outbound inspection (mirrors core ``GeneratedDraft``)."""

    draft_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    organization_id: str = ""
    action: str = "reply"  # reply | forward | escalate
    subject: str | None = None
    body: str
    citations: list[str] = Field(default_factory=list, description="Citation ids the model claimed")
    recipients: list[str] = Field(default_factory=list)
    model_name: str | None = None
    category: str | None = None

    @classmethod
    def from_any(cls, obj: Any) -> DraftCandidate:
        if isinstance(obj, DraftCandidate):
            return obj
        raw_citations = _get(obj, "citations", "knowledge_chunks", default=[]) or []
        citations = [
            c if isinstance(c, str) else str(_get(c, "chunk_id", "id", "citation_id", default=c))
            for c in raw_citations
        ]
        return cls(
            draft_id=str(_get(obj, "draft_id", "id", default="") or ""),
            message_id=str(_get(obj, "message_id", default="") or ""),
            thread_id=str(_get(obj, "thread_id", default="") or ""),
            organization_id=str(_get(obj, "organization_id", default="") or ""),
            action=str(_get(obj, "action", default="reply") or "reply"),
            subject=_get(obj, "subject"),
            body=str(_get(obj, "body", "draft", "text", default="") or ""),
            citations=citations,
            recipients=[_email_str(r).lower() for r in (_get(obj, "recipients", default=[]) or [])],
            model_name=_get(obj, "model_name", "model"),
            category=_get(obj, "category"),
        )

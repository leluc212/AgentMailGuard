"""Pure domain entities and value objects.

Requirements:
- R1.4: Provider-neutral domain objects (NormalizedMessage, ThreadRef, DraftRef).
- R5.2: Domain entity modeling.
- R14.8: Fixed ContextPackage assembly order.
- R18.4, R18.5: ProcessingEvent telemetry.
- GEMINI.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True)
class EmailAddress:
    """Email address with optional display name."""

    email: str
    name: str | None = None

    def __str__(self) -> str:
        if self.name:
            return f"{self.name} <{self.email}>"
        return self.email


@dataclass(frozen=True)
class AttachmentRef:
    """Metadata reference to an email attachment stored in object storage."""

    filename: str
    mime_type: str
    size_bytes: int
    object_key: str
    checksum: str | None = None


@dataclass
class NormalizedMessage:
    """Provider-neutral normalized email message entity (R1.4, R4.8, R5.2)."""

    message_id: UUID | str
    thread_id: UUID | str
    mailbox_id: UUID | str
    organization_id: UUID | str
    provider: str
    provider_message_id: str
    sender: EmailAddress
    received_at: datetime
    rfc822_message_id: str | None = None
    in_reply_to: str | None = None
    references_ids: list[str] = field(default_factory=list)
    recipients: list[EmailAddress] = field(default_factory=list)
    cc: list[EmailAddress] = field(default_factory=list)
    subject: str = ""
    subject_normalized: str = ""
    body_text: str = ""
    body_text_clean: str = ""
    snippet: str = ""
    raw_object_key: str | None = None
    html_object_key: str | None = None
    direction: str = "inbound"  # inbound | outbound
    attachments: list[AttachmentRef] = field(default_factory=list)
    normalization_failed: bool = False


@dataclass
class Classification:
    """Result emitted by the cascading triage engine (R6, R7)."""

    category: str
    intent: str | None = None
    priority: str = "normal"  # urgent | high | normal | low
    reply_required: bool = True
    workflow_hint: str = "ai"  # ai | template | none
    retrieval_required: bool = True
    confidence: float = 1.0
    decided_by: str = "default"  # rule | ml | llm | default
    latency_ms: int = 0
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    """A retrieved knowledge chunk candidate during hybrid RAG search (R10, R11)."""

    chunk_id: str
    document_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    external_id: str | None = None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    lexical_score: float | None = None
    vector_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None


@dataclass
class ContextPackage:
    """Assembled generation context enforcing fixed prompt order (R14.8)."""

    agent_instructions: str
    category_instructions: str
    current_message: NormalizedMessage
    thread_summary: str | None = None
    recent_messages: list[NormalizedMessage] = field(default_factory=list)
    retrieved_chunks: list[Candidate] = field(default_factory=list)
    business_data: dict[str, Any] = field(default_factory=dict)

    def get_ordered_sections(self) -> list[tuple[str, str]]:
        """Return assembled prompt sections in strictly fixed assembly order (R14.8).

        Fixed assembly order (do not reorder):
        1. Agent instructions (static prefix for prompt caching)
        2. Category instructions (from agent profile)
        3. Thread summary (if exists)
        4. Recent thread messages (last N)
        5. Current email (clean body)
        6. Retrieved knowledge (top chunks with citation identifiers)
        7. Business data (labelled distinctly from knowledge)
        """
        sections: list[tuple[str, str]] = [
            ("agent_instructions", self.agent_instructions),
            ("category_instructions", self.category_instructions),
        ]

        if self.thread_summary:
            sections.append(("thread_summary", self.thread_summary))

        if self.recent_messages:
            recent_texts: list[str] = []
            for msg in self.recent_messages:
                sender_str = str(msg.sender)
                body = msg.body_text_clean or msg.body_text
                recent_texts.append(f"From {sender_str} at {msg.received_at}:\n{body}")
            sections.append(("recent_thread_messages", "\n---\n".join(recent_texts)))

        curr_body = self.current_message.body_text_clean or self.current_message.body_text
        sections.append(("current_email", curr_body))

        if self.retrieved_chunks:
            chunk_texts: list[str] = []
            for chunk in self.retrieved_chunks:
                cid = chunk.external_id or chunk.chunk_id
                chunk_texts.append(f"[CITATION: {cid}]\n{chunk.content}")
            sections.append(("retrieved_knowledge", "\n\n".join(chunk_texts)))

        if self.business_data:
            biz_lines = [f"{k}: {v}" for k, v in sorted(self.business_data.items())]
            sections.append(("business_data", "[BUSINESS DATA]\n" + "\n".join(biz_lines)))

        return sections


@dataclass
class Job:
    """Asynchronous email processing job entity (R18.1, R19.4)."""

    id: UUID | str = field(default_factory=uuid4)
    organization_id: UUID | str = ""
    message_id: UUID | str | None = None
    thread_id: UUID | str | None = None
    job_type: str = "generate_reply"
    state: str = "RECEIVED"
    attempt: int = 0
    max_attempts: int = 5
    idempotency_key: str = ""
    result_ref: dict[str, Any] | None = None
    queue_name: str | None = None
    priority: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    next_retry_at: datetime | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ProcessingEvent:
    """Telemetry event recording state machine transitions and audit trails (R18.4)."""

    organization_id: UUID | str
    state_to: str
    id: int | None = None
    job_id: UUID | str | None = None
    message_id: UUID | str | None = None
    event_type: str = "state_transition"
    state_from: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Mailbox:
    """Mailbox account entity representing a configured mail account (R1.1, R2.1, R5.2)."""

    id: UUID | str
    organization_id: UUID | str
    provider: str
    address: str
    display_name: str | None = None
    status: str = "active"  # active | paused | needs_reauth
    credentials_ref: str | None = None


@dataclass
class Checkpoint:
    """Mailbox synchronization checkpoint state (R2.1, R2.8)."""

    mailbox_id: UUID | str
    history_id: str | None = None
    delta_link: str | None = None
    sync_state: str = "idle"  # idle | syncing | full_resync | error
    last_sync_at: datetime | None = None
    last_full_sync_at: datetime | None = None
    pending_followup: bool = False


@dataclass
class Subscription:
    """Webhook/push notification subscription with an email provider (R1.1, R2.2)."""

    mailbox_id: UUID | str
    subscription_id: str
    expires_at: datetime
    provider: str
    resource: str | None = None
    client_state: str | None = None


@dataclass
class RawMessage:
    """Provider-neutral raw message payload container prior to MIME parsing (R1.4, R4.10)."""

    provider_message_id: str
    raw_payload: bytes | str
    provider_thread_id: str | None = None
    internal_date: datetime | None = None
    history_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawThread:
    """Collection of raw messages belonging to a provider thread (R1.1, R1.4)."""

    provider_thread_id: str
    messages: list[RawMessage] = field(default_factory=list)


@dataclass
class SyncResult:
    """Result of an incremental or full mailbox synchronization (R1.1, R2.1)."""

    messages: list[RawMessage]
    new_checkpoint: Checkpoint
    requires_full_resync: bool = False
    has_more: bool = False


@dataclass
class OutboundReply:
    """Outbound reply draft or message submission data (R1.1, R1.4, R17.1)."""

    thread_id: UUID | str
    mailbox_id: UUID | str
    organization_id: UUID | str
    to: list[EmailAddress]
    body_text: str
    subject: str = ""
    cc: list[EmailAddress] = field(default_factory=list)
    body_html: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    draft_id: str | None = None


@dataclass(frozen=True)
class DraftRef:
    """Reference to an email draft created in the provider mailbox (R1.4, R17.1)."""

    provider_draft_id: str
    provider_message_id: str | None = None
    provider_thread_id: str | None = None


@dataclass(frozen=True)
class SentRef:
    """Reference to a message dispatched through the provider mailbox (R1.1, R1.4, R17.4)."""

    provider_message_id: str
    provider_thread_id: str | None = None
    sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ThreadRef:
    """Reference to an email thread in the provider or domain (R1.4)."""

    provider_thread_id: str
    message_count: int = 0

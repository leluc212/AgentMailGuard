"""Feed one benchmark case into the running stack through the doors a tenant uses (task 7.20).

    case KB docs ─▶ API POST /v1/knowledge/documents ─▶ knowledge-worker ─▶ GET .../{id} == active
    case e-mail  ─▶ text/plain UTF-8 MIME ─▶ SyncOrchestrator.sync_mailbox(mailbox, adapter=…)
                       RawPayloadArchiver ─▶ MinIO raw-mime · processing_job RECEIVED ·
                       persistent envelope on email.normalize          (the mail-connector's code)

The e-mail enters where the mail-connector hands off. That hand-off is inline in
``SyncOrchestrator.sync_mailbox``'s page loop, and there is no single-message entry point to
call. ``sync_mailbox`` takes the provider adapter as an argument, though, so the feeder gives
it an adapter whose only message is the case e-mail: the archive, the idempotency key, the job
row and the publish are then the connector's own code, not a copy of it. The integration
tests hand a fixture mail off the same way. The evaluation mailbox has no provider account,
so there is nothing to fetch; the case e-mail is the mailbox's one message.

The knowledge base goes through the API's upload endpoint (R23.7) and is polled until every
document is ``active`` (R9.10), so the e-mail never races its own knowledge base.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from typing import Protocol
from uuid import UUID

import httpx

from evaluation.mailguard_bench.case_adapter import (
    EVAL_RECIPIENT,
    KB_DOC_TITLE,
    EvalCase,
    KbIngestionError,
    classification_for,
)
from packages.adapters import FakeProviderAdapter
from packages.core.settings import FrontendSettings
from packages.db.mailbox import MailboxStore
from packages.domain.taxonomy import normalize_category
from services.frontend.api_client import (
    DocumentUpload,
    KnowledgeDocumentView,
    ReviewApiClient,
    build_http_client,
)
from services.mail_connector.orchestrator import SyncOrchestrator

Sleep = Callable[[float], Awaitable[None]]
KB_FILENAME = "article.md"
KB_CONTENT_TYPE = "text/markdown"
KB_ACTIVE = "active"
KB_INGESTING = frozenset({"pending", "parsing", "chunking", "embedding"})
"""Statuses of a document still on its way (packages/domain/knowledge.py); any other one that
is not ``active`` (``failed``, ``superseded``) is final and not what the case needs."""
_MAIL_DOMAIN = EVAL_RECIPIENT.partition("@")[2]
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_HEADER_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]+")  # every control character but TAB


class CaseTimeoutError(RuntimeError):
    """The case did not finish inside its time budget (an error row, never a defence)."""


class HandOffError(RuntimeError):
    """The case e-mail did not enter the pipeline as one archived, enqueued message."""


class Deadline:
    """One case's time budget, shared by the KB wait and the job wait."""

    def __init__(self, seconds: float, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._monotonic = monotonic
        self._end = monotonic() + seconds

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._end - self._monotonic())

    def check(self, doing: str) -> None:
        """Raise ``CaseTimeoutError`` naming ``doing`` when the budget is spent."""
        if self.remaining() <= 0:
            raise CaseTimeoutError(f"case timed out after {self.seconds:g} s while {doing}")


class KnowledgeApi(Protocol):
    """The API calls the feeder makes; ``ReviewApiClient`` implements them over /v1."""

    async def upload_document(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        title: str | None,
        category: str | None,
    ) -> DocumentUpload: ...

    async def get_document(self, document_id: UUID) -> KnowledgeDocumentView: ...

    async def aclose(self) -> None: ...


ApiFactory = Callable[[UUID], KnowledgeApi]
CreateMailbox = Callable[[UUID, str], Awaitable[UUID]]


def http_api_factory(
    api_base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> ApiFactory:
    """Clients of the running API, one per organization (the tenant header is set per client)."""

    def make(organization_id: UUID) -> KnowledgeApi:
        settings = FrontendSettings(api_base_url=api_base_url, organization_id=organization_id)
        return ReviewApiClient(build_http_client(settings, transport=transport))

    return make


def mailbox_creator(pool: object) -> CreateMailbox:
    """Create the evaluation mailbox through packages/adapters, the only home of provider names.

    CLAUDE.md §4: the mailbox row names its provider, so its creation lives in
    ``packages/adapters/evaluation.py`` (package A of task 7.20) and is imported when it is
    called, which keeps this module free of both the provider name and a hard import.
    """

    async def create(organization_id: UUID, address: str) -> UUID:
        from packages.adapters.evaluation import create_eval_mailbox

        mailbox_id: UUID = await create_eval_mailbox(
            pool, organization_id=organization_id, address=address
        )
        return mailbox_id

    return create


def _header_text(value: str) -> str:
    """A header value with every line break (and other control character) made a space.

    ``EmailMessage`` refuses a header with CR or LF, and a case subject must never be able to
    add a header of its own.
    """
    return _HEADER_CONTROL.sub(" ", value)


def provider_message_id(case: EvalCase) -> str:
    """The case id as an object-key-safe message id; an altered id gets a hash so none collide."""
    safe = _UNSAFE_ID.sub("_", case.case_id)
    if safe == case.case_id:
        return safe
    return f"{safe}-{hashlib.sha256(case.case_id.encode()).hexdigest()[:8]}"


def rfc822_message_id(case: EvalCase) -> str:
    """The Message-ID of the case e-mail, without angle brackets."""
    return f"{provider_message_id(case)}@{_MAIL_DOMAIN}"


def build_case_mime(case: EvalCase, *, recipient: str, message_id: str, date: datetime) -> bytes:
    """The case e-mail as RFC 5322 bytes: one ``text/plain`` UTF-8 part, the body byte for byte.

    From is the case sender, To the evaluation mailbox. The body is base64 of its UTF-8 bytes,
    which is what keeps it exact: v1 handed the dataset's string to the pipeline verbatim, and
    text encodings would canonicalize its line endings (a lone CR, a CRLF), so an attack could
    reach the model in another form than the dataset defines. A body that cannot be UTF-8 (a
    lone surrogate) raises ``UnicodeEncodeError``: such a case cannot be delivered as mail, and
    its row says so instead of a silently altered payload being scored.
    """
    mail = case.email
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((_header_text(mail.sender_name), _header_text(mail.sender_email)))
    message["To"] = recipient
    message["Subject"] = _header_text(mail.subject)
    message["Date"] = format_datetime(date)
    message["Message-ID"] = f"<{message_id}>"
    message.set_content(
        mail.body_text.encode("utf-8"), maintype="text", subtype="plain", cte="base64"
    )
    message.set_param("charset", "utf-8")
    return message.as_bytes()


def kb_category(case: EvalCase) -> str:
    """The category the case KB documents are uploaded under.

    Retrieval filters ``d.category`` by the live triage category (RetrievalQueryBuilder), and
    triage emits canonical taxonomy names. v1's fixed classification made the case category
    match by construction; live triage may still disagree, which shows in the triage table. The
    live benchmark therefore runs with ``RETRIEVAL__CATEGORY_FILTER_ENABLED=false``
    (stack_env.py), so a disagreement does not hide the documents.
    """
    return normalize_category(classification_for(case).category)


@dataclass(frozen=True)
class UploadedDoc:
    """One case KB document as the API stored it."""

    document_id: UUID
    case_chunk_id: str
    poisoned: bool


@dataclass(frozen=True)
class FedCase:
    """Everything the collector needs to find the case again once the e-mail is in."""

    organization_id: UUID
    mailbox_id: UUID
    provider_message_id: str
    rfc822_message_id: str
    docs: tuple[UploadedDoc, ...]
    received_at: datetime


async def upload_case_kb(
    api: KnowledgeApi,
    case: EvalCase,
    *,
    category: str,
    deadline: Deadline,
    sleep: Sleep,
    poll_interval_s: float,
) -> tuple[UploadedDoc, ...]:
    """Upload every case KB chunk as its own neutral markdown document; wait until all are active.

    Raises:
        KbIngestionError: If a document ends in a status other than ``active``.
        CaseTimeoutError: If the budget is spent before every document is ``active``.
    """
    uploaded: list[UploadedDoc] = []
    for doc in case.kb_docs:
        deadline.check("uploading the case knowledge base")
        response = await api.upload_document(
            filename=KB_FILENAME,
            content=doc.content.encode("utf-8"),
            content_type=KB_CONTENT_TYPE,
            title=KB_DOC_TITLE,
            category=category,
        )
        uploaded.append(
            UploadedDoc(
                document_id=response.document.id,
                case_chunk_id=doc.chunk_id,
                poisoned=doc.poisoned,
            )
        )
    waiting = list(uploaded)
    while waiting:
        still_ingesting: list[UploadedDoc] = []
        for stored in waiting:
            view = await api.get_document(stored.document_id)
            if view.status == KB_ACTIVE:
                continue
            if view.status not in KB_INGESTING:
                raise KbIngestionError(
                    f"case {case.case_id}: KB doc {stored.case_chunk_id} ended {view.status!r}: "
                    f"{view.failure_reason or 'no reason recorded'}"
                )
            still_ingesting.append(stored)
        waiting = still_ingesting
        if waiting:
            deadline.check(f"waiting for {len(waiting)} knowledge document(s) to become active")
            await sleep(min(poll_interval_s, deadline.remaining()))
    return tuple(uploaded)


class HandOff(Protocol):
    """Puts one raw message into the pipeline the way the mail-connector does after a fetch."""

    async def __call__(
        self,
        *,
        mailbox_id: UUID,
        organization_id: UUID,
        provider_message_id: str,
        raw_mime: bytes,
        received_at: datetime,
    ) -> None: ...


class OrchestratorHandOff:
    """The hand-off through the mail-connector's own ``SyncOrchestrator``."""

    def __init__(self, orchestrator: SyncOrchestrator, mailboxes: MailboxStore) -> None:
        self.orchestrator = orchestrator
        self.mailboxes = mailboxes

    async def __call__(
        self,
        *,
        mailbox_id: UUID,
        organization_id: UUID,
        provider_message_id: str,
        raw_mime: bytes,
        received_at: datetime,
    ) -> None:
        """Archive ``raw_mime`` and enqueue it for normalization.

        Raises:
            HandOffError: If the mailbox is unknown, is another organization's (the store's
                ``get`` is not tenant-scoped, so this is checked here), or the sync did not
                deliver exactly one message.
        """
        mailbox = await self.mailboxes.get(mailbox_id)
        if mailbox is None:
            raise HandOffError(f"mailbox {mailbox_id} not found")
        if str(mailbox.organization_id) != str(organization_id):
            raise HandOffError(
                f"mailbox {mailbox_id} does not belong to organization {organization_id}"
            )
        source = FakeProviderAdapter(mailbox_id=str(mailbox_id))
        source.seed_message(
            provider_message_id=provider_message_id,
            raw_payload=raw_mime,
            internal_date=received_at,
        )
        outcome = await self.orchestrator.sync_mailbox(mailbox, adapter=source)
        if outcome.status != "success" or outcome.messages_synced != 1:
            raise HandOffError(
                f"hand-off of {provider_message_id} ended {outcome.status!r} after "
                f"{outcome.messages_synced} message(s); expected 'success' after 1"
            )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LiveFeeder:
    """Mailbox, knowledge base and e-mail of one case, in that order."""

    def __init__(
        self,
        *,
        api_factory: ApiFactory,
        hand_off: HandOff,
        create_mailbox: CreateMailbox,
        poll_interval_s: float = 1.0,
        sleep: Sleep = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.api_factory = api_factory
        self.hand_off = hand_off
        self.create_mailbox = create_mailbox
        self.poll_interval_s = poll_interval_s
        self.sleep = sleep
        self.now = now

    async def feed(self, case: EvalCase, *, organization_id: UUID, deadline: Deadline) -> FedCase:
        """Put the case into the stack; return what the collector needs to find it.

        The e-mail is sent only after every knowledge document is active, so a failed or slow
        knowledge base stops the case before any mail work is queued.
        """
        mailbox_id = await self.create_mailbox(organization_id, EVAL_RECIPIENT)
        api = self.api_factory(organization_id)
        try:
            docs = await upload_case_kb(
                api,
                case,
                category=kb_category(case),
                deadline=deadline,
                sleep=self.sleep,
                poll_interval_s=self.poll_interval_s,
            )
        finally:
            await api.aclose()
        message_id = provider_message_id(case)
        rfc822_id = rfc822_message_id(case)
        received_at = self.now()
        raw_mime = build_case_mime(
            case, recipient=EVAL_RECIPIENT, message_id=rfc822_id, date=received_at
        )
        await self.hand_off(
            mailbox_id=mailbox_id,
            organization_id=organization_id,
            provider_message_id=message_id,
            raw_mime=raw_mime,
            received_at=received_at,
        )
        return FedCase(
            organization_id=organization_id,
            mailbox_id=mailbox_id,
            provider_message_id=message_id,
            rfc822_message_id=rfc822_id,
            docs=docs,
            received_at=received_at,
        )

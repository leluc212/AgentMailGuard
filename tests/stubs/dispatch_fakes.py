"""Dispatch test doubles (tasks 6.5–6.7): a call-counting fake provider with Gmail send
semantics and crash points, and an in-memory world for DispatchService tests.

``RecordingFake`` subclasses the contract-tested ``FakeProviderAdapter`` and keeps its own
record of sends, so these tests depend only on the fake's ``create_draft``:
- ``send_draft`` removes the draft (Gmail deletes a sent draft), so ``get_draft_status``
  then reports MISSING and ``find_sent_message`` finds the sent copy by our Message-ID;
- ``fail_send`` = "before" | "after" raises one Transient before or after the provider
  accepted the send (the ambiguous failure of design §5.8 step 4);
- ``crash_at`` in ``CRASH_POINTS`` parks the calling worker forever at that point, which is
  how the forced-redelivery tests kill a worker mid-step;
- ``pause_at`` in ``CRASH_POINTS`` holds the first caller there until ``resume`` is set, so a
  test can run a second delivery of the same job while the first is mid-dispatch;
- ``find_draft`` finds an unsent draft by our Message-ID (the orphan-draft lookup of 6.5).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.adapters.exceptions import NotFound, Transient
from packages.adapters.fake import FakeProviderAdapter
from packages.db.dispatch import InMemoryDispatchStore
from packages.dispatch.service import DispatchService
from packages.domain import DispatchMode, ProviderDraftStatus
from packages.domain.entities import (
    DraftRef,
    EmailAddress,
    EmailThread,
    GeneratedDraft,
    Job,
    Mailbox,
    NormalizedMessage,
    OutboundReply,
    SentRef,
)
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry

CRASH_POINTS = (
    "before_create_draft",
    "after_create_draft",  # the provider holds the draft; its id is not recorded yet
    "before_send_draft",
    "after_send_draft",
)


class RecordingFake(FakeProviderAdapter):
    """FakeProviderAdapter with call counts, Gmail send semantics and crash points."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter[str] = Counter()
        self.replies: dict[str, OutboundReply] = {}
        self.sent: dict[str, SentRef] = {}
        self.find_queries: list[str] = []
        self.fail_send: str | None = None
        self.status_override: ProviderDraftStatus | None = None
        self.hide_sent = False
        self.refs: dict[str, DraftRef] = {}
        self.crash_at: str | None = None
        self.crash_reached = asyncio.Event()
        self._hang = asyncio.Event()
        self.pause_at: str | None = None
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()

    async def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None  # only the first worker to get here is killed
            self.crash_reached.set()
            await self._hang.wait()  # never set: the worker is gone
        if self.pause_at == point:
            self.pause_at = None  # only the first caller is held
            self.paused.set()
            await self.resume.wait()

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        await self._maybe_crash("before_create_draft")
        ref = await super().create_draft(mailbox, reply)
        self.calls["create_draft"] += 1
        self.replies[ref.provider_draft_id] = reply
        self.refs[ref.provider_draft_id] = ref
        await self._maybe_crash("after_create_draft")
        return ref

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        await self._maybe_crash("before_send_draft")
        self._maybe_raise_fault("send_draft", mailbox)
        if self.fail_send == "before":
            self.fail_send = None
            raise Transient(
                "connection reset before the send", provider="fake", mailbox_id=str(mailbox.id)
            )
        if provider_draft_id not in self.replies or provider_draft_id in self.sent:
            raise NotFound(
                f"Draft '{provider_draft_id}' not found",
                provider="fake",
                mailbox_id=str(mailbox.id),
            )
        self.calls["send_draft"] += 1
        ref = SentRef(
            provider_message_id=f"sent-rec-{self.calls['send_draft']}",
            sent_at=datetime.now(UTC),
        )
        self.sent[provider_draft_id] = ref
        if self.fail_send == "after":
            self.fail_send = None
            raise Transient(
                "timeout after the provider accepted the send",
                provider="fake",
                mailbox_id=str(mailbox.id),
            )
        await self._maybe_crash("after_send_draft")
        return ref

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        self._maybe_raise_fault("get_draft_status", mailbox)
        self.calls["get_draft_status"] += 1
        if self.status_override is not None:
            return self.status_override
        if provider_draft_id in self.sent or provider_draft_id not in self.replies:
            return ProviderDraftStatus.MISSING
        return ProviderDraftStatus.DRAFT

    async def find_sent_message(
        self, mailbox: Mailbox, provider_thread_id: str, provider_message_id: str
    ) -> SentRef | None:
        self._maybe_raise_fault("find_sent_message", mailbox)
        self.calls["find_sent_message"] += 1
        self.find_queries.append(provider_message_id)
        if self.hide_sent:
            return None
        for draft_id, ref in self.sent.items():
            if self.replies[draft_id].message_id == provider_message_id:
                return ref
        return None

    async def find_draft(
        self, mailbox: Mailbox, provider_thread_id: str, message_id: str
    ) -> DraftRef | None:
        self._maybe_raise_fault("find_draft", mailbox)
        self.calls["find_draft"] += 1
        for draft_id, reply in self.replies.items():
            if draft_id in self.sent or str(reply.thread_id) != provider_thread_id:
                continue
            if reply.message_id == message_id:
                return self.refs[draft_id]
        return None

    def delete_draft(self, provider_draft_id: str) -> None:
        """A person deletes the draft in the mailbox before it is sent."""
        super().delete_draft(provider_draft_id)
        self.replies.pop(provider_draft_id, None)


def registry_with(
    mode: DispatchMode, *, auto_send: bool = False, category: str = "billing"
) -> TaxonomyRegistry:
    """A taxonomy whose ``category`` uses ``mode`` (every other category keeps its default)."""
    registry = TaxonomyRegistry()
    definition = registry.get(category)
    assert definition is not None
    registry.register_category(
        replace(definition, dispatch_mode=mode, auto_send_eligible=auto_send)
    )
    return registry


@dataclass
class DispatchWorld:
    """One approved billing draft ready for dispatch, on in-memory stores."""

    store: InMemoryDispatchStore
    fake: RecordingFake
    service: DispatchService
    org_id: UUID
    job_id: UUID
    draft_id: UUID
    mailbox: Mailbox
    thread: EmailThread
    original: NormalizedMessage
    registry: TaxonomyRegistry


async def build_dispatch_world(
    *,
    mode: DispatchMode = DispatchMode.CREATE_DRAFT,
    draft_status: str = "approved",
    job_state: JobState = JobState.DRAFTED,
    provider_thread_id: str | None = "th-001",
    auto_send: bool = False,
    recheck_delay_s: float = 0.0,
    provider_draft_id: str | None = None,
) -> DispatchWorld:
    """Seed mailbox, thread, inbound email (category billing), job and draft."""
    org_id, mailbox_id, thread_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    mailbox = Mailbox(
        id=mailbox_id,
        organization_id=org_id,
        provider="fake",
        address="support@acme.example",
        display_name="Acme Support",
    )
    thread = EmailThread(
        id=thread_id,
        organization_id=org_id,
        mailbox_id=mailbox_id,
        subject_normalized="where is order 82915",
        provider_thread_id=provider_thread_id,
        participants=["alice@customer.example", "support@acme.example"],
    )
    original = NormalizedMessage(
        message_id=uuid4(),
        thread_id=thread_id,
        mailbox_id=mailbox_id,
        organization_id=org_id,
        provider="fake",
        provider_message_id="prov-msg-001",
        sender=EmailAddress("alice@customer.example", "Alice"),
        received_at=datetime(2026, 9, 28, 8, 0, tzinfo=UTC),
        rfc822_message_id="orig-1@customer.example",
        references_ids=["root-0@customer.example"],
        recipients=[EmailAddress("support@acme.example")],
        subject="Where is order 82915?",
        body_text="What is the status of order 82915?",
    )
    store = InMemoryDispatchStore()
    store.add_mailbox(mailbox)
    store.add_thread(thread)
    store.add_message(original)
    store.set_category(original.message_id, "billing")
    await store.jobs.create_job(
        Job(
            id=job_id,
            organization_id=org_id,
            message_id=original.message_id,
            thread_id=thread_id,
            state=job_state.value,
            idempotency_key=f"gen-{uuid4()}",
        )
    )
    draft = GeneratedDraft(
        organization_id=org_id,
        message_id=original.message_id,
        thread_id=thread_id,
        job_id=job_id,
        subject="Re: Where is order 82915?",
        body="Order ORD-82915 was dispatched on 24 September.",
        status=draft_status,
        provider_draft_id=provider_draft_id,
        provider_draft_message_id=f"{provider_draft_id}-msg" if provider_draft_id else None,
    )
    store.add_draft(draft)
    fake = RecordingFake()
    registry = registry_with(mode, auto_send=auto_send)
    service = DispatchService(
        store=store,
        adapter_for=lambda _mailbox: fake,
        registry=registry,
        confirm_recheck_delay_s=recheck_delay_s,
    )
    return DispatchWorld(
        store=store,
        fake=fake,
        service=service,
        org_id=org_id,
        job_id=job_id,
        draft_id=draft.id,
        mailbox=mailbox,
        thread=thread,
        original=original,
        registry=registry,
    )

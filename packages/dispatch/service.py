"""Exactly-once dispatch of an approved draft in five steps (design.md §5.8, ADR-0009).

Requirements:
- R17.1 / R17.6 / R16.8: the category's ``dispatch_mode`` (default create_draft) decides
  whether the provider draft is sent; nothing is dispatched without an approval unless the
  category is ``auto_send_eligible``.
- R17.2: the reply comes from ``build_outbound_reply`` with the provider thread id; a null
  provider thread id fails permanently.
- R17.3 / R19.2 / R19.3: the claim stores the dispatch key; the provider draft is the durable
  handle that lets a redelivery confirm instead of sending twice.
- R17.4 / R17.7: finish persists the provider ref and, in send_reply mode, the outbound
  ``email_message``, in one transaction with DISPATCHED -> COMPLETED.
- R17.5: adapter RateLimited / Transient propagate (the job stays DISPATCHED and the broker
  retry ladder redelivers); Permanent / NotFound / AuthExpired, MissingProviderThreadError
  and DispatchPermanentError are dead-lettered by the dispatch-worker.
- R19.3 / tasks.md 6.5: the five steps run under the store's per-job lock, so two deliveries
  of one job never overlap (DispatchJobBusyError defers the second); a redelivery with no
  recorded handle adopts the provider draft an earlier delivery created (find_draft) instead
  of creating a second one; and a send on a handle this delivery did not create is always
  preceded by step 4.

The provider send never runs inside a database transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter_for_mailbox
from packages.core.idempotency import derive_idempotency_key
from packages.db.dispatch import ClaimStatus, DispatchContext, DispatchStore
from packages.dispatch.reply import MissingProviderThreadError, build_outbound_reply
from packages.domain import DispatchMode, ProviderDraftStatus
from packages.domain.entities import (
    EmailAddress,
    GeneratedDraft,
    Mailbox,
    NormalizedMessage,
    OutboundReply,
    SentRef,
)
from packages.domain.review import DraftStatus
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry, get_default_registry

logger = logging.getLogger(__name__)

AdapterResolver = Callable[[Mailbox], MailProviderAdapter]
DEFAULT_DISPATCH_QUEUE = "email.dispatch"
_APPROVED = frozenset({DraftStatus.APPROVED.value, DraftStatus.DISPATCHED.value})
_OUTBOUND_NAMESPACE = uuid5(NAMESPACE_URL, "rag-email/outbound-message")


class DispatchOutcome(StrEnum):
    """How one dispatch delivery ended."""

    COMPLETED_DRAFT = "completed_draft"
    COMPLETED_SENT = "completed_sent"
    ALREADY_DONE = "already_done"
    NOT_DISPATCHABLE = "not_dispatchable"


class DispatchPermanentError(Exception):
    """Retrying cannot help; the dispatch-worker dead-letters the job with this reason."""


class DispatchJobBusyError(Exception):
    """Another delivery of the same job holds its dispatch lock; this one is deferred.

    Not a failure: the dispatch-worker re-queues the delivery without using a retry attempt
    and without touching the job.
    """


def bare_message_id(value: str | None) -> str | None:
    """A Message-ID as stored in email_message: no angle brackets (parser convention)."""
    if value is None:
        return None
    cleaned = value.strip().strip("<>").strip()
    return cleaned or None


def message_id_domain(address: str) -> str:
    """Right-hand side of our Message-ID: the mailbox's own domain."""
    return address.rsplit("@", 1)[-1].strip().lower() or "localhost"


def outbound_message(
    ctx: DispatchContext, draft: GeneratedDraft, reply: OutboundReply, sent: SentRef
) -> NormalizedMessage:
    """The sent reply as an ``email_message`` row, direction='outbound' (R17.7).

    ``provider_message_id`` is the provider's id of the SENT copy, the id mailbox sync later
    sees, so the (org, mailbox, provider_message_id) dedup recognises our own reply.
    """
    references = [ref for ref in (bare_message_id(r) for r in reply.references) if ref]
    return NormalizedMessage(
        message_id=uuid5(_OUTBOUND_NAMESPACE, str(draft.id)),
        thread_id=ctx.thread.id,
        mailbox_id=ctx.mailbox.id,
        organization_id=ctx.job.organization_id,
        provider=ctx.mailbox.provider,
        provider_message_id=sent.provider_message_id,
        sender=EmailAddress(email=ctx.mailbox.address, name=ctx.mailbox.display_name),
        received_at=sent.sent_at,
        rfc822_message_id=bare_message_id(reply.message_id),
        in_reply_to=bare_message_id(reply.in_reply_to),
        references_ids=references,
        recipients=list(reply.to),
        cc=list(reply.cc),
        subject=reply.subject,
        subject_normalized=ctx.thread.subject_normalized,
        body_text=reply.body_text,
        body_text_clean=draft.body,
        snippet=draft.body[:200],
        direction="outbound",
    )


class DispatchService:
    """Runs design §5.8's five steps for one job; safe to call again for the same job."""

    def __init__(
        self,
        *,
        store: DispatchStore,
        adapter_for: AdapterResolver = get_adapter_for_mailbox,
        registry: TaxonomyRegistry | None = None,
        dispatch_queue: str = DEFAULT_DISPATCH_QUEUE,
        confirm_recheck_delay_s: float = 2.0,
    ) -> None:
        self._store = store
        self._adapter_for = adapter_for
        self._registry = registry
        self._dispatch_queue = dispatch_queue
        self._recheck_delay_s = confirm_recheck_delay_s

    async def dispatch(self, *, organization_id: UUID, job_id: UUID) -> DispatchOutcome:
        """Dispatch the job's approved draft exactly once (design.md §5.8).

        Serialized per job: a repeated approve re-publishes while the job is DRAFTED,
        DISPATCHED or RETRY_PENDING, and the consumer runs several deliveries at once, so
        two deliveries of one job can arrive together. The loser raises
        DispatchJobBusyError instead of running any step.
        """
        async with self._store.job_lock(organization_id, job_id) as held:
            if not held:
                raise DispatchJobBusyError(
                    f"Job {job_id} is being dispatched by another delivery; deferring this one"
                )
            return await self._dispatch_locked(organization_id, job_id)

    async def mark_dispatch_route(self, *, organization_id: UUID, job_id: UUID) -> None:
        """Route an operator replay of this job to the dispatch-worker (R18.7).

        The claim writes queue_name too, but a failure before or during the claim (load
        finds nothing, a key conflict rolls the claim back) would leave the generation
        queue there, and the replay would regenerate the draft.
        """
        await self._store.set_dispatch_queue(
            organization_id=organization_id, job_id=job_id, queue_name=self._dispatch_queue
        )

    async def _dispatch_locked(self, organization_id: UUID, job_id: UUID) -> DispatchOutcome:
        ctx = await self._store.load(organization_id, job_id)
        if ctx is None:
            raise DispatchPermanentError(
                f"Job {job_id} has no dispatchable draft in organization {organization_id}"
            )
        if ctx.job.state == JobState.COMPLETED.value:
            return DispatchOutcome.ALREADY_DONE
        mode, auto_send = self._policy(ctx.category)
        if ctx.draft.status not in _APPROVED and not auto_send:
            logger.warning(
                "Dispatch of job %s skipped: draft %s is '%s' and not approved",
                job_id,
                ctx.draft.id,
                ctx.draft.status,
            )
            return DispatchOutcome.NOT_DISPATCHABLE

        # 1 claim
        key = derive_idempotency_key(
            organization_id=organization_id,
            mailbox_id=ctx.mailbox.id,
            provider_message_id=ctx.original.provider_message_id,
            operation_type="dispatch",
        )
        claim = await self._store.claim(
            organization_id=organization_id,
            job_id=job_id,
            draft_id=ctx.draft.id,
            idempotency_key=key,
            queue_name=self._dispatch_queue,
        )
        if claim.status is ClaimStatus.ALREADY_COMPLETED:
            return DispatchOutcome.ALREADY_DONE
        if claim.status is ClaimStatus.NOT_DISPATCHABLE:
            logger.warning("Dispatch of job %s skipped: job is %s", job_id, claim.job.state)
            return DispatchOutcome.NOT_DISPATCHABLE
        draft = claim.draft

        provider_thread_id = ctx.thread.provider_thread_id
        if provider_thread_id is None or not provider_thread_id.strip():
            raise MissingProviderThreadError(draft_id=str(draft.id), thread_id=str(ctx.thread.id))
        reply = build_outbound_reply(
            draft=draft,
            original=ctx.original,
            provider_thread_id=provider_thread_id,
            message_id_domain=message_id_domain(ctx.mailbox.address),
        )
        adapter = self._adapter_for(ctx.mailbox)

        # 2 draft: reuse the stored handle, adopt an unrecorded one, or create it once
        created_here = False
        if not draft.provider_draft_id and ctx.job.state != JobState.DRAFTED.value:
            # A redelivery or an operator replay: an earlier delivery may have died after the
            # provider accepted create_draft and before the id was recorded (tasks.md 6.5).
            orphan = (
                await adapter.find_draft(ctx.mailbox, provider_thread_id, reply.message_id)
                if reply.message_id
                else None
            )
            if orphan is not None and orphan.provider_draft_id:
                logger.info(
                    "Job %s: adopting unrecorded provider draft %s",
                    job_id,
                    orphan.provider_draft_id,
                )
                draft = await self._store.record_provider_draft(
                    organization_id=organization_id,
                    draft_id=draft.id,
                    provider_draft_id=orphan.provider_draft_id,
                    provider_draft_message_id=orphan.provider_message_id,
                )
        if not draft.provider_draft_id:
            ref = await adapter.create_draft(ctx.mailbox, reply)
            if not ref.provider_draft_id:
                raise DispatchPermanentError("The provider returned an empty draft id")
            stored = await self._store.record_provider_draft(
                organization_id=organization_id,
                draft_id=draft.id,
                provider_draft_id=ref.provider_draft_id,
                provider_draft_message_id=ref.provider_message_id,
            )
            created_here = stored.provider_draft_id == ref.provider_draft_id
            if not created_here:
                # Cannot happen under the job lock; if it does, the stored handle may already
                # be sent, so step 4 runs before any send.
                logger.warning(
                    "Provider draft %s left unused: draft %s already holds %s",
                    ref.provider_draft_id,
                    draft.id,
                    stored.provider_draft_id,
                )
            draft = stored
        provider_draft_id = draft.provider_draft_id
        if not provider_draft_id:
            raise DispatchPermanentError(f"Draft {draft.id} has no provider draft handle")

        if mode is DispatchMode.CREATE_DRAFT:
            # The customer has received nothing: no outbound email_message (R17.7, 6.7).
            await self._store.finish(
                organization_id=organization_id,
                job_id=job_id,
                draft_id=draft.id,
                mode=mode,
                provider_ref=provider_draft_id,
                outbound=None,
            )
            logger.info("Job %s: provider draft %s created", job_id, provider_draft_id)
            return DispatchOutcome.COMPLETED_DRAFT

        # 3 send / 4 confirm (every handle this delivery did not create is confirmed first)
        sent = await self._send_exactly_once(
            adapter, ctx, draft, provider_draft_id, reply, confirm_first=not created_here
        )
        # 5 finish
        await self._store.finish(
            organization_id=organization_id,
            job_id=job_id,
            draft_id=draft.id,
            mode=mode,
            provider_ref=sent.provider_message_id,
            outbound=outbound_message(ctx, draft, reply, sent),
        )
        logger.info("Job %s: reply sent as %s", job_id, sent.provider_message_id)
        return DispatchOutcome.COMPLETED_SENT

    def _policy(self, category: str | None) -> tuple[DispatchMode, bool]:
        registry = self._registry or get_default_registry()
        definition = registry.get(category) if category else None
        if definition is None:
            return DispatchMode.CREATE_DRAFT, False
        return DispatchMode(str(definition.dispatch_mode)), bool(definition.auto_send_eligible)

    async def _send_exactly_once(
        self,
        adapter: MailProviderAdapter,
        ctx: DispatchContext,
        draft: GeneratedDraft,
        provider_draft_id: str,
        reply: OutboundReply,
        *,
        confirm_first: bool,
    ) -> SentRef:
        """Step 3, preceded by step 4 whenever an earlier delivery may have sent already."""
        if confirm_first:
            status = await adapter.get_draft_status(ctx.mailbox, provider_draft_id)
            if status == ProviderDraftStatus.DRAFT and self._recheck_delay_s > 0:
                # A just-accepted send can take a moment to show (Graph 202, design §5.8).
                await asyncio.sleep(self._recheck_delay_s)
                status = await adapter.get_draft_status(ctx.mailbox, provider_draft_id)
            if status == ProviderDraftStatus.SENT:
                return SentRef(
                    provider_message_id=draft.provider_draft_message_id or provider_draft_id
                )
            if status == ProviderDraftStatus.MISSING:
                return await self._find_sent(adapter, ctx, draft, reply, provider_draft_id)
        return await adapter.send_draft(ctx.mailbox, provider_draft_id)

    async def _find_sent(
        self,
        adapter: MailProviderAdapter,
        ctx: DispatchContext,
        draft: GeneratedDraft,
        reply: OutboundReply,
        provider_draft_id: str,
    ) -> SentRef:
        provider_thread_id = ctx.thread.provider_thread_id or ""
        # Graph keeps the draft's immutable id on the sent copy; Gmail's drafts.send gives the
        # sent copy a new id, so only our Message-ID finds it there (Task 5 contract rule).
        lookups = [
            key for key in dict.fromkeys((draft.provider_draft_message_id, reply.message_id)) if key
        ]
        if not lookups:
            raise DispatchPermanentError(
                f"Provider draft {provider_draft_id} is gone and there is no message id to find"
            )
        for lookup in lookups:
            found = await adapter.find_sent_message(ctx.mailbox, provider_thread_id, lookup)
            if found is not None:
                return found
        raise DispatchPermanentError(
            f"Provider draft {provider_draft_id} is gone and thread {provider_thread_id} "
            f"holds no sent message {' or '.join(lookups)}; a person may have deleted the "
            "draft. Check the mailbox, then replay or close the job."
        )

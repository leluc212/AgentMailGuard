"""Email subject normalization and thread association service.

Requirements:
- R4.5: Normalize subjects by stripping reply/forward prefixes for thread matching.
- R4.6: Associate each message with a thread using provider thread id when available,
        else In-Reply-To/References, else normalized subject + participant set.
- Maintain email_thread counters, timestamps, and participant sets.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from enum import StrEnum
from uuid import UUID, uuid4

from packages.db.thread import ThreadStore
from packages.domain.entities import EmailAddress, EmailThread, NormalizedMessage

logger = logging.getLogger(__name__)


class ThreadAssociationReason(StrEnum):
    """Reason code identifying which tier of the association hierarchy matched (R4.6)."""

    PROVIDER_THREAD_ID = "provider_thread_id"
    IN_REPLY_TO = "in_reply_to"
    REFERENCES = "references"
    SUBJECT_PARTICIPANTS = "subject_participants"
    NEW_THREAD = "new_thread"


@dataclass(frozen=True)
class ThreadAssociationResult:
    """Outcome of thread association containing thread ID, metadata, and matched tier."""

    thread_id: UUID
    is_new: bool
    reason: ThreadAssociationReason
    thread: EmailThread


def _extract_single_email(addr: EmailAddress | str | None) -> str | None:
    if addr is None:
        return None
    if isinstance(addr, EmailAddress):
        clean = addr.email.strip().lower()
        return clean if clean else None
    # Parse string like "Name <email@example.com>" or "email@example.com"
    _, parsed_email = parseaddr(str(addr))
    clean = parsed_email.strip().lower() if parsed_email else str(addr).strip().lower()
    return clean if clean and "@" in clean else None


def extract_participant_emails(
    sender: EmailAddress | str | None,
    recipients: Sequence[EmailAddress | str] | None,
    cc: Sequence[EmailAddress | str] | None = None,
) -> list[str]:
    """Extract clean, lowercase, deduplicated, sorted participant email addresses."""
    participants: set[str] = set()

    sender_email = _extract_single_email(sender)
    if sender_email:
        participants.add(sender_email)

    if recipients:
        for r in recipients:
            em = _extract_single_email(r)
            if em:
                participants.add(em)

    if cc:
        for c in cc:
            em = _extract_single_email(c)
            if em:
                participants.add(em)

    return sorted(participants)


class ThreadAssociator:
    """Orchestrates message-to-thread association following design.md §5.2 priority rules."""

    def __init__(self, store: ThreadStore, default_window_days: int = 14) -> None:
        self.store = store
        self.default_window_days = default_window_days

    async def associate_message(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        subject_normalized: str,
        sender: EmailAddress | str | None,
        recipients: Sequence[EmailAddress | str] | None,
        cc: Sequence[EmailAddress | str] | None = None,
        provider_thread_id: str | None = None,
        in_reply_to: str | None = None,
        references_ids: Sequence[str] | None = None,
        received_at: datetime | None = None,
        window_days: int | None = None,
    ) -> ThreadAssociationResult:
        """Associate an incoming email with an existing thread or create a new thread (R4.6).

        Hierarchy (first match wins):
        1. Provider thread_id
        2. In-Reply-To / References -> existing message
        3. Normalized subject + overlapping participant set within time window
        4. Otherwise: create new thread
        """
        org_u = organization_id if isinstance(organization_id, UUID) else UUID(str(organization_id))
        mbx_u = mailbox_id if isinstance(mailbox_id, UUID) else UUID(str(mailbox_id))
        msg_time = received_at or datetime.now(UTC)
        participants = extract_participant_emails(sender, recipients, cc)
        clean_subj = subject_normalized.strip()
        days = window_days if window_days is not None else self.default_window_days
        window = timedelta(days=days)

        # Tier 1: Provider thread_id
        if provider_thread_id and provider_thread_id.strip():
            clean_prov_id = provider_thread_id.strip()
            existing_thread = await self.store.find_by_provider_thread_id(
                org_u, mbx_u, clean_prov_id
            )
            if existing_thread:
                updated = await self.store.update_thread_on_message(
                    thread_id=existing_thread.id,
                    organization_id=org_u,
                    message_time=msg_time,
                    participants=participants,
                    provider_thread_id=clean_prov_id,
                )
                logger.debug(
                    "Associated message with thread %s via provider_thread_id (%s)",
                    updated.id,
                    clean_prov_id,
                )
                return ThreadAssociationResult(
                    thread_id=updated.id,
                    is_new=False,
                    reason=ThreadAssociationReason.PROVIDER_THREAD_ID,
                    thread=updated,
                )

        # Tier 2a: In-Reply-To Message-ID
        if in_reply_to and in_reply_to.strip():
            clean_in_reply_to = in_reply_to.strip().strip("<>")
            matched_thread_id = await self.store.find_by_rfc822_message_id(
                org_u, mbx_u, clean_in_reply_to
            )
            if matched_thread_id:
                updated = await self.store.update_thread_on_message(
                    thread_id=matched_thread_id,
                    organization_id=org_u,
                    message_time=msg_time,
                    participants=participants,
                    provider_thread_id=provider_thread_id,
                )
                logger.debug(
                    "Associated message with thread %s via in_reply_to (%s)",
                    updated.id,
                    clean_in_reply_to,
                )
                return ThreadAssociationResult(
                    thread_id=updated.id,
                    is_new=False,
                    reason=ThreadAssociationReason.IN_REPLY_TO,
                    thread=updated,
                )

        # Tier 2b: References Message-IDs (evaluated latest reference backwards)
        if references_ids:
            clean_refs = [r.strip().strip("<>") for r in references_ids if r.strip()]
            matched_thread_id = await self.store.find_by_references(org_u, mbx_u, clean_refs)
            if matched_thread_id:
                updated = await self.store.update_thread_on_message(
                    thread_id=matched_thread_id,
                    organization_id=org_u,
                    message_time=msg_time,
                    participants=participants,
                    provider_thread_id=provider_thread_id,
                )
                logger.debug(
                    "Associated message with thread %s via references (%s)",
                    updated.id,
                    clean_refs,
                )
                return ThreadAssociationResult(
                    thread_id=updated.id,
                    is_new=False,
                    reason=ThreadAssociationReason.REFERENCES,
                    thread=updated,
                )

        # Tier 3: Normalized subject + overlapping participants within time window
        if clean_subj:
            existing_thread = await self.store.find_by_subject_and_participants(
                organization_id=org_u,
                mailbox_id=mbx_u,
                subject_normalized=clean_subj,
                participants=participants,
                window=window,
                reference_time=msg_time,
            )
            if existing_thread:
                updated = await self.store.update_thread_on_message(
                    thread_id=existing_thread.id,
                    organization_id=org_u,
                    message_time=msg_time,
                    participants=participants,
                    provider_thread_id=provider_thread_id,
                )
                logger.debug(
                    "Associated message with thread %s via subject + participants (%s)",
                    updated.id,
                    clean_subj,
                )
                return ThreadAssociationResult(
                    thread_id=updated.id,
                    is_new=False,
                    reason=ThreadAssociationReason.SUBJECT_PARTICIPANTS,
                    thread=updated,
                )

        # Tier 4: New thread
        new_thread_id = uuid4()
        new_thread = EmailThread(
            id=new_thread_id,
            organization_id=org_u,
            mailbox_id=mbx_u,
            subject_normalized=clean_subj,
            provider_thread_id=provider_thread_id,
            participants=participants,
            first_message_at=msg_time,
            last_message_at=msg_time,
            message_count=1,
            status="open",
        )
        created = await self.store.create_thread(new_thread)
        logger.debug("Created new thread %s for subject %s", created.id, clean_subj)

        return ThreadAssociationResult(
            thread_id=created.id,
            is_new=True,
            reason=ThreadAssociationReason.NEW_THREAD,
            thread=created,
        )

    async def associate_normalized_message(
        self,
        message: NormalizedMessage,
        provider_thread_id: str | None = None,
        window_days: int | None = None,
    ) -> ThreadAssociationResult:
        """Associate a NormalizedMessage and mutate its thread_id to the resolved thread."""
        result = await self.associate_message(
            organization_id=message.organization_id,
            mailbox_id=message.mailbox_id,
            subject_normalized=message.subject_normalized,
            sender=message.sender,
            recipients=message.recipients,
            cc=message.cc,
            provider_thread_id=provider_thread_id,
            in_reply_to=message.in_reply_to,
            references_ids=message.references_ids,
            received_at=message.received_at,
            window_days=window_days,
        )
        message.thread_id = result.thread_id
        return result

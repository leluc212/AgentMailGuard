"""Email message persistence and deduplication pipeline (R4.8, R5.4, R5.6, R4.7, R5.8).

Orchestrates thread association, message insertion with ON CONFLICT DO NOTHING,
attachment metadata persistence, and downstream job dispatch suppression for duplicates.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from packages.db.message import MessageStore
from packages.db.thread import ThreadStore
from packages.domain.entities import AttachmentRef, EmailThread, NormalizedMessage
from services.email_worker.threading import ThreadAssociator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailPersistenceResult:
    """Outcome of email persistence and deduplication."""

    success: bool
    is_duplicate: bool
    message: NormalizedMessage
    thread: EmailThread | None
    should_dispatch: bool


class EmailPersister:
    """Coordinates thread association and atomic message/attachment persistence."""

    def __init__(
        self,
        message_store: MessageStore,
        thread_store: ThreadStore,
        thread_associator: ThreadAssociator | None = None,
    ) -> None:
        self.message_store = message_store
        self.thread_store = thread_store
        self.thread_associator = thread_associator or ThreadAssociator(thread_store)

    async def persist(
        self,
        message: NormalizedMessage,
        attachments: Sequence[AttachmentRef] | None = None,
        provider_thread_id: str | None = None,
        window_days: int | None = None,
    ) -> EmailPersistenceResult:
        """Persist normalized email with thread association and duplicate suppression (R4.8).

        1. Fast-check if message already exists by provider_message_id. If duplicate,
           exit early with should_dispatch=False.
        2. Associate message with thread (updating counters or creating thread).
        3. Insert message and attachments into database with ON CONFLICT DO NOTHING.
        4. If DB indicates duplicate (racing worker), return should_dispatch=False.
        5. Otherwise return should_dispatch=True to trigger downstream triage job.
        """
        # 1. Fast duplicate check
        existing = await self.message_store.get_message_by_provider_id(
            organization_id=message.organization_id,
            mailbox_id=message.mailbox_id,
            provider_message_id=message.provider_message_id,
        )
        if existing is not None:
            logger.info(
                "Duplicate email detected via fast-check: org=%s, mailbox=%s, provider_msg_id=%s",
                message.organization_id,
                message.mailbox_id,
                message.provider_message_id,
            )
            thread = await self.thread_store.get_thread(
                organization_id=message.organization_id,
                thread_id=existing.thread_id,
            )
            return EmailPersistenceResult(
                success=True,
                is_duplicate=True,
                message=existing,
                thread=thread,
                should_dispatch=False,
            )

        # 2. Thread association
        assoc_result = await self.thread_associator.associate_normalized_message(
            message=message,
            provider_thread_id=provider_thread_id,
            window_days=window_days,
        )
        if not message.subject_normalized and assoc_result.thread.subject_normalized:
            message.subject_normalized = assoc_result.thread.subject_normalized

        # 3. Atomic message & attachment insertion with ON CONFLICT DO NOTHING
        insert_result = await self.message_store.insert_message(
            message=message,
            attachments=attachments,
        )

        # 4. Check if racing insertion marked it duplicate
        if insert_result.is_duplicate:
            logger.info(
                "Duplicate email detected via DB constraint: org=%s, mbx=%s, msg=%s",
                message.organization_id,
                message.mailbox_id,
                message.provider_message_id,
            )
            return EmailPersistenceResult(
                success=True,
                is_duplicate=True,
                message=message,
                thread=assoc_result.thread,
                should_dispatch=False,
            )

        # 5. Check if normalization failed (R4.9: never discard, persist with normalization_failed)
        if message.normalization_failed:
            logger.warning(
                "Persisted email with normalization_failed=True: org=%s, msg=%s, raw_key=%s",
                message.organization_id,
                message.message_id,
                message.raw_object_key,
            )
            return EmailPersistenceResult(
                success=True,
                is_duplicate=False,
                message=message,
                thread=assoc_result.thread,
                should_dispatch=False,
            )

        # 6. Newly persisted message: ready for triage job dispatch
        return EmailPersistenceResult(
            success=True,
            is_duplicate=False,
            message=message,
            thread=assoc_result.thread,
            should_dispatch=True,
        )

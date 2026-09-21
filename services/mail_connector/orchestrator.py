"""Mailbox synchronization orchestrator (R2.4, R2.7, R2.8, R2.9, R1.5).

Coordinates MailProviderAdapter, CheckpointStore, ObjectStorage, and Broker,
enforcing strict checkpoint advancement ordering (R2.8), bounded full re-sync (R2.7),
and in-flight notification coalescing (R2.9).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from packages.adapters.exceptions import AuthExpired, RateLimited
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter_for_mailbox
from packages.broker.envelope import JobEnvelope
from packages.core.archive import RawPayloadArchiver
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import AppSettings
from packages.core.storage import StorageProtocol
from packages.db.checkpoint import CheckpointStore
from packages.db.job import JobStore
from packages.db.mailbox import MailboxStore
from packages.domain.entities import Checkpoint, Job, Mailbox
from packages.domain.state_machine import JobState
from packages.observability.metrics import PipelineMetrics
from packages.observability.tracing import trace_span

logger = logging.getLogger(__name__)


@runtime_checkable
class PublisherProtocol(Protocol):
    """Protocol for AMQP message publication."""

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class SyncOutcome:
    """Result of a sync orchestration pass."""

    coalesced: bool
    messages_synced: int
    status: str = "success"  # success | coalesced | needs_reauth | rate_limited | error
    retry_after_s: int | None = None
    new_checkpoint: Checkpoint | None = None


class SyncOrchestrator:
    """Provider-neutral synchronization orchestrator implementing design.md §5.1."""

    def __init__(
        self,
        checkpoint_store: CheckpointStore,
        storage_client: StorageProtocol,
        publisher: PublisherProtocol,
        adapter_resolver: Callable[[Mailbox], MailProviderAdapter] | None = None,
        mailbox_store: MailboxStore | None = None,
        job_store: JobStore | None = None,
        archiver: RawPayloadArchiver | None = None,
        metrics: PipelineMetrics | None = None,
        settings: AppSettings | None = None,
        max_pages: int = 50,
    ) -> None:
        self.checkpoint_store = checkpoint_store
        self.storage_client = storage_client
        self.publisher = publisher
        self.adapter_resolver = adapter_resolver or get_adapter_for_mailbox
        self.mailbox_store = mailbox_store
        self.job_store = job_store
        self.settings = settings or AppSettings()
        self.archiver = archiver or RawPayloadArchiver(
            storage_client=self.storage_client,
            settings=self.settings,
        )
        self.metrics = metrics
        self.max_pages = max_pages

    async def sync_mailbox(
        self,
        mailbox: Mailbox,
        adapter: MailProviderAdapter | None = None,
    ) -> SyncOutcome:
        """Execute synchronization for a mailbox.

        Implements the canonical sync algorithm from specs/design.md §5.1:
        1. In-flight check: if already syncing, mark pending follow-up and return (R2.9).
        2. Resolve provider adapter via registry or explicit override.
        3. Load current checkpoint.
        4. Loop over pages:
           a. adapter.synchronize(mailbox, cp)
           b. If requires_full_resync: set sync_state='full_resync', reset cursor, loop (R2.7).
           c. For each raw message:
               - Store raw MIME blob in object storage (R4.10, R5.8).
               - Publish persistent JobEnvelope to email.normalize (R3.1).
            d. Save checkpoint ONLY AFTER all fetched messages from window
               are durably stored & published (R2.8).
        5. Check and clear pending_followup; loop again if set (R2.9).
        6. Release in-flight lock on completion.
        7. On AuthExpired: mark mailbox needs_reauth, set error state, and halt (R1.5, R2.10).
        """
        # 1. In-flight check & coalescing (R2.9)
        acquired = await self.checkpoint_store.try_acquire_lock(mailbox.id, mailbox.organization_id)
        if not acquired:
            logger.info("Mailbox %s sync already in-flight; marking pending follow-up", mailbox.id)
            await self.checkpoint_store.mark_pending_followup(mailbox.id)
            return SyncOutcome(
                coalesced=True,
                messages_synced=0,
                status="coalesced",
            )

        if adapter is None:
            adapter = self.adapter_resolver(mailbox)

        total_synced = 0
        final_checkpoint: Checkpoint | None = None

        try:
            while True:
                cp = await self.checkpoint_store.get(mailbox.id)
                if cp is None:
                    cp = Checkpoint(
                        mailbox_id=mailbox.id,
                        organization_id=mailbox.organization_id,
                        sync_state="syncing",
                    )

                page_count = 0
                while True:
                    page_count += 1
                    if page_count > self.max_pages:
                        logger.warning(
                            "Mailbox %s exceeded safety page limit (%d)",
                            mailbox.id,
                            self.max_pages,
                        )
                        break

                    try:
                        result = await adapter.synchronize(mailbox, cp)
                    except AuthExpired:
                        logger.error(
                            "AuthExpired for mailbox %s; marking needs_reauth and stopping (R1.5)",
                            mailbox.id,
                        )
                        if self.mailbox_store:
                            await self.mailbox_store.update_status(mailbox.id, "needs_reauth")
                        await self.checkpoint_store.release_lock(mailbox.id, next_state="error")
                        return SyncOutcome(
                            coalesced=False,
                            messages_synced=total_synced,
                            status="needs_reauth",
                        )
                    except RateLimited as exc:
                        retry_sec = int(exc.retry_after) if exc.retry_after is not None else None
                        logger.warning(
                            "RateLimited for mailbox %s; retry-after %ss",
                            mailbox.id,
                            retry_sec,
                        )
                        await self.checkpoint_store.release_lock(mailbox.id, next_state="error")
                        return SyncOutcome(
                            coalesced=False,
                            messages_synced=total_synced,
                            status="rate_limited",
                            retry_after_s=retry_sec,
                        )

                    # Bounded full re-sync if provider reported expired/invalid checkpoint (R2.7)
                    if result.requires_full_resync:
                        logger.warning(
                            "Invalid/expired checkpoint for mailbox %s. Full resync (R2.7)",
                            mailbox.id,
                        )
                        cp = Checkpoint(
                            mailbox_id=mailbox.id,
                            organization_id=mailbox.organization_id,
                            history_id=None,
                            delta_link=None,
                            sync_state="full_resync",
                            last_sync_at=datetime.now(UTC),
                            last_full_sync_at=datetime.now(UTC),
                        )
                        await self.checkpoint_store.save(cp)
                        continue

                    # Process messages: store raw blob & publish normalization job
                    for raw in result.messages:
                        # 1. Store raw MIME payload in object storage (R4.10, R5.8)
                        with trace_span(
                            "mail.archive_raw",
                            attributes={
                                "mailbox_id": str(mailbox.id),
                                "organization_id": str(mailbox.organization_id),
                                "provider_message_id": raw.provider_message_id,
                            },
                        ):
                            archived_ref = await self.archiver.archive(
                                organization_id=mailbox.organization_id,
                                mailbox_id=mailbox.id,
                                provider_message_id=raw.provider_message_id,
                                raw_payload=raw.raw_payload,
                                provider=mailbox.provider,
                                provider_thread_id=raw.provider_thread_id,
                                received_at=raw.internal_date,
                            )
                            if self.metrics:
                                self.metrics.raw_payloads_archived_total.labels(
                                    provider=mailbox.provider, status="success"
                                ).inc()
                                self.metrics.raw_payload_size_bytes.labels(
                                    provider=mailbox.provider
                                ).observe(float(archived_ref.size_bytes))

                        # 2. Derive deterministic idempotency key (R19.2)
                        idem_key = derive_idempotency_key(
                            organization_id=mailbox.organization_id,
                            mailbox_id=mailbox.id,
                            provider_message_id=raw.provider_message_id,
                            operation_type="normalize",
                        )

                        # 3. Create processing_job in state RECEIVED (R18.1, Task 2.1)
                        job_id_val = str(uuid4())
                        if self.job_store is not None:
                            ingest_job = Job(
                                id=UUID(job_id_val),
                                organization_id=mailbox.organization_id,
                                message_id=None,
                                thread_id=None,
                                job_type="email_pipeline",
                                state=JobState.RECEIVED.value,
                                idempotency_key=idem_key,
                            )
                            persisted_job, _ = await self.job_store.create_job(
                                job=ingest_job,
                                initial_event_payload={
                                    "provider": mailbox.provider,
                                    "provider_message_id": raw.provider_message_id,
                                    "mailbox_id": str(mailbox.id),
                                    "raw_object_key": archived_ref.object_key,
                                },
                            )
                            job_id_val = str(persisted_job.id)

                        # 4. Publish persistent JobEnvelope to email.normalize (R3.1, R7.3, §7.3)
                        envelope = JobEnvelope(
                            job_id=job_id_val,
                            idempotency_key=idem_key,
                            organization_id=str(mailbox.organization_id),
                            mailbox_id=str(mailbox.id),
                            message_id=raw.provider_message_id,
                            thread_id=raw.provider_thread_id or "",
                            job_type="normalize_email",
                            attempt=0,
                            classification={},
                            payload={
                                "job_id": job_id_val,
                                "raw_object_key": archived_ref.object_key,
                                "raw_bucket": archived_ref.bucket,
                                "sha256": archived_ref.sha256,
                                "size_bytes": archived_ref.size_bytes,
                                "provider": mailbox.provider,
                                "provider_message_id": raw.provider_message_id,
                                "provider_thread_id": raw.provider_thread_id,
                                "internal_date": (
                                    raw.internal_date.isoformat() if raw.internal_date else None
                                ),
                                "history_id": raw.history_id,
                            },
                        )
                        await self.publisher.publish(
                            exchange_name=self.settings.broker.exchange_email_process,
                            routing_key=self.settings.broker.queue_normalize,
                            envelope=envelope,
                        )
                        total_synced += 1

                    # Checkpoint write ordering (R2.8): ONLY AFTER all fetched messages
                    # from that window are durably stored in object storage and published.
                    new_cp = result.new_checkpoint
                    new_cp.organization_id = mailbox.organization_id
                    new_cp.sync_state = "syncing"
                    new_cp.last_sync_at = datetime.now(UTC)
                    if cp.last_full_sync_at and not new_cp.last_full_sync_at:
                        new_cp.last_full_sync_at = cp.last_full_sync_at

                    await self.checkpoint_store.save(new_cp)
                    cp = new_cp
                    final_checkpoint = new_cp

                    if not result.has_more:
                        break

                # Coalescing: check if further notifications arrived during sync (R2.9)
                if await self.checkpoint_store.has_pending_followup(mailbox.id):
                    logger.info(
                        "Mailbox %s has pending follow-up; executing another sync pass (R2.9)",
                        mailbox.id,
                    )
                    await self.checkpoint_store.clear_pending_followup(mailbox.id)
                    continue

                break

            # Completed sync successfully; mark idle
            await self.checkpoint_store.release_lock(mailbox.id, next_state="idle")
            if final_checkpoint:
                final_checkpoint.sync_state = "idle"

            return SyncOutcome(
                coalesced=False,
                messages_synced=total_synced,
                status="success",
                new_checkpoint=final_checkpoint,
            )

        except Exception:
            logger.exception("Uncaught error during synchronization for mailbox %s", mailbox.id)
            await self.checkpoint_store.release_lock(mailbox.id, next_state="error")
            raise

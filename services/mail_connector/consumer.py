"""Mail sync request consumer (RA.10; design.md §5.1, §7.1; R2.1, R2.11, R3.3, R3.5, R23.6).

Consumes `sync_mailbox` envelopes from `mail.sync.requested` (published by the webhook
receivers and POST /v1/mailboxes/{id}/resync) and runs SyncOrchestrator.sync_mailbox.
Unresolvable or cross-tenant envelopes raise FatalError (dead-letter, never retried).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.adapters.exceptions import NotFound, Permanent, Transient
from packages.adapters.protocol import MailProviderAdapter
from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.mailbox import MailboxStore
from packages.domain.entities import Mailbox
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.mail_connector.orchestrator import SyncOrchestrator, SyncOutcome

logger = logging.getLogger(__name__)

SYNC_JOB_TYPE = "sync_mailbox"
NON_SYNCING_STATUSES = frozenset({"needs_reauth", "paused"})


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


class MailSyncConsumer(BaseConsumer):
    """Consumer mapping `sync_mailbox` envelopes onto SyncOrchestrator.sync_mailbox."""

    def __init__(
        self,
        orchestrator: SyncOrchestrator,
        mailbox_store: MailboxStore,
        adapter_resolver: Callable[[Mailbox], MailProviderAdapter] | None = None,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        settings = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=settings.queue_mail_sync,
            broker_settings=settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=None,  # sync envelopes have no processing_job row
            metrics=metrics,
        )
        self.orchestrator = orchestrator
        self.mailbox_store = mailbox_store
        self.adapter_resolver = adapter_resolver or orchestrator.adapter_resolver

    async def resolve_mailbox(self, envelope: JobEnvelope) -> Mailbox:
        """Resolve and tenant-check the target mailbox, or raise FatalError."""
        org_id = _parse_uuid(envelope.organization_id)
        if org_id is None:
            raise FatalError(
                f"Missing or invalid organization_id '{envelope.organization_id}' (R23.6)"
            )
        mailbox_id = _parse_uuid(envelope.mailbox_id)
        if mailbox_id is None:
            raise FatalError(f"Unresolvable mailbox_id '{envelope.mailbox_id}'")

        mailbox = await self.mailbox_store.get(mailbox_id)  # DB errors propagate -> retry
        if mailbox is None:
            raise FatalError(f"Mailbox {mailbox_id} not found")
        if _parse_uuid(str(mailbox.organization_id)) != org_id:
            raise FatalError(
                f"Mailbox {mailbox_id} does not belong to organization {org_id} (R23.6)"
            )
        return mailbox

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        if envelope.job_type != SYNC_JOB_TYPE:
            raise FatalError(
                f"Unsupported job_type '{envelope.job_type}' on queue '{self.queue_name}'"
            )

        mailbox = await self.resolve_mailbox(envelope)

        if mailbox.status in NON_SYNCING_STATUSES:
            logger.warning(
                "Skipping sync for mailbox %s in status '%s' (R1.5, R2.10)",
                mailbox.id,
                mailbox.status,
            )
            return

        try:
            adapter = self.adapter_resolver(mailbox)
        except (NotFound, Permanent) as err:
            raise FatalError(f"No usable provider adapter for mailbox {mailbox.id}: {err}") from err

        full_resync = bool(envelope.payload.get("full_resync", False))
        if envelope.payload.get("since") or envelope.payload.get("until"):
            logger.warning(
                "Mailbox %s resync time window (since=%s, until=%s) is not supported by the "
                "adapter protocol; running checkpoint-based sync",
                mailbox.id,
                envelope.payload.get("since"),
                envelope.payload.get("until"),
            )

        try:
            outcome = await self.orchestrator.sync_mailbox(
                mailbox, adapter=adapter, full_resync=full_resync
            )
        except Transient as err:
            raise TransientError(
                f"Transient provider failure for mailbox {mailbox.id}: {err}"
            ) from err
        except (NotFound, Permanent) as err:
            raise FatalError(f"Permanent provider failure for mailbox {mailbox.id}: {err}") from err

        self._handle_outcome(mailbox, outcome, full_resync=full_resync)

    def _handle_outcome(self, mailbox: Mailbox, outcome: SyncOutcome, *, full_resync: bool) -> None:
        if outcome.status == "rate_limited":
            raise TransientError(
                f"Provider rate limited mailbox {mailbox.id} (retry_after={outcome.retry_after_s}s)"
            )
        if outcome.coalesced and full_resync:
            # A pending follow-up does not carry the full_resync flag; retry the request later.
            raise TransientError(f"Mailbox {mailbox.id} busy; full re-sync deferred")
        if outcome.status == "needs_reauth":
            logger.error("Mailbox %s marked needs_reauth during sync; not retrying", mailbox.id)
            return
        logger.info(
            "Mailbox %s sync finished: status=%s messages=%d coalesced=%s",
            mailbox.id,
            outcome.status,
            outcome.messages_synced,
            outcome.coalesced,
        )

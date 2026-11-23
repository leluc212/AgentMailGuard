"""Demo ingest consumer (ADR-0018 decision 3, design.md §5.9, R25.1, R25.3).

Consumes ``demo_inject`` envelopes from ``mail.demo.inject`` (published by
``POST /v1/demo/messages``) and hands their emails to ``SyncOrchestrator.sync_mailbox`` with an
inbound adapter that holds only those emails. The archive, the idempotency key, the job row with
its run options and the publish to ``email.normalize`` are the orchestrator's own code, so a demo
email takes the same path as a fetched one.

Started only with ``DEMO__ENABLED=true``. Prefetch is 1: the orchestrator coalesces a second
sync of a mailbox that is already syncing into a follow-up of the first (R2.9), and that
follow-up would read the first request's adapter, so one request at a time keeps every demo
email; a coalesced request is retried rather than lost.
"""

from __future__ import annotations

import logging

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.adapters.demo import (
    DEMO_INJECT_JOB_TYPE,
    inbound_adapter,
    is_demo_mailbox,
    messages_from_payload,
)
from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.mailbox import MailboxStore
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.mail_connector.consumer import resolve_tenant_mailbox
from services.mail_connector.orchestrator import SyncOrchestrator

logger = logging.getLogger(__name__)


class DemoInjectConsumer(BaseConsumer):
    """Consumer mapping ``demo_inject`` envelopes onto ``SyncOrchestrator.sync_mailbox``."""

    def __init__(
        self,
        orchestrator: SyncOrchestrator,
        mailbox_store: MailboxStore,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        settings = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=settings.queue_demo_inject,
            broker_settings=settings,
            retry_settings=retry_settings,
            prefetch_count=1,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=None,  # the job row is created by the orchestrator, per message
            metrics=metrics,
        )
        self.orchestrator = orchestrator
        self.mailbox_store = mailbox_store

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        if envelope.job_type != DEMO_INJECT_JOB_TYPE:
            raise FatalError(
                f"Unsupported job_type '{envelope.job_type}' on queue '{self.queue_name}'"
            )
        mailbox = await resolve_tenant_mailbox(self.mailbox_store, envelope)
        if not is_demo_mailbox(mailbox):
            raise FatalError(f"Mailbox {mailbox.id} is not a demo mailbox; refusing to inject")
        try:
            messages = messages_from_payload(envelope.payload)
        except ValueError as err:
            raise FatalError(f"Demo inject {envelope.job_id}: {err}") from err

        outcome = await self.orchestrator.sync_mailbox(
            mailbox,
            adapter=inbound_adapter(mailbox.id, messages),
            run_options=envelope.run_options,
        )
        if outcome.coalesced:
            raise TransientError(f"Demo mailbox {mailbox.id} busy; demo inject deferred")
        if outcome.status != "success" or outcome.messages_synced != len(messages):
            raise TransientError(
                f"Demo inject {envelope.job_id} ended {outcome.status!r} after "
                f"{outcome.messages_synced} of {len(messages)} message(s)"
            )
        logger.info(
            "Demo inject %s delivered %d message(s) to mailbox %s (run_options=%s)",
            envelope.job_id,
            outcome.messages_synced,
            mailbox.id,
            envelope.run_options,
        )

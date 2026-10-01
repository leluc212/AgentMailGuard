"""Mail Connector Service entrypoint (RA.10; design.md §3.2, §5.1).

Hosts the MailSyncConsumer on 'mail.sync.requested', the subscription renewal loop (R2.10),
the single QueueMonitor for the stack (R7.5, R21.4), and the LeaseReaper (R19.8) only when
LEASE_REAPER__ENABLED=true. Keep this service at one replica: renewal selection has no
locking and queue-depth gauges would duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from packages.broker.lease_reaper import LeaseReaper
from packages.broker.queue_monitor import QueueMonitor
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import MailConnectorSettings
from packages.core.storage import get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.job import PostgresJobStore
from packages.db.mailbox import PostgresMailboxStore
from packages.db.subscription import PostgresSubscriptionStore
from services.mail_connector.background import BackgroundLoop
from services.mail_connector.consumer import MailSyncConsumer
from services.mail_connector.orchestrator import SyncOrchestrator
from services.mail_connector.renewal import SubscriptionRenewalJob

logger = logging.getLogger("mail_connector.service")

SERVICE_NAME = "mail_connector"
DEFAULT_PORT = 8001
QUEUE_MONITOR_INTERVAL_S = 15.0


async def build_components(res: WorkerResources) -> list[StartFn]:
    settings = res.settings
    mailbox_store = PostgresMailboxStore(res.db_pool)
    job_store = PostgresJobStore(res.db_pool)

    orchestrator = SyncOrchestrator(
        checkpoint_store=PostgresCheckpointStore(res.db_pool),
        storage_client=get_storage_client(settings.object_storage),
        publisher=res.publisher,
        mailbox_store=mailbox_store,
        job_store=job_store,
        metrics=res.metrics,
        settings=settings,
    )
    consumer = MailSyncConsumer(
        orchestrator=orchestrator,
        mailbox_store=mailbox_store,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.mail_connector_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )

    renewal_job = SubscriptionRenewalJob(
        subscription_store=PostgresSubscriptionStore(res.db_pool),
        mailbox_store=mailbox_store,
        metrics=res.metrics,
        settings=settings,
    )
    renewal_loop = BackgroundLoop(
        "subscription-renewal",
        lambda stop_event: renewal_job.run_loop(stop_event=stop_event),
        stop_timeout_s=settings.telemetry.drain_timeout_s,
    )
    res.shutdown.register_drain_callback(renewal_loop.stop)

    queue_monitor = QueueMonitor(  # registers its own stop() as a drain callback
        connection=res.connection,
        broker_settings=settings.broker,
        metrics=res.metrics,
        shutdown_coordinator=res.shutdown,
    )

    async def start_queue_monitor() -> None:
        await queue_monitor.start(interval_seconds=QUEUE_MONITOR_INTERVAL_S)

    start_fns: list[StartFn] = [consumer.start, renewal_loop.start, start_queue_monitor]

    if settings.lease_reaper.enabled:
        reaper = LeaseReaper(
            job_store=job_store,
            settings=settings.lease_reaper,
            publisher=res.publisher,
            metrics=res.metrics,
        )
        res.shutdown.register_drain_callback(reaper.stop)

        async def start_reaper() -> None:
            reaper.start()

        start_fns.append(start_reaper)
    else:
        logger.warning("Lease reaper disabled (LEASE_REAPER__ENABLED=false)")

    return start_fns


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=MailConnectorSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()

"""Email Normalization Worker Service entrypoint (Phase 1; R4, R3.2, R20.7, R20.8).

Consumes 'email.normalize', persists canonical messages, and dispatches triage jobs.
Process lifecycle (topology, health, graceful drain) comes from WorkerRuntime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import EmailWorkerSettings
from packages.core.storage import get_storage_client
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread import PostgresThreadStore
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister

SERVICE_NAME = "email_worker"
DEFAULT_PORT = 8002


def build_consumer(res: WorkerResources) -> EmailNormalizationConsumer:
    """Compose the production normalization consumer from shared resources."""
    settings = res.settings
    return EmailNormalizationConsumer(
        normalizer=EmailNormalizer(),
        persister=EmailPersister(
            message_store=PostgresMessageStore(res.db_pool),
            thread_store=PostgresThreadStore(res.db_pool),
        ),
        storage_client=get_storage_client(settings.object_storage),
        object_storage_settings=settings.object_storage,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.email_worker_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        job_store=PostgresJobStore(res.db_pool),
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    return [build_consumer(res).start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=EmailWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()

"""dispatch-worker entrypoint (tasks 6.5, 6.6; design.md §3.2, §5.8, ADR-0009).

One DispatchConsumer on ``email.dispatch`` runs DispatchService's five steps on the
Postgres dispatch store. The provider adapter is resolved per mailbox through the adapter
registry (R1.3), so this service names no provider. Process lifecycle (topology, /healthz,
/readyz, /metrics, graceful drain) comes from WorkerRuntime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.routing import load_categories_from_yaml
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import DispatchWorkerSettings
from packages.db.dispatch import PostgresDispatchStore
from packages.db.job import PostgresJobStore
from packages.dispatch.service import AdapterResolver, DispatchService
from services.dispatch_worker.consumer import DispatchConsumer

SERVICE_NAME = "dispatch_worker"
DEFAULT_PORT = 8006


def build_consumer(
    res: WorkerResources,
    *,
    service: DispatchService | None = None,
    adapter_resolver: AdapterResolver | None = None,
) -> DispatchConsumer:
    """Compose the dispatch consumer from shared resources.

    Tests inject ``service`` (unit) or ``adapter_resolver`` (the 6.9 end-to-end test, which
    hands every mailbox the fake adapter without patching the registry).
    """
    settings = res.settings
    queue = settings.broker.queue_dispatch
    dispatch = service
    if dispatch is None:
        store = PostgresDispatchStore(res.db_pool)
        dispatch = (
            DispatchService(store=store, adapter_for=adapter_resolver, dispatch_queue=queue)
            if adapter_resolver is not None
            else DispatchService(store=store, dispatch_queue=queue)
        )
    return DispatchConsumer(
        queue,
        service=dispatch,
        job_store=PostgresJobStore(res.db_pool),
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.dispatch_worker_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    """Load the category dispatch modes, then start the one dispatch consumer."""
    routing = res.settings.routing
    if routing.categories_config_path:
        load_categories_from_yaml(routing.categories_config_path)
    return [build_consumer(res).start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=DispatchWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()

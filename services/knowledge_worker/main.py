"""Knowledge Ingestion Worker Service entrypoint (Phase 3; R9.1, R5.10, R3.2).

Consumes 'knowledge.ingest' and runs parse -> chunk -> embed -> persist. Refuses to start
when the configured embedding dimension disagrees with the VECTOR(n) column (R5.10).
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AppSettings, KnowledgeWorkerSettings
from packages.core.storage import get_storage_client
from packages.db.job import PostgresJobStore
from packages.db.knowledge import PostgresKnowledgeStore
from packages.db.migrator import verify_database_vector_dimension
from packages.knowledge.embedder import get_embedder
from packages.knowledge.pipeline import KnowledgeIngestionPipeline
from services.knowledge_worker.consumer import KnowledgeIngestConsumer

SERVICE_NAME = "knowledge_worker"
DEFAULT_PORT = 8005


async def assert_vector_dimension(settings: AppSettings) -> None:
    """Fail fast when the embedding dimension disagrees with VECTOR(n) (R5.10).

    Raises ValueError (from assert_embedding_dimension) on mismatch.
    """
    await verify_database_vector_dimension(
        settings.database.asyncpg_dsn,
        configured_dimension=settings.embedding.dimension,
    )


def build_consumer(res: WorkerResources) -> KnowledgeIngestConsumer:
    """Compose the production ingestion consumer from shared resources."""
    settings = res.settings
    pipeline = KnowledgeIngestionPipeline(
        store=PostgresKnowledgeStore(res.db_pool),
        embedder=get_embedder(settings.embedding),
        storage=get_storage_client(settings.object_storage),
        bucket_name=settings.object_storage.bucket_knowledge,
    )
    return KnowledgeIngestConsumer(
        pipeline=pipeline,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.knowledge_worker_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        job_store=PostgresJobStore(res.db_pool),
        metrics=res.metrics,
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    await assert_vector_dimension(res.settings)
    return [build_consumer(res).start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=KnowledgeWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()

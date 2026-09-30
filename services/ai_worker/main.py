"""AI generation worker entrypoint (task 4.13b; design.md §3.2, §5.4, §5.7).

Hosts Context Builder + Hybrid RAG + Reply Agent in one process: one shared pipeline
(summarizer -> context builder -> complexity router -> drafting service) and one
AIWorkerConsumer per configured lane queue ``email.<category>.<priority>``. Process lifecycle
(topology, /healthz, /readyz, graceful drain) comes from WorkerRuntime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from typing import Any

from packages.broker.routing import (
    declared_category_queues,
    is_queue_consumed,
    load_categories_from_yaml,
    resolve_configured_consumers,
)
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.business.postgres import PostgresBusinessDataProvider
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.context.summarizer import ThreadSummarizer
from packages.core.settings import AIWorkerSettings, AppSettings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.migrator import verify_database_vector_dimension
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.taxonomy import get_default_registry
from packages.knowledge.embedder import Embedder, get_embedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm import AgentProfileRegistry, InstrumentedLLMProvider, SinglePassGenerator
from packages.llm.budget import CallKind
from packages.llm.factory import create_llm_provider
from packages.llm.inference_metrics import start_token_counter_warmup
from packages.llm.protocol import LLMProvider
from packages.llm.router import ComplexityRouter
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.query_builder import QueryBuilderConfig, RetrievalQueryBuilder
from packages.retrieval.rerank import RerankService, build_rerank_service
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.drafting import DraftingService

SERVICE_NAME = "ai_worker"
DEFAULT_PORT = 8004
PRIORITY_LANE_SUFFIX = ".priority"


def resolve_lane_queues(settings: AppSettings) -> list[str]:
    """Declared category lane queues that the configured consumers claim (R7.6, G.2).

    Globs such as ``email.*.priority`` are expanded; configured names that are not declared
    are ignored, because a passive declare of them would fail at start.
    """
    routing = settings.routing
    if routing.categories_config_path:
        load_categories_from_yaml(routing.categories_config_path)
    registry = get_default_registry()
    declared = declared_category_queues(routing, registry)
    consumers = resolve_configured_consumers(routing, registry)
    return [q for q in declared if is_queue_consumed(q, consumers)]


def lane_prefetch(settings: AppSettings, queue_name: str) -> int:
    """Bounded prefetch per lane (R3.4, R7.2)."""
    concurrency = settings.concurrency
    if queue_name.endswith(PRIORITY_LANE_SUFFIX):
        return concurrency.ai_worker_priority_prefetch
    return concurrency.ai_worker_normal_prefetch


def build_consumers(
    res: WorkerResources,
    *,
    llm_provider: LLMProvider | None = None,
    token_counter: TokenCounter | None = None,
    embedder: Embedder | None = None,
    rerank_service: RerankService | None = None,
    drafting_factory: Callable[..., Any] | None = None,
) -> list[AIWorkerConsumer]:
    """Compose one shared generation pipeline and one consumer per lane.

    ``llm_provider``, ``embedder`` and ``rerank_service`` replace what the settings would build,
    so a test that composes the real worker can keep every model call and download out of it.

    ``drafting_factory`` replaces ``DraftingService`` (the evaluation guard-worker's guarded
    drafting step). It is called with exactly the keyword arguments ``DraftingService(...)``
    receives and must return an object with the same public interface: ``draft`` and
    ``generator``. Default: the stock ``DraftingService``.
    """
    settings = res.settings
    # The corpus model embeds every query (R5.10); tokens are counted (R9.11).
    query_embedder = embedder or get_embedder(settings.embedding, metrics=res.metrics)
    # An injected service wins. Otherwise RETRIEVAL__RERANK_ENABLED=false builds none, and the
    # cross-encoder of the default one loads on the first rerank (R11.1, R11.5).
    reranking = rerank_service or build_rerank_service(settings.retrieval, metrics=res.metrics)
    counter = token_counter or TokenCounter()
    provider = llm_provider or create_llm_provider(settings.llm)
    jobs = PostgresJobStore(res.db_pool)
    messages = PostgresMessageStore(res.db_pool)
    states = PostgresThreadStateStore(res.db_pool)
    # One registry: the generator picks the template, the builder reads context_policy (§5.4).
    profile_registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
    business = settings.business_data

    summarizer = ThreadSummarizer(
        llm=InstrumentedLLMProvider(
            provider,
            kind=CallKind.SUMMARIZE,
            metrics=res.metrics,
            price_table=settings.llm.price_table,
        ),
        store=states,
        settings=settings.summarization,
        token_counter=counter,
    )
    context_builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(
            settings=settings.summarization,
            token_counter=counter,
            metrics=res.metrics,
            thread_state_store=states,
            message_store=messages,
        ),
        retriever=HybridRetriever(
            PostgresSearchBackend(pool=res.db_pool, metrics=res.metrics),
            timeout_seconds=settings.retrieval.retrieval_timeout_ms / 1000,
            default_top_n=settings.retrieval.top_n,
            rrf_k=settings.retrieval.rrf_k,
            metrics=res.metrics,
            embedder=query_embedder,
        ),
        # RETRIEVAL__CATEGORY_FILTER_ENABLED: only the live benchmark turns the filter off (R12.4).
        query_builder=RetrievalQueryBuilder(QueryBuilderConfig.from_settings(settings.retrieval)),
        business_data_provider=PostgresBusinessDataProvider(
            res.db_pool,
            snapshot_orders=business.snapshot_orders,
            snapshot_tickets=business.snapshot_tickets,
            statement_timeout_ms=business.timeout_ms,
        ),
        job_store=jobs,
        top_k=settings.retrieval.top_k,
        profile_registry=profile_registry,
        business_timeout_ms=business.timeout_ms,
        metrics=res.metrics,
        rerank_service=reranking,
    )
    make_drafting: Callable[..., Any] = drafting_factory or DraftingService
    drafting = make_drafting(
        # The plain provider: SinglePassGenerator wraps it per job with its own budget and
        # telemetry; a pre-built BudgetedLLMProvider would keep its own metrics/price table.
        generator=SinglePassGenerator(
            llm_provider=provider,
            profile_registry=profile_registry,
            metrics=res.metrics,
            price_table=settings.llm.price_table,
        ),
        job_store=jobs,
        persistence=PostgresDraftPersistence(res.db_pool),
        price_table=settings.llm.price_table,
        metrics=res.metrics,
    )
    router = ComplexityRouter(
        settings.complexity_router,
        token_counter=counter,
        metrics=res.metrics,
        tiers_settings=settings.llm,
    )

    return [
        AIWorkerConsumer(
            queue,
            job_store=jobs,
            message_store=messages,
            context_builder=context_builder,
            router=router,
            drafting=drafting,
            summarizer=summarizer,
            broker_settings=settings.broker,
            retry_settings=settings.retry,
            prefetch_count=lane_prefetch(settings, queue),
            connection=res.connection,
            shutdown_coordinator=res.shutdown,
            metrics=res.metrics,
        )
        for queue in resolve_lane_queues(settings)
    ]


async def build_components(res: WorkerResources) -> list[StartFn]:
    """Warm the tokenizers off the event loop, then compose and start every lane consumer."""
    settings = res.settings
    # R5.10: refuse to start when EMBEDDING__DIMENSION disagrees with VECTOR(n).
    await verify_database_vector_dimension(
        settings.database.asyncpg_dsn, configured_dimension=settings.embedding.dimension
    )
    embedder = get_embedder(settings.embedding, metrics=res.metrics)
    start_token_counter_warmup()
    counter = await asyncio.to_thread(TokenCounter)
    consumers = build_consumers(res, token_counter=counter, embedder=embedder)
    provider = consumers[0].drafting.generator.llm_provider if consumers else None
    # After every consumer's close(): the HTTP clients close once all lanes have drained.
    for aclose in (getattr(provider, "aclose", None), getattr(embedder, "aclose", None)):
        if aclose is not None:
            res.shutdown.register_cleanup_callback(aclose)
    return [consumer.start for consumer in consumers]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=AIWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()

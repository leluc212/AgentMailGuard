"""AI-worker composition root: lanes, prefetch, and the telemetry wiring rules (4.13b)."""

from __future__ import annotations

from typing import Any

import pytest

from packages.broker.routing import is_queue_consumed, resolve_configured_consumers
from packages.business.postgres import PostgresBusinessDataProvider
from packages.core.settings import AIWorkerSettings, AppSettings, CategoryRoutingSettings
from packages.domain.taxonomy import TaxonomyRegistry, get_default_registry
from packages.knowledge.token_counter import TokenCounter
from packages.llm import InstrumentedLLMProvider
from packages.llm.budget import BudgetedLLMProvider, CallKind
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.main import (
    DEFAULT_PORT,
    SERVICE_NAME,
    build_components,
    build_consumers,
    lane_prefetch,
    resolve_lane_queues,
)
from tests.stubs.worker_resources import fake_worker_resources


def test_service_identity() -> None:
    assert (SERVICE_NAME, DEFAULT_PORT) == ("ai_worker", 8004)


def test_default_lanes_are_the_configured_declared_queues() -> None:
    settings = AppSettings()
    lanes = resolve_lane_queues(settings)
    assert lanes
    consumers = resolve_configured_consumers(settings.routing)
    assert all(is_queue_consumed(q, consumers) for q in lanes)
    assert "email.support.normal" in lanes and "email.support.priority" in lanes


def test_default_lanes_cover_every_category_of_the_taxonomy() -> None:
    """Amendment G.2: no declared lane is left without a consumer."""
    settings = AppSettings()
    lanes = set(resolve_lane_queues(settings))
    declared = {
        f"email.{category}.{lane}"
        for category in get_default_registry().all_categories()
        for lane in settings.routing.priority_lanes
    }
    assert lanes == declared
    assert "email.administration.priority" in lanes


def test_a_category_added_by_configuration_gets_a_consumer_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TaxonomyRegistry()
    registry.register_from_dict({"category": "g2_added_category"})
    monkeypatch.setattr("services.ai_worker.main.get_default_registry", lambda: registry)
    settings = AppSettings(routing=CategoryRoutingSettings(categories_config_path=""))

    lanes = resolve_lane_queues(settings)

    assert "email.g2_added_category.normal" in lanes
    assert "email.g2_added_category.priority" in lanes


def test_lane_resolution_expands_globs_against_declared_queues() -> None:
    """Review Focus 2."""
    settings = AppSettings(
        routing=CategoryRoutingSettings(configured_consumers=["email.*.priority", "email.nope.x"])
    )
    lanes = resolve_lane_queues(settings)
    assert lanes and all(q.endswith(".priority") for q in lanes)
    assert "email.nope.x" not in lanes
    nothing = AppSettings(routing=CategoryRoutingSettings(configured_consumers=["email.nope.*"]))
    assert resolve_lane_queues(nothing) == []


def test_prefetch_follows_the_lane() -> None:
    settings = AppSettings()
    c = settings.concurrency
    assert lane_prefetch(settings, "email.billing.priority") == c.ai_worker_priority_prefetch
    assert lane_prefetch(settings, "email.billing.normal") == c.ai_worker_normal_prefetch


def test_one_consumer_per_lane_sharing_one_instrumented_pipeline() -> None:
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)
    consumers = build_consumers(res, token_counter=TokenCounter())

    assert [c.queue_name for c in consumers] == resolve_lane_queues(settings)
    first = consumers[0]
    assert all(c.drafting is first.drafting for c in consumers)
    assert {c.prefetch_count for c in consumers} <= {
        settings.concurrency.ai_worker_normal_prefetch,
        settings.concurrency.ai_worker_priority_prefetch,
    }
    generator = first.drafting.generator
    assert not isinstance(generator.llm_provider, BudgetedLLMProvider)
    assert generator.metrics is res.metrics
    assert generator.price_table == settings.llm.price_table
    assert first.drafting.metrics is res.metrics
    assert first.drafting.price_table == settings.llm.price_table
    assert first.summarizer is not None
    summarize_provider = first.summarizer.llm
    assert isinstance(summarize_provider, InstrumentedLLMProvider)
    assert summarize_provider.kind == CallKind.SUMMARIZE
    assert summarize_provider.metrics is res.metrics
    assert isinstance(first.context_builder.retriever, HybridRetriever)


async def test_build_components_warms_the_counter_and_starts_every_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.ai_worker.main as ai_main

    started: list[bool] = []
    monkeypatch.setattr(ai_main, "start_token_counter_warmup", lambda: started.append(True))

    async def _no_db_check(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ai_main, "verify_database_vector_dimension", _no_db_check)
    settings = AIWorkerSettings()

    start_fns = await build_components(fake_worker_resources(settings))

    assert started == [True]
    assert len(start_fns) == len(resolve_lane_queues(settings))


def test_router_follows_the_configured_cascade_settings() -> None:
    """R15.3/R15.6: the worker's router must read ROUTER_* settings, not its own defaults."""
    from packages.core.settings import ComplexityRouterSettings

    settings = AIWorkerSettings(
        complexity_router=ComplexityRouterSettings(
            force_single_tier=True, confidence_threshold=0.99
        )
    )
    res = fake_worker_resources(settings)
    router = build_consumers(res, token_counter=TokenCounter())[0].router

    assert router.settings.force_single_tier is True
    assert router.settings.confidence_threshold == 0.99
    assert router.tiers_settings is settings.llm


async def test_retriever_embeds_with_the_configured_model_and_timeout() -> None:
    """3.16: the corpus model embeds queries (R5.10), counted (R9.11), under R10.9's timeout."""
    from packages.core.settings import EmbeddingSettings, RetrievalSettings

    settings = AIWorkerSettings(
        embedding=EmbeddingSettings(model_name="embed-test"),
        retrieval=RetrievalSettings(retrieval_timeout_ms=750),
    )
    res = fake_worker_resources(settings)
    retriever = build_consumers(res, token_counter=TokenCounter())[0].context_builder.retriever

    assert isinstance(retriever, HybridRetriever) and retriever.embedder is not None
    assert retriever.embedder.model_name == "embed-test"
    assert retriever.embedder.dimension == settings.embedding.dimension
    assert retriever.lexical_timeout_seconds == retriever.vector_timeout_seconds == 0.75
    await retriever.embedder.embed_query("password reset")
    assert res.metrics.embedding_tokens_total.labels(model="embed-test")._value.get() > 0


async def test_build_components_checks_the_vector_dimension_and_closes_the_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5.10 at startup; the real (HTTP) embedder shared by every lane is closed on shutdown."""
    import services.ai_worker.main as ai_main
    from packages.core.settings import EmbeddingSettings

    checked: list[int | None] = []

    async def _check(_dsn: object, configured_dimension: int | None = None) -> None:
        checked.append(configured_dimension)

    monkeypatch.setattr(ai_main, "start_token_counter_warmup", lambda: None)
    monkeypatch.setattr(ai_main, "verify_database_vector_dimension", _check)
    settings = AIWorkerSettings(embedding=EmbeddingSettings(mock=False))
    res = fake_worker_resources(settings)

    start_fns: list[Any] = list(await build_components(res))  # bound AIWorkerConsumer.start

    assert checked == [settings.embedding.dimension]
    embedders = {id(fn.__self__.context_builder.retriever.embedder) for fn in start_fns}
    assert len(embedders) == 1
    embedder = start_fns[0].__self__.context_builder.retriever.embedder
    callbacks = res.shutdown._cleanup_callbacks
    last_consumer_close = max(i for i, cb in enumerate(callbacks) if cb.__name__ == "close")
    assert callbacks.index(embedder.aclose) > last_consumer_close  # closes after lanes drain


def test_context_builder_shares_the_generator_registry_and_uses_the_postgres_provider() -> None:
    """5.4: the builder reads context_policy from the generator's registry; no stub remains."""
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)
    first = build_consumers(res, token_counter=TokenCounter())[0]
    builder = first.context_builder

    assert builder.profile_registry is first.drafting.generator.profile_registry
    assert isinstance(builder.business_data_provider, PostgresBusinessDataProvider)
    assert builder.business_timeout_ms == settings.business_data.timeout_ms
    assert builder.metrics is res.metrics

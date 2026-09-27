"""AI-worker composition root: lanes, prefetch, and the telemetry wiring rules (4.13b)."""

from __future__ import annotations

import pytest

from packages.core.settings import AIWorkerSettings, AppSettings, CategoryRoutingSettings
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
    assert set(lanes) <= set(settings.routing.configured_consumers)
    assert "email.support.normal" in lanes and "email.support.priority" in lanes


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

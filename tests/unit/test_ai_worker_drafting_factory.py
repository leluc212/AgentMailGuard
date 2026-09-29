"""``build_consumers(drafting_factory=...)`` swaps the drafting step of every lane consumer.

The evaluation guard-worker runs the ai-worker's own consumers with a guarded drafting service.
The factory is called with exactly the keyword arguments ``DraftingService(...)`` receives, so a
service with the same public interface drops in.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from packages.core.settings import AIWorkerSettings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.knowledge.token_counter import TokenCounter
from packages.llm import SinglePassGenerator
from services.ai_worker.drafting import DraftingService
from services.ai_worker.main import build_consumers
from tests.stubs.worker_resources import fake_worker_resources


def test_drafting_factory_supplies_the_drafting_service() -> None:
    settings = AIWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)
    received: list[dict[str, Any]] = []
    guarded = MagicMock(name="guarded_drafting")

    def factory(**kwargs: Any) -> Any:
        received.append(kwargs)
        return guarded

    consumers = build_consumers(res, token_counter=TokenCounter(), drafting_factory=factory)

    assert len(received) == 1  # one drafting service, shared by every lane
    assert consumers and all(c.drafting is guarded for c in consumers)
    kwargs = received[0]
    assert set(kwargs) == {"generator", "job_store", "persistence", "price_table", "metrics"}
    assert isinstance(kwargs["generator"], SinglePassGenerator)
    assert kwargs["job_store"] is consumers[0].jobs
    assert isinstance(kwargs["persistence"], PostgresDraftPersistence)
    assert kwargs["price_table"] == settings.llm.price_table
    assert kwargs["metrics"] is res.metrics


def test_drafting_factory_kwargs_are_exactly_what_drafting_service_takes() -> None:
    """A factory that forwards its kwargs builds the stock service: nothing extra, nothing lost."""
    res = fake_worker_resources(AIWorkerSettings(_env_file=None))

    consumers = build_consumers(
        res, token_counter=TokenCounter(), drafting_factory=lambda **kw: DraftingService(**kw)
    )

    drafting = consumers[0].drafting
    assert isinstance(drafting, DraftingService)
    assert drafting.metrics is res.metrics and drafting.generator.metrics is res.metrics


def test_without_a_drafting_factory_the_stock_drafting_service_is_used() -> None:
    res = fake_worker_resources(AIWorkerSettings(_env_file=None))

    consumers = build_consumers(res, token_counter=TokenCounter())

    assert type(consumers[0].drafting) is DraftingService

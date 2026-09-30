"""The retrieval budget covers a hosted embedding call (R10.9).

The vector branch spends RETRIEVAL__RETRIEVAL_TIMEOUT_MS on the query embedding plus the ANN
search. 500 ms let a hosted embedding call time out and silently degrade retrieval to lexical,
so the default is 3000 ms.
"""

from __future__ import annotations

import pytest

from packages.core.settings import AIWorkerSettings, AppSettings, RetrievalSettings
from packages.knowledge.token_counter import TokenCounter
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.main import build_consumers
from tests.stubs.worker_resources import fake_worker_resources


def test_default_budget_is_three_seconds() -> None:
    assert RetrievalSettings().retrieval_timeout_ms == 3000
    assert AppSettings(_env_file=None).retrieval.retrieval_timeout_ms == 3000


def test_environment_variable_still_overrides_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL__RETRIEVAL_TIMEOUT_MS", "750")

    assert AppSettings(_env_file=None).retrieval.retrieval_timeout_ms == 750


def test_the_ai_worker_retriever_gets_the_default_budget() -> None:
    """With nothing configured a hosted embedding call still fits the vector branch."""
    settings = AIWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    retriever = build_consumers(res, token_counter=TokenCounter())[0].context_builder.retriever

    assert isinstance(retriever, HybridRetriever)
    assert retriever.lexical_timeout_seconds == retriever.vector_timeout_seconds == 3.0

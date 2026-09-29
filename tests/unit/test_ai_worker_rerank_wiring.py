"""AI-worker composition root builds the reranker from RETRIEVAL__RERANK_* (R11.1, R11.5; 7.20).

The ai-worker hands one RerankService to the Context Builder its lanes share. Composing the
worker loads no model: the cross-encoder loads on the first rerank, so these tests run with no
torch import, no weights and no network.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from packages.core.settings import AIWorkerSettings, RetrievalSettings
from packages.knowledge.token_counter import TokenCounter
from packages.retrieval.rerank import CrossEncoderReranker, RerankService
from services.ai_worker.main import build_consumers
from tests.stubs.worker_resources import fake_worker_resources


def test_every_lane_shares_a_context_builder_with_the_default_cross_encoder() -> None:
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)

    consumers = build_consumers(res, token_counter=TokenCounter())

    builder = consumers[0].context_builder
    assert all(consumer.context_builder is builder for consumer in consumers)
    service = builder.rerank_service
    assert isinstance(service, RerankService)
    assert isinstance(service.reranker, CrossEncoderReranker)
    assert service.reranker.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert service.reranker.model_dir is None
    assert (service.timeout_seconds, service.default_top_k) == (1.0, settings.retrieval.top_k)
    assert service.metrics is res.metrics


def test_the_rerank_settings_reach_the_builder_and_its_reranker() -> None:
    settings = AIWorkerSettings(
        retrieval=RetrievalSettings(
            rerank_model="org/other-reranker",
            rerank_model_dir="/app/.cache/reranker",
            rerank_timeout_ms=250,
            top_k=3,
        )
    )

    builder = build_consumers(fake_worker_resources(settings), token_counter=TokenCounter())[
        0
    ].context_builder

    service = builder.rerank_service
    assert service is not None and isinstance(service.reranker, CrossEncoderReranker)
    assert service.reranker.model_name == "org/other-reranker"
    assert service.reranker.model_dir == "/app/.cache/reranker"
    assert service.timeout_seconds == 0.25
    assert builder.top_k == service.default_top_k == 3


def test_rerank_enabled_false_builds_no_service() -> None:
    """RETRIEVAL__RERANK_ENABLED is honoured: the builder keeps RRF order and loads nothing."""
    settings = AIWorkerSettings(retrieval=RetrievalSettings(rerank_enabled=False))

    builder = build_consumers(fake_worker_resources(settings), token_counter=TokenCounter())[
        0
    ].context_builder

    assert builder.rerank_service is None


def test_composing_the_worker_loads_no_model() -> None:
    module = MagicMock()

    with patch.dict(sys.modules, {"sentence_transformers": module}):
        build_consumers(fake_worker_resources(AIWorkerSettings()), token_counter=TokenCounter())

    module.CrossEncoder.assert_not_called()

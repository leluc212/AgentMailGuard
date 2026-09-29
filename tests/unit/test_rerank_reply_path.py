"""Reranker in the reply path: model storage, one-time load, budget (R11.1, R11.5; task 7.20).

The ai-worker loads the cross-encoder from the image's model folder with no network, loads it
once and off the event loop, and keeps that one-time load out of the per-rerank budget so the
first job of a fresh worker is reranked like every other. Every model here is a fake: nothing
loads torch, reads weights or touches the network.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import threading
import time
import types
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from packages.core.settings import RetrievalSettings
from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload
from packages.retrieval.models import Candidate
from packages.retrieval.rerank import (
    CrossEncoderReranker,
    RerankerUnavailableError,
    RerankPolicy,
    RerankService,
    StubReranker,
    WarmableReranker,
    build_rerank_service,
)


def _candidate(chunk_id: str, content: str = "text", fused_score: float = 0.03) -> Candidate:
    return Candidate(
        chunk_id=chunk_id, document_id="doc-1", content=content, fused_score=fused_score
    )


def _fake_sentence_transformers(
    *, load_seconds: float = 0.0
) -> tuple[types.ModuleType, list[dict[str, Any]]]:
    """A sentence_transformers whose CrossEncoder records how, where and when it was built.

    predict() scores a pair by the length of its passage, so the longest chunk ranks first.
    """
    loads: list[dict[str, Any]] = []

    class CrossEncoder:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            time.sleep(load_seconds)
            loads.append({"model": model_name, "kwargs": kwargs, "thread": threading.get_ident()})

        def predict(
            self, pairs: list[tuple[str, str]], activation_fn: Callable[[Any], Any] | None = None
        ) -> list[float]:
            return [float(len(passage)) for _, passage in pairs]

    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = CrossEncoder  # type: ignore[attr-defined]
    return module, loads


class TestCrossEncoderModelStorage:
    """R11.1, R11.5: with a model folder the load never touches the network."""

    async def test_model_dir_is_read_as_an_offline_cache_folder(self) -> None:
        module, loads = _fake_sentence_transformers()
        reranker = CrossEncoderReranker("org/model", model_dir="/app/.cache/reranker")

        with patch.dict(sys.modules, {"sentence_transformers": module}):
            await reranker.rerank("q", [_candidate("c1")])

        assert loads == [
            {
                "model": "org/model",
                "kwargs": {
                    "device": None,
                    "cache_folder": "/app/.cache/reranker",
                    "local_files_only": True,
                },
                "thread": loads[0]["thread"],
            }
        ]

    @pytest.mark.parametrize("blank", [None, "", "   "])
    async def test_no_model_dir_means_the_default_hugging_face_cache(
        self, blank: str | None
    ) -> None:
        module, loads = _fake_sentence_transformers()
        reranker = CrossEncoderReranker("org/model", model_dir=blank)

        with patch.dict(sys.modules, {"sentence_transformers": module}):
            await reranker.rerank("q", [_candidate("c1")])

        assert reranker.model_dir is None
        assert loads[0]["kwargs"] == {"device": None}

    async def test_a_model_dir_that_holds_no_model_is_unavailable_not_a_download(self) -> None:
        """local_files_only makes the missing folder an error, which the service turns into RRF."""

        class Offline:
            def __init__(self, *_a: Any, **kwargs: Any) -> None:
                assert kwargs["local_files_only"] is True
                raise OSError("no cached model in /app/.cache/reranker")

        module = types.ModuleType("sentence_transformers")
        module.CrossEncoder = Offline  # type: ignore[attr-defined]
        reranker = CrossEncoderReranker("org/model", model_dir="/app/.cache/reranker")

        with (
            patch.dict(sys.modules, {"sentence_transformers": module}),
            pytest.raises(RerankerUnavailableError, match="no cached model"),
        ):
            await reranker.rerank("q", [_candidate("c1")])


class TestCrossEncoderLoad:
    """R11.1: one load per process, and never on the event loop."""

    async def test_load_runs_off_the_event_loop(self) -> None:
        module, loads = _fake_sentence_transformers(load_seconds=0.02)
        reranker = CrossEncoderReranker("org/model")

        with patch.dict(sys.modules, {"sentence_transformers": module}):
            await reranker.rerank("q", [_candidate("c1")])

        assert loads[0]["thread"] != threading.get_ident()

    async def test_racing_first_calls_load_the_model_once(self) -> None:
        module, loads = _fake_sentence_transformers(load_seconds=0.1)
        reranker = CrossEncoderReranker("org/model")
        pool = [_candidate("short", "a"), _candidate("long", "a much longer passage")]

        with patch.dict(sys.modules, {"sentence_transformers": module}):
            results = await asyncio.gather(*(reranker.rerank("q", pool) for _ in range(4)))

        assert len(loads) == 1
        assert [[c.chunk_id for c in ranked] for ranked in results] == [["long", "short"]] * 4

    def test_warm_up_loads_the_model_once(self) -> None:
        module, loads = _fake_sentence_transformers()
        reranker = CrossEncoderReranker("org/model")

        with patch.dict(sys.modules, {"sentence_transformers": module}):
            reranker.warm_up()
            reranker.warm_up()

        assert len(loads) == 1

    def test_warm_up_fails_as_unavailable_and_does_not_retry(self) -> None:
        reranker = CrossEncoderReranker("org/model")

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with pytest.raises(RerankerUnavailableError, match="Failed to load CrossEncoder"):
                reranker.warm_up()
            module, loads = _fake_sentence_transformers()
            with (
                patch.dict(sys.modules, {"sentence_transformers": module}),
                pytest.raises(RerankerUnavailableError, match="marked unavailable"),
            ):
                reranker.warm_up()

        assert loads == []


def _sigmoid(logit: float) -> float:
    return 1 / (1 + math.exp(-logit))


class _LogitModel:
    """A CrossEncoder as sentence-transformers builds it: predict() ends in an activation.

    The real predict applies ``activation_fn`` when it is given and the model's own default
    otherwise. The ms-marco cross-encoders default to identity, so predict() returns raw logits;
    most other cross-encoders default to a sigmoid and return probabilities.
    """

    def __init__(
        self,
        logits: dict[str, float],
        *,
        default_activation: Callable[[float], float] = lambda logit: logit,
    ) -> None:
        self.logits = logits
        self.default_activation = default_activation

    def predict(
        self, pairs: list[tuple[str, str]], activation_fn: Callable[[Any], Any] | None = None
    ) -> list[float]:
        activation = activation_fn or self.default_activation
        return [activation(self.logits[passage]) for _, passage in pairs]


class TestCrossEncoderScores:
    """R11.1: rerank_score is a relevance probability, the scale the relevance settings use.

    RETRIEVAL__RELEVANCE_FLOOR and ROUTER_MIN_RELEVANCE_SCORE are 0..1 settings, and the
    complexity router compares rerank_score with the second one.
    """

    @staticmethod
    def _reranker(model: _LogitModel) -> CrossEncoderReranker:
        reranker = CrossEncoderReranker("org/model")
        reranker._model = model
        return reranker

    async def test_the_score_is_the_probability_the_models_logit_stands_for(self) -> None:
        # The ms-marco logits the sentence-transformers docs show: 8.6 for a match, -4.3 for a miss.
        model = _LogitModel({"match": 8.6, "near": 0.3, "miss": -4.3})
        pool = [_candidate(name, name) for name in ("miss", "near", "match")]

        ranked = await self._reranker(model).rerank("q", pool)

        assert [c.chunk_id for c in ranked] == ["match", "near", "miss"]
        assert [c.rerank_score for c in ranked] == pytest.approx(
            [_sigmoid(8.6), _sigmoid(0.3), _sigmoid(-4.3)]
        )
        assert all(c.rerank_score is not None and 0.0 <= c.rerank_score <= 1.0 for c in ranked)

    async def test_a_model_that_already_ends_in_a_sigmoid_is_not_squashed_twice(self) -> None:
        """RETRIEVAL__RERANK_MODEL may name a reranker whose predict() returns probabilities."""
        model = _LogitModel({"match": 8.6, "miss": -4.3}, default_activation=_sigmoid)

        ranked = await self._reranker(model).rerank(
            "q", [_candidate("miss", "miss"), _candidate("match", "match")]
        )

        assert [c.rerank_score for c in ranked] == pytest.approx([_sigmoid(8.6), _sigmoid(-4.3)])

    async def test_extreme_logits_saturate_instead_of_overflowing(self) -> None:
        model = _LogitModel({"hi": 1000.0, "lo": -1000.0})

        ranked = await self._reranker(model).rerank(
            "q", [_candidate("lo", "lo"), _candidate("hi", "hi")]
        )

        assert [(c.chunk_id, c.rerank_score) for c in ranked] == [("hi", 1.0), ("lo", 0.0)]


class _WarmableReranker:
    """A reranker with the warm_up() seam RerankService uses to load a model before the clock."""

    def __init__(
        self, *, warm_seconds: Sequence[float] = (0.0,), warm_error: Exception | None = None
    ):
        self._warm_seconds = list(warm_seconds)
        self._warm_error = warm_error
        self.warm_calls = 0

    def warm_up(self) -> None:
        call = self.warm_calls
        self.warm_calls += 1
        time.sleep(self._warm_seconds[min(call, len(self._warm_seconds) - 1)])
        if self._warm_error is not None:
            raise self._warm_error

    async def rerank(
        self, query: str, candidates: Sequence[Candidate], top_k: int | None = None
    ) -> list[Candidate]:
        """Reverse the RRF order, so a reranked result differs from a fallback."""
        ranked = [
            replace(c, rerank_score=1.0 - i / 10) for i, c in enumerate(reversed(list(candidates)))
        ]
        return ranked[:top_k] if top_k is not None else ranked


class TestRerankServiceReadiness:
    """The one-time model load is not rerank latency (R11.6) and cannot eat the rerank budget."""

    def test_the_seam_is_a_runtime_protocol(self) -> None:
        assert isinstance(_WarmableReranker(), WarmableReranker)
        assert isinstance(CrossEncoderReranker(), WarmableReranker)
        assert not isinstance(StubReranker(), WarmableReranker)

    async def test_first_rerank_waits_for_the_load_outside_the_rerank_budget(self) -> None:
        reranker = _WarmableReranker(warm_seconds=(0.3,))
        service = RerankService(reranker, timeout_seconds=0.1)

        result = await service.rerank("q", [_candidate("c1"), _candidate("c2")])

        assert reranker.warm_calls == 1
        assert result.rerank_applied and not result.fallback_recorded
        assert [c.chunk_id for c in result.candidates] == ["c2", "c1"]
        assert result.latency_ms < 200, "the 300 ms load must not count as rerank latency"

    async def test_an_unloadable_model_falls_back_as_unavailable(self) -> None:
        metrics = create_pipeline_metrics()
        reranker = _WarmableReranker(warm_error=RerankerUnavailableError("no such model"))
        service = RerankService(reranker, metrics=metrics)

        result = await service.rerank(
            "q", [_candidate("c1"), _candidate("c2")], organization_id="org-1"
        )

        assert not result.rerank_applied and result.fallback_recorded
        assert result.fallback_reason == "no such model"
        assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]
        payload, _ = generate_metrics_payload(metrics.registry)
        assert 'rerank_fallback_total{reason="unavailable",tenant="org-1"}' in payload.decode()

    async def test_a_load_past_the_load_budget_falls_back_and_the_next_call_reranks(self) -> None:
        reranker = _WarmableReranker(warm_seconds=(0.3, 0.0))
        service = RerankService(reranker, load_timeout_seconds=0.05)
        pool = [_candidate("c1"), _candidate("c2")]

        first = await service.rerank("q", pool)
        await asyncio.sleep(0.4)  # the abandoned load finishes in its thread
        second = await service.rerank("q", pool)

        assert not first.rerank_applied and first.fallback_recorded
        assert "not loaded" in (first.fallback_reason or "")
        assert second.rerank_applied
        assert reranker.warm_calls == 2

    async def test_concurrent_first_reranks_share_one_load(self) -> None:
        reranker = _WarmableReranker(warm_seconds=(0.1,))
        service = RerankService(reranker)
        pool = [_candidate("c1"), _candidate("c2")]

        results = await asyncio.gather(*(service.rerank("q", pool) for _ in range(4)))

        assert reranker.warm_calls == 1
        assert all(r.rerank_applied for r in results)

    async def test_a_disabled_policy_never_loads_the_model(self) -> None:
        reranker = _WarmableReranker()
        service = RerankService(reranker, policy=RerankPolicy(enabled=False))

        result = await service.rerank("q", [_candidate("c1")])

        assert not result.rerank_applied and not result.fallback_recorded
        assert reranker.warm_calls == 0

    async def test_warm_up_ahead_of_time_loads_once(self) -> None:
        reranker = _WarmableReranker()
        service = RerankService(reranker)

        await service.warm_up()
        await service.rerank("q", [_candidate("c1")])

        assert reranker.warm_calls == 1

    def test_load_budget_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="load_timeout_seconds must be > 0"):
            RerankService(StubReranker(), load_timeout_seconds=0)


class TestRerankLogEvent:
    """R21.3: every rerank attempt leaves one structured event."""

    @staticmethod
    def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
        return [
            r.__dict__["fields"]
            for r in caplog.records
            if r.name == "packages.retrieval.rerank" and r.getMessage() == "rerank"
        ]

    async def test_applied_rerank_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        service = RerankService(StubReranker(), default_top_k=2)
        pool = [_candidate(f"c{i}") for i in range(4)]

        with caplog.at_level(logging.INFO, logger="packages.retrieval.rerank"):
            await service.rerank("q", pool, organization_id="org-1")

        (event,) = self._events(caplog)
        assert event["outcome"] == "applied"
        assert (event["candidates"], event["selected"]) == (4, 2)
        assert event["latency_ms"] >= 0

    async def test_fallback_is_logged_with_its_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = RerankService(StubReranker(is_available=False))

        with caplog.at_level(logging.INFO, logger="packages.retrieval.rerank"):
            await service.rerank("q", [_candidate("c1")], organization_id="org-1")

        (event,) = self._events(caplog)
        assert event["outcome"] == "unavailable"
        assert "unavailable" in event["fallback_reason"]

    async def test_timeout_is_logged_as_a_timeout(self, caplog: pytest.LogCaptureFixture) -> None:
        service = RerankService(StubReranker(delay_seconds=0.3), timeout_seconds=0.05)

        with caplog.at_level(logging.INFO, logger="packages.retrieval.rerank"):
            await service.rerank("q", [_candidate("c1")])

        (event,) = self._events(caplog)
        assert event["outcome"] == "timeout"


class TestBuildRerankService:
    """R11.1, R11.5: RETRIEVAL__RERANK_* decide what the ai-worker builds."""

    def test_disabled_builds_nothing(self) -> None:
        assert build_rerank_service(RetrievalSettings(rerank_enabled=False)) is None

    def test_defaults_build_the_minilm_cross_encoder_with_a_one_second_budget(self) -> None:
        metrics = create_pipeline_metrics()

        service = build_rerank_service(RetrievalSettings(), metrics=metrics)

        assert service is not None
        assert isinstance(service.reranker, CrossEncoderReranker)
        assert service.reranker.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert service.reranker.model_dir is None
        assert service.timeout_seconds == 1.0
        assert service.default_top_k == 5
        assert service.metrics is metrics
        assert service.policy.is_enabled(organization_id="org-1", category="billing")

    def test_settings_reach_the_reranker_and_the_service(self) -> None:
        settings = RetrievalSettings(
            rerank_model="org/other-reranker",
            rerank_model_dir="/app/.cache/reranker",
            rerank_timeout_ms=250,
            top_k=3,
        )

        service = build_rerank_service(settings)

        assert service is not None
        assert isinstance(service.reranker, CrossEncoderReranker)
        assert service.reranker.model_name == "org/other-reranker"
        assert service.reranker.model_dir == "/app/.cache/reranker"
        assert service.timeout_seconds == 0.25
        assert service.default_top_k == 3

    def test_building_the_service_loads_no_model(self) -> None:
        """The load waits for the first rerank: composing a worker stays cheap and offline."""
        module = MagicMock()

        with patch.dict(sys.modules, {"sentence_transformers": module}):
            assert build_rerank_service(RetrievalSettings()) is not None

        module.CrossEncoder.assert_not_called()

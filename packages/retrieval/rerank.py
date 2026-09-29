"""Cross-encoder semantic reranker wrapper and policy coordination.

Requirements:
- R11.1: Support optional cross-encoder / semantic reranker over fused candidate set.
- R11.2: Allow reranking to be disabled by configuration, per organization and per category.
- R11.3: Pass configurable top-K to generation (defaulting to 4–6 chunks).
- R11.5: IF reranker is unavailable, THEN fall back to RRF order, record fallback, and continue.
- R11.6: Record rerank_latency_ms separately from retrieval_latency_ms.
- R21.3: One structured ``rerank`` event per attempt; R21.4: Observability metrics
  (rerank_fallback_total, rerank_latency_ms).
- specs/design.md §5.5: Cross-encoder rerank over fused candidates with RRF fallback.

In the ai-worker's reply path (task 7.20) ``build_rerank_service`` composes the RerankService
from the RETRIEVAL__RERANK_* settings. The model is read from RETRIEVAL__RERANK_MODEL_DIR with no
network, loaded once and off the event loop, and that one-time load is kept out of the per-rerank
budget, so the first job of a fresh worker is reranked like every other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from packages.retrieval.models import Candidate

if TYPE_CHECKING:
    from packages.core.settings import RetrievalSettings
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)

RERANK_LOG_EVENT = "rerank"
DEFAULT_LOAD_TIMEOUT_SECONDS = 60.0
"""How long the first rerank waits for the model to load before it falls back to RRF order."""


class RerankerUnavailableError(Exception):
    """Raised when the semantic reranker is not available or fails to initialize."""


class Reranker(Protocol):
    """Protocol for semantic reranking implementations (R11.1)."""

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        top_k: int | None = None,
    ) -> list[Candidate]:
        """Rerank candidates based on semantic relevance to query.

        Args:
            query: User search query or email topic.
            candidates: Sequence of retrieved candidate chunks (typically in RRF order).
            top_k: Optional maximum number of chunks to return.

        Returns:
            Reranked list of Candidate objects with rerank_score populated.
        """
        ...


@runtime_checkable
class WarmableReranker(Protocol):
    """A reranker whose model loads on demand and can be loaded ahead of the first rerank.

    RerankService loads such a reranker before it starts the rerank clock (R11.6): the load is a
    one-time cost of seconds that would otherwise make a fresh worker's first job fall back.
    """

    def warm_up(self) -> None:
        """Load the model now. Blocking: the service calls it in a worker thread."""
        ...


@dataclass
class RerankPolicy:
    """Controls whether cross-encoder reranking is enabled or disabled (R11.2).

    Supports toggling rerank globally, per organization ID, and per category.
    """

    enabled: bool = True
    disabled_organizations: set[str] = field(default_factory=set)
    disabled_categories: set[str] = field(default_factory=set)
    enabled_organizations: set[str] | None = None
    enabled_categories: set[str] | None = None

    def is_enabled(
        self,
        organization_id: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Evaluate if reranking should execute for the given organization and category.

        Fulfills R11.2.
        """
        if not self.enabled:
            return False
        if organization_id and organization_id in self.disabled_organizations:
            return False
        if category and category in self.disabled_categories:
            return False
        if (
            self.enabled_organizations is not None
            and organization_id not in self.enabled_organizations
        ):
            return False
        return not (self.enabled_categories is not None and category not in self.enabled_categories)


@dataclass
class RerankResult:
    """Encapsulates output of a reranking invocation.

    Fulfills R11.5 (fallback tracking) and R11.6 (rerank latency recording).
    """

    candidates: list[Candidate] = field(default_factory=list)
    rerank_applied: bool = False
    fallback_recorded: bool = False
    fallback_reason: str | None = None
    latency_ms: float = 0.0


class CrossEncoderReranker:
    """Production CrossEncoder wrapper.

    Fulfills R11.1. Lazily loads SentenceTransformers CrossEncoder if available,
    raising RerankerUnavailableError if dependencies are missing or model load fails.

    ``rerank_score`` is the model's own output. For the default ms-marco cross-encoders that is
    an unbounded raw logit (the sentence-transformers docs show 8.6 for a match and -4.3 for a
    miss), not a probability between 0 and 1.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        *,
        device: str | None = None,
        model_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        """Configure the reranker; nothing is loaded until ``warm_up`` or the first rerank.

        Args:
            model_name: Hugging Face model id of the cross-encoder.
            device: Torch device, or None to let sentence-transformers choose.
            model_dir: Folder that holds the model in the Hugging Face cache layout (the image
                bakes it there at build time). When set the model is read from it with
                ``local_files_only``, so a missing model is an error and never a download
                (R11.5). None or blank uses the default Hugging Face cache, which downloads on
                first use.
        """
        self.model_name = model_name
        self.device = device
        self.model_dir = (
            str(model_dir) if model_dir is not None and str(model_dir).strip() else None
        )
        self._model: Any = None
        self._available: bool | None = None
        # rerank() loads in a worker thread: racing first calls must build the model once.
        self._load_lock = threading.Lock()

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:  # another thread finished loading while this one waited
                return self._model
            if self._available is False:
                raise RerankerUnavailableError(
                    f"Reranker model {self.model_name} is marked unavailable"
                )

            started = time.perf_counter()
            try:
                from sentence_transformers import CrossEncoder

                kwargs: dict[str, Any] = {"device": self.device}
                if self.model_dir is not None:
                    kwargs.update(cache_folder=self.model_dir, local_files_only=True)
                self._model = CrossEncoder(self.model_name, **kwargs)
                self._available = True
            except Exception as err:
                self._available = False
                raise RerankerUnavailableError(
                    f"Failed to load CrossEncoder model {self.model_name}: {err}"
                ) from err
            logger.info(
                "Loaded reranker model %s from %s in %.1fs",
                self.model_name,
                self.model_dir or "the default Hugging Face cache",
                time.perf_counter() - started,
            )
            return self._model

    def warm_up(self) -> None:
        """Load the model now (R11.1). Blocking: call it in a worker thread, never on the loop.

        Raises:
            RerankerUnavailableError: If the dependency or the model cannot be loaded.
        """
        self._load_model()

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        top_k: int | None = None,
    ) -> list[Candidate]:
        """Compute cross-encoder relevance scores for query-candidate pairs."""
        if not candidates:
            return []

        loop = asyncio.get_running_loop()
        model = self._model
        if model is None:
            # The first load imports torch and reads the weights, which takes seconds: keep it
            # off the event loop. RerankService normally loads the model before it gets here.
            model = await loop.run_in_executor(None, self._load_model)
        pairs = [(query, c.content) for c in candidates]

        # Run CPU/GPU bound prediction in threadpool to avoid blocking asyncio loop
        scores = await loop.run_in_executor(None, model.predict, pairs)

        scored: list[Candidate] = []
        for cand, score in zip(candidates, scores, strict=True):
            scored.append(replace(cand, rerank_score=float(score)))

        # Sort descending by rerank_score, tie-breaking by fused_score then chunk_id
        scored.sort(
            key=lambda c: (
                -float(c.rerank_score or 0.0),
                -float(c.fused_score or 0.0),
                c.chunk_id,
            )
        )

        return scored[:top_k] if top_k is not None else scored


class StubReranker:
    """Deterministic in-memory reranker double for hermetic testing (R24.5)."""

    def __init__(
        self,
        *,
        score_map: dict[str, float] | None = None,
        default_score: float = 0.8,
        delay_seconds: float = 0.0,
        should_raise: Exception | None = None,
        is_available: bool = True,
    ) -> None:
        self.score_map = score_map or {}
        self.default_score = default_score
        self.delay_seconds = delay_seconds
        self.should_raise = should_raise
        self.is_available = is_available
        self.calls: list[tuple[str, int]] = []

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        top_k: int | None = None,
    ) -> list[Candidate]:
        self.calls.append((query, len(candidates)))

        if not self.is_available:
            raise RerankerUnavailableError("Stub reranker explicitly marked unavailable")

        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        if self.should_raise:
            raise self.should_raise

        if not candidates:
            return []

        scored: list[Candidate] = []
        for i, c in enumerate(candidates):
            score = self.score_map.get(c.chunk_id, self.default_score - (i * 0.01))
            scored.append(replace(c, rerank_score=float(score)))

        scored.sort(
            key=lambda c: (
                -float(c.rerank_score or 0.0),
                -float(c.fused_score or 0.0),
                c.chunk_id,
            )
        )

        return scored[:top_k] if top_k is not None else scored


def _log_rerank(
    outcome: str,
    *,
    organization_id: str | None,
    category: str | None,
    model: str | None,
    candidates: int,
    selected: int,
    top_k: int,
    latency_ms: float,
    fallback_reason: str | None = None,
) -> None:
    """Emit the structured ``rerank`` event of one attempt (R21.3).

    ``outcome`` is ``applied`` or the fallback reason (``timeout``, ``unavailable``, ``error``).
    """
    with contextlib.suppress(Exception):  # logging must never fail the job
        fields: dict[str, Any] = {
            "outcome": outcome,
            "model": model,
            "category": category,
            "candidates": candidates,
            "selected": selected,
            "top_k": top_k,
            "latency_ms": round(latency_ms, 2),
        }
        if fallback_reason is not None:
            fields["fallback_reason"] = fallback_reason
        logger.info(RERANK_LOG_EVENT, extra={"organization_id": organization_id, "fields": fields})


class RerankService:
    """Coordinates policy-based cross-encoder reranking and RRF fallback.

    Fulfills R11.1, R11.2, R11.3, R11.5, R11.6, and R21.4.
    """

    def __init__(
        self,
        reranker: Reranker,
        *,
        policy: RerankPolicy | None = None,
        timeout_seconds: float = 1.0,
        default_top_k: int = 5,
        metrics: PipelineMetrics | None = None,
        load_timeout_seconds: float = DEFAULT_LOAD_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        if default_top_k <= 0:
            raise ValueError(f"default_top_k must be > 0, got {default_top_k}")
        if load_timeout_seconds <= 0:
            raise ValueError(f"load_timeout_seconds must be > 0, got {load_timeout_seconds}")

        self.reranker = reranker
        self.policy = policy or RerankPolicy()
        self.timeout_seconds = timeout_seconds
        self.default_top_k = default_top_k
        self.metrics = metrics
        self.load_timeout_seconds = load_timeout_seconds
        self._model_ready = False
        # Concurrent first reranks queue here instead of each holding a worker thread.
        self._load_lock = asyncio.Lock()

    async def warm_up(self) -> None:
        """Load a WarmableReranker's model before the rerank clock starts (R11.1, R11.6).

        The first load imports torch and reads the weights, which takes seconds. Inside the
        rerank budget it would make the first job of every fresh worker fall back to RRF order,
        so it gets its own budget, ``load_timeout_seconds``. The load runs once: concurrent
        callers wait for it, and later calls return at once. A reranker without ``warm_up`` is
        left alone.

        Raises:
            RerankerUnavailableError: If the model cannot be loaded, or not within the budget
                (the load then carries on in its thread and a later call finds it ready).
        """
        reranker = self.reranker
        if self._model_ready or not isinstance(reranker, WarmableReranker):
            return
        async with self._load_lock:
            if self._model_ready:
                return
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(reranker.warm_up), timeout=self.load_timeout_seconds
                )
            except TimeoutError as err:
                raise RerankerUnavailableError(
                    f"Reranker model not loaded after {self.load_timeout_seconds}s"
                ) from err
            self._model_ready = True

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        *,
        organization_id: str | None = None,
        category: str | None = None,
        top_k: int | None = None,
        timeout: float | None = None,
    ) -> RerankResult:
        """Execute semantic reranking with policy check, timeout, and RRF fallback.

        Fulfills R11.1, R11.2, R11.5, and R11.6.
        """
        k = top_k if top_k is not None else self.default_top_k
        if not candidates:
            return RerankResult(candidates=[])

        # Step 1: Policy check (R11.2)
        if not self.policy.is_enabled(organization_id=organization_id, category=category):
            logger.debug(
                "Reranking disabled for org=%s, category=%s; preserving RRF order",
                organization_id,
                category,
            )
            return RerankResult(
                candidates=list(candidates)[:k],
                rerank_applied=False,
                fallback_recorded=False,
                fallback_reason=None,
                latency_ms=0.0,
            )

        # Step 2: Load the model outside the rerank budget, then attempt reranking with timeout
        # (R11.1, R11.6). A model that cannot load falls back like any other failure (R11.5).
        to = timeout if timeout is not None else self.timeout_seconds
        start_time = time.perf_counter()

        try:
            await self.warm_up()
            start_time = time.perf_counter()  # the one-time model load is not rerank latency
            reranked = await asyncio.wait_for(
                self.reranker.rerank(query, candidates, top_k=k),
                timeout=to,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            # Record rerank latency histogram (R11.6, R21.4)
            if self.metrics is not None:
                with contextlib.suppress(Exception):
                    self.metrics.rerank_latency_ms.observe(elapsed_ms)
            _log_rerank(
                "applied",
                organization_id=organization_id,
                category=category,
                model=getattr(self.reranker, "model_name", None),
                candidates=len(candidates),
                selected=len(reranked),
                top_k=k,
                latency_ms=elapsed_ms,
            )

            return RerankResult(
                candidates=reranked,
                rerank_applied=True,
                fallback_recorded=False,
                fallback_reason=None,
                latency_ms=elapsed_ms,
            )

        except TimeoutError:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            msg = f"Timeout after {to}s"
            logger.warning(
                "Reranker timed out after %.2fs for tenant %s; falling back to RRF order (R11.5)",
                to,
                organization_id,
            )
            if self.metrics is not None:
                org = organization_id or "unknown"
                with contextlib.suppress(Exception):
                    self.metrics.rerank_latency_ms.observe(elapsed_ms)
                    self.metrics.rerank_fallback_total.labels(tenant=org, reason="timeout").inc()
            _log_rerank(
                "timeout",
                organization_id=organization_id,
                category=category,
                model=getattr(self.reranker, "model_name", None),
                candidates=len(candidates),
                selected=min(k, len(candidates)),
                top_k=k,
                latency_ms=elapsed_ms,
                fallback_reason=msg,
            )

            return RerankResult(
                candidates=list(candidates)[:k],
                rerank_applied=False,
                fallback_recorded=True,
                fallback_reason=msg,
                latency_ms=elapsed_ms,
            )

        except Exception as err:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            msg = str(err)
            reason = "unavailable" if isinstance(err, RerankerUnavailableError) else "error"
            logger.warning(
                "Reranker unavailable/failed for tenant %s (%s); falling back to RRF order (R11.5)",
                organization_id,
                err,
            )
            if self.metrics is not None:
                org = organization_id or "unknown"
                with contextlib.suppress(Exception):
                    self.metrics.rerank_latency_ms.observe(elapsed_ms)
                    self.metrics.rerank_fallback_total.labels(tenant=org, reason=reason).inc()
            _log_rerank(
                reason,
                organization_id=organization_id,
                category=category,
                model=getattr(self.reranker, "model_name", None),
                candidates=len(candidates),
                selected=min(k, len(candidates)),
                top_k=k,
                latency_ms=elapsed_ms,
                fallback_reason=msg,
            )

            return RerankResult(
                candidates=list(candidates)[:k],
                rerank_applied=False,
                fallback_recorded=True,
                fallback_reason=msg,
                latency_ms=elapsed_ms,
            )


def build_rerank_service(
    settings: RetrievalSettings, *, metrics: PipelineMetrics | None = None
) -> RerankService | None:
    """Compose the ai-worker's RerankService from the RETRIEVAL__RERANK_* settings (R11.1, R11.5).

    Returns None when RETRIEVAL__RERANK_ENABLED is false: the Context Builder then keeps RRF
    order and no model is ever loaded. Otherwise the service wraps a CrossEncoderReranker for
    RETRIEVAL__RERANK_MODEL, read from RETRIEVAL__RERANK_MODEL_DIR when that is set, with the
    RETRIEVAL__RERANK_TIMEOUT_MS budget and RETRIEVAL__TOP_K as its default cut. Building it
    loads nothing: the model loads on the first rerank (or ``RerankService.warm_up``).
    """
    if not settings.rerank_enabled:
        return None
    return RerankService(
        CrossEncoderReranker(settings.rerank_model, model_dir=settings.rerank_model_dir),
        timeout_seconds=settings.rerank_timeout_ms / 1000,
        default_top_k=settings.top_k,
        metrics=metrics,
    )

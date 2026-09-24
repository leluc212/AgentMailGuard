"""Cross-encoder semantic reranker wrapper and policy coordination.

Requirements:
- R11.1: Support optional cross-encoder / semantic reranker over fused candidate set.
- R11.2: Allow reranking to be disabled by configuration, per organization and per category.
- R11.3: Pass configurable top-K to generation (defaulting to 4–6 chunks).
- R11.5: IF reranker is unavailable, THEN fall back to RRF order, record fallback, and continue.
- R11.6: Record rerank_latency_ms separately from retrieval_latency_ms.
- R21.4: Observability metrics (rerank_fallback_total, rerank_latency_ms).
- specs/design.md §5.5: Cross-encoder rerank over fused candidates with RRF fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from packages.retrieval.models import Candidate

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)


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
        return not (
            self.enabled_categories is not None
            and category not in self.enabled_categories
        )


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
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        *,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self._model: Any = None
        self._available: bool | None = None

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._available is False:
            raise RerankerUnavailableError(
                f"Reranker model {self.model_name} is marked unavailable"
            )

        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name, device=self.device)
            self._available = True
            return self._model
        except Exception as err:
            self._available = False
            raise RerankerUnavailableError(
                f"Failed to load CrossEncoder model {self.model_name}: {err}"
            ) from err

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        top_k: int | None = None,
    ) -> list[Candidate]:
        """Compute cross-encoder relevance scores for query-candidate pairs."""
        if not candidates:
            return []

        model = self._load_model()
        pairs = [(query, c.content) for c in candidates]

        # Run CPU/GPU bound prediction in threadpool to avoid blocking asyncio loop
        loop = asyncio.get_running_loop()
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
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        if default_top_k <= 0:
            raise ValueError(f"default_top_k must be > 0, got {default_top_k}")

        self.reranker = reranker
        self.policy = policy or RerankPolicy()
        self.timeout_seconds = timeout_seconds
        self.default_top_k = default_top_k
        self.metrics = metrics

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

        # Step 2: Attempt reranking with timeout (R11.1, R11.6)
        to = timeout if timeout is not None else self.timeout_seconds
        start_time = time.perf_counter()

        try:
            reranked = await asyncio.wait_for(
                self.reranker.rerank(query, candidates, top_k=k),
                timeout=to,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            # Record rerank latency histogram (R11.6, R21.4)
            if self.metrics is not None:
                with contextlib.suppress(Exception):
                    self.metrics.rerank_latency_ms.observe(elapsed_ms)

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
                    self.metrics.rerank_fallback_total.labels(
                        tenant=org, reason="timeout"
                    ).inc()

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
                "Reranker unavailable/failed for tenant %s (%s); "
                "falling back to RRF order (R11.5)",
                organization_id,
                err,
            )
            if self.metrics is not None:
                org = organization_id or "unknown"
                with contextlib.suppress(Exception):
                    self.metrics.rerank_fallback_total.labels(
                        tenant=org, reason=reason
                    ).inc()

            return RerankResult(
                candidates=list(candidates)[:k],
                rerank_applied=False,
                fallback_recorded=True,
                fallback_reason=msg,
                latency_ms=elapsed_ms,
            )

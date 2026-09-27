"""Concurrent hybrid retrieval orchestrator with per-branch timeouts and degradation.

Requirements:
- R10.5: Concurrent execution of lexical and vector search branches.
- R10.6: IF one branch fails or times out, THEN THE SYSTEM SHALL degrade to the
  surviving branch, record retrieval_degraded=true, and continue.
- R10.9: Enforce configurable retrieval timeout and do not block worker indefinitely.
- R10.8: Return for every candidate its lexical rank, vector rank, fused score,
  and source metadata.
- R21.4: Observability metrics recording (retrieval_degraded_total, retrieval_latency_ms).
- R23.3: Debug visibility into both branch results and final candidates.
- specs/design.md §5.5: Hybrid search orchestration and fallback semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.protocol import SearchBackend
from packages.retrieval.rrf import DEFAULT_RRF_K, fuse_lexical_and_vector

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)


class RetrievalError(Exception):
    """Raised when retrieval fails completely and cannot degrade to any branch."""


@dataclass
class RetrievalResult:
    """Encapsulates the complete output of a hybrid retrieval operation.

    Fulfills R10.6, R10.8, and R23.3 (retrieval debug inspection).
    """

    candidates: list[Candidate] = field(default_factory=list)
    retrieval_degraded: bool = False
    surviving_branch: str | None = None  # "lexical", "vector", or None
    lexical_candidates: list[Candidate] = field(default_factory=list)
    vector_candidates: list[Candidate] = field(default_factory=list)
    lexical_error: str | None = None
    vector_error: str | None = None
    lexical_latency_ms: float = 0.0
    vector_latency_ms: float = 0.0
    total_latency_ms: float = 0.0


class HybridRetriever:
    """Coordinates concurrent hybrid retrieval with per-branch timeouts and degradation.

    Fulfills R10.5, R10.6, R10.8, and R10.9.
    """

    def __init__(
        self,
        backend: SearchBackend,
        *,
        timeout_seconds: float = 2.0,
        lexical_timeout_seconds: float | None = None,
        vector_timeout_seconds: float | None = None,
        default_top_n: int = 20,
        rrf_k: int = DEFAULT_RRF_K,
        raise_on_both_failed: bool = False,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        """Initialize the hybrid retriever.

        Args:
            backend: Underlying SearchBackend implementation (e.g. PostgresSearchBackend).
            timeout_seconds: Default per-branch timeout in seconds (default 2.0s).
            lexical_timeout_seconds: Specific timeout override for lexical branch.
            vector_timeout_seconds: Specific timeout override for vector branch.
            default_top_n: Number of candidates requested from each branch (R10.2).
            rrf_k: Smoothing constant for RRF fusion, default 60 (R10.3).
            raise_on_both_failed: If True, raise RetrievalError when both branches fail.
            metrics: Optional PipelineMetrics instance for Prometheus observability (R21.4).
        """
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        if default_top_n <= 0:
            raise ValueError(f"default_top_n must be > 0, got {default_top_n}")

        self.backend = backend
        self.timeout_seconds = timeout_seconds
        self.lexical_timeout_seconds = (
            lexical_timeout_seconds if lexical_timeout_seconds is not None else timeout_seconds
        )
        self.vector_timeout_seconds = (
            vector_timeout_seconds if vector_timeout_seconds is not None else timeout_seconds
        )
        self.default_top_n = default_top_n
        self.rrf_k = rrf_k
        self.raise_on_both_failed = raise_on_both_failed
        self.metrics = metrics

    async def retrieve(
        self,
        query: RetrievalQuery,
        *,
        top_n: int | None = None,
        timeout: float | None = None,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
        limit: int | None = None,
    ) -> RetrievalResult:
        """Execute concurrent hybrid retrieval with per-branch timeouts and degradation.

        Args:
            query: Multi-modal retrieval query carrying intent, identifiers, and filters.
            top_n: Optional candidate count override per branch (default self.default_top_n).
            timeout: Optional per-branch timeout override in seconds.
            lexical_weight: Weight applied to lexical branch RRF scores (default 1.0).
            vector_weight: Weight applied to vector branch RRF scores (default 1.0).
            limit: Maximum number of fused candidates to return (default None).

        Returns:
            RetrievalResult containing fused candidates, degradation flag, and debug telemetry.

        Raises:
            RetrievalError: If both branches fail and raise_on_both_failed is True.
        """
        n = top_n if top_n is not None else self.default_top_n
        lex_to = timeout if timeout is not None else self.lexical_timeout_seconds
        vec_to = timeout if timeout is not None else self.vector_timeout_seconds

        start_total = time.perf_counter()

        async def _run_lexical() -> tuple[list[Candidate], str | None, float]:
            t0 = time.perf_counter()
            try:
                res = await asyncio.wait_for(self.backend.lexical(query, n), timeout=lex_to)
                dt = (time.perf_counter() - t0) * 1000.0
                return res, None, dt
            except TimeoutError:
                dt = (time.perf_counter() - t0) * 1000.0
                msg = f"Timeout after {lex_to}s"
                logger.warning(
                    "Lexical branch timed out after %.2fs for tenant %s",
                    lex_to,
                    query.organization_id,
                )
                return [], msg, dt
            except Exception as err:
                dt = (time.perf_counter() - t0) * 1000.0
                logger.warning(
                    "Lexical branch failed for tenant %s: %s",
                    query.organization_id,
                    err,
                )
                return [], str(err), dt

        async def _run_vector() -> tuple[list[Candidate], str | None, float]:
            t0 = time.perf_counter()
            try:
                res = await asyncio.wait_for(self.backend.vector(query, n), timeout=vec_to)
                dt = (time.perf_counter() - t0) * 1000.0
                return res, None, dt
            except TimeoutError:
                dt = (time.perf_counter() - t0) * 1000.0
                msg = f"Timeout after {vec_to}s"
                logger.warning(
                    "Vector branch timed out after %.2fs for tenant %s",
                    vec_to,
                    query.organization_id,
                )
                return [], msg, dt
            except Exception as err:
                dt = (time.perf_counter() - t0) * 1000.0
                logger.warning(
                    "Vector branch failed for tenant %s: %s",
                    query.organization_id,
                    err,
                )
                return [], str(err), dt

        # Execute branches concurrently (R10.5)
        (
            (lex_candidates, lex_err, lex_latency),
            (vec_candidates, vec_err, vec_latency),
        ) = await asyncio.gather(_run_lexical(), _run_vector())

        total_latency = (time.perf_counter() - start_total) * 1000.0

        lex_failed = lex_err is not None
        vec_failed = vec_err is not None

        retrieval_degraded = False
        surviving_branch: str | None = None
        fused: list[Candidate] = []

        if lex_failed and vec_failed:
            retrieval_degraded = True
            surviving_branch = None
            fused = []
            logger.error(
                "Both retrieval branches failed for tenant %s (lexical: %s, vector: %s)",
                query.organization_id,
                lex_err,
                vec_err,
            )
            if self.metrics is not None:
                org = query.organization_id or "unknown"
                with contextlib.suppress(Exception):
                    self.metrics.retrieval_degraded_total.labels(
                        tenant=org, failed_branch="both"
                    ).inc()

            if self.raise_on_both_failed:
                if self.metrics is not None:
                    with contextlib.suppress(Exception):
                        self.metrics.retrieval_latency_ms.labels(mode="failed").observe(
                            total_latency
                        )
                raise RetrievalError(
                    f"Both retrieval branches failed for tenant {query.organization_id}: "
                    f"lexical: {lex_err}, vector: {vec_err}"
                )

        elif lex_failed:
            # Degrade to surviving vector branch (R10.6)
            retrieval_degraded = True
            surviving_branch = "vector"
            fused = fuse_lexical_and_vector(
                lexical_candidates=None,
                vector_candidates=vec_candidates,
                k=self.rrf_k,
                lexical_weight=lexical_weight,
                vector_weight=vector_weight,
                limit=limit,
            )
            logger.warning(
                "Retrieval degraded for tenant %s: lexical branch failed (%s), "
                "continuing on surviving vector branch (%d candidates)",
                query.organization_id,
                lex_err,
                len(vec_candidates),
            )
            if self.metrics is not None:
                org = query.organization_id or "unknown"
                with contextlib.suppress(Exception):
                    self.metrics.retrieval_degraded_total.labels(
                        tenant=org, failed_branch="lexical"
                    ).inc()

        elif vec_failed:
            # Degrade to surviving lexical branch (R10.6)
            retrieval_degraded = True
            surviving_branch = "lexical"
            fused = fuse_lexical_and_vector(
                lexical_candidates=lex_candidates,
                vector_candidates=None,
                k=self.rrf_k,
                lexical_weight=lexical_weight,
                vector_weight=vector_weight,
                limit=limit,
            )
            logger.warning(
                "Retrieval degraded for tenant %s: vector branch failed (%s), "
                "continuing on surviving lexical branch (%d candidates)",
                query.organization_id,
                vec_err,
                len(lex_candidates),
            )
            if self.metrics is not None:
                org = query.organization_id or "unknown"
                with contextlib.suppress(Exception):
                    self.metrics.retrieval_degraded_total.labels(
                        tenant=org, failed_branch="vector"
                    ).inc()

        else:
            # Both branches succeeded normally
            retrieval_degraded = False
            surviving_branch = None
            fused = fuse_lexical_and_vector(
                lexical_candidates=lex_candidates,
                vector_candidates=vec_candidates,
                k=self.rrf_k,
                lexical_weight=lexical_weight,
                vector_weight=vector_weight,
                limit=limit,
            )

        # Record retrieval latency metric (R21.4)
        if self.metrics is not None:
            if lex_failed and vec_failed:
                mode = "failed"
            elif retrieval_degraded:
                mode = "degraded"
            else:
                mode = "hybrid"
            with contextlib.suppress(Exception):
                self.metrics.retrieval_latency_ms.labels(mode=mode).observe(total_latency)

        return RetrievalResult(
            candidates=fused,
            retrieval_degraded=retrieval_degraded,
            surviving_branch=surviving_branch,
            lexical_candidates=lex_candidates,
            vector_candidates=vec_candidates,
            lexical_error=lex_err,
            vector_error=vec_err,
            lexical_latency_ms=lex_latency,
            vector_latency_ms=vec_latency,
            total_latency_ms=total_latency,
        )

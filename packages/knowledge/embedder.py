"""Provider-abstracted embedding service with batching, retries, and token accounting.

Requirements:
- R9.6: Generate and store one embedding per chunk, recording the embedding model name
  and dimension.
- R9.11: Track embedding_tokens_total for cost accounting.
- R21.4: Prometheus metrics exposure for embedding_tokens_total.
- R5.10: Validate embedding vector dimension matches configured width (1536).
- specs/design.md §5.6 & §6.1: Knowledge embedding data model and contracts.
- GEMINI.md §8: External systems faked in CI via FakeEmbedder.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from packages.core.settings import EmbeddingSettings
from packages.observability.metrics import PipelineMetrics, get_metrics

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "text-embedding-3-small"
DEFAULT_DIMENSION = 1536
DEFAULT_BATCH_SIZE = 64


class EmbeddingError(Exception):
    """Base exception for embedding operations."""


class EmbeddingTimeoutError(EmbeddingError):
    """Raised when an embedding request exceeds its allotted timeout."""


class EmbeddingRateLimitError(EmbeddingError):
    """Raised when an embedding request is rate limited after retry exhaustion."""


class EmbeddingDimensionMismatchError(EmbeddingError):
    """Raised when returned vector dimension does not match configured dimension (R5.10)."""


@dataclass(frozen=True)
class EmbeddingResult:
    """Execution output of an embedding generation request (R9.6, R9.11)."""

    embeddings: list[list[float]]
    model: str
    dimension: int
    token_count: int
    latency_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Embedder(Protocol):
    """Abstract interface for all embedding providers (R9.6, design.md §5.6)."""

    @property
    def model_name(self) -> str:
        """Name of the underlying embedding model."""
        ...

    @property
    def dimension(self) -> int:
        """Vector dimensionality produced by this embedder."""
        ...

    @property
    def batch_size(self) -> int:
        """Maximum number of texts processed in a single API call."""
        ...

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        """Generate vector embeddings for a list of document chunk texts."""
        ...

    async def embed_query(self, query: str) -> list[float]:
        """Generate a vector embedding for a single search query string."""
        ...


class HttpEmbedder(Embedder):
    """OpenAI-compatible HTTP embedding client with micro-batching and retries."""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        model_name: str = DEFAULT_MODEL_NAME,
        dimension: int = DEFAULT_DIMENSION,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        retry_delay_s: float = 0.5,
        client: httpx.AsyncClient | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._dimension = dimension
        self._batch_size = max(1, batch_size)
        self._timeout_s = timeout_s
        self._max_retries = max(0, max_retries)
        self._retry_delay_s = max(0.0, retry_delay_s)
        self._client = client
        self._owns_client = client is None
        self._metrics = metrics

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def _get_metrics(self) -> PipelineMetrics | None:
        if self._metrics is None:
            with contextlib.suppress(Exception):
                self._metrics = get_metrics()
        return self._metrics

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        """Generate vector embeddings with automatic sequential micro-batching."""
        if not texts:
            return EmbeddingResult(
                embeddings=[],
                model=self._model_name,
                dimension=self._dimension,
                token_count=0,
                latency_ms=0,
            )

        start_time = time.perf_counter()
        all_embeddings: list[list[float]] = []
        total_tokens = 0

        # Micro-batch sequentially to respect batch_size and provider rate limits
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            batch_embeddings, batch_tokens = await self._embed_batch_with_retry(batch)
            all_embeddings.extend(batch_embeddings)
            total_tokens += batch_tokens

        elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))

        # Prometheus metric accounting (R9.11, R21.4)
        metrics = self._get_metrics()
        if metrics is not None and total_tokens > 0:
            metrics.embedding_tokens_total.labels(model=self._model_name).inc(total_tokens)

        return EmbeddingResult(
            embeddings=all_embeddings,
            model=self._model_name,
            dimension=self._dimension,
            token_count=total_tokens,
            latency_ms=elapsed_ms,
        )

    async def embed_query(self, query: str) -> list[float]:
        """Generate a single vector embedding for search queries."""
        res = await self.embed_texts([query])
        if res.embeddings:
            return res.embeddings[0]
        return [0.0] * self._dimension

    async def _embed_batch_with_retry(self, batch: list[str]) -> tuple[list[list[float]], int]:
        """Execute a single batch request with exponential backoff on transient errors."""
        payload: dict[str, Any] = {
            "input": batch,
            "model": self._model_name,
        }
        if self._dimension:
            payload["dimensions"] = self._dimension

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        endpoint = f"{self._base_url}/embeddings"
        client = await self._get_client()

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(
                    endpoint, json=payload, headers=headers, timeout=self._timeout_s
                )

                if response.status_code == 429:
                    if attempt < self._max_retries:
                        delay = self._calculate_backoff(attempt, response.headers)
                        logger.warning(
                            "Embedding API rate limited (429). Retrying in %.2fs (attempt %d/%d)",
                            delay,
                            attempt + 1,
                            self._max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise EmbeddingRateLimitError(
                        f"Embedding rate limit exceeded after {self._max_retries} retries"
                    )

                if response.status_code in (500, 502, 503, 504):
                    if attempt < self._max_retries:
                        delay = self._calculate_backoff(attempt)
                        logger.warning(
                            "Embedding API server error (%d). Retrying in %.2fs (attempt %d/%d)",
                            response.status_code,
                            delay,
                            attempt + 1,
                            self._max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    err_msg = (
                        f"Embedding server error {response.status_code} "
                        f"after retries: {response.text}"
                    )
                    raise EmbeddingError(err_msg)

                response.raise_for_status()
                data = response.json()

                # Extract and order embeddings
                items = data.get("data", [])
                items.sort(key=lambda x: int(x.get("index", 0)))
                embeddings = [item["embedding"] for item in items]

                # Validate vector dimensionality (R5.10)
                for idx, vec in enumerate(embeddings):
                    if len(vec) != self._dimension:
                        raise EmbeddingDimensionMismatchError(
                            f"Returned embedding dimension {len(vec)} does not match "
                            f"configured dimension {self._dimension} at index {idx}"
                        )

                tokens = int(data.get("usage", {}).get("prompt_tokens", 0))
                return embeddings, tokens

            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    delay = self._calculate_backoff(attempt)
                    logger.warning("Embedding request timeout. Retrying in %.2fs", delay)
                    await asyncio.sleep(delay)
                    continue
                raise EmbeddingTimeoutError(
                    f"Embedding request timed out after {self._timeout_s}s"
                ) from exc

            except httpx.NetworkError as exc:
                if attempt < self._max_retries:
                    delay = self._calculate_backoff(attempt)
                    logger.warning("Embedding network error (%s). Retrying in %.2fs", exc, delay)
                    await asyncio.sleep(delay)
                    continue
                raise EmbeddingError(f"Embedding network transport failure: {exc}") from exc

            except httpx.HTTPStatusError as exc:
                # 4xx client errors (400, 401, 403) fail fast immediately
                raise EmbeddingError(
                    f"Embedding client error HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise EmbeddingError("Failed to generate embeddings after all retry attempts")

    def _calculate_backoff(self, attempt: int, headers: httpx.Headers | None = None) -> float:
        """Calculate backoff duration checking Retry-After header or exponential ladder."""
        if headers and "Retry-After" in headers:
            try:
                return float(headers["Retry-After"])
            except ValueError:
                pass
        return float(self._retry_delay_s * (2**attempt))


class FakeEmbedder(Embedder):
    """Deterministic, mockable Embedder for offline CI and hermetic testing (GEMINI.md §8)."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        dimension: int = DEFAULT_DIMENSION,
        batch_size: int = DEFAULT_BATCH_SIZE,
        simulate_latency_ms: int = 0,
        error_to_raise: Exception | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self._model_name = model_name
        self._dimension = dimension
        self._batch_size = max(1, batch_size)
        self._simulate_latency_ms = simulate_latency_ms
        self._error_to_raise = error_to_raise
        self._metrics = metrics
        self.recorded_calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def set_error(self, error: Exception | None) -> None:
        """Inject an exception to be raised on subsequent embed invocations."""
        self._error_to_raise = error

    def clear_recorded_calls(self) -> None:
        """Clear recorded invocation history."""
        self.recorded_calls.clear()

    def _generate_deterministic_vector(self, text: str) -> list[float]:
        """Generate a deterministic unit-length vector using SHA-256 seed."""
        vec: list[float] = []
        base_hash = hashlib.sha256(text.encode("utf-8")).digest()

        for i in range(self._dimension):
            h = hashlib.sha256(base_hash + i.to_bytes(4, "big")).digest()
            val = int.from_bytes(h[:4], "big", signed=True) / (2**31)
            vec.append(val)

        # L2 normalize so cosine similarity equals dot product
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        """Generate deterministic vectors for testing."""
        start_time = time.perf_counter()

        call_record = {
            "texts": list(texts),
            "timestamp": time.time(),
        }
        self.recorded_calls.append(call_record)

        if self._simulate_latency_ms > 0:
            await asyncio.sleep(self._simulate_latency_ms / 1000.0)

        if self._error_to_raise is not None:
            raise self._error_to_raise

        if not texts:
            return EmbeddingResult(
                embeddings=[],
                model=self._model_name,
                dimension=self._dimension,
                token_count=0,
                latency_ms=0,
            )

        embeddings = [self._generate_deterministic_vector(t) for t in texts]

        # Approximate tokens based on whitespace words (~1.3 tokens per word)
        total_tokens = sum(max(1, int(len(t.split()) * 1.3)) for t in texts)
        elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))

        if self._metrics is not None and total_tokens > 0:
            self._metrics.embedding_tokens_total.labels(model=self._model_name).inc(total_tokens)

        return EmbeddingResult(
            embeddings=embeddings,
            model=self._model_name,
            dimension=self._dimension,
            token_count=total_tokens,
            latency_ms=elapsed_ms,
        )

    async def embed_query(self, query: str) -> list[float]:
        """Generate a deterministic query vector."""
        res = await self.embed_texts([query])
        return res.embeddings[0] if res.embeddings else [0.0] * self._dimension


def get_embedder(
    settings: EmbeddingSettings | None = None,
    client: httpx.AsyncClient | None = None,
    metrics: PipelineMetrics | None = None,
) -> Embedder:
    """Factory creating configured Embedder (FakeEmbedder in mock/CI, HttpEmbedder in live)."""
    cfg = settings or EmbeddingSettings()

    if cfg.mock:
        return FakeEmbedder(
            model_name=cfg.model_name,
            dimension=cfg.dimension,
            batch_size=cfg.batch_size,
            metrics=metrics,
        )

    return HttpEmbedder(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model_name=cfg.model_name,
        dimension=cfg.dimension,
        batch_size=cfg.batch_size,
        timeout_s=cfg.timeout_s,
        max_retries=cfg.max_retries,
        retry_delay_s=cfg.retry_delay_s,
        client=client,
        metrics=metrics,
    )

"""Unit tests for the provider-abstracted embedding service.

Requirements:
- R9.6: Generate and store one embedding per chunk, recording the embedding model name
  and dimension.
- R9.11: Track embedding_tokens_total for cost accounting.
- R21.4: Prometheus metrics exposure for embedding_tokens_total.
- R5.10: Validate embedding vector dimension matches configured width (1536).
- specs/design.md §5.6 & §6.1: Knowledge embedding data model and contracts.
- CLAUDE.md §8: External systems faked in CI via FakeEmbedder.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest

from packages.core.settings import EmbeddingSettings
from packages.knowledge.embedder import (
    Embedder,
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingRateLimitError,
    EmbeddingTimeoutError,
    FakeEmbedder,
    HttpEmbedder,
    get_embedder,
)
from packages.observability.metrics import create_pipeline_metrics


def _make_openai_embedding_response(
    texts: list[str], dimension: int = 1536, prompt_tokens: int = 10
) -> dict[str, Any]:
    """Generate a valid OpenAI-compatible /embeddings API JSON payload."""
    data = []
    for idx, _ in enumerate(texts):
        # Deterministic dummy vector with correct dimension
        vec = [0.1] * dimension
        data.append({"object": "embedding", "index": idx, "embedding": vec})

    return {
        "object": "list",
        "data": data,
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


class TestEmbedderProtocol:
    """Verify Embedder interface contracts."""

    def test_protocol_conformance(self) -> None:
        http_embedder = HttpEmbedder()
        fake_embedder = FakeEmbedder()
        assert isinstance(http_embedder, Embedder)
        assert isinstance(fake_embedder, Embedder)
        assert http_embedder.dimension == 1536
        assert fake_embedder.dimension == 1536
        assert http_embedder.model_name == "text-embedding-3-small"
        assert fake_embedder.model_name == "text-embedding-3-small"


class TestHttpEmbedder:
    """Verify HttpEmbedder with mock transports for batching, retries, and errors."""

    @pytest.mark.asyncio
    async def test_single_batch_success(self) -> None:
        texts = ["First chunk content", "Second chunk content"]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/embeddings")
            body = json.loads(request.content)
            assert body["input"] == texts
            assert body["model"] == "text-embedding-3-small"
            assert body["dimensions"] == 1536
            return httpx.Response(
                status_code=200,
                json=_make_openai_embedding_response(texts, dimension=1536, prompt_tokens=25),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, dimension=1536)

        result = await embedder.embed_texts(texts)
        assert len(result.embeddings) == 2
        assert len(result.embeddings[0]) == 1536
        assert len(result.embeddings[1]) == 1536
        assert result.model == "text-embedding-3-small"
        assert result.dimension == 1536
        assert result.token_count == 25
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_auto_micro_batching(self) -> None:
        """When input exceeds batch_size, embedder splits into sequential batches."""
        total_texts = [f"Chunk number {i}" for i in range(150)]
        batch_size = 64
        received_batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            batch = body["input"]
            received_batch_sizes.append(len(batch))
            payload = _make_openai_embedding_response(
                batch, dimension=1536, prompt_tokens=len(batch)
            )
            return httpx.Response(status_code=200, json=payload)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, batch_size=batch_size)

        result = await embedder.embed_texts(total_texts)
        assert received_batch_sizes == [64, 64, 22]
        assert len(result.embeddings) == 150
        assert result.token_count == 150

    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client)

        result = await embedder.embed_texts([])
        assert call_count == 0
        assert result.embeddings == []
        assert result.token_count == 0

    @pytest.mark.asyncio
    async def test_embed_query_convenience(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["input"] == ["query string"]
            return httpx.Response(
                status_code=200,
                json=_make_openai_embedding_response(["query string"], dimension=1536),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client)

        query_vec = await embedder.embed_query("query string")
        assert len(query_vec) == 1536

    @pytest.mark.asyncio
    async def test_retry_on_429_rate_limit(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    status_code=429,
                    headers={"Retry-After": "0.01"},
                    json={"error": {"message": "Rate limit exceeded"}},
                )
            return httpx.Response(
                status_code=200,
                json=_make_openai_embedding_response(["test"], dimension=1536, prompt_tokens=5),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, max_retries=2, retry_delay_s=0.01)

        result = await embedder.embed_texts(["test"])
        assert attempts == 2
        assert len(result.embeddings) == 1

    @pytest.mark.asyncio
    async def test_retry_on_503_server_error(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(status_code=503, text="Service Unavailable")
            return httpx.Response(
                status_code=200,
                json=_make_openai_embedding_response(["recovered"], dimension=1536),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, max_retries=3, retry_delay_s=0.01)

        result = await embedder.embed_texts(["recovered"])
        assert attempts == 3
        assert len(result.embeddings) == 1

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_rate_limit(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=429, json={"error": "Too Many Requests"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, max_retries=2, retry_delay_s=0.01)

        with pytest.raises(EmbeddingRateLimitError, match="Embedding rate limit exceeded"):
            await embedder.embed_texts(["text"])

    @pytest.mark.asyncio
    async def test_fail_fast_on_client_error(self) -> None:
        """4xx errors (except 429) fail fast without retrying."""
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status_code=401, text="Unauthorized API key")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, max_retries=3)

        with pytest.raises(EmbeddingError, match="HTTP 401"):
            await embedder.embed_texts(["text"])
        assert calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [422, 503])
    async def test_provider_error_body_never_reaches_the_error_message(
        self, status_code: int
    ) -> None:
        """CLAUDE.md: a provider echoing its input must not carry email text into errors."""
        text = "Customer Jane Roe asks about the card ending 4242"

        def handler(request: httpx.Request) -> httpx.Response:
            sent = json.loads(request.content)["input"]
            return httpx.Response(status_code=status_code, json={"detail": [{"input": sent}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, max_retries=0, retry_delay_s=0.0)

        with pytest.raises(EmbeddingError) as excinfo:
            await embedder.embed_texts([text])
        assert "Jane Roe" not in str(excinfo.value)
        assert str(status_code) in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Connection timed out")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, max_retries=1, retry_delay_s=0.01)

        with pytest.raises(EmbeddingTimeoutError, match="timed out"):
            await embedder.embed_texts(["text"])

    @pytest.mark.asyncio
    async def test_dimension_mismatch_error(self) -> None:
        """R5.10: If returned vector dimension != configured dimension, fail fast."""

        def handler(_request: httpx.Request) -> httpx.Response:
            # Return 768 dimensions when 1536 configured
            return httpx.Response(
                status_code=200,
                json=_make_openai_embedding_response(["text"], dimension=768),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, dimension=1536)
        with pytest.raises(
            EmbeddingDimensionMismatchError, match="Returned embedding dimension 768"
        ):
            await embedder.embed_texts(["text"])

    @pytest.mark.asyncio
    async def test_prometheus_token_metrics(self) -> None:
        """R9.11 & R21.4: Verify embedding_tokens_total counter increments."""
        metrics = create_pipeline_metrics()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_make_openai_embedding_response(["chunk"], dimension=1536, prompt_tokens=42),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        embedder = HttpEmbedder(client=client, metrics=metrics)

        before = metrics.embedding_tokens_total.labels(model="text-embedding-3-small")._value.get()
        await embedder.embed_texts(["chunk"])
        after = metrics.embedding_tokens_total.labels(model="text-embedding-3-small")._value.get()

        assert after == before + 42


class TestFakeEmbedder:
    """Verify deterministic FakeEmbedder for CI and testing."""

    @pytest.mark.asyncio
    async def test_deterministic_vectors(self) -> None:
        fake = FakeEmbedder(dimension=1536)
        text_a = "Account recovery procedure for locked users"
        text_b = "Billing invoices and subscription fees"

        res_a1 = await fake.embed_texts([text_a])
        res_a2 = await fake.embed_texts([text_a])
        res_b = await fake.embed_texts([text_b])

        # Deterministic: Identical text -> identical vector
        assert res_a1.embeddings[0] == res_a2.embeddings[0]
        # Distinct: Different text -> different vector
        assert res_a1.embeddings[0] != res_b.embeddings[0]

        # Correct dimension
        assert len(res_a1.embeddings[0]) == 1536
        # Unit normalized (norm ≈ 1.0)
        norm = math.sqrt(sum(x * x for x in res_a1.embeddings[0]))
        assert abs(norm - 1.0) < 1e-5

    @pytest.mark.asyncio
    async def test_fake_embedder_query(self) -> None:
        fake = FakeEmbedder(dimension=1536)
        vec = await fake.embed_query("search query")
        assert len(vec) == 1536
        assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-5

    @pytest.mark.asyncio
    async def test_fake_embedder_error_injection(self) -> None:
        fake = FakeEmbedder()
        fake.set_error(RuntimeError("Forced failure"))

        with pytest.raises(RuntimeError, match="Forced failure"):
            await fake.embed_texts(["test"])

        # Clearing error restores normal behavior
        fake.set_error(None)
        res = await fake.embed_texts(["test"])
        assert len(res.embeddings) == 1

    @pytest.mark.asyncio
    async def test_fake_embedder_call_recording(self) -> None:
        fake = FakeEmbedder()
        await fake.embed_texts(["alpha", "beta"])
        assert len(fake.recorded_calls) == 1
        assert fake.recorded_calls[0]["texts"] == ["alpha", "beta"]

        fake.clear_recorded_calls()
        assert len(fake.recorded_calls) == 0


class TestGetEmbedderFactory:
    """Verify factory behavior for mock and live configurations."""

    def test_factory_returns_fake_in_mock_mode(self) -> None:
        settings = EmbeddingSettings(mock=True)
        embedder = get_embedder(settings)
        assert isinstance(embedder, FakeEmbedder)
        assert embedder.dimension == 1536

    def test_factory_returns_http_in_live_mode(self) -> None:
        settings = EmbeddingSettings(
            mock=False, api_key="sk-test", base_url="https://api.openai.com/v1"
        )
        embedder = get_embedder(settings)
        assert isinstance(embedder, HttpEmbedder)
        assert embedder.dimension == 1536
        assert embedder.model_name == "text-embedding-3-small"

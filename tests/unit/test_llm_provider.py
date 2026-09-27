"""Unit tests for packages/llm abstractions and providers (R14.5, design.md §5.7).

Verifies protocol conformance, FakeLLMProvider mock facilities, and HttpLLMProvider
OpenAI-compatible request/response handling using httpx.MockTransport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.llm.client import (
    DEFAULT_LOCAL_MODELS,
    DEFAULT_MODEL_MAP,
    DEFAULT_OPENAI_MODELS,
    HttpLLMProvider,
    LocalLLMProvider,
    OpenAILLMProvider,
)
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)


class TestLLMProtocolAndFakeProvider:
    """Validate protocol compliance and mock facilities of FakeLLMProvider."""

    def test_protocol_conformance(self) -> None:
        """Assert FakeLLMProvider and HttpLLMProvider conform to LLMProvider."""
        fake = FakeLLMProvider()
        http_provider = HttpLLMProvider()
        assert isinstance(fake, LLMProvider)
        assert isinstance(http_provider, LLMProvider)

    @pytest.mark.asyncio
    async def test_fake_provider_default_response(self) -> None:
        """Verify default response generation and call recording."""
        fake = FakeLLMProvider()
        messages = [ChatMessage(role="user", content="Hello test")]

        result = await fake.generate(messages=messages, tier=ModelTier.FAST)

        assert isinstance(result, LLMResult)
        assert result.content["category"] == "support"
        assert result.tier == ModelTier.FAST
        assert result.model == "fake-fast-model"
        assert result.latency_ms >= 1
        assert len(fake.recorded_calls) == 1
        assert fake.recorded_calls[0]["messages"] == messages

    @pytest.mark.asyncio
    async def test_fake_provider_canned_queue(self) -> None:
        """Verify FIFO queue of canned responses."""
        fake = FakeLLMProvider()
        fake.queue_response({"category": "billing", "intent": "invoice"})
        fake.queue_response({"category": "sales", "intent": "demo"})

        r1 = await fake.generate(messages=[ChatMessage(role="user", content="msg 1")])
        r2 = await fake.generate(messages=[ChatMessage(role="user", content="msg 2")])
        r3 = await fake.generate(messages=[ChatMessage(role="user", content="msg 3")])

        assert r1.content["category"] == "billing"
        assert r2.content["category"] == "sales"
        assert r3.content["category"] == "support"  # falls back to default

    @pytest.mark.asyncio
    async def test_fake_provider_dynamic_responder(self) -> None:
        """Verify dynamic response generation callback."""

        def responder(
            messages: list[ChatMessage],
            schema: dict[str, Any] | None,
            tier: ModelTier,
        ) -> dict[str, Any]:
            prompt = messages[-1].content
            return {"category": "sales" if "pricing" in prompt else "support"}

        fake = FakeLLMProvider(responder=responder)
        r1 = await fake.generate(messages=[ChatMessage(role="user", content="tell me pricing")])
        r2 = await fake.generate(messages=[ChatMessage(role="user", content="system down")])

        assert r1.content["category"] == "sales"
        assert r2.content["category"] == "support"

    @pytest.mark.asyncio
    async def test_fake_provider_error_injection(self) -> None:
        """Verify simulated provider exceptions."""
        fake = FakeLLMProvider()
        fake.set_error(LLMTimeoutError("Simulation timed out"))

        with pytest.raises(LLMTimeoutError, match="Simulation timed out"):
            await fake.generate(messages=[ChatMessage(role="user", content="test")])

        fake.set_error(None)
        res = await fake.generate(messages=[ChatMessage(role="user", content="test")])
        assert res.content["category"] == "support"

    @pytest.mark.asyncio
    async def test_fake_provider_tier_model_names(self) -> None:
        """Verify tier resolution in FakeLLMProvider."""
        fake = FakeLLMProvider()
        fast_res = await fake.generate(
            messages=[ChatMessage(role="user", content="a")],
            tier=ModelTier.ROUTINE,
        )
        strong_res = await fake.generate(
            messages=[ChatMessage(role="user", content="b")],
            tier=ModelTier.STRONG,
        )
        assert fast_res.model == "fake-fast-model"
        assert strong_res.model == "fake-strong-model"

    @pytest.mark.asyncio
    async def test_fake_provider_extra_params(self) -> None:
        """Verify arbitrary params (**params per R14.5) are accepted and recorded."""
        fake = FakeLLMProvider()
        result = await fake.generate(
            messages=[ChatMessage(role="user", content="hello")],
            tier=ModelTier.FAST,
            seed=42,
            custom_option="custom_val",
        )
        assert result is not None
        assert len(fake.recorded_calls) == 1
        assert fake.recorded_calls[0]["params"] == {"seed": 42, "custom_option": "custom_val"}


class TestHttpLLMProvider:
    """Validate HttpLLMProvider behavior against mock HTTP transports."""

    def test_tier_resolution(self) -> None:
        """Verify tier-to-model mapping resolution."""
        provider = HttpLLMProvider()
        assert provider.resolve_model(ModelTier.FAST) == DEFAULT_MODEL_MAP[ModelTier.FAST]
        assert provider.resolve_model(ModelTier.ROUTINE) == DEFAULT_MODEL_MAP[ModelTier.ROUTINE]
        assert provider.resolve_model(ModelTier.STRONG) == DEFAULT_MODEL_MAP[ModelTier.STRONG]
        assert (
            provider.resolve_model(ModelTier.HIGH_CAPABILITY)
            == DEFAULT_MODEL_MAP[ModelTier.HIGH_CAPABILITY]
        )
        assert provider.resolve_model(ModelTier.FALLBACK) == DEFAULT_MODEL_MAP[ModelTier.FALLBACK]

        custom = HttpLLMProvider(model_map={ModelTier.FAST: "custom-small"})
        assert custom.resolve_model(ModelTier.FAST) == "custom-small"

    @pytest.mark.asyncio
    async def test_successful_structured_generation(self) -> None:
        """Verify mock chat completion request and JSON parsing."""
        expected_json = {
            "category": "billing",
            "intent": "refund_request",
            "priority": "high",
            "reply_required": True,
            "workflow_hint": "ai",
            "retrieval_required": True,
            "confidence": 0.98,
        }

        def mock_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            body = json.loads(request.content.decode("utf-8"))
            assert body["model"] == "gpt-4o-mini"
            assert "response_format" in body
            assert request.headers["Authorization"] == "Bearer mock-key"

            payload = {
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(expected_json),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 45,
                    "total_tokens": 165,
                },
            }
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        provider = HttpLLMProvider(
            base_url="https://api.openai.com/v1",
            api_key="mock-key",
            client=client,
        )

        result = await provider.generate(
            messages=[ChatMessage(role="user", content="Please refund my bill")],
            schema={"type": "object"},
            tier=ModelTier.FAST,
        )

        assert result.content == expected_json
        assert result.model == "gpt-4o-mini"
        assert result.input_tokens == 120
        assert result.output_tokens == 45
        assert result.latency_ms >= 0
        assert result.raw_finish_reason == "stop"

        await provider.aclose()

    @pytest.mark.asyncio
    async def test_http_timeout_error_handling(self) -> None:
        """Verify httpx.TimeoutException maps to LLMTimeoutError."""

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Mock socket read timed out")

        transport = httpx.MockTransport(timeout_handler)
        client = httpx.AsyncClient(transport=transport)
        provider = HttpLLMProvider(client=client, timeout_s=0.5)

        with pytest.raises(LLMTimeoutError, match="LLM request timed out"):
            await provider.generate(messages=[ChatMessage(role="user", content="Hi")])

        await provider.aclose()

    @pytest.mark.asyncio
    async def test_http_status_error_handling(self) -> None:
        """Verify 4xx/5xx responses map to LLMResponseError."""

        def error_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})

        transport = httpx.MockTransport(error_handler)
        client = httpx.AsyncClient(transport=transport)
        provider = HttpLLMProvider(client=client)

        with pytest.raises(LLMResponseError, match="status 429"):
            await provider.generate(messages=[ChatMessage(role="user", content="Hi")])

        await provider.aclose()

    @pytest.mark.asyncio
    async def test_malformed_json_response_handling(self) -> None:
        """Verify unparseable JSON when schema is required maps to LLMSchemaValidationError."""

        def malformed_handler(request: httpx.Request) -> httpx.Response:
            payload = {
                "choices": [
                    {
                        "message": {"content": "I am not valid JSON {"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(malformed_handler)
        client = httpx.AsyncClient(transport=transport)
        provider = HttpLLMProvider(client=client)

        with pytest.raises(LLMSchemaValidationError, match="Failed to parse structured JSON"):
            await provider.generate(
                messages=[ChatMessage(role="user", content="Hi")],
                schema={"type": "object"},
            )

        await provider.aclose()

    @pytest.mark.asyncio
    async def test_truncated_json_carries_finish_reason(self) -> None:
        """A length stop cuts the JSON off; the parse error must say why (4.13a, I3)."""

        def truncated_handler(request: httpx.Request) -> httpx.Response:
            payload = {
                "choices": [{"message": {"content": '{"action": "re'}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1000},
            }
            return httpx.Response(200, json=payload)

        provider = HttpLLMProvider(
            client=httpx.AsyncClient(transport=httpx.MockTransport(truncated_handler))
        )

        with pytest.raises(LLMSchemaValidationError) as excinfo:
            await provider.generate(
                messages=[ChatMessage(role="user", content="Hi")], schema={"type": "object"}
            )

        assert excinfo.value.finish_reason == "length"
        await provider.aclose()


class TestOpenAIAndLocalProviders:
    """Validate OpenAILLMProvider and LocalLLMProvider specializations (R14.7)."""

    def test_protocol_conformance(self) -> None:
        openai_p = OpenAILLMProvider(api_key="mock-key")
        local_p = LocalLLMProvider()
        assert isinstance(openai_p, LLMProvider)
        assert isinstance(local_p, LLMProvider)

    def test_openai_tier_resolution(self) -> None:
        openai_p = OpenAILLMProvider(api_key="mock-key")
        assert openai_p.resolve_model(ModelTier.FAST) == DEFAULT_OPENAI_MODELS[ModelTier.FAST]
        assert openai_p.resolve_model(ModelTier.ROUTINE) == DEFAULT_OPENAI_MODELS[ModelTier.ROUTINE]
        assert openai_p.resolve_model(ModelTier.STRONG) == DEFAULT_OPENAI_MODELS[ModelTier.STRONG]
        assert (
            openai_p.resolve_model(ModelTier.HIGH_CAPABILITY)
            == DEFAULT_OPENAI_MODELS[ModelTier.HIGH_CAPABILITY]
        )

    def test_local_tier_resolution(self) -> None:
        local_p = LocalLLMProvider()
        assert local_p.resolve_model(ModelTier.FAST) == DEFAULT_LOCAL_MODELS[ModelTier.FAST]
        assert local_p.resolve_model(ModelTier.ROUTINE) == DEFAULT_LOCAL_MODELS[ModelTier.ROUTINE]
        assert local_p.resolve_model(ModelTier.STRONG) == DEFAULT_LOCAL_MODELS[ModelTier.STRONG]
        assert (
            local_p.resolve_model(ModelTier.HIGH_CAPABILITY)
            == DEFAULT_LOCAL_MODELS[ModelTier.HIGH_CAPABILITY]
        )

    @pytest.mark.asyncio
    async def test_openai_provider_generation(self) -> None:
        def mock_handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-sk-key"
            payload = {
                "choices": [
                    {
                        "message": {"content": json.dumps({"status": "ok"})},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 8},
            }
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)
        provider = OpenAILLMProvider(api_key="test-sk-key", client=client)

        result = await provider.generate(
            messages=[ChatMessage(role="user", content="Ping")],
            schema={"type": "object"},
            tier=ModelTier.FAST,
        )

        assert result.content == {"status": "ok"}
        assert result.model == DEFAULT_OPENAI_MODELS[ModelTier.FAST]
        assert result.input_tokens == 15
        assert result.output_tokens == 8

        await provider.aclose()

    @pytest.mark.asyncio
    async def test_local_provider_generation(self) -> None:
        def mock_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "localhost"
            assert request.url.port == 11434
            payload = {
                "choices": [
                    {
                        "message": {"content": json.dumps({"action": "summarize"})},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            }
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)
        provider = LocalLLMProvider(client=client)

        result = await provider.generate(
            messages=[ChatMessage(role="user", content="Local prompt")],
            schema={"type": "object"},
            tier=ModelTier.FAST,
        )

        assert result.content == {"action": "summarize"}
        assert result.model == DEFAULT_LOCAL_MODELS[ModelTier.FAST]

        await provider.aclose()


@pytest.mark.asyncio
async def test_refusal_with_null_content_raises_schema_error_not_type_error() -> None:
    """A `content: null` response must raise LLMSchemaValidationError, never TypeError.

    OpenAI-compatible endpoints send the content key with a JSON null for a refusal, a
    content-filter block, or a tool-call-only message. `.get("content", "")` returns None
    for that shape, so json.loads(None) raises and the error handler then subscripts None.
    A TypeError is not an LLMError, so it escapes every retry classification the pipeline
    has and bypasses the schema-repair path entirely.
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        )

    provider = HttpLLMProvider(
        base_url="https://api.openai.com/v1",
        api_key="mock-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(LLMSchemaValidationError) as exc_info:
        await provider.generate(
            messages=[ChatMessage(role="user", content="hi")],
            schema={"type": "object"},
            tier=ModelTier.FAST,
        )

    # The repair path needs raw_content to be a string it can show back to the model.
    assert exc_info.value.raw_content == ""


@pytest.mark.asyncio
async def test_missing_content_key_also_yields_string_raw_content() -> None:
    """A message with no content key at all must behave the same way."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"role": "assistant"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    provider = HttpLLMProvider(
        base_url="https://api.openai.com/v1",
        api_key="mock-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(LLMSchemaValidationError) as exc_info:
        await provider.generate(
            messages=[ChatMessage(role="user", content="hi")],
            schema={"type": "object"},
            tier=ModelTier.FAST,
        )
    assert exc_info.value.raw_content == ""

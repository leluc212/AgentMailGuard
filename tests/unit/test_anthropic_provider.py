"""Unit tests for AnthropicLLMProvider (R14.5, R14.7, design.md §5.7).

Verifies:
- Protocol conformance to LLMProvider.
- Model tier resolution for Claude models.
- System prompt extraction and message formatting for Claude Messages API.
- Tool use structured JSON schema parsing.
- Token usage accounting and latency recording.
- Error mappings (timeout, 4xx/5xx API errors, schema errors).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.llm.anthropic import DEFAULT_ANTHROPIC_MODELS, AnthropicLLMProvider
from packages.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)


def test_anthropic_provider_conforms_to_protocol() -> None:
    provider = AnthropicLLMProvider(api_key="mock-key")
    assert isinstance(provider, LLMProvider)


def test_anthropic_model_resolution() -> None:
    provider = AnthropicLLMProvider(api_key="mock-key")
    assert provider.resolve_model(ModelTier.FAST) == DEFAULT_ANTHROPIC_MODELS[ModelTier.FAST]
    assert provider.resolve_model(ModelTier.ROUTINE) == DEFAULT_ANTHROPIC_MODELS[ModelTier.ROUTINE]
    assert provider.resolve_model(ModelTier.STRONG) == DEFAULT_ANTHROPIC_MODELS[ModelTier.STRONG]
    assert (
        provider.resolve_model(ModelTier.HIGH_CAPABILITY)
        == DEFAULT_ANTHROPIC_MODELS[ModelTier.HIGH_CAPABILITY]
    )


@pytest.mark.asyncio
async def test_anthropic_successful_structured_generation() -> None:
    """Verify Messages API payload with tools and structured output."""
    captured_request: dict[str, Any] = {}

    expected_json = {
        "action": "reply",
        "draft": "Dear customer, your request is being processed.",
        "confidence": 0.96,
        "knowledge_chunks": ["DOC-1"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-anthropic-key"
        assert request.headers["anthropic-version"] == "2023-06-01"

        captured_request = json.loads(request.content.decode("utf-8"))
        assert captured_request["model"] == DEFAULT_ANTHROPIC_MODELS[ModelTier.FAST]
        assert captured_request["system"] == "You are a helpful assistant."
        assert len(captured_request["messages"]) == 1
        assert captured_request["messages"][0]["role"] == "user"
        assert captured_request["messages"][0]["content"] == "Hello"

        # Mock Claude tool_use response
        response_body = {
            "id": "msg_01X9DPzWvC9x9v",
            "type": "message",
            "role": "assistant",
            "model": DEFAULT_ANTHROPIC_MODELS[ModelTier.FAST],
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "structured_response",
                    "input": expected_json,
                }
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 150,
                "output_tokens": 60,
            },
        }
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)
    provider = AnthropicLLMProvider(api_key="test-anthropic-key", client=client)

    result = await provider.generate(
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello"),
        ],
        schema={"type": "object"},
        tier=ModelTier.FAST,
    )

    assert isinstance(result, LLMResult)
    assert result.content == expected_json
    assert result.model == DEFAULT_ANTHROPIC_MODELS[ModelTier.FAST]
    assert result.tier == ModelTier.FAST
    assert result.input_tokens == 150
    assert result.output_tokens == 60
    assert result.latency_ms >= 0
    assert result.raw_finish_reason == "tool_use"

    await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_timeout_error_handling() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Anthropic connection timed out")

    transport = httpx.MockTransport(timeout_handler)
    client = httpx.AsyncClient(transport=transport)
    provider = AnthropicLLMProvider(api_key="key", client=client, timeout_s=1.0)

    with pytest.raises(LLMTimeoutError, match="timed out"):
        await provider.generate(messages=[ChatMessage(role="user", content="Hi")])

    await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_http_error_handling() -> None:
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "Invalid key"},
            },
        )

    transport = httpx.MockTransport(error_handler)
    client = httpx.AsyncClient(transport=transport)
    provider = AnthropicLLMProvider(api_key="bad-key", client=client)

    with pytest.raises(LLMResponseError, match="status 401"):
        await provider.generate(messages=[ChatMessage(role="user", content="Hi")])

    await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_malformed_tool_output() -> None:
    def malformed_handler(request: httpx.Request) -> httpx.Response:
        response_body = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude",
            "content": [{"type": "text", "text": "I could not generate valid structured output."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(malformed_handler)
    client = httpx.AsyncClient(transport=transport)
    provider = AnthropicLLMProvider(api_key="key", client=client)

    with pytest.raises(LLMSchemaValidationError, match="Failed to extract structured tool_use"):
        await provider.generate(
            messages=[ChatMessage(role="user", content="Hi")],
            schema={"type": "object"},
        )

    await provider.aclose()

"""Contract test runners for all LLMProvider implementations (R14.5, R14.7, design.md §1083).

Validates:
- FakeLLMProvider (offline CI stub, R24.5)
- OpenAILLMProvider (hosted OpenAI API)
- AnthropicLLMProvider (hosted Claude Messages API)
- LocalLLMProvider (OpenAI-compatible local server, e.g. Ollama/vLLM)
All providers must pass identical contract assertions using hermetic mock transports.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from packages.llm.anthropic import DEFAULT_ANTHROPIC_MODELS, AnthropicLLMProvider
from packages.llm.client import (
    DEFAULT_LOCAL_MODELS,
    DEFAULT_OPENAI_MODELS,
    LocalLLMProvider,
    OpenAILLMProvider,
)
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import (
    LLMProvider,
    LLMResponseError,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)
from packages.llm.testing import LLMProviderContractSuite


class TestFakeLLMProviderContract(LLMProviderContractSuite):
    """Proves FakeLLMProvider passes canonical LLMProvider contract suite."""

    def create_provider(self) -> LLMProvider:
        return FakeLLMProvider(
            default_response={"category": "billing", "confidence": 0.95},
        )

    def create_failing_provider(self, error_kind: str) -> LLMProvider:
        fake = FakeLLMProvider()
        if error_kind == "timeout":
            fake.set_error(LLMTimeoutError("Simulation timeout"))
        elif error_kind == "http_error":
            fake.set_error(LLMResponseError("Simulation HTTP 500 error"))
        elif error_kind == "malformed_json":
            fake.set_error(LLMSchemaValidationError("Simulation schema validation error"))
        else:
            raise ValueError(f"Unknown error kind: {error_kind}")
        return fake


class TestOpenAILLMProviderContract(LLMProviderContractSuite):
    """Proves OpenAILLMProvider passes canonical LLMProvider contract suite."""

    def create_provider(self) -> LLMProvider:
        def normal_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            model = body.get("model", DEFAULT_OPENAI_MODELS[ModelTier.ROUTINE])
            content_str = json.dumps({"category": "support", "confidence": 0.9})
            response_json = {
                "choices": [
                    {
                        "message": {"content": content_str},
                        "finish_reason": "stop",
                    }
                ],
                "model": model,
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
            return httpx.Response(200, json=response_json)

        transport = httpx.MockTransport(normal_handler)
        client = httpx.AsyncClient(transport=transport)
        return OpenAILLMProvider(api_key="mock-key", client=client)

    def create_failing_provider(self, error_kind: str) -> LLMProvider:
        if error_kind == "timeout":
            def timeout_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.TimeoutException("OpenAI timeout")
            transport = httpx.MockTransport(timeout_handler)
        elif error_kind == "http_error":
            def error_handler(request: httpx.Request) -> httpx.Response:
                err = {"error": {"message": "OpenAI Internal Server Error"}}
                return httpx.Response(500, json=err)
            transport = httpx.MockTransport(error_handler)
        elif error_kind == "malformed_json":
            def malformed_handler(request: httpx.Request) -> httpx.Response:
                choice = {"message": {"content": "{not valid json"}, "finish_reason": "stop"}
                return httpx.Response(
                    200,
                    json={
                        "choices": [choice],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                    },
                )
            transport = httpx.MockTransport(malformed_handler)
        else:
            raise ValueError(f"Unknown error kind: {error_kind}")

        client = httpx.AsyncClient(transport=transport)
        return OpenAILLMProvider(api_key="mock-key", client=client)


class TestAnthropicLLMProviderContract(LLMProviderContractSuite):
    """Proves AnthropicLLMProvider passes canonical LLMProvider contract suite."""

    def create_provider(self) -> LLMProvider:
        def normal_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            model = body.get("model", DEFAULT_ANTHROPIC_MODELS[ModelTier.ROUTINE])
            response_json: dict[str, Any] = {
                "id": "msg_contract_01",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "structured_response",
                        "input": {"category": "inquiry", "confidence": 0.88},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 80, "output_tokens": 30},
            }
            return httpx.Response(200, json=response_json)

        transport = httpx.MockTransport(normal_handler)
        client = httpx.AsyncClient(transport=transport)
        return AnthropicLLMProvider(api_key="mock-anthropic-key", client=client)

    def create_failing_provider(self, error_kind: str) -> LLMProvider:
        if error_kind == "timeout":
            def timeout_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.TimeoutException("Anthropic timeout")
            transport = httpx.MockTransport(timeout_handler)
        elif error_kind == "http_error":
            def error_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(529, json={"error": {"message": "Anthropic Overloaded"}})
            transport = httpx.MockTransport(error_handler)
        elif error_kind == "malformed_json":
            def malformed_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_01",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude",
                        "content": [{"type": "text", "text": "Unparseable plain text"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                    },
                )
            transport = httpx.MockTransport(malformed_handler)
        else:
            raise ValueError(f"Unknown error kind: {error_kind}")

        client = httpx.AsyncClient(transport=transport)
        return AnthropicLLMProvider(api_key="mock-key", client=client)


class TestLocalLLMProviderContract(LLMProviderContractSuite):
    """Proves LocalLLMProvider passes canonical LLMProvider contract suite."""

    def create_provider(self) -> LLMProvider:
        def normal_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            model = body.get("model", DEFAULT_LOCAL_MODELS[ModelTier.ROUTINE])
            content_str = json.dumps({"category": "local", "confidence": 0.85})
            response_json = {
                "choices": [
                    {
                        "message": {"content": content_str},
                        "finish_reason": "stop",
                    }
                ],
                "model": model,
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            }
            return httpx.Response(200, json=response_json)

        transport = httpx.MockTransport(normal_handler)
        client = httpx.AsyncClient(transport=transport)
        return LocalLLMProvider(client=client)

    def create_failing_provider(self, error_kind: str) -> LLMProvider:
        if error_kind == "timeout":
            def timeout_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.TimeoutException("Local endpoint timeout")
            transport = httpx.MockTransport(timeout_handler)
        elif error_kind == "http_error":
            def error_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(502, text="Bad Gateway to Local LLM")
            transport = httpx.MockTransport(error_handler)
        elif error_kind == "malformed_json":
            def malformed_handler(request: httpx.Request) -> httpx.Response:
                choice = {"message": {"content": "garbage json"}, "finish_reason": "stop"}
                return httpx.Response(
                    200,
                    json={
                        "choices": [choice],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                    },
                )
            transport = httpx.MockTransport(malformed_handler)
        else:
            raise ValueError(f"Unknown error kind: {error_kind}")

        client = httpx.AsyncClient(transport=transport)
        return LocalLLMProvider(client=client)

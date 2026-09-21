"""Deterministic, fixture-driven FakeLLMProvider for offline CI and testing (GEMINI.md §8).

Provides configurable stub responses, dynamic schema-driven generation, failure
injection, latency simulation, and call recording without live external network calls.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from packages.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResult,
    ModelTier,
)

ResponderFunc = Callable[[list[ChatMessage], dict[str, Any] | None, ModelTier], dict[str, Any]]


class FakeLLMProvider(LLMProvider):
    """Deterministic, mockable LLMProvider implementation for testing."""

    def __init__(
        self,
        default_response: dict[str, Any] | None = None,
        canned_responses: list[dict[str, Any]] | None = None,
        responder: ResponderFunc | None = None,
        simulate_latency_ms: int = 1,
        error_to_raise: Exception | None = None,
    ) -> None:
        self._default_response = default_response or {
            "category": "support",
            "intent": "general_help",
            "priority": "normal",
            "reply_required": True,
            "workflow_hint": "ai",
            "retrieval_required": True,
            "confidence": 0.95,
        }
        self._canned_responses = list(canned_responses) if canned_responses else []
        self._responder = responder
        self._simulate_latency_ms = simulate_latency_ms
        self._error_to_raise = error_to_raise
        self.recorded_calls: list[dict[str, Any]] = []

    def queue_response(self, response: dict[str, Any]) -> None:
        """Enqueue a canned response to be returned by future generate() calls."""
        self._canned_responses.append(response)

    def set_error(self, error: Exception | None) -> None:
        """Inject an exception to be raised on subsequent generate() calls."""
        self._error_to_raise = error

    def clear_recorded_calls(self) -> None:
        """Clear recorded invocation history."""
        self.recorded_calls.clear()

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMResult:
        """Simulate a structured completion generation call."""
        start_time = time.perf_counter()

        # Record call arguments for test verification
        call_record = {
            "messages": messages,
            "schema": schema,
            "tier": tier,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timestamp": time.time(),
        }
        self.recorded_calls.append(call_record)

        # Simulate latency if requested
        if self._simulate_latency_ms > 0:
            await asyncio.sleep(self._simulate_latency_ms / 1000.0)

        # Inject configured failure if set
        if self._error_to_raise is not None:
            raise self._error_to_raise

        # Determine response content
        if self._responder is not None:
            content = self._responder(messages, schema, tier)
        elif self._canned_responses:
            content = self._canned_responses.pop(0)
        else:
            content = dict(self._default_response)

        # Approximate token usage
        input_chars = sum(len(m.content) for m in messages)
        input_tokens = max(1, input_chars // 4)
        output_tokens = max(1, len(str(content)) // 4)

        elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
        if tier in (ModelTier.FAST, ModelTier.ROUTINE):
            model_name = "fake-fast-model"
        else:
            model_name = "fake-strong-model"

        return LLMResult(
            content=content,
            model=model_name,
            tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            raw_finish_reason="stop",
            raw_response={"mock": True},
        )

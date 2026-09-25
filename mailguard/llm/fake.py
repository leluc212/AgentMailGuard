"""Deterministic offline provider for tests and CI (zero credentials, zero network)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from mailguard.llm.protocol import ChatMessage, LLMProvider, LLMResult, ModelTier

Responder = Callable[[list[ChatMessage], dict[str, Any] | None], dict[str, Any]]


class FakeLLMProvider(LLMProvider):
    """Returns canned / rule-driven JSON. Records every call for assertions."""

    def __init__(
        self,
        default_response: dict[str, Any] | None = None,
        responder: Responder | None = None,
        model_name: str = "fake-guard-model",
        error_to_raise: Exception | None = None,
    ) -> None:
        self._default = default_response or {}
        self._responder = responder
        self._queue: list[dict[str, Any]] = []
        self._error = error_to_raise
        self.model_name = model_name
        self.calls: list[dict[str, Any]] = []

    def queue(self, *responses: dict[str, Any]) -> None:
        self._queue.extend(responses)

    def set_error(self, error: Exception | None) -> None:
        self._error = error

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMResult:
        start = time.perf_counter()
        self.calls.append({"messages": messages, "schema": schema, "tier": tier})
        if self._error is not None:
            raise self._error
        if self._queue:
            content = self._queue.pop(0)
        elif self._responder is not None:
            content = self._responder(messages, schema)
        else:
            content = dict(self._default)
        in_tokens = max(1, sum(len(m.content) for m in messages) // 4)
        return LLMResult(
            content=content,
            model=self.model_name,
            tier=tier,
            input_tokens=in_tokens,
            output_tokens=max(1, len(str(content)) // 4),
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            raw_response={"fake": True},
        )


def keyword_responder(rules: dict[str, dict[str, Any]], default: dict[str, Any]) -> Responder:
    """Responder returning ``rules[kw]`` when ``kw`` occurs in the last user message."""

    def _respond(messages: list[ChatMessage], _schema: dict[str, Any] | None) -> dict[str, Any]:
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        lowered = user_text.lower()
        for kw, resp in rules.items():
            if kw.lower() in lowered:
                return dict(resp)
        return dict(default)

    return _respond

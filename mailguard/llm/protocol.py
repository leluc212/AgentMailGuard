"""Provider-neutral LLM contract (mirrors the rag-email core so instances interoperate)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ModelTier(StrEnum):
    FAST = "fast"
    ROUTINE = "routine"
    STRONG = "strong"
    HIGH_CAPABILITY = "high_capability"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMResult:
    content: dict[str, Any]
    model: str
    tier: ModelTier = ModelTier.FAST
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_finish_reason: str = "stop"
    raw_response: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Best-effort plain text view of the response."""
        if "raw_text" in self.content:
            return str(self.content["raw_text"])
        return str(self.content)


@runtime_checkable
class LLMProvider(Protocol):
    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMResult: ...


class LLMError(Exception):
    """Base class for provider failures."""


class LLMTimeoutError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMSchemaValidationError(LLMError):
    pass

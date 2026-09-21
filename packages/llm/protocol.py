"""Core protocol and data contracts for LLM providers (R14.5, design.md §5.7).

All model interactions across the system MUST go through the LLMProvider protocol.
Direct third-party SDK imports (e.g. openai, anthropic) are prohibited outside
packages/llm per GEMINI.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ModelTier(StrEnum):
    """Tiered model categories for complexity-based routing (R15, design.md §5.7)."""

    FAST = "fast"  # Tier 1 fast model (e.g. gpt-4o-mini) for triage and summarization
    ROUTINE = "routine"  # Alias for fast/routine tier
    STRONG = "strong"  # Tier 2 strong model (e.g. gpt-4o) for high-confidence draft generation
    HIGH_CAPABILITY = "high_capability"  # Alias for strong tier
    FALLBACK = "fallback"  # Tier 3 fallback model (e.g. claude-3-haiku)


@dataclass(frozen=True)
class ChatMessage:
    """Canonical chat message payload for conversational LLM prompting."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResult:
    """Execution output from an LLMProvider invocation (design.md §5.7)."""

    content: dict[str, Any]  # Schema-validated JSON payload
    model: str  # Concrete model identifier used (e.g. "gpt-4o-mini")
    tier: ModelTier
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_finish_reason: str = "stop"
    raw_response: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Abstract provider interface for all language model calls (R14.5)."""

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMResult:
        """Execute a structured completion request against the selected model tier."""
        ...


class LLMError(Exception):
    """Base exception for all LLM provider failures."""


class LLMTimeoutError(LLMError):
    """Raised when an LLM provider request exceeds its allotted timeout."""


class LLMResponseError(LLMError):
    """Raised when an LLM provider returns an HTTP or API error response."""


class LLMSchemaValidationError(LLMError):
    """Raised when an LLM provider returns output violating the expected schema."""

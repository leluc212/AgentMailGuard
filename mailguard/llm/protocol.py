"""Provider-neutral LLM contract (mirrors the rag-email core so instances interoperate)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class CallProvenance:
    """Which endpoint served one call, as a router (OpenRouter) reports it.

    Present on a result only when the provider was asked for the router's metadata. ``None``
    fields mean the response did not say (a missing ``openrouter_metadata``), which a caller that
    pinned a provider must treat as unverified, never as a match.
    """

    requested_model: str
    served_provider: str | None = None
    attempt: int | None = None  # 1-indexed; above 1 means the router fell back to another endpoint
    summary: str | None = None
    generation_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    finish_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    provenance: CallProvenance | None = None

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
    """The model answered, but not with the requested JSON object.

    ``reason`` says how it failed (see ``mailguard.llm.structured.fallback_reason``):
    ``non_json`` (prose, no JSON object), ``schema_missing`` (JSON without the schema's
    required fields) or ``invalid_fields`` (fields present but null, mistyped or out of range).
    """

    def __init__(self, message: str = "", *, reason: str = "invalid_fields") -> None:
        super().__init__(message)
        self.reason = reason

"""Core protocol and data contracts for LLM providers (R14.5, design.md §5.7).

All model interactions across the system MUST go through the LLMProvider protocol.
Direct third-party SDK imports (e.g. openai, anthropic) are prohibited outside
packages/llm per CLAUDE.md §4.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class CallProvenance:
    """Which endpoint served one model call, as a router (OpenRouter) reports it.

    Present only when the provider asked the router for its metadata. A ``None`` field means
    the response did not say; a caller that pinned a provider must treat that as unverified,
    never as a match (``packages/llm/provenance.py``).
    """

    requested_model: str
    served_provider: str | None = None
    attempt: int | None = None  # 1-indexed; above 1 means the router fell back to another endpoint
    summary: str | None = None
    generation_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None  # USD, as the router billed the call
    finish_reason: str | None = None
    # the response field that named the provider (openrouter_metadata.endpoints | .summary, or
    # response.provider, the body's top-level field, when the metadata names none)
    provider_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, as a result row and the run meta store it."""
        return asdict(self)


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
    provenance: CallProvenance | None = None  # set when the router's metadata was asked for


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
        **params: Any,
    ) -> LLMResult:
        """Execute a structured completion request against the selected model tier (R14.5).

        A ``model`` in ``params`` names the concrete model in place of the tier's, and the
        result's ``model`` is then that name: tokens and cost are recorded under it (R21.4).
        """
        ...


class LLMError(Exception):
    """Base exception for all LLM provider failures."""


class LLMTimeoutError(LLMError):
    """Raised when an LLM provider request exceeds its allotted timeout."""


class LLMResponseError(LLMError):
    """Raised when an LLM provider returns an HTTP or API error response.

    Carries what a caller may branch on without parsing the message: the HTTP status, the
    router's ``error.metadata.limit_source`` (OpenRouter's 402s: ``openrouter_in_flight_budget``
    is transient, ``openrouter_credits`` and ``openrouter_key_limit`` are not) and the
    ``Retry-After`` seconds. All ``None`` for a transport-level failure.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        limit_source: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.limit_source = limit_source
        self.retry_after_s = retry_after_s


class LLMProviderMismatchError(LLMResponseError):
    """A call was served by another provider than the pinned one, or after a fallback attempt.

    An error of the call, never a result: a benchmark case it hits is an error row, not a
    defence. The message starts with ``provider_mismatch`` (the runners look for that marker).
    """

    def __init__(
        self,
        message: str,
        *,
        expected: tuple[str, ...],
        served: str | None,
        provenance: CallProvenance,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.served = served
        self.provenance = provenance


class LLMSchemaValidationError(LLMError):
    """Raised when an LLM provider returns output violating the expected schema.

    Carries the offending raw text when the provider has it, so a schema repair retry can
    show the model what it actually emitted instead of guessing (R16.3).
    """

    def __init__(
        self,
        message: str,
        raw_content: str | None = None,
        *,
        finish_reason: str | None = None,
        provenance: CallProvenance | None = None,
    ) -> None:
        super().__init__(message)
        self.provenance = provenance  # the call that produced the text, when the router said
        self.raw_content = raw_content
        # The provider's stop reason when known; a length stop means truncation at max_tokens.
        self.finish_reason = finish_reason

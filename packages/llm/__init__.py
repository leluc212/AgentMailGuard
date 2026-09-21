"""LLM abstractions, providers, and structured schemas (R14.5, design.md §5.7)."""

from packages.llm.client import HttpLLMProvider
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)

__all__ = [
    "ChatMessage",
    "FakeLLMProvider",
    "HttpLLMProvider",
    "LLMError",
    "LLMProvider",
    "LLMResponseError",
    "LLMResult",
    "LLMSchemaValidationError",
    "LLMTimeoutError",
    "ModelTier",
]

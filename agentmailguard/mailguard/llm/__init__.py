"""LLM access for the guard layers.

The ``LLMProvider`` protocol is signature-compatible with the rag-email core
(``packages.llm.protocol.LLMProvider``) so a core provider instance can be passed
straight into any guard stage, and ``GuardedLLMProvider`` (L3) can wrap it.
"""

from mailguard.llm.fake import FakeLLMProvider, keyword_responder
from mailguard.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)
from mailguard.llm.registry import ModelRegistry, build_provider
from mailguard.llm.structured import call_structured, compact_schema

__all__ = [
    "ChatMessage",
    "FakeLLMProvider",
    "LLMError",
    "LLMProvider",
    "LLMResponseError",
    "LLMResult",
    "LLMSchemaValidationError",
    "LLMTimeoutError",
    "ModelRegistry",
    "ModelTier",
    "build_provider",
    "call_structured",
    "compact_schema",
    "keyword_responder",
]

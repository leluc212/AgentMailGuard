"""LLM abstractions, providers, and structured schemas (R14.5, design.md §5.7)."""

from packages.llm.anthropic import (
    DEFAULT_ANTHROPIC_MODELS,
    AnthropicLLMProvider,
)
from packages.llm.client import (
    DEFAULT_LOCAL_MODELS,
    DEFAULT_MODEL_MAP,
    DEFAULT_OPENAI_MODELS,
    HttpLLMProvider,
    LocalLLMProvider,
    OpenAILLMProvider,
)
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
from packages.llm.testing import LLMProviderContractSuite

__all__ = [
    "AnthropicLLMProvider",
    "ChatMessage",
    "DEFAULT_ANTHROPIC_MODELS",
    "DEFAULT_LOCAL_MODELS",
    "DEFAULT_MODEL_MAP",
    "DEFAULT_OPENAI_MODELS",
    "FakeLLMProvider",
    "HttpLLMProvider",
    "LLMError",
    "LLMProvider",
    "LLMProviderContractSuite",
    "LLMResponseError",
    "LLMResult",
    "LLMSchemaValidationError",
    "LLMTimeoutError",
    "LocalLLMProvider",
    "ModelTier",
    "OpenAILLMProvider",
]

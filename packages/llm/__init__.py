"""LLM abstractions, providers, and structured schemas (R14.5, design.md §5.7)."""

from packages.llm.anthropic import (
    DEFAULT_ANTHROPIC_MODELS,
    AnthropicLLMProvider,
)
from packages.llm.budget import (
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallBudgetViolationError,
    CallKind,
    CallRecord,
)
from packages.llm.client import (
    DEFAULT_LOCAL_MODELS,
    DEFAULT_MODEL_MAP,
    DEFAULT_OPENAI_MODELS,
    HttpLLMProvider,
    LocalLLMProvider,
    OpenAILLMProvider,
)
from packages.llm.factory import create_llm_provider
from packages.llm.fake import FakeLLMProvider
from packages.llm.generator import (
    GenerationResult,
    SinglePassGenerator,
)
from packages.llm.profile import (
    AgentProfile,
    AgentProfileRegistry,
    ContextPolicy,
)
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
from packages.llm.router import (
    EscalationReason,
    RoutingDecision,
    count_requested_actions,
)
from packages.llm.testing import LLMProviderContractSuite

__all__ = [
    "AgentProfile",
    "AgentProfileRegistry",
    "AnthropicLLMProvider",
    "BudgetedLLMProvider",
    "CallBudgetExceededError",
    "CallBudgetTracker",
    "CallBudgetViolationError",
    "CallKind",
    "CallRecord",
    "ChatMessage",
    "ContextPolicy",
    "DEFAULT_ANTHROPIC_MODELS",
    "DEFAULT_LOCAL_MODELS",
    "DEFAULT_MODEL_MAP",
    "DEFAULT_OPENAI_MODELS",
    "EscalationReason",
    "FakeLLMProvider",
    "GenerationResult",
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
    "RoutingDecision",
    "SinglePassGenerator",
    "count_requested_actions",
    "create_llm_provider",
]

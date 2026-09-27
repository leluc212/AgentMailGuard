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
from packages.llm.citations import (
    CitationVerdict,
    build_citation_index,
    verify_citations,
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
    ComplexityRouter,
    EscalationReason,
    RoutingDecision,
    count_requested_actions,
)
from packages.llm.validation import (
    DraftReplyPayload,
    DraftSchemaContractError,
    DraftValidationError,
    UnvalidatedDraftError,
    assert_schema_matches_contract,
    build_repair_messages,
    validate_draft_payload,
)

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
    "CitationVerdict",
    "ChatMessage",
    "ComplexityRouter",
    "ContextPolicy",
    "DEFAULT_ANTHROPIC_MODELS",
    "DEFAULT_LOCAL_MODELS",
    "DEFAULT_MODEL_MAP",
    "DEFAULT_OPENAI_MODELS",
    "DraftReplyPayload",
    "DraftSchemaContractError",
    "DraftValidationError",
    "EscalationReason",
    "FakeLLMProvider",
    "GenerationResult",
    "HttpLLMProvider",
    "LLMError",
    "LLMProvider",
    "LLMResponseError",
    "LLMResult",
    "LLMSchemaValidationError",
    "LLMTimeoutError",
    "LocalLLMProvider",
    "ModelTier",
    "OpenAILLMProvider",
    "RoutingDecision",
    "SinglePassGenerator",
    "UnvalidatedDraftError",
    "assert_schema_matches_contract",
    "build_citation_index",
    "build_repair_messages",
    "count_requested_actions",
    "create_llm_provider",
    "validate_draft_payload",
    "verify_citations",
]

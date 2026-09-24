"""Agent profile definitions, context policies, and profile registry (R14.1, R14.2, R14.6).

Specialization by configuration rather than chained multi-agent pipelines (R14.4, design.md §5.7).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from packages.llm.protocol import ModelTier


class ContextPolicy(StrEnum):
    """Context assembly policies for generation (R14.1, design.md §5.7)."""

    THREAD_PLUS_RAG = "thread_plus_rag"
    THREAD_PLUS_RAG_PLUS_BUSINESS = "thread_plus_rag_plus_business"
    THREAD_ONLY = "thread_only"
    RAG_ONLY = "rag_only"


class AgentProfile(BaseModel):
    """Agent profile specifying domain specialization and prompt contracts (R14.1, R14.6).

    Attributes:
        profile: Unique identifier for the profile (e.g. 'technical_support', 'billing').
        knowledge_domain: Domain identifier for targeted knowledge retrieval (e.g. 'support').
        response_style: Target tone/style (e.g. 'professional', 'precise_formal', 'concise').
        model_tier: Required model capability tier (routine vs high_capability).
        context_policy: Context assembly policy (e.g. thread_plus_rag).
        prompt_template: Relative path or identifier of versioned Jinja2 prompt template.
        output_schema: Relative path or JSON schema dictionary for structured generation.
        prompt_version: Version identifier recorded on generated drafts (R14.6).
        categories: List of classification categories mapped to this profile.
        agent_instructions: Optional general instructions for the profile.
        category_instructions: Optional specific category instructions for prompt-prefix caching.
        description: Human-readable description of the profile's role.
    """

    profile: str
    knowledge_domain: str
    response_style: str
    model_tier: ModelTier = ModelTier.ROUTINE
    context_policy: ContextPolicy | str = ContextPolicy.THREAD_PLUS_RAG
    prompt_template: str
    output_schema: str | dict[str, Any]
    prompt_version: str
    categories: list[str] = Field(default_factory=list)
    agent_instructions: str | None = None
    category_instructions: str | None = None
    description: str | None = None

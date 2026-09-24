"""Context Builder orchestrator for prompt assembly and RAG gating (R14.8, R6.6, R18.1).

Orchestrates:
1. Resolving static agent and category instructions (cacheable prefix).
2. Gathering thread conversation context via ThreadContextAssembler (Task 4.3).
3. Conditionally invoking hybrid RAG only when retrieval_required=True (R6.6).
4. Fetching business data (stubbed until Phase 5, R13).
5. Emitting ContextPackage in fixed assembly order (R14.8, design.md §5.4).
6. Transitioning processing job state from QUEUED to CONTEXT_READY (R18.1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from packages.domain.entities import NormalizedMessage

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@runtime_checkable
class BusinessDataProvider(Protocol):
    """Protocol for fetching transactional business facts (R13)."""

    async def get_business_data(
        self,
        organization_id: UUID | str,
        message: NormalizedMessage,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Fetch customer or transactional business data for prompt injection."""
        ...


class StubBusinessDataProvider:
    """Stub business data provider returning empty or default business facts until Phase 5."""

    async def get_business_data(
        self,
        organization_id: UUID | str,
        message: NormalizedMessage,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Return empty business data dictionary in Phase 4 stub."""
        return {}


@runtime_checkable
class InstructionProvider(Protocol):
    """Protocol for providing agent and category prompt instructions (R14.1, R14.8)."""

    def get_instructions(self, category: str) -> tuple[str, str]:
        """Return (agent_instructions, category_instructions)."""
        ...


class DefaultInstructionProvider:
    """Default static instruction provider returning cacheable prompt instructions."""

    DEFAULT_AGENT_INSTRUCTIONS = (
        "You are an enterprise AI assistant for customer email correspondence. "
        "Provide professional, concise, and helpful responses grounded in the "
        "provided thread history and reference knowledge."
    )

    CATEGORY_INSTRUCTIONS: dict[str, str] = {
        "billing": (
            "Provide clear account and billing guidance, citing invoice details when applicable."
        ),
        "support": (
            "Address technical questions with structured diagnostic steps and procedural guidance."
        ),
        "sales": "Offer product capabilities, tier options, and next steps for procurement.",
        "general_inquiry": "Answer inquiries accurately and provide polite assistance.",
        "scheduling": "Coordinate dates, times, and calendar confirmations efficiently.",
        "administration": (
            "Process account changes following administrative verification protocols."
        ),
    }

    def get_instructions(self, category: str) -> tuple[str, str]:
        """Return static (agent_instructions, category_instructions) for prompt-prefix caching."""
        cat_key = category.lower().strip() if category else "general_inquiry"
        cat_instr = self.CATEGORY_INSTRUCTIONS.get(
            cat_key,
            f"Process {category} requests adhering to enterprise operational standards.",
        )
        return self.DEFAULT_AGENT_INSTRUCTIONS, cat_instr

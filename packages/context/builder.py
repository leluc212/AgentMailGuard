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

from packages.domain.entities import (
    Candidate as DomainCandidate,
)
from packages.domain.entities import (
    Classification,
    ContextPackage,
    Job,
    NormalizedMessage,
    ThreadState,
)
from packages.domain.state_machine import JobState
from packages.retrieval.query_builder import RetrievalQueryBuilder

if TYPE_CHECKING:
    from packages.context.assembly import ThreadContextAssembler
    from packages.db.job import JobStore
    from packages.retrieval.retriever import HybridRetriever

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


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


class ContextBuilder:
    """Orchestrates thread context, hybrid RAG, and business data into ContextPackage (R14.8).

    Enforces:
    - R6.6: Skip hybrid RAG when retrieval_required == False.
    - R14.8: Strict fixed assembly order: agent_instructions, category_instructions,
      thread_summary, recent_messages, current_email, retrieved_knowledge, business_data.
    - R18.1: Transition Job state QUEUED -> CONTEXT_READY.
    """

    def __init__(
        self,
        thread_assembler: ThreadContextAssembler,
        retriever: HybridRetriever | None = None,
        query_builder: RetrievalQueryBuilder | None = None,
        business_data_provider: BusinessDataProvider | None = None,
        instruction_provider: InstructionProvider | None = None,
        job_store: JobStore | None = None,
        top_k: int = 5,
    ) -> None:
        self.thread_assembler = thread_assembler
        self.retriever = retriever
        self.query_builder = query_builder or RetrievalQueryBuilder()
        self.business_data_provider = business_data_provider or StubBusinessDataProvider()
        self.instruction_provider = instruction_provider or DefaultInstructionProvider()
        self.job_store = job_store
        self.top_k = top_k

    async def build_context(
        self,
        job: Job,
        message: NormalizedMessage,
        classification: Classification | None = None,
        thread_messages: list[NormalizedMessage] | None = None,
        thread_state: ThreadState | None = None,
    ) -> ContextPackage:
        """Gather context from all subsystems and emit ordered ContextPackage."""
        org_id = _to_uuid(job.organization_id)
        thread_id = _to_uuid(job.thread_id or message.thread_id)

        # 1. Resolve static agent and category instructions (cacheable prefix)
        category = classification.category if classification else "general_inquiry"
        agent_instr, cat_instr = self.instruction_provider.get_instructions(category)

        # 2. Assemble thread context (Task 4.3, R8.5)
        thread_ctx = await self.thread_assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=message,
            thread_messages=thread_messages,
            thread_state=thread_state,
        )

        # 3. Conditional Hybrid RAG (R6.6)
        retrieval_required = (
            classification.retrieval_required
            if classification is not None
            else True
        )

        retrieved_chunks: list[DomainCandidate] = []
        if retrieval_required and self.retriever is not None:
            query = self.query_builder.build(
                message=message,
                classification=classification,
                thread_summary=thread_ctx.summary,
            )
            retrieval_result = await self.retriever.retrieve(query)
            top_candidates = retrieval_result.candidates[: self.top_k]
            for c in top_candidates:
                ext_id = (
                    getattr(c, "external_id", None)
                    or c.metadata.get("external_id")
                    or c.chunk_id
                )
                retrieved_chunks.append(
                    DomainCandidate(
                        chunk_id=c.chunk_id,
                        document_id=c.document_id,
                        content=c.content,
                        metadata=dict(c.metadata),
                        external_id=ext_id,
                        lexical_rank=c.lexical_rank,
                        vector_rank=c.vector_rank,
                        lexical_score=c.lexical_score,
                        vector_score=c.vector_score,
                        fused_score=c.fused_score,
                        rerank_score=c.rerank_score,
                    )
                )

        # 4. Transactional Business Data (stubbed until Phase 5, R13)
        intent = classification.intent if classification else None
        business_data = await self.business_data_provider.get_business_data(
            organization_id=org_id,
            message=message,
            intent=intent,
        )

        # 5. Emit ContextPackage with fixed assembly order (R14.8)
        pkg = ContextPackage(
            agent_instructions=agent_instr,
            category_instructions=cat_instr,
            current_message=message,
            thread_summary=thread_ctx.summary,
            recent_messages=thread_ctx.recent_messages,
            retrieved_chunks=retrieved_chunks,
            business_data=business_data,
        )

        # 6. State Machine transition: QUEUED -> CONTEXT_READY (R18.1)
        if self.job_store is not None and job.state == JobState.QUEUED:
            updated_job, _ = await self.job_store.transition_job_state(
                organization_id=org_id,
                job_id=_to_uuid(job.id),
                target_state=JobState.CONTEXT_READY,
                payload={
                    "retrieval_performed": retrieval_required,
                    "retrieved_chunks_count": len(retrieved_chunks),
                    "thread_has_summary": thread_ctx.has_summary,
                    "tokens_saved": thread_ctx.tokens_saved,
                },
            )
            job.state = updated_job.state

        return pkg

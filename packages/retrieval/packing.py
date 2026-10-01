"""Context packing, Top-K selection, and hard token budget enforcement.

Requirements:
- R11.3: Pass configurable top-K to generation, defaulting to 4–6 chunks (default 5).
- R11.4: Enforce a maximum retrieved-context token budget and truncate at chunk boundaries.
- R11.7: Record final context token count.
- specs/design.md §5.4 & §5.5: Retrieved knowledge packed into context package with citations.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from packages.retrieval.models import Candidate

logger = logging.getLogger(__name__)


@runtime_checkable
class TokenCounterProtocol(Protocol):
    """Protocol for token counting abstractions."""

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in the given text."""
        ...


@dataclass(frozen=True)
class PackedChunk:
    """An individual knowledge chunk packed for LLM context generation.

    Carries chunk identification, content, token count, and citation ID (R11.3, R11.4, R16.5).
    """

    chunk_id: str
    document_id: str
    content: str
    token_count: int
    citation_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    fused_score: float | None = None
    rerank_score: float | None = None


@dataclass
class PackedContext:
    """The packed retrieval context ready for prompt injection and generation.

    Fulfills R11.3, R11.4, and R11.7.
    """

    chunks: list[PackedChunk]
    total_tokens: int
    token_budget: int
    top_k: int
    truncated_by_budget: bool
    candidate_count_initial: int
    candidate_count_selected: int

    @property
    def citation_ids(self) -> list[str]:
        """List of all citation IDs present in the packed context."""
        return [c.citation_id for c in self.chunks]

    @property
    def chunk_ids(self) -> list[str]:
        """List of all chunk IDs present in the packed context."""
        return [c.chunk_id for c in self.chunks]

    def format_knowledge_section(self) -> str:
        """Format retrieved knowledge section for prompt injection (design.md §5.4)."""
        if not self.chunks:
            return ""
        blocks: list[str] = []
        for chunk in self.chunks:
            blocks.append(f"{chunk.citation_id} (Document: {chunk.document_id})\n{chunk.content}")
        return "\n\n".join(blocks)


@dataclass
class PackingConfig:
    """Configuration for context packing and token budget enforcement.

    Defaults align with R11.3 (4–6 chunks, midpoint 5) and default token budget (2048 tokens).
    """

    default_top_k: int = 5
    default_token_budget: int = 2048
    min_top_k: int = 1
    max_top_k: int = 20
    count_formatting_tokens: bool = False
    org_top_k: dict[str, int] = field(default_factory=dict)
    org_token_budget: dict[str, int] = field(default_factory=dict)
    category_top_k: dict[str, int] = field(default_factory=dict)
    category_token_budget: dict[str, int] = field(default_factory=dict)


class ContextPacker:
    """Packs ranked candidate chunks into a bounded LLM context package.

    Enforces:
    1. Configurable Top-K (R11.3, default 5).
    2. Hard token budget ceiling (R11.4).
    3. Strict truncation at chunk boundaries — never mid-chunk (R11.4).
    4. Deterministic citation IDs (e.g., [1], [2], ...) for verified citations (R16.5).
    """

    def __init__(
        self,
        token_counter: TokenCounterProtocol | None = None,
        config: PackingConfig | None = None,
    ) -> None:
        self.config = config or PackingConfig()
        if token_counter is not None:
            self.token_counter: TokenCounterProtocol = token_counter
        else:
            from packages.knowledge.token_counter import TokenCounter

            self.token_counter = TokenCounter()

    def resolve_top_k(
        self,
        top_k: int | None = None,
        organization_id: str | None = None,
        category: str | None = None,
    ) -> int:
        """Determine effective top-K based on parameters and configuration policy."""
        if top_k is not None:
            return max(self.config.min_top_k, min(self.config.max_top_k, top_k))
        if organization_id and organization_id in self.config.org_top_k:
            return self.config.org_top_k[organization_id]
        if category and category in self.config.category_top_k:
            return self.config.category_top_k[category]
        return self.config.default_top_k

    def resolve_token_budget(
        self,
        token_budget: int | None = None,
        organization_id: str | None = None,
        category: str | None = None,
    ) -> int:
        """Determine effective token budget based on parameters and configuration policy."""
        if token_budget is not None:
            return max(0, token_budget)
        if organization_id and organization_id in self.config.org_token_budget:
            return self.config.org_token_budget[organization_id]
        if category and category in self.config.category_token_budget:
            return self.config.category_token_budget[category]
        return self.config.default_token_budget

    def pack(
        self,
        candidates: Sequence[Candidate],
        *,
        top_k: int | None = None,
        token_budget: int | None = None,
        organization_id: str | None = None,
        category: str | None = None,
        citation_prefix: str = "",
    ) -> PackedContext:
        """Pack candidates into a bounded PackedContext.

        Args:
            candidates: Sequence of ranked candidates (from RRF or reranker).
            top_k: Optional top-K override.
            token_budget: Optional token budget override.
            organization_id: Optional organization ID for policy overrides.
            category: Optional classification category for policy overrides.
            citation_prefix: Optional prefix for citation IDs (e.g. 'chunk-' for [chunk-1]).

        Returns:
            PackedContext containing only whole chunks that fit within the token budget.
        """
        effective_top_k = self.resolve_top_k(
            top_k=top_k, organization_id=organization_id, category=category
        )
        effective_budget = self.resolve_token_budget(
            token_budget=token_budget,
            organization_id=organization_id,
            category=category,
        )

        initial_count = len(candidates)
        if not candidates or effective_budget <= 0:
            return PackedContext(
                chunks=[],
                total_tokens=0,
                token_budget=effective_budget,
                top_k=effective_top_k,
                truncated_by_budget=initial_count > 0 and effective_budget <= 0,
                candidate_count_initial=initial_count,
                candidate_count_selected=0,
            )

        candidate_slice = list(candidates)[:effective_top_k]
        packed_chunks: list[PackedChunk] = []
        accumulated_tokens = 0
        truncated_by_budget = False

        for idx, candidate in enumerate(candidate_slice, start=1):
            citation_id = f"[{citation_prefix}{idx}]"

            # Check if token count is already cached in candidate metadata
            cached_tokens = candidate.metadata.get("token_count")
            if (
                isinstance(cached_tokens, int)
                and cached_tokens >= 0
                and not self.config.count_formatting_tokens
            ):
                chunk_tokens = cached_tokens
            else:
                text_to_measure = (
                    f"{citation_id} (Document: {candidate.document_id})\n{candidate.content}"
                    if self.config.count_formatting_tokens
                    else candidate.content
                )
                chunk_tokens = self.token_counter.count_tokens(text_to_measure)

            # R11.4: Strict chunk boundary enforcement. Never mid-chunk truncate!
            if accumulated_tokens + chunk_tokens > effective_budget:
                logger.info(
                    "Context packing token budget reached (%d + %d > %d). "
                    "Truncating at chunk boundary at rank %d.",
                    accumulated_tokens,
                    chunk_tokens,
                    effective_budget,
                    idx,
                )
                truncated_by_budget = True
                break

            accumulated_tokens += chunk_tokens
            packed_chunks.append(
                PackedChunk(
                    chunk_id=candidate.chunk_id,
                    document_id=candidate.document_id,
                    content=candidate.content,
                    token_count=chunk_tokens,
                    citation_id=citation_id,
                    metadata=dict(candidate.metadata),
                    fused_score=candidate.fused_score,
                    rerank_score=candidate.rerank_score,
                )
            )

        return PackedContext(
            chunks=packed_chunks,
            total_tokens=accumulated_tokens,
            token_budget=effective_budget,
            top_k=effective_top_k,
            truncated_by_budget=truncated_by_budget,
            candidate_count_initial=initial_count,
            candidate_count_selected=len(packed_chunks),
        )

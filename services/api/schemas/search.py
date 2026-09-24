"""Pydantic schemas for the retrieval debug endpoint (R23.3).

Exposes full diagnostic transparency into query construction, multi-branch candidate
retrieval, RRF score fusion, semantic reranking, and final chunk selection per specs/design.md §5.5.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class RetrievalDebugRequest(BaseModel):
    """Request payload for the retrieval debug endpoint (R23.3).

    Allows debugging either structured inbound email content (subject, body, intent)
    or direct search query strings with optional entity overrides.
    """

    query: str | None = Field(
        default=None,
        description="Direct search query string or user question.",
    )
    subject: str | None = Field(
        default=None,
        description="Email subject line for intent/context extraction.",
    )
    body_text: str | None = Field(
        default=None,
        description="Clean inbound email body text for entity and keyword extraction.",
    )
    intent: str | None = Field(
        default=None,
        description="Classified email intent (e.g. billing_inquiry, support_request).",
    )
    category: str | None = Field(
        default=None,
        description="Target document category filter (e.g. billing, procedures, support).",
    )
    thread_summary: str | None = Field(
        default=None,
        description="Prior thread history summary to enrich semantic retrieval.",
    )
    identifiers: list[str] = Field(
        default_factory=list,
        description="Explicit entity identifiers to inject verbatim (e.g. ['INV-2026-01829']).",
    )
    lexical_terms: list[str] = Field(
        default_factory=list,
        description="Explicit keywords to augment lexical search.",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata filters applied inside search branches.",
    )
    query_vector: list[float] | None = Field(
        default=None,
        description="Optional dense embedding vector override (skips embedder call if provided).",
    )
    top_n: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of candidate chunks requested per branch (R10.2).",
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of final chunks selected for prompt packing (R11.3).",
    )
    apply_rerank: bool = Field(
        default=True,
        description="Whether to execute semantic cross-encoder reranking (R11.1, R11.2).",
    )
    lexical_weight: float = Field(
        default=1.0,
        ge=0.0,
        description="RRF fusion weight for lexical branch.",
    )
    vector_weight: float = Field(
        default=1.0,
        ge=0.0,
        description="RRF fusion weight for vector branch.",
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        description="RRF smoothing constant k, default 60 (R10.3).",
    )


class ConstructedQueryDebug(BaseModel):
    """Detailed view of the synthesized retrieval query (R23.3, R12.1–R12.6)."""

    semantic_text: str = Field(
        description="Semantic text representation used for dense vector embedding.",
    )
    lexical_text: str = Field(
        description="Linearized lexical search text with verbatim identifiers.",
    )
    lexical_terms: list[str] = Field(
        default_factory=list,
        description="Extracted or supplied keywords.",
    )
    identifiers: list[str] = Field(
        default_factory=list,
        description="Extracted or supplied entity codes (R12.3).",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Scoped tenant and document metadata filters (R10.4).",
    )
    query_vector_present: bool = Field(
        description="Whether a dense embedding vector was supplied or generated.",
    )
    query_vector_dimension: int | None = Field(
        default=None,
        description="Dimensionality of dense vector if present (e.g. 1536).",
    )


class CandidateDebugItem(BaseModel):
    """Representation of a retrieved chunk candidate across pipeline stages (R10.8, R23.3)."""

    chunk_id: str = Field(description="Unique knowledge chunk identifier.")
    document_id: str = Field(description="Parent knowledge document identifier.")
    content: str = Field(description="Knowledge chunk text content.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Source document and chunk metadata.",
    )
    lexical_rank: int | None = Field(
        default=None,
        description="1-based ranking from lexical branch, or None if not returned.",
    )
    vector_rank: int | None = Field(
        default=None,
        description="1-based ranking from vector branch, or None if not returned.",
    )
    lexical_score: float | None = Field(
        default=None,
        description="Raw relevance score from full-text search.",
    )
    vector_score: float | None = Field(
        default=None,
        description="Cosine similarity score from vector search.",
    )
    fused_score: float | None = Field(
        default=None,
        description="Reciprocal Rank Fusion score (R10.3).",
    )
    rerank_score: float | None = Field(
        default=None,
        description="Semantic cross-encoder relevance score (R11.1).",
    )


class RetrievalExplanation(BaseModel):
    """Diagnostic metrics and ranking decisions for the retrieval run (R23.3)."""

    retrieval_degraded: bool = Field(
        default=False,
        description="Whether one of the search branches failed or timed out (R10.6).",
    )
    surviving_branch: str | None = Field(
        default=None,
        description="Branch used exclusively if retrieval degraded ('lexical' or 'vector').",
    )
    rerank_applied: bool = Field(
        default=False,
        description="Whether cross-encoder reranking was successfully applied.",
    )
    rerank_fallback_recorded: bool = Field(
        default=False,
        description="Whether reranking fell back to RRF order (R11.5).",
    )
    rerank_fallback_reason: str | None = Field(
        default=None,
        description="Failure or timeout reason if reranker fell back.",
    )
    lexical_count: int = Field(
        default=0,
        description="Count of candidates returned by lexical branch.",
    )
    vector_count: int = Field(
        default=0,
        description="Count of candidates returned by vector branch.",
    )
    fused_count: int = Field(
        default=0,
        description="Count of unique candidates produced after RRF fusion.",
    )
    selected_count: int = Field(
        default=0,
        description="Count of final candidates selected for context packing.",
    )
    lexical_latency_ms: float = Field(
        default=0.0,
        description="Execution duration of lexical search branch in milliseconds.",
    )
    vector_latency_ms: float = Field(
        default=0.0,
        description="Execution duration of vector search branch in milliseconds.",
    )
    retrieval_latency_ms: float = Field(
        default=0.0,
        description="Total duration of hybrid retrieval phase in milliseconds.",
    )
    rerank_latency_ms: float = Field(
        default=0.0,
        description="Duration of reranking phase in milliseconds.",
    )
    total_latency_ms: float = Field(
        default=0.0,
        description="End-to-end debug execution latency in milliseconds.",
    )


class RetrievalDebugResponse(BaseModel):
    """Complete diagnostic response explaining every ranking decision (R23.3)."""

    organization_id: UUID = Field(
        description="Scoping tenant organization UUID (R23.6).",
    )
    constructed_query: ConstructedQueryDebug = Field(
        description="Synthesized query parameters passed to the retrieval system.",
    )
    lexical_results: list[CandidateDebugItem] = Field(
        default_factory=list,
        description="Lexical branch candidate results with ranks and scores (R10.8).",
    )
    vector_results: list[CandidateDebugItem] = Field(
        default_factory=list,
        description="Vector branch candidate results with ranks and scores (R10.8).",
    )
    fused_results: list[CandidateDebugItem] = Field(
        default_factory=list,
        description="RRF fused candidates with combined scores (R10.3).",
    )
    rerank_results: list[CandidateDebugItem] = Field(
        default_factory=list,
        description="Candidates after semantic cross-encoder reranking (R11.1).",
    )
    selected_chunks: list[CandidateDebugItem] = Field(
        default_factory=list,
        description="Final selected candidate chunks passed to prompt packing (R11.3).",
    )
    explanation: RetrievalExplanation = Field(
        description="Diagnostic metadata and ranking decision breakdown.",
    )

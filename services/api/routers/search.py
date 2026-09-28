"""Retrieval debug endpoint implementation (R23.3, R23.1, R23.6, R10.8).

Provides POST /v1/search/debug returning constructed query, both branch result
lists with ranks, fused scores, rerank scores, and final selection per design.md §5.5.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from packages.retrieval.models import Candidate
from packages.retrieval.retriever import HybridRetriever
from services.api.dependencies import (
    EmbedderDep,
    QueryBuilderDep,
    RerankServiceDep,
    SearchBackendDep,
    get_organization_id,
)
from services.api.schemas.search import (
    CandidateDebugItem,
    ConstructedQueryDebug,
    RetrievalDebugRequest,
    RetrievalDebugResponse,
    RetrievalExplanation,
)

logger = logging.getLogger(__name__)

search_router = APIRouter(prefix="/search", tags=["search"])


def _to_candidate_item(c: Candidate) -> CandidateDebugItem:
    """Map domain Candidate entity to CandidateDebugItem response model."""
    return CandidateDebugItem(
        chunk_id=c.chunk_id,
        document_id=c.document_id,
        content=c.content,
        metadata=dict(c.metadata),
        lexical_rank=c.lexical_rank,
        vector_rank=c.vector_rank,
        lexical_score=c.lexical_score,
        vector_score=c.vector_score,
        fused_score=c.fused_score,
        rerank_score=c.rerank_score,
    )


@search_router.post(
    "/debug",
    response_model=RetrievalDebugResponse,
    summary="Retrieval Debug Endpoint",
    description=(
        "Executes end-to-end multi-branch retrieval, score fusion, and reranking "
        "returning complete diagnostic visibility into query construction, branch candidate "
        "ranks, fused scores, cross-encoder scores, and final selection per R23.3."
    ),
)
async def retrieval_debug(
    request_data: RetrievalDebugRequest,
    org_id: Annotated[UUID, Depends(get_organization_id)],
    search_backend: SearchBackendDep,
    embedder: EmbedderDep,
    rerank_service: RerankServiceDep,
    query_builder: QueryBuilderDep,
    request: Request,
) -> RetrievalDebugResponse:
    """Execute end-to-end retrieval and return complete ranking diagnostics (R23.3)."""
    total_start = time.perf_counter()
    org_str = str(org_id)

    # 1. Synthesize metadata filters scoped to authenticated tenant (R23.6, R10.4)
    filters = dict(request_data.filters)
    filters["organization_id"] = org_str
    if request_data.category:
        filters["category"] = request_data.category

    # 2. Construct RetrievalQuery using domain query builder (R12.1–R12.6)
    if (
        request_data.subject
        or request_data.body_text
        or request_data.intent
        or request_data.thread_summary
    ):
        query = query_builder.build(
            subject=request_data.subject or "",
            body_text=request_data.body_text or "",
            intent=request_data.intent,
            category=request_data.category,
            thread_summary=request_data.thread_summary,
            organization_id=org_str,
            extra_filters=filters,
        )
    else:
        query_text = request_data.query or ""
        query = query_builder.build(
            subject=query_text,
            body_text="",
            intent=None,
            category=request_data.category,
            thread_summary=None,
            organization_id=org_str,
            extra_filters=filters,
        )

    # Merge explicit identifier and lexical term overrides if supplied
    if request_data.identifiers:
        seen_ids = set(query.identifiers)
        for ident in request_data.identifiers:
            if ident not in seen_ids:
                query.identifiers.append(ident)
                seen_ids.add(ident)

    if request_data.lexical_terms:
        seen_terms = set(query.lexical_terms)
        for term in request_data.lexical_terms:
            if term not in seen_terms:
                query.lexical_terms.append(term)
                seen_terms.add(term)

    # 3. Dense query vector (R10.1): an explicit override wins; otherwise the retriever embeds
    #    semantic_text inside the vector branch, under the retrieval timeout (R10.9).
    if request_data.query_vector is not None:
        query.query_vector = list(request_data.query_vector)

    # 4. Concurrent Hybrid Retrieval (R10.5, R10.6, R10.8)
    metrics = getattr(request.app.state, "metrics", None)
    settings = request.app.state.settings
    retriever = HybridRetriever(
        backend=search_backend,
        rrf_k=request_data.rrf_k,
        metrics=metrics,
        raise_on_both_failed=False,
        embedder=embedder,
        timeout_seconds=settings.retrieval.retrieval_timeout_ms / 1000,
    )

    retrieval_result = await retriever.retrieve(
        query=query,
        limit=request_data.top_n,
        lexical_weight=request_data.lexical_weight,
        vector_weight=request_data.vector_weight,
    )

    # 5. Semantic Cross-Encoder Reranking (R11.1–R11.5)
    rerank_applied = False
    rerank_fallback_recorded = False
    rerank_fallback_reason: str | None = None
    rerank_results_list: list[Candidate] = []
    selected: list[Candidate] = []
    rerank_latency_ms = 0.0

    if request_data.apply_rerank and retrieval_result.candidates:
        rerank_res = await rerank_service.rerank(
            query=query.semantic_text,
            candidates=retrieval_result.candidates,
            organization_id=org_str,
            category=query.category,
            top_k=request_data.top_k,
        )
        rerank_applied = rerank_res.rerank_applied
        rerank_fallback_recorded = rerank_res.fallback_recorded
        rerank_fallback_reason = rerank_res.fallback_reason
        rerank_results_list = rerank_res.candidates
        selected = rerank_res.candidates[: request_data.top_k]
        rerank_latency_ms = rerank_res.latency_ms
    else:
        selected = retrieval_result.candidates[: request_data.top_k]

    total_latency_ms = (time.perf_counter() - total_start) * 1000.0

    # 6. Assemble complete debug response (R23.3)
    constructed_query_debug = ConstructedQueryDebug(
        semantic_text=query.semantic_text,
        lexical_text=query.lexical_text,
        lexical_terms=list(query.lexical_terms),
        identifiers=list(query.identifiers),
        filters=dict(query.filters),
        query_vector_present=retrieval_result.query_vector_dimension is not None,
        query_vector_dimension=retrieval_result.query_vector_dimension,
    )

    explanation = RetrievalExplanation(
        retrieval_degraded=retrieval_result.retrieval_degraded,
        surviving_branch=retrieval_result.surviving_branch,
        rerank_applied=rerank_applied,
        rerank_fallback_recorded=rerank_fallback_recorded,
        rerank_fallback_reason=rerank_fallback_reason,
        lexical_count=len(retrieval_result.lexical_candidates),
        vector_count=len(retrieval_result.vector_candidates),
        fused_count=len(retrieval_result.candidates),
        selected_count=len(selected),
        lexical_latency_ms=retrieval_result.lexical_latency_ms,
        vector_latency_ms=retrieval_result.vector_latency_ms,
        retrieval_latency_ms=retrieval_result.total_latency_ms,
        rerank_latency_ms=rerank_latency_ms,
        total_latency_ms=total_latency_ms,
    )

    return RetrievalDebugResponse(
        organization_id=org_id,
        constructed_query=constructed_query_debug,
        lexical_results=[_to_candidate_item(c) for c in retrieval_result.lexical_candidates],
        vector_results=[_to_candidate_item(c) for c in retrieval_result.vector_candidates],
        fused_results=[_to_candidate_item(c) for c in retrieval_result.candidates],
        rerank_results=[_to_candidate_item(c) for c in rerank_results_list],
        selected_chunks=[_to_candidate_item(c) for c in selected],
        explanation=explanation,
    )

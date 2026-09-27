"""In-memory conforming fake implementation of SearchBackend for tests and contract suite.

Requirements:
- R10.7: SearchBackend interface abstraction.
- R10.4: Pre-filtering by organization_id, category, and document status inside each branch.
- R10.8: Candidate returns lexical rank, vector rank, scores, and source metadata.
- R12.3: Exact identifier matching for invoice/order codes.
- specs/design.md §5.5: SearchBackend reference behavior.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from packages.retrieval.models import Candidate, RetrievalQuery


def _to_str(val: UUID | str | None) -> str:
    if val is None:
        return ""
    return str(val)


def _cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Calculate cosine similarity between two numeric vectors in [-1.0, 1.0]."""
    dot = 0.0
    norm1 = 0.0
    norm2 = 0.0
    for a, b in zip(v1, v2, strict=False):
        dot += a * b
        norm1 += a * a
        norm2 += b * b
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    sim = dot / (math.sqrt(norm1) * math.sqrt(norm2))
    return max(-1.0, min(1.0, sim))


@dataclass
class IndexedChunk:
    """Stored chunk record in FakeSearchBackend."""

    chunk_id: str
    document_id: str
    organization_id: str
    content: str
    category: str | None = None
    document_status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


class FakeSearchBackend:
    """In-memory conforming implementation of the SearchBackend protocol."""

    def __init__(self) -> None:
        self._chunks: dict[str, IndexedChunk] = {}
        # Fault injection hooks for degradation testing (R10.6, R10.9)
        self.simulate_lexical_error: Exception | None = None
        self.simulate_vector_error: Exception | None = None
        self.lexical_delay_s: float = 0.0
        self.vector_delay_s: float = 0.0

    def add_chunk(
        self,
        chunk_id: UUID | str,
        document_id: UUID | str,
        organization_id: UUID | str,
        content: str,
        category: str | None = None,
        document_status: str = "active",
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Register or update an indexed chunk in the in-memory corpus."""
        cid = _to_str(chunk_id)
        self._chunks[cid] = IndexedChunk(
            chunk_id=cid,
            document_id=_to_str(document_id),
            organization_id=_to_str(organization_id),
            content=content,
            category=category,
            document_status=document_status,
            metadata=dict(metadata or {}),
            embedding=list(embedding) if embedding is not None else None,
        )

    def clear(self) -> None:
        """Clear all indexed chunks and fault injection hooks."""
        self._chunks.clear()
        self.simulate_lexical_error = None
        self.simulate_vector_error = None
        self.lexical_delay_s = 0.0
        self.vector_delay_s = 0.0

    async def lexical(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        """Execute in-memory lexical keyword search."""
        if self.lexical_delay_s > 0:
            await asyncio.sleep(self.lexical_delay_s)
        if self.simulate_lexical_error is not None:
            raise self.simulate_lexical_error

        org_id = q.organization_id
        if not org_id:
            return []

        target_status = q.status
        target_category = q.category
        top_limit = max(1, top_n)

        # Tokenize search inputs
        query_text = q.lexical_text.lower()
        search_terms = set(re.findall(r"\w+", query_text))
        identifiers = [ident.strip() for ident in q.identifiers if ident.strip()]

        candidates: list[tuple[float, IndexedChunk]] = []

        for chunk in self._chunks.values():
            # Mandatory tenant isolation (R5.3, R10.4)
            if chunk.organization_id != org_id:
                continue

            # Status filter
            if target_status and chunk.document_status != target_status:
                continue

            # Category filter
            if target_category and chunk.category != target_category:
                continue

            content_lower = chunk.content.lower()
            score = 0.0

            # High weight for exact identifier match (R12.3)
            for ident in identifiers:
                if ident.lower() in content_lower or ident in chunk.metadata.get("external_id", ""):
                    score += 10.0

            # Match keyword terms
            chunk_words = set(re.findall(r"\w+", content_lower))
            matched_terms = search_terms.intersection(chunk_words)
            if matched_terms:
                score += float(len(matched_terms))

            # Include if there is a match or if no search terms were provided (wildcard match)
            if score > 0.0 or not search_terms:
                candidates.append((score, chunk))

        # Sort descending by score
        candidates.sort(key=lambda x: x[0], reverse=True)
        results: list[Candidate] = []

        for rank, (score, chunk) in enumerate(candidates[:top_limit], start=1):
            results.append(
                Candidate(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    content=chunk.content,
                    metadata=dict(chunk.metadata),
                    lexical_rank=rank,
                    vector_rank=None,
                    lexical_score=round(score, 4),
                    vector_score=None,
                    fused_score=None,
                    rerank_score=None,
                )
            )

        return results

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        """Execute in-memory vector similarity search."""
        if self.vector_delay_s > 0:
            await asyncio.sleep(self.vector_delay_s)
        if self.simulate_vector_error is not None:
            raise self.simulate_vector_error

        org_id = q.organization_id
        if not org_id or not q.query_vector:
            return []

        target_status = q.status
        target_category = q.category
        top_limit = max(1, top_n)
        query_vec = q.query_vector

        scored_chunks: list[tuple[float, IndexedChunk]] = []

        for chunk in self._chunks.values():
            # Mandatory tenant isolation (R5.3, R10.4)
            if chunk.organization_id != org_id:
                continue

            # Status filter
            if target_status and chunk.document_status != target_status:
                continue

            # Category filter
            if target_category and chunk.category != target_category:
                continue

            if chunk.embedding is None:
                continue

            similarity = _cosine_similarity(query_vec, chunk.embedding)
            scored_chunks.append((similarity, chunk))

        # Sort descending by cosine similarity
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        results: list[Candidate] = []

        for rank, (sim, chunk) in enumerate(scored_chunks[:top_limit], start=1):
            results.append(
                Candidate(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    content=chunk.content,
                    metadata=dict(chunk.metadata),
                    lexical_rank=None,
                    vector_rank=rank,
                    lexical_score=None,
                    vector_score=round(sim, 6),
                    fused_score=None,
                    rerank_score=None,
                )
            )

        return results

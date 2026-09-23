"""Domain data models and value objects for the retrieval and hybrid search subsystem.

Requirements:
- R10.7: SearchBackend interface abstraction.
- R10.8: Return for every candidate its lexical rank, vector rank, fused score,
  and source metadata.
- R12.3: Verbatim injection of exact identifiers into lexical query.
- specs/design.md §5.5: Candidate and RetrievalQuery data models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class RetrievalQuery:
    """Query object passed to SearchBackend containing multi-modal search inputs (design.md §5.5).

    Constructed from email intent, thread summary, extracted keywords, and entity identifiers.
    """

    semantic_text: str  # email intent + thread topic, natural language
    lexical_terms: list[str] = field(default_factory=list)  # keywords
    identifiers: list[str] = field(default_factory=list)  # INV-…, ORDER-…, INC… (R12.3)
    filters: dict[str, Any] = field(default_factory=dict)  # org_id, category, status
    query_vector: list[float] | None = None  # dense vector representation of semantic_text

    @property
    def organization_id(self) -> str | None:
        """Retrieve tenant organization ID from query filters."""
        val = self.filters.get("organization_id")
        if val is None:
            return None
        return str(val) if isinstance(val, UUID) else str(val)

    @property
    def category(self) -> str | None:
        """Retrieve target document category filter if specified."""
        val = self.filters.get("category")
        return str(val) if val is not None else None

    @property
    def status(self) -> str:
        """Retrieve target document status filter, defaulting to 'active'."""
        val = self.filters.get("status", "active")
        return str(val) if val is not None else "active"

    @property
    def lexical_text(self) -> str:
        """Linearized query string for full-text / lexical search.

        Combines extracted lexical terms and exact identifiers (R12.3).
        Falls back to semantic_text if no discrete terms were extracted.
        """
        parts: list[str] = []
        if self.identifiers:
            parts.extend(self.identifiers)
        if self.lexical_terms:
            parts.extend(self.lexical_terms)
        if not parts and self.semantic_text:
            return self.semantic_text
        return " ".join(parts)


@dataclass
class Candidate:
    """A retrieved knowledge chunk candidate carrying multi-branch ranking signals.

    Fulfills design.md §5.5 and R10.8. Enables transparent tracking of lexical
    rank/score, vector rank/score, RRF fused score, and cross-encoder rerank score.
    """

    chunk_id: str
    document_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    lexical_rank: int | None = None
    vector_rank: int | None = None
    lexical_score: float | None = None
    vector_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None

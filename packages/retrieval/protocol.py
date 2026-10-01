"""SearchBackend protocol definition for hybrid retrieval.

Requirements:
- R10.7: Hide retrieval behind a SearchBackend interface so backend implementations
  (PostgresSearchBackend, OpenSearchBackend) can be swapped without touching pipeline code.
- R10.4: Apply metadata filters (organization_id, category, document status) inside both branches.
- R10.8: Return for every candidate its lexical rank, vector rank, fused score, and source metadata.
- specs/design.md §5.5: SearchBackend Protocol contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from packages.retrieval.models import Candidate, RetrievalQuery


@runtime_checkable
class SearchBackend(Protocol):
    """Protocol defining the storage abstraction interface for candidate chunk retrieval (R10.7).

    Implementations must apply tenant scoping (organization_id) and metadata filters
    inside each branch query rather than as post-filtering (R10.4).
    """

    async def lexical(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        """Execute lexical / full-text search against the knowledge chunk corpus.

        Args:
            q: Retrieval query containing lexical terms, identifiers, and tenant/metadata filters.
            top_n: Maximum candidate chunks to return (default 20, R10.2).

        Returns:
            List of Candidate objects ordered by lexical relevance, with:
            - chunk_id, document_id, content, metadata populated
            - lexical_rank populated (1..top_n)
            - lexical_score populated
            - vector_rank and vector_score set to None
        """
        ...

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        """Execute dense vector similarity search against the knowledge chunk corpus.

        Args:
            q: Retrieval query containing query_vector and tenant/metadata filters.
            top_n: Maximum candidate chunks to return (default 20, R10.2).

        Returns:
            List of Candidate objects ordered by vector similarity, with:
            - chunk_id, document_id, content, metadata populated
            - vector_rank populated (1..top_n)
            - vector_score populated (similarity score, e.g. 1 - cosine_distance)
            - lexical_rank and lexical_score set to None

            A backend that can tell whether a filtered ANN query came back short returns
            ``BranchCandidates`` with ``underfilled`` set (R10.10); the retriever records the
            flag as unknown for a plain list.
        """
        ...

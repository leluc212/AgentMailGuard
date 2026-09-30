"""Reusable contract test suite for SearchBackend implementations.

Requirements:
- Task 3.7: Contract test suite the PostgreSQL implementation must pass — and any future backend.
- R10.7: SearchBackend interface abstraction.
- R10.4: Filters applied inside each branch.
- R10.8: Return candidate carrying lexical rank, vector rank, scores, and metadata.
- R12.3: Exact identifier matching in lexical search.
- specs/design.md §5.5 & §1083: One shared contract suite every implementation must pass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

import pytest

from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.protocol import SearchBackend


class SearchBackendContractSuite(ABC):
    """Abstract contract test suite for SearchBackend implementations.

    Any implementation (FakeSearchBackend, PostgresSearchBackend, OpenSearchBackend)
    must subclass this suite and implement `create_backend()` and `seed_chunk()`.
    """

    @abstractmethod
    def create_backend(self) -> SearchBackend:
        """Create and return an instance of the SearchBackend under test."""
        raise NotImplementedError

    @abstractmethod
    async def seed_chunk(
        self,
        backend: SearchBackend,
        chunk_id: str,
        document_id: str,
        organization_id: str,
        content: str,
        category: str | None = None,
        document_status: str = "active",
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Seed a chunk record into the backend under test."""
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_satisfies_protocol(self) -> None:
        """Assert the backend conforms to the runtime SearchBackend protocol (R10.7)."""
        backend = self.create_backend()
        assert isinstance(backend, SearchBackend)

    @pytest.mark.asyncio
    async def test_lexical_returns_candidates_with_ranks_and_scores(self) -> None:
        """Assert lexical() returns Candidate objects with 1-based ranks and scores (R10.8)."""
        backend = self.create_backend()
        org_id = str(uuid4())
        doc_id = str(uuid4())
        cid1 = str(uuid4())
        cid2 = str(uuid4())

        await self.seed_chunk(
            backend=backend,
            chunk_id=cid1,
            document_id=doc_id,
            organization_id=org_id,
            content="Enterprise email billing configuration and payment policies.",
            metadata={"heading_path": ["Billing", "Policies"], "section": "Policies"},
        )
        await self.seed_chunk(
            backend=backend,
            chunk_id=cid2,
            document_id=doc_id,
            organization_id=org_id,
            content="General server setup guidelines without billing details.",
            metadata={"heading_path": ["Setup"], "section": "Setup"},
        )

        query = RetrievalQuery(
            semantic_text="How do I configure enterprise email billing?",
            lexical_terms=["billing", "payment", "configuration"],
            filters={"organization_id": org_id, "status": "active"},
        )

        candidates = await backend.lexical(query, top_n=10)

        assert len(candidates) >= 1
        top = candidates[0]
        assert isinstance(top, Candidate)
        assert top.chunk_id == cid1
        assert top.document_id == doc_id
        assert "billing" in top.content.lower()
        # Verify rank and score semantics
        assert top.lexical_rank == 1
        assert top.lexical_score is not None and top.lexical_score > 0
        assert top.vector_rank is None
        assert top.vector_score is None
        assert top.fused_score is None
        assert top.rerank_score is None
        # Verify metadata retention
        assert top.metadata.get("section") == "Policies"

    @pytest.mark.asyncio
    async def test_lexical_matches_a_chunk_holding_only_some_of_a_long_query(self) -> None:
        """A chunk with 2 of 8 query terms is a match, and more matching terms rank higher.

        Guards against an AND-style lexical query, which matched nothing for a ~20-word email
        query and left hybrid retrieval vector-only (Amendment 1 G.1, R10.1).
        """
        backend = self.create_backend()
        org_id = str(uuid4())
        doc_id = str(uuid4())
        some, many, none = str(uuid4()), str(uuid4()), str(uuid4())
        await self.seed_chunk(
            backend,
            some,
            doc_id,
            org_id,
            "Enterprise subscription notes for the team.",
        )
        await self.seed_chunk(
            backend,
            many,
            doc_id,
            org_id,
            "Enterprise subscription renewal refund: the invoice charge is reversed.",
        )
        await self.seed_chunk(
            backend, none, doc_id, org_id, "Office opening hours and holiday calendar."
        )

        query = RetrievalQuery(
            semantic_text="",
            lexical_terms=[
                "billed",
                "twice",
                "enterprise",
                "subscription",
                "renewal",
                "refund",
                "invoice",
                "charge",
            ],
            filters={"organization_id": org_id, "status": "active"},
        )

        candidates = await backend.lexical(query, top_n=10)

        assert [c.chunk_id for c in candidates] == [many, some]
        assert [c.lexical_rank for c in candidates] == [1, 2]
        top, second = candidates
        assert top.lexical_score is not None and second.lexical_score is not None
        assert top.lexical_score > second.lexical_score > 0

    @pytest.mark.asyncio
    async def test_lexical_respects_top_n(self) -> None:
        """Assert lexical() respects the requested top_n limit."""
        backend = self.create_backend()
        org_id = str(uuid4())
        doc_id = str(uuid4())

        for i in range(8):
            await self.seed_chunk(
                backend=backend,
                chunk_id=str(uuid4()),
                document_id=doc_id,
                organization_id=org_id,
                content=f"Knowledge article {i} about customer onboarding and signup.",
            )

        query = RetrievalQuery(
            semantic_text="customer onboarding",
            lexical_terms=["customer", "onboarding"],
            filters={"organization_id": org_id, "status": "active"},
        )

        top_3 = await backend.lexical(query, top_n=3)
        assert len(top_3) == 3
        ranks = [c.lexical_rank for c in top_3]
        assert ranks == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_lexical_enforces_tenant_isolation(self) -> None:
        """Assert lexical() strictly isolates tenants and leaks zero cross-tenant data (R5.3)."""
        backend = self.create_backend()
        target_org = str(uuid4())
        other_org = str(uuid4())

        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=other_org,
            content="Confidential financial report for foreign organization.",
        )
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=target_org,
            content="Financial report for target organization.",
        )

        query = RetrievalQuery(
            semantic_text="financial report",
            lexical_terms=["financial", "report"],
            filters={"organization_id": target_org, "status": "active"},
        )

        results = await backend.lexical(query, top_n=10)
        assert len(results) == 1
        assert "target" in results[0].content

    @pytest.mark.asyncio
    async def test_lexical_enforces_status_and_category_filters(self) -> None:
        """Assert lexical() applies status and category filters inside the query (R10.4)."""
        backend = self.create_backend()
        org_id = str(uuid4())

        # 1. Matching category + active status
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=org_id,
            category="billing",
            document_status="active",
            content="Active billing payment procedures.",
        )
        # 2. Matching category + superseded status
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=org_id,
            category="billing",
            document_status="superseded",
            content="Superseded billing payment procedures.",
        )
        # 3. Non-matching category + active status
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=org_id,
            category="technical_support",
            document_status="active",
            content="Active technical support payment procedures.",
        )

        query = RetrievalQuery(
            semantic_text="billing payment",
            lexical_terms=["billing", "payment"],
            filters={"organization_id": org_id, "category": "billing", "status": "active"},
        )

        results = await backend.lexical(query, top_n=10)
        assert len(results) == 1
        assert "Active billing payment procedures." in results[0].content

    @pytest.mark.asyncio
    async def test_lexical_matches_exact_identifiers_verbatim(self) -> None:
        """Assert lexical() extracts and matches exact identifiers verbatim (R12.3)."""
        backend = self.create_backend()
        org_id = str(uuid4())
        doc_id = str(uuid4())
        special_invoice = "INV-2026-01829"

        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=doc_id,
            organization_id=org_id,
            content=f"Payment received for invoice {special_invoice} via wire transfer.",
            metadata={"external_id": special_invoice},
        )
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=doc_id,
            organization_id=org_id,
            content="Payment received for invoice INV-1999-99999 via credit card.",
        )

        query = RetrievalQuery(
            semantic_text="where is the wire transfer payment?",
            identifiers=[special_invoice],
            filters={"organization_id": org_id, "status": "active"},
        )

        results = await backend.lexical(query, top_n=10)
        assert len(results) >= 1
        assert results[0].lexical_rank == 1
        assert special_invoice in results[0].content

    @pytest.mark.asyncio
    async def test_vector_returns_candidates_with_ranks_and_scores(self) -> None:
        """Assert vector() returns Candidate objects with vector ranks and scores (R10.8)."""
        backend = self.create_backend()
        org_id = str(uuid4())
        doc_id = str(uuid4())

        # Two chunks with distinct 4D unit embeddings
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=doc_id,
            organization_id=org_id,
            content="Account authentication troubleshooting and password reset.",
            embedding=[1.0, 0.0, 0.0, 0.0],
            metadata={"heading_path": ["Auth", "Passwords"]},
        )
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=doc_id,
            organization_id=org_id,
            content="Network configuration and firewall setup.",
            embedding=[0.0, 1.0, 0.0, 0.0],
            metadata={"heading_path": ["Network"]},
        )

        # Query vector close to chunk 1
        query = RetrievalQuery(
            semantic_text="I cannot log in to my account",
            query_vector=[0.9, 0.1, 0.0, 0.0],
            filters={"organization_id": org_id, "status": "active"},
        )

        candidates = await backend.vector(query, top_n=5)
        assert len(candidates) == 2
        top = candidates[0]
        assert top.vector_rank == 1
        assert top.vector_score is not None and top.vector_score > 0.8
        assert top.lexical_rank is None
        assert top.lexical_score is None
        assert "authentication" in top.content

        second = candidates[1]
        assert second.vector_rank == 2
        assert second.vector_score is not None and second.vector_score < top.vector_score

    @pytest.mark.asyncio
    async def test_vector_respects_top_n(self) -> None:
        """Assert vector() respects top_n limit."""
        backend = self.create_backend()
        org_id = str(uuid4())
        doc_id = str(uuid4())

        for i in range(6):
            await self.seed_chunk(
                backend=backend,
                chunk_id=str(uuid4()),
                document_id=doc_id,
                organization_id=org_id,
                content=f"Vector document chunk {i}",
                embedding=[0.5, 0.5, float(i) / 10.0, 0.0],
            )

        query = RetrievalQuery(
            semantic_text="sample query",
            query_vector=[0.5, 0.5, 0.0, 0.0],
            filters={"organization_id": org_id, "status": "active"},
        )

        top_2 = await backend.vector(query, top_n=2)
        assert len(top_2) == 2
        assert [c.vector_rank for c in top_2] == [1, 2]

    @pytest.mark.asyncio
    async def test_vector_enforces_tenant_isolation(self) -> None:
        """Assert vector() strictly isolates tenants (R5.3)."""
        backend = self.create_backend()
        org1 = str(uuid4())
        org2 = str(uuid4())

        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=org1,
            content="Tenant 1 proprietary document",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await self.seed_chunk(
            backend=backend,
            chunk_id=str(uuid4()),
            document_id=str(uuid4()),
            organization_id=org2,
            content="Tenant 2 proprietary document",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

        query = RetrievalQuery(
            semantic_text="proprietary document",
            query_vector=[1.0, 0.0, 0.0, 0.0],
            filters={"organization_id": org1, "status": "active"},
        )

        results = await backend.vector(query, top_n=10)
        assert len(results) == 1
        assert "Tenant 1" in results[0].content

    @pytest.mark.asyncio
    async def test_empty_results_clean_handling(self) -> None:
        """Assert empty results or non-matching filters return empty list gracefully."""
        backend = self.create_backend()
        query = RetrievalQuery(
            semantic_text="non-existent search term",
            lexical_terms=["nonexistentwordxyz123"],
            query_vector=[0.1, 0.2, 0.3, 0.4],
            filters={"organization_id": str(uuid4()), "status": "active"},
        )

        lex_res = await backend.lexical(query, top_n=5)
        vec_res = await backend.vector(query, top_n=5)
        assert lex_res == []
        assert vec_res == []

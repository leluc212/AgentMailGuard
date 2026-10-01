"""Unit tests for retrieval models (RetrievalQuery and Candidate).

Requirements:
- R10.8: Candidate carries lexical rank, vector rank, fused score, and source metadata.
- R12.3: Identifiers injected into lexical search.
- specs/design.md §5.5: Data contracts.
"""

from uuid import uuid4

from packages.retrieval.models import Candidate, RetrievalQuery


def test_retrieval_query_defaults_and_properties() -> None:
    org_id = uuid4()
    q = RetrievalQuery(
        semantic_text="Customer cannot log in to dashboard",
        lexical_terms=["login", "dashboard"],
        identifiers=["INV-2026-999"],
        filters={"organization_id": org_id, "category": "technical_support", "status": "active"},
        query_vector=[0.1, 0.2, 0.3],
    )

    assert q.semantic_text == "Customer cannot log in to dashboard"
    assert q.lexical_terms == ["login", "dashboard"]
    assert q.identifiers == ["INV-2026-999"]
    assert q.organization_id == str(org_id)
    assert q.category == "technical_support"
    assert q.status == "active"
    assert q.query_vector == [0.1, 0.2, 0.3]
    assert q.lexical_text == "INV-2026-999 login dashboard"


def test_retrieval_query_string_org_id() -> None:
    q = RetrievalQuery(
        semantic_text="Sample text",
        filters={"organization_id": "org-string-123"},
    )
    assert q.organization_id == "org-string-123"
    assert q.category is None
    assert q.status == "active"  # default status


def test_retrieval_query_empty_filters() -> None:
    q = RetrievalQuery(semantic_text="Only semantic")
    assert q.organization_id is None
    assert q.category is None
    assert q.status == "active"
    assert q.lexical_text == "Only semantic"


def test_candidate_dataclass_fields_and_defaults() -> None:
    # Minimal candidate
    c1 = Candidate(
        chunk_id="chk-01",
        document_id="doc-01",
        content="Sample chunk content",
    )
    assert c1.chunk_id == "chk-01"
    assert c1.document_id == "doc-01"
    assert c1.content == "Sample chunk content"
    assert c1.metadata == {}
    assert c1.lexical_rank is None
    assert c1.vector_rank is None
    assert c1.lexical_score is None
    assert c1.vector_score is None
    assert c1.fused_score is None
    assert c1.rerank_score is None

    # Fully populated candidate carrying both ranks, both scores, fused and rerank scores (R10.8)
    c2 = Candidate(
        chunk_id="chk-02",
        document_id="doc-02",
        content="Enterprise billing terms",
        metadata={"heading_path": ["Billing"], "section": "Terms", "external_id": "DOC-100-01"},
        lexical_rank=1,
        vector_rank=2,
        lexical_score=4.5,
        vector_score=0.92,
        fused_score=0.0325,
        rerank_score=0.88,
    )
    assert c2.lexical_rank == 1
    assert c2.vector_rank == 2
    assert c2.lexical_score == 4.5
    assert c2.vector_score == 0.92
    assert c2.fused_score == 0.0325
    assert c2.rerank_score == 0.88
    assert c2.metadata["external_id"] == "DOC-100-01"

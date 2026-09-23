"""Unit tests verifying SearchBackendContractSuite against FakeSearchBackend.

Requirements:
- Task 3.7: Contract test suite every backend implementation must pass.
- R10.7: SearchBackend interface abstraction.
- R10.8: Candidate returns lexical rank, vector rank, scores, and metadata.
"""

from typing import Any

import pytest

from packages.retrieval import (
    FakeSearchBackend,
    RetrievalQuery,
    SearchBackend,
    SearchBackendContractSuite,
)


class TestFakeSearchBackendContract(SearchBackendContractSuite):
    """Proves that FakeSearchBackend passes the canonical SearchBackendContractSuite."""

    def create_backend(self) -> SearchBackend:
        return FakeSearchBackend()

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
        assert isinstance(backend, FakeSearchBackend)
        backend.add_chunk(
            chunk_id=chunk_id,
            document_id=document_id,
            organization_id=organization_id,
            content=content,
            category=category,
            document_status=document_status,
            metadata=metadata,
            embedding=embedding,
        )


@pytest.mark.asyncio
async def test_fake_search_backend_fault_injection() -> None:
    """Verify FakeSearchBackend fault simulation for downstream resilience testing."""
    backend = FakeSearchBackend()
    backend.add_chunk(
        chunk_id="chk-1",
        document_id="doc-1",
        organization_id="org-1",
        content="Test content",
        embedding=[1.0, 0.0],
    )

    query = RetrievalQuery(
        semantic_text="query",
        lexical_terms=["test"],
        query_vector=[1.0, 0.0],
        filters={"organization_id": "org-1", "status": "active"},
    )

    # 1. Normal execution
    assert len(await backend.lexical(query)) == 1
    assert len(await backend.vector(query)) == 1

    # 2. Simulate lexical failure
    backend.simulate_lexical_error = RuntimeError("FTS engine unavailable")
    with pytest.raises(RuntimeError, match="FTS engine unavailable"):
        await backend.lexical(query)
    # Vector branch still succeeds
    assert len(await backend.vector(query)) == 1

    # 3. Simulate vector failure
    backend.simulate_lexical_error = None
    backend.simulate_vector_error = TimeoutError("Vector index timeout")
    assert len(await backend.lexical(query)) == 1
    with pytest.raises(TimeoutError, match="Vector index timeout"):
        await backend.vector(query)

    # 4. Clear resets state
    backend.clear()
    assert await backend.lexical(query) == []
    assert await backend.vector(query) == []

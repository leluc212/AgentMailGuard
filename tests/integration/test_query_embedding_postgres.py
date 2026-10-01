"""3.16: the production retrieval path embeds the query and searches pgvector (R10.1).

Runs against the isolated test database (rag_email_test). Before 3.16 the vector branch
received no query_vector and returned nothing, so a chunk with no lexical overlap with the
query was unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.knowledge.embedder import FakeEmbedder
from packages.retrieval.models import RetrievalQuery
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.retriever import HybridRetriever
from tests.integration.test_postgres_search_backend import _seed_postgres_chunk

CONTENT = "Aurora teapot calibration requires the blue valve at step four."


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def test_embedded_query_reaches_pgvector_and_stays_in_tenant(
    db_pool: asyncpg.Pool[Any],
) -> None:
    embedder = FakeEmbedder()
    vector = await embedder.embed_query(CONTENT)
    org, other_org = str(uuid4()), str(uuid4())
    own_chunk = str(uuid4())
    await _seed_postgres_chunk(
        db_pool, own_chunk, str(uuid4()), org, CONTENT, category="support", embedding=vector
    )
    await _seed_postgres_chunk(
        db_pool,
        str(uuid4()),
        str(uuid4()),
        other_org,
        CONTENT,
        category="support",
        embedding=vector,
    )
    # No lexical overlap: only the vector branch can find the chunk.
    query = RetrievalQuery(
        semantic_text=CONTENT,
        lexical_terms=["zzqxv"],
        filters={"organization_id": org, "status": "active"},
    )
    backend = PostgresSearchBackend(db_pool)

    without = await HybridRetriever(backend).retrieve(query)
    result = await HybridRetriever(backend, embedder=embedder).retrieve(query)

    assert without.candidates == []
    assert not result.retrieval_degraded
    assert result.lexical_candidates == []
    assert [c.chunk_id for c in result.candidates] == [own_chunk]
    assert result.candidates[0].vector_rank == 1
    assert result.query_vector_dimension == embedder.dimension

"""The Postgres lexical branch sends an OR tsquery, never an AND of every word (Amendment G.1).

Requirements: R10.1 (FTS branch), R10.4 (filters inside the branch), R12.3 (identifiers verbatim).

The SQL itself runs in ``tests/integration/test_postgres_search_backend.py`` (live PostgreSQL).
Here a recording pool shows what the backend hands PostgreSQL: the query text, its parameters and
the filters, and that a query with no usable term skips the branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.postgres import PostgresSearchBackend


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


class _Pool:
    def __init__(self) -> None:
        self.conn = _Conn()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        yield self.conn


def _backend() -> tuple[PostgresSearchBackend, _Conn]:
    pool = _Pool()
    return PostgresSearchBackend(pool), pool.conn


def _query(**kw: Any) -> RetrievalQuery:
    filters = {"organization_id": str(uuid4()), "category": "billing", "status": "active"}
    return RetrievalQuery(
        semantic_text="ignored when terms exist",
        filters=filters,
        **kw,
    )


async def test_lexical_sends_an_or_tsquery_of_the_query_terms() -> None:
    backend, conn = _backend()
    q = _query(
        lexical_terms=["billed", "twice", "enterprise", "renewal", "refund"],
        identifiers=["INV-2026-01829"],
    )

    await backend.lexical(q, top_n=7)

    ((sql, args),) = conn.calls
    assert "websearch_to_tsquery" not in sql
    assert "to_tsquery('english', $2)" in sql
    assert "ts_rank_cd" in sql
    tsquery_text = args[1]
    assert tsquery_text == (
        "'inv-2026-01829' | 'billed' | 'twice' | 'enterprise' | 'renewal' | 'refund'"
    )
    assert " & " not in tsquery_text


async def test_lexical_filters_and_limit_are_unchanged() -> None:
    backend, conn = _backend()
    q = _query(lexical_terms=["refund", "invoice"])

    await backend.lexical(q, top_n=7)

    ((sql, args),) = conn.calls
    assert "c.organization_id = $1" in sql
    assert "d.status = $3" in sql and "d.category = $4" in sql
    assert args[0] == UUID(q.organization_id or "")
    assert args[2:] == ("active", "billing", 7)


async def test_lexical_falls_back_to_the_semantic_text_as_an_or_query() -> None:
    backend, conn = _backend()
    q = _query()
    q.semantic_text = "Intent: refund. Subject: double billing on invoice."

    await backend.lexical(q)

    ((_, args),) = conn.calls
    assert args[1] == "'intent' | 'refund' | 'subject' | 'double' | 'billing' | 'invoice'"


async def test_a_query_with_only_stop_words_skips_the_lexical_branch() -> None:
    backend, conn = _backend()
    q = _query(lexical_terms=["the", "and", "of"])

    assert await backend.lexical(q) == []
    assert conn.calls == []


async def test_hybrid_uses_the_or_query_and_keeps_the_filters() -> None:
    backend, conn = _backend()
    q = _query(lexical_terms=["refund", "invoice"], query_vector=[0.1, 0.2, 0.3])

    await backend.hybrid(q, top_n=5, k=60, fuse_limit=3)

    sql, args = next((s, a) for s, a in conn.calls if "FULL OUTER JOIN" in s)
    assert "websearch_to_tsquery" not in sql
    assert "to_tsquery('english', $2)" in sql
    assert args[1] == "'refund' | 'invoice'"
    assert args[2:4] == ("active", "billing")


async def test_hybrid_with_only_stop_words_is_vector_only(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _backend()
    q = _query(lexical_terms=["the", "and"], query_vector=[0.1, 0.2, 0.3])
    seen: list[str] = []

    async def vector(query: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        seen.append("vector")
        return []

    async def lexical(query: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        seen.append("lexical")
        return []

    monkeypatch.setattr(backend, "vector", vector)
    monkeypatch.setattr(backend, "lexical", lexical)

    await backend.hybrid(q, top_n=5, k=60, fuse_limit=3)

    assert seen == ["vector"]

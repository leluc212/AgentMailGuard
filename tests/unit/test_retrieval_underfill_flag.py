"""Whether a filtered ANN query came back short travels with the retrieval, per call (R10.10).

The backend used to remember it in one attribute shared by every lane and never reset: a
retrieval could not tell whether its own vector branch under-filled. The vector branch now returns
``BranchCandidates`` carrying the flag, and ``RetrievalResult.retrieval_underfilled`` reports it:
True or False when the backend could tell, None (unknown) when it could not or the branch failed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

from packages.retrieval.models import BranchCandidates, Candidate, RetrievalQuery
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.retriever import HybridRetriever, RetrievalResult

QUERY = RetrievalQuery(
    semantic_text="how do I reset my password",
    filters={"organization_id": str(uuid4())},
    query_vector=[0.1, 0.2, 0.3],
)


def _candidate(name: str) -> Candidate:
    return Candidate(chunk_id=f"chunk-{name}", document_id=f"doc-{name}", content=name)


def test_branch_candidates_are_a_list_that_carries_the_flag() -> None:
    hits = BranchCandidates([_candidate("a"), _candidate("b")], underfilled=True)

    assert isinstance(hits, list)
    assert [c.chunk_id for c in hits] == ["chunk-a", "chunk-b"]
    assert hits.underfilled is True
    assert BranchCandidates().underfilled is False


class _Backend:
    """A SearchBackend whose vector branch answers with whatever the test hands it."""

    def __init__(
        self,
        vector: list[Candidate] | Exception,
        lexical: list[Candidate] | Exception | None = None,
    ) -> None:
        self._vector = vector
        self._lexical = lexical if lexical is not None else [_candidate("lex")]

    async def lexical(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        if isinstance(self._lexical, Exception):
            raise self._lexical
        return self._lexical

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        if isinstance(self._vector, Exception):
            raise self._vector
        return self._vector


async def _retrieve(backend: _Backend) -> RetrievalResult:
    return await HybridRetriever(backend).retrieve(QUERY)


async def test_an_underfilled_vector_branch_is_reported() -> None:
    result = await _retrieve(_Backend(BranchCandidates([_candidate("v")], underfilled=True)))

    assert result.retrieval_underfilled is True
    assert result.retrieval_degraded is False  # widened and answered: nothing was dropped


async def test_a_vector_branch_that_checked_and_was_full_reports_false() -> None:
    result = await _retrieve(_Backend(BranchCandidates([_candidate("v")])))

    assert result.retrieval_underfilled is False


async def test_a_backend_that_cannot_tell_leaves_the_flag_unknown() -> None:
    result = await _retrieve(_Backend([_candidate("v")]))

    assert result.retrieval_underfilled is None


async def test_a_failed_vector_branch_leaves_the_flag_unknown() -> None:
    result = await _retrieve(_Backend(RuntimeError("embedder down")))

    assert result.retrieval_degraded is True
    assert result.retrieval_underfilled is None


async def test_the_flag_belongs_to_the_vector_branch() -> None:
    backend = _Backend(
        BranchCandidates([_candidate("v")], underfilled=True), lexical=RuntimeError("fts down")
    )

    result = await _retrieve(backend)

    assert result.retrieval_degraded is True
    assert result.retrieval_underfilled is True


def test_a_retrieval_result_starts_with_the_flag_unknown() -> None:
    assert RetrievalResult().retrieval_underfilled is None


# --- PostgresSearchBackend.vector, against an in-memory connection ---------------------------


def _row(rank: int) -> dict[str, Any]:
    return {
        "chunk_id": f"chunk-{rank}",
        "document_id": f"doc-{rank}",
        "content": f"content {rank}",
        "heading_path": None,
        "section": None,
        "category": None,
        "external_id": None,
        "metadata": {},
        "score": 1.0 - rank / 100,
        "rnk": rank,
    }


class _Conn:
    """The asyncpg calls ``vector`` makes: a short first answer, a fuller one once widened."""

    def __init__(self, *, first: list[dict[str, Any]], full: list[dict[str, Any]], total: int):
        self.first, self.full, self.total = first, full, total
        self.statements: list[str] = []
        self.counts = 0
        self._widened = False

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append(sql)
        if "relaxed_order" in sql:
            self._widened = True
        elif "iterative_scan = 'off'" in sql:
            self._widened = False
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.full if self._widened else self.first

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.counts += 1
        return self.total


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        yield self.conn


def _backend(conn: _Conn) -> PostgresSearchBackend:
    return PostgresSearchBackend(pool=cast(Any, _Pool(conn)), default_top_n=5)


async def _vector(backend: PostgresSearchBackend, top_n: int = 5) -> BranchCandidates:
    hits = await backend.vector(QUERY, top_n)
    assert isinstance(hits, BranchCandidates)
    return hits


async def test_a_full_answer_is_not_underfilled_and_costs_no_count_query() -> None:
    rows = [_row(i) for i in range(1, 6)]
    conn = _Conn(first=rows, full=rows, total=99)

    hits = await _vector(_backend(conn))

    assert len(hits) == 5 and hits.underfilled is False
    assert conn.counts == 0


async def test_a_short_answer_from_a_small_corpus_is_not_underfilled() -> None:
    rows = [_row(i) for i in range(1, 3)]
    conn = _Conn(first=rows, full=rows, total=2)  # everything the tenant has came back

    hits = await _vector(_backend(conn))

    assert len(hits) == 2 and hits.underfilled is False
    assert not any(sql.startswith("SET") for sql in conn.statements)


async def test_a_short_answer_with_more_available_is_underfilled_and_widened() -> None:
    full = [_row(i) for i in range(1, 6)]
    conn = _Conn(first=full[:2], full=full, total=9)
    backend = _backend(conn)

    hits = await _vector(backend)

    assert hits.underfilled is True
    assert [c.chunk_id for c in hits] == [f"chunk-{i}" for i in range(1, 6)]  # the widened answer
    assert any("ef_search = 200" in sql for sql in conn.statements)
    assert backend.last_retrieval_underfilled is True


async def test_the_flag_is_per_call_not_sticky() -> None:
    full = [_row(i) for i in range(1, 6)]
    conn = _Conn(first=full[:2], full=full, total=9)
    backend = _backend(conn)

    first = await _vector(backend)
    conn.first = full  # the next query is answered in full
    second = await _vector(backend)

    assert first.underfilled is True
    assert second.underfilled is False

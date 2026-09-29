# Dense Query Embedding in the Production Retrieval Path (task 3.16) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every production retrieval (the ai-worker's `ContextBuilder` and the API's `/v1/search/debug`) embeds the query with the configured embedder, so the pgvector branch of hybrid retrieval returns candidates. A slow or failing embedder degrades retrieval to lexical-only within the configured retrieval timeout instead of blocking.

**Architecture:** `HybridRetriever` gets an optional `embedder`. When a query arrives without a `query_vector`, the **vector branch** embeds `semantic_text` and then runs the ANN search. Both steps share one `asyncio.wait_for` under the vector-branch timeout. An embedder error, timeout, or unusable vector (wrong length or all zeros) therefore becomes an ordinary vector-branch failure, and the retriever's existing degradation path takes over: the lexical results are kept, `retrieval_degraded=true`, and `retrieval_degraded_total{failed_branch="vector"}` is incremented. Lexical search runs concurrently, so embedding adds no latency to the lexical branch. The worker and API composition roots construct the embedder with `get_embedder(settings.embedding, metrics=...)`, which counts `embedding_tokens_total`, and pass `settings.retrieval.retrieval_timeout_ms`, which is configured today but never read.

**Tech Stack:** Python 3.12, asyncio, asyncpg + pgvector (PostgreSQL 16), FastAPI, prometheus_client, pytest (+ pytest-asyncio auto mode), uv, Docker Compose.

**Spec:**
- `specs/tasks.md` task 3.16 (R10.1, R10.9, R9.11), read with `specs/requirements.md` R10.1–R10.10, R9.11, R5.10, NFR5.
- `specs/design.md` §5.5 (hybrid retrieval) and §"Startup assertion (R5.10)".
- Source of truth: `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md` (hybrid retrieval: "Lexical retrieval performs particularly well on exact identifiers, whereas semantic vector retrieval is valuable for natural-language similarity").

## Global Constraints

- R10.1: WHEN retrieval is required, THE SYSTEM SHALL execute a PostgreSQL FTS query and a pgvector ANN query against the same chunk corpus.
- R10.6: IF one branch fails or times out, THEN THE SYSTEM SHALL degrade to the surviving branch, record `retrieval_degraded=true`, and continue.
- R10.9: THE SYSTEM SHALL enforce a configurable retrieval timeout and SHALL NOT let a slow query block the worker indefinitely. The configured value is `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` (default `500`).
- R9.11: THE SYSTEM SHALL track `embedding_tokens_total` for cost accounting.
- R5.10: the configured embedding dimension must equal the `VECTOR(n)` column width, or the service refuses to start. The query must be embedded with the same model and dimension as the corpus (`EMBEDDING__MODEL_NAME`, `EMBEDDING__DIMENSION`).
- NFR5: hybrid retrieval 50–250 ms.
- `RetrievalQueryBuilder` stays synchronous and makes no calls (R12: "Executes synchronously in sub-millisecond time").
- Never log prompt text, email text, query text, or draft text (CLAUDE.md, requirements §0.5).
- CI runs offline: `EMBEDDING__MOCK=true` selects `FakeEmbedder` (R24.5).
- Do not run `tests/integration` against the live stack. Integration tests use the isolated `rag_email_test` database and vhost (RA.2).
- Commands to check: `make ci` (ruff format --check, ruff check, strict mypy, unit and integration tests).

## Review Focus

1. **A slow or hung embedder** (a hosted API stalling, retries with backoff) must not hold retrieval past the vector-branch timeout. Lexical results still come back, flagged degraded. Pinned by Task 1 `test_slow_embedder_degrades_to_lexical_within_the_timeout` and Task 3 `test_slow_embedder_does_not_block_the_debug_endpoint`.
2. **An unusable query vector** (all zeros, which `HttpEmbedder.embed_query` returns on an empty response, or the wrong length) must be treated as a vector-branch failure, not sent to pgvector, where the cosine distance to a zero vector is undefined. Pinned by Task 1 `test_unusable_query_vector_is_a_vector_branch_failure`.
3. **A caller-supplied `query_vector`** (the debug API's override) is never re-embedded, and the caller's `RetrievalQuery` object is never mutated. Pinned by Task 1 `test_supplied_vector_is_not_re_embedded` and `test_embedding_does_not_mutate_the_callers_query`.
4. **An email with an empty subject and body** (empty `semantic_text`) makes no embedding call and is not reported as degraded. Pinned by Task 1 `test_empty_semantic_text_skips_embedding_without_degrading`.
5. **Tenant isolation on the vector branch:** an embedded query must return only the caller's tenant's chunks, even when another tenant has an identical chunk. Pinned by Task 1 `test_embedded_query_reaches_pgvector_and_stays_in_tenant` (live PostgreSQL).

---

## File Structure

- **Modify:** `packages/retrieval/retriever.py`. `HybridRetriever` gains an `embedder` and embeds inside the vector branch. `RetrievalResult` gains `query_vector_dimension`. Adds `UnusableQueryVectorError`.
- **Modify:** `services/ai_worker/main.py`. It builds the configured embedder with metrics, passes it and the configured timeout to `HybridRetriever`, registers `aclose`, and runs the R5.10 dimension check at startup.
- **Modify:** `services/knowledge_worker/main.py`. Its ingestion embedder counts `embedding_tokens_total` and is closed on shutdown.
- **Modify:** `docker-compose.yml`. Forwards `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL` and `EMBEDDING__API_KEY` so a real embedder can run under compose, with the same model in every service.
- **Modify:** `services/api/main.py`. `app.state.settings` is set in `create_app`, and the lifespan attaches the configured embedder and closes it on shutdown.
- **Modify:** `services/api/routers/search.py`. The retriever embeds under the configured timeout, and the debug fields come from `RetrievalResult.query_vector_dimension`.
- **Create:** `tests/integration/test_query_embedding_postgres.py`. The production path runs against live pgvector.
- **Create:** `scripts/retrieval_gate.py` and a `make retrieval-gate` target. The live-stack proof: upload, ingest, API debug, then an email through the ai-worker.
- **Modify:** `docs/configuration.md` §2.5 and §2.7, `specs/tasks.md` 3.16 and the R9/R10 coverage rows.
- **Tests modified:** `tests/unit/test_retriever.py`, `tests/unit/test_ai_worker_main.py`, `tests/unit/test_worker_mains.py`, `tests/unit/test_runtime_image_contract.py`, `tests/unit/test_retrieval_debug_api.py`.

---

### Task 1: HybridRetriever embeds queries inside the vector branch

**Files:**
- Modify: `packages/retrieval/retriever.py` (imports; `RetrievalResult` at lines 38–55; `HybridRetriever.__init__` at lines 63–103; `_run_vector` at lines 161–183; every `RetrievalResult(...)` built in `retrieve`)
- Test: `tests/unit/test_retriever.py` (append a class)
- Test: `tests/integration/test_query_embedding_postgres.py` (create)

**Interfaces:**
- Consumes: `packages.knowledge.embedder.Embedder` (protocol: `model_name`, `dimension`, `async embed_query(str) -> list[float]`), `FakeEmbedder(model_name=..., dimension=..., simulate_latency_ms=..., error_to_raise=..., metrics=...)`, `EmbeddingError`.
- Produces:
  - `HybridRetriever(backend, *, timeout_seconds=2.0, lexical_timeout_seconds=None, vector_timeout_seconds=None, default_top_n=20, rrf_k=60, raise_on_both_failed=False, metrics=None, embedder: Embedder | None = None)`, with the attribute `retriever.embedder`.
  - `RetrievalResult.query_vector_dimension: int | None`: the length of the vector the vector branch searched with, or `None` when it searched without one.
  - `class UnusableQueryVectorError(ValueError)`.

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/unit/test_retriever.py`:

```python
class _RecordingVectorBackend(SlowMockBackend):
    """Records the query the vector branch receives."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.vector_queries: list[RetrievalQuery] = []

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        self.vector_queries.append(q)
        return await super().vector(q, top_n)


class _FixedVectorEmbedder(FakeEmbedder):
    """Returns a fixed vector, to model a misbehaving provider."""

    def __init__(self, vector: list[float], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vector = vector

    async def embed_query(self, query: str) -> list[float]:
        self.recorded_calls.append({"texts": [query]})
        return list(self._vector)


def _unembedded_query(text: str = "how do I reset my enterprise password") -> RetrievalQuery:
    return RetrievalQuery(
        semantic_text=text,
        lexical_terms=["reset", "password"],
        filters={"organization_id": "00000000-0000-0000-0000-000000000001"},
    )


class TestQueryEmbedding:
    """3.16: the vector branch embeds the query (R10.1) under the timeout (R10.9)."""

    async def test_vector_branch_embeds_the_semantic_text(self) -> None:
        backend = _RecordingVectorBackend(vector_candidates=[_make_candidate("c_vec")])
        embedder = FakeEmbedder()
        query = _unembedded_query()

        result = await HybridRetriever(backend, embedder=embedder).retrieve(query)

        assert embedder.recorded_calls[0]["texts"] == [query.semantic_text]
        searched = backend.vector_queries[0].query_vector
        assert searched == await embedder.embed_query(query.semantic_text)
        assert result.query_vector_dimension == embedder.dimension
        assert not result.retrieval_degraded
        assert [c.chunk_id for c in result.vector_candidates] == ["c_vec"]

    async def test_embedding_does_not_mutate_the_callers_query(self) -> None:
        query = _unembedded_query()
        await HybridRetriever(_RecordingVectorBackend(), embedder=FakeEmbedder()).retrieve(query)
        assert query.query_vector is None

    async def test_supplied_vector_is_not_re_embedded(self, query: RetrievalQuery) -> None:
        backend = _RecordingVectorBackend()
        embedder = FakeEmbedder()

        result = await HybridRetriever(backend, embedder=embedder).retrieve(query)

        assert embedder.recorded_calls == []
        assert backend.vector_queries[0].query_vector == query.query_vector
        assert result.query_vector_dimension == len(query.query_vector or [])

    async def test_slow_embedder_degrades_to_lexical_within_the_timeout(self) -> None:
        metrics = create_pipeline_metrics()
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        retriever = HybridRetriever(
            backend,
            embedder=FakeEmbedder(simulate_latency_ms=2000),
            timeout_seconds=0.05,
            metrics=metrics,
        )

        t0 = time.perf_counter()
        result = await retriever.retrieve(_unembedded_query())
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, f"retrieval blocked for {elapsed:.2f}s behind the embedder"
        assert result.retrieval_degraded and result.surviving_branch == "lexical"
        assert [c.chunk_id for c in result.candidates] == ["c_lex"]
        assert result.vector_error is not None and "Timeout" in result.vector_error
        assert result.query_vector_dimension is None
        assert backend.vector_queries == []
        payload, _ = generate_metrics_payload(metrics.registry)
        assert 'failed_branch="vector"' in payload.decode()

    async def test_embedder_error_degrades_to_lexical(self) -> None:
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        embedder = FakeEmbedder(error_to_raise=EmbeddingError("provider unavailable"))

        result = await HybridRetriever(backend, embedder=embedder).retrieve(_unembedded_query())

        assert result.retrieval_degraded and result.surviving_branch == "lexical"
        assert result.vector_error == "provider unavailable"
        assert backend.vector_queries == []

    @pytest.mark.parametrize("vector", [[0.0] * 1536, [0.1] * 8], ids=["zeros", "short"])
    async def test_unusable_query_vector_is_a_vector_branch_failure(
        self, vector: list[float]
    ) -> None:
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        embedder = _FixedVectorEmbedder(vector)

        result = await HybridRetriever(backend, embedder=embedder).retrieve(_unembedded_query())

        assert result.retrieval_degraded and result.surviving_branch == "lexical"
        assert result.vector_error is not None and "unusable query vector" in result.vector_error
        assert backend.vector_queries == []

    async def test_empty_semantic_text_skips_embedding_without_degrading(self) -> None:
        backend = _RecordingVectorBackend(lexical_candidates=[_make_candidate("c_lex")])
        embedder = FakeEmbedder()

        result = await HybridRetriever(backend, embedder=embedder).retrieve(
            _unembedded_query("   ")
        )

        assert embedder.recorded_calls == []
        assert not result.retrieval_degraded
        assert result.query_vector_dimension is None

    async def test_query_embedding_tokens_are_counted(self) -> None:
        metrics = create_pipeline_metrics()
        embedder = FakeEmbedder(model_name="embed-test", metrics=metrics)

        await HybridRetriever(_RecordingVectorBackend(), embedder=embedder).retrieve(
            _unembedded_query()
        )

        counted = metrics.embedding_tokens_total.labels(model="embed-test")._value.get()
        assert counted > 0
```

Add the imports at the top of `tests/unit/test_retriever.py`, next to the existing ones:

```python
from packages.knowledge.embedder import EmbeddingError, FakeEmbedder
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_retriever.py -k TestQueryEmbedding -q`
Expected: FAIL. Every test errors with `TypeError: HybridRetriever.__init__() got an unexpected keyword argument 'embedder'`.

- [ ] **Step 3: Implement the embedding in the vector branch**

In `packages/retrieval/retriever.py`:

1. Add the imports and the error type next to the existing ones:

```python
import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packages.knowledge.embedder import Embedder
```

```python
class UnusableQueryVectorError(ValueError):
    """The embedder returned a vector the ANN search cannot use (wrong length or all zeros)."""
```

2. Add a field to `RetrievalResult`, after `total_latency_ms`:

```python
    query_vector_dimension: int | None = None  # length of the vector searched, None if none
```

3. `HybridRetriever.__init__`: add the keyword parameter `embedder: Embedder | None = None` after `metrics`, document it in the Args block as `embedder: Optional Embedder used to embed semantic_text when a query arrives without a query_vector (R10.1). Must be the corpus model and dimension (R5.10).`, and store `self.embedder = embedder`.

4. Replace the body of `_run_vector` so embedding and search share the vector-branch timeout, and record the searched dimension:

```python
        vector_dimension: int | None = None

        async def _embed_then_search() -> list[Candidate]:
            nonlocal vector_dimension
            vector_query = query
            if (
                not query.query_vector
                and self.embedder is not None
                and query.semantic_text.strip()
            ):
                vector = await self.embedder.embed_query(query.semantic_text)
                if len(vector) != self.embedder.dimension or not any(vector):
                    raise UnusableQueryVectorError(
                        f"unusable query vector (length {len(vector)}, "
                        f"expected {self.embedder.dimension} with a non-zero value)"
                    )
                vector_query = dataclasses.replace(query, query_vector=vector)
            if vector_query.query_vector:
                vector_dimension = len(vector_query.query_vector)
            return await self.backend.vector(vector_query, n)

        async def _run_vector() -> tuple[list[Candidate], str | None, float]:
            t0 = time.perf_counter()
            try:
                # Query embedding and ANN search share one budget (R10.9): a slow embedder
                # fails this branch and retrieval degrades to lexical (R10.6).
                res = await asyncio.wait_for(_embed_then_search(), timeout=vec_to)
                dt = (time.perf_counter() - t0) * 1000.0
                return res, None, dt
            except TimeoutError:
                dt = (time.perf_counter() - t0) * 1000.0
                msg = f"Timeout after {vec_to}s"
                logger.warning(
                    "Vector branch timed out after %.2fs for tenant %s",
                    vec_to,
                    query.organization_id,
                )
                return [], msg, dt
            except Exception as err:
                dt = (time.perf_counter() - t0) * 1000.0
                logger.warning(
                    "Vector branch failed for tenant %s: %s",
                    query.organization_id,
                    err,
                )
                return [], str(err), dt
```

   A timed-out branch never finishes a search, so `vector_dimension` stays `None` for it. The dimension is set just before the ANN call, so a timeout during the search still reports the vector that was used.

5. Every `RetrievalResult(...)` constructed inside `retrieve` gets `query_vector_dimension=vector_dimension`. Run `grep -n "RetrievalResult(" packages/retrieval/retriever.py` and add the keyword to each call inside `retrieve`.

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest tests/unit/test_retriever.py -q`
Expected: PASS (existing tests plus 9 new).

- [ ] **Step 5: Write the failing integration test against live pgvector**

Create `tests/integration/test_query_embedding_postgres.py`:

```python
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
        db_pool, str(uuid4()), str(uuid4()), other_org, CONTENT, category="support",
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
```

- [ ] **Step 6: Run the integration test**

Run: `uv run pytest tests/integration/test_query_embedding_postgres.py -q`
Expected: PASS with Step 3 in place. To confirm the test is not vacuous, temporarily delete the `embedder=embedder` argument in the second `HybridRetriever(...)`. It must fail with `assert [] == ['<uuid>']`. Restore the argument.

- [ ] **Step 7: Lint, type-check, commit**

Run: `uv run ruff format packages/retrieval/retriever.py tests/unit/test_retriever.py tests/integration/test_query_embedding_postgres.py && uv run ruff check packages/retrieval tests/unit/test_retriever.py tests/integration/test_query_embedding_postgres.py && uv run mypy packages/retrieval tests/unit/test_retriever.py tests/integration/test_query_embedding_postgres.py`
Expected: `All checks passed!` and `Success: no issues found`.

```bash
git add packages/retrieval/retriever.py tests/unit/test_retriever.py tests/integration/test_query_embedding_postgres.py
git commit -m "feat(retrieval): embed the query inside the vector branch under its timeout [task 3.16] [R10.1, R10.6, R10.9]"
```

---

### Task 2: Workers compose the configured embedder, timeout and token metrics

**Files:**
- Modify: `services/ai_worker/main.py` (imports; `build_consumers` signature and the `HybridRetriever(...)` at lines 102–107; `build_components`)
- Modify: `services/knowledge_worker/main.py:38-54` (`build_consumer`)
- Modify: `docker-compose.yml` (`x-app-env`, after `EMBEDDING__MOCK`)
- Test: `tests/unit/test_ai_worker_main.py`, `tests/unit/test_worker_mains.py`, `tests/unit/test_runtime_image_contract.py`

**Interfaces:**
- Consumes: Task 1 `HybridRetriever(..., embedder=...)`; `packages.knowledge.embedder.get_embedder(settings: EmbeddingSettings | None, client=None, metrics: PipelineMetrics | None = None) -> Embedder`; `packages.db.migrator.verify_database_vector_dimension(conn_or_dsn, configured_dimension)`; `res.shutdown.register_cleanup_callback(async_fn)`.
- Produces: `build_consumers(res, *, llm_provider=None, token_counter=None, embedder: Embedder | None = None)`. The retriever of every consumer is `HybridRetriever(..., embedder=<configured>, timeout_seconds=settings.retrieval.retrieval_timeout_ms / 1000)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_ai_worker_main.py`:

```python
async def test_retriever_embeds_with_the_configured_model_and_timeout() -> None:
    """3.16: the corpus model embeds queries (R5.10), counted (R9.11), under R10.9's timeout."""
    from packages.core.settings import EmbeddingSettings, RetrievalSettings

    settings = AIWorkerSettings(
        embedding=EmbeddingSettings(model_name="embed-test"),
        retrieval=RetrievalSettings(retrieval_timeout_ms=750),
    )
    res = fake_worker_resources(settings)
    retriever = build_consumers(res, token_counter=TokenCounter())[0].context_builder.retriever

    assert isinstance(retriever, HybridRetriever) and retriever.embedder is not None
    assert retriever.embedder.model_name == "embed-test"
    assert retriever.embedder.dimension == settings.embedding.dimension
    assert retriever.lexical_timeout_seconds == retriever.vector_timeout_seconds == 0.75
    await retriever.embedder.embed_query("password reset")
    assert res.metrics.embedding_tokens_total.labels(model="embed-test")._value.get() > 0


async def test_build_components_checks_the_vector_dimension_and_closes_the_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.ai_worker.main as ai_main

    checked: list[int | None] = []

    async def _check(_dsn: object, configured_dimension: int | None = None) -> None:
        checked.append(configured_dimension)

    monkeypatch.setattr(ai_main, "start_token_counter_warmup", lambda: None)
    monkeypatch.setattr(ai_main, "verify_database_vector_dimension", _check)
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)

    await build_components(res)

    assert checked == [settings.embedding.dimension]
```

In the existing `test_build_components_warms_the_counter_and_starts_every_consumer`, add this line after the existing `monkeypatch.setattr(ai_main, "start_token_counter_warmup", ...)` line, so the unit test never opens a database connection:

```python
    async def _no_db_check(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ai_main, "verify_database_vector_dimension", _no_db_check)
```

Append to `tests/unit/test_worker_mains.py`:

```python
async def test_knowledge_worker_counts_ingestion_embedding_tokens() -> None:
    """R9.11: ingestion embeddings are counted in embedding_tokens_total."""
    settings = KnowledgeWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    embedder = knowledge_main.build_consumer(res).pipeline.embedder
    await embedder.embed_texts(["refund policy for annual plans"])

    model = settings.embedding.model_name
    assert res.metrics.embedding_tokens_total.labels(model=model)._value.get() > 0
```

In `tests/unit/test_runtime_image_contract.py`, extend the parameter list of `test_compose_forwards_the_switch_into_app_containers` with `"EMBEDDING__MODEL_NAME"`, `"EMBEDDING__BASE_URL"` and `"EMBEDDING__API_KEY"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_ai_worker_main.py tests/unit/test_worker_mains.py tests/unit/test_runtime_image_contract.py -q`
Expected: FAIL.
- `test_retriever_embeds_with_the_configured_model_and_timeout` fails with `assert ... retriever.embedder is not None`.
- `test_build_components_checks...` fails with `AttributeError: ... has no attribute 'verify_database_vector_dimension'`.
- `test_knowledge_worker_counts_ingestion_embedding_tokens` fails with `assert 0.0 > 0`.
- The three new compose parameters fail with `assert 'EMBEDDING__MODEL_NAME' in {...}`.

- [ ] **Step 3: Implement the composition**

`services/ai_worker/main.py`:

```python
from packages.db.migrator import verify_database_vector_dimension
from packages.knowledge.embedder import Embedder, get_embedder
```

Change the `build_consumers` signature to take the embedder and default it:

```python
def build_consumers(
    res: WorkerResources,
    *,
    llm_provider: LLMProvider | None = None,
    token_counter: TokenCounter | None = None,
    embedder: Embedder | None = None,
) -> list[AIWorkerConsumer]:
```

At the top of its body (next to where `settings = res.settings` is read), add:

```python
    # The corpus model embeds every query (R5.10); tokens are counted (R9.11).
    query_embedder = embedder or get_embedder(settings.embedding, metrics=res.metrics)
```

Replace the retriever construction:

```python
        retriever=HybridRetriever(
            PostgresSearchBackend(pool=res.db_pool, metrics=res.metrics),
            timeout_seconds=settings.retrieval.retrieval_timeout_ms / 1000,
            default_top_n=settings.retrieval.top_n,
            rrf_k=settings.retrieval.rrf_k,
            metrics=res.metrics,
            embedder=query_embedder,
        ),
```

In `build_components`, first thing in the body, run the R5.10 check. Then build the embedder once, register its `aclose`, and pass it to `build_consumers`:

```python
    settings = res.settings
    # R5.10: refuse to start when EMBEDDING__DIMENSION disagrees with VECTOR(n).
    await verify_database_vector_dimension(
        settings.database.asyncpg_dsn, configured_dimension=settings.embedding.dimension
    )
    embedder = get_embedder(settings.embedding, metrics=res.metrics)
    embedder_close = getattr(embedder, "aclose", None)
    if embedder_close is not None:
        res.shutdown.register_cleanup_callback(embedder_close)
```

Then add `embedder=embedder` to the existing `build_consumers(...)` call in `build_components`.

`services/knowledge_worker/main.py`, `build_consumer`:

```python
    embedder = get_embedder(settings.embedding, metrics=res.metrics)  # R9.11
    embedder_close = getattr(embedder, "aclose", None)
    if embedder_close is not None:
        res.shutdown.register_cleanup_callback(embedder_close)
    pipeline = KnowledgeIngestionPipeline(
        store=PostgresKnowledgeStore(res.db_pool),
        embedder=embedder,
        storage=get_storage_client(settings.object_storage),
        bucket_name=settings.object_storage.bucket_knowledge,
    )
```

`docker-compose.yml`, in `x-app-env` directly after `EMBEDDING__MOCK: ${EMBEDDING__MOCK:-true}`:

```yaml
  # Same embedding model in every service: the corpus and the queries must match (R5.10).
  EMBEDDING__MODEL_NAME: ${EMBEDDING__MODEL_NAME:-text-embedding-3-small}
  EMBEDDING__BASE_URL: ${EMBEDDING__BASE_URL:-https://api.openai.com/v1}
  EMBEDDING__API_KEY: ${EMBEDDING__API_KEY:-}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_ai_worker_main.py tests/unit/test_worker_mains.py tests/unit/test_runtime_image_contract.py -q && docker compose config --quiet && echo compose-valid`
Expected: PASS, then `compose-valid`.

- [ ] **Step 5: Run the composed-worker integration tests**

Run: `uv run pytest tests/integration/test_ai_worker_service_integration.py -q`
Expected: PASS (2 passed). They build through `build_consumers`, which now defaults to the configured `FakeEmbedder`.

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff format services/ai_worker/main.py services/knowledge_worker/main.py tests/unit/test_ai_worker_main.py tests/unit/test_worker_mains.py tests/unit/test_runtime_image_contract.py && uv run ruff check services tests/unit && uv run mypy services tests/unit/test_ai_worker_main.py tests/unit/test_worker_mains.py`
Expected: `All checks passed!` and `Success: no issues found`.

```bash
git add services/ai_worker/main.py services/knowledge_worker/main.py docker-compose.yml tests/unit/test_ai_worker_main.py tests/unit/test_worker_mains.py tests/unit/test_runtime_image_contract.py
git commit -m "feat(ai-worker): embed retrieval queries with the configured model and timeout [task 3.16] [R5.10, R9.11, R10.9]"
```

---

### Task 3: The API's retrieval debug route uses the same guarded path

**Files:**
- Modify: `services/api/main.py` (`create_app`: set `app.state.settings`; lifespan: attach the embedder and close it on shutdown)
- Modify: `services/api/routers/search.py` (step 3 "Dense vector embedding" at lines 125–132; `HybridRetriever(...)` at lines 136–141; `ConstructedQueryDebug(...)` at lines 177–185)
- Test: `tests/unit/test_retrieval_debug_api.py`

**Interfaces:**
- Consumes: Task 1 `HybridRetriever(..., embedder=..., timeout_seconds=...)` and `RetrievalResult.query_vector_dimension`; `APISettings` (subclass of `AppSettings`, so it has `retrieval` and `embedding`); `get_embedder`.
- Produces: `app.state.settings: APISettings`, always set by `create_app`. `app.state.embedder` is set by the lifespan when it is enabled. `EmbedderDep` keeps its `FakeEmbedder` fallback for apps built with `lifespan_enabled=False`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_retrieval_debug_api.py`:

```python
class TestQueryEmbeddingGuard:
    """3.16 / R10.9: the debug route embeds under the configured retrieval timeout."""

    async def test_slow_embedder_does_not_block_the_debug_endpoint(
        self, fake_backend: FakeSearchBackend
    ) -> None:
        import time

        from packages.core.settings import APISettings, RetrievalSettings

        app = create_app(
            settings=APISettings(retrieval=RetrievalSettings(retrieval_timeout_ms=100)),
            lifespan_enabled=False,
        )
        app.state.search_backend = fake_backend
        app.state.embedder = FakeEmbedder(simulate_latency_ms=3000)
        app.state.metrics = create_pipeline_metrics()
        app.state.rerank_service = RerankService(reranker=StubReranker())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            t0 = time.perf_counter()
            resp = await ac.post(
                "/v1/search/debug",
                headers={"X-Organization-ID": str(TEST_ORG_ID)},
                json={"query": "invoice INV-2026-01829 payment", "apply_rerank": False},
            )
            elapsed = time.perf_counter() - t0

        assert resp.status_code == status.HTTP_200_OK
        assert elapsed < 1.5, f"debug endpoint blocked {elapsed:.2f}s behind the embedder"
        body = resp.json()
        assert body["explanation"]["retrieval_degraded"] is True
        assert body["explanation"]["surviving_branch"] == "lexical"
        assert body["constructed_query"]["query_vector_present"] is False

    async def test_embedded_query_dimension_is_reported(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/v1/search/debug",
            headers={"X-Organization-ID": str(TEST_ORG_ID)},
            json={"query": "password reset procedure", "apply_rerank": False},
        )
        assert resp.status_code == status.HTTP_200_OK
        constructed = resp.json()["constructed_query"]
        assert constructed["query_vector_present"] is True
        assert constructed["query_vector_dimension"] == 1536

    def test_create_app_exposes_its_settings(self) -> None:
        from packages.core.settings import APISettings, RetrievalSettings

        settings = APISettings(retrieval=RetrievalSettings(retrieval_timeout_ms=123))
        app = create_app(settings=settings, lifespan_enabled=False)
        assert app.state.settings is settings
```

If the existing tests post to a different path than `/v1/search/debug`, use the path they use (`grep -n "search/debug" tests/unit/test_retrieval_debug_api.py`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_retrieval_debug_api.py -k TestQueryEmbeddingGuard -q`
Expected: FAIL.
- `test_slow_embedder_does_not_block...` fails on `elapsed < 1.5`, because today the route awaits the 3 s embed with no timeout.
- `test_create_app_exposes_its_settings` fails with `AttributeError: 'State' object has no attribute 'settings'`.
- `test_embedded_query_dimension_is_reported` passes today. Keep it: it guards the reporting change in Step 3.

- [ ] **Step 3: Implement**

`services/api/main.py`, in `create_app` directly after the `FastAPI(...)` instance is created:

```python
    app.state.settings = active_settings
```

In `app_lifespan`, after the object-storage block (step 4) and before the `yield`:

```python
        # 5. Query embedder: the corpus model (R5.10), counted (R9.11), used by /search/debug.
        from packages.knowledge.embedder import get_embedder

        embedder = get_embedder(
            active_settings.embedding, metrics=getattr(app_instance.state, "metrics", None)
        )
        app_instance.state.embedder = embedder
```

In the shutdown section after the `yield`, next to the publisher cleanup:

```python
        embedder_close = getattr(embedder, "aclose", None)
        if embedder_close is not None:
            await embedder_close()
```

`services/api/routers/search.py`: replace step 3 so that only an explicit override is applied and the retriever does the embedding:

```python
    # 3. Dense query vector (R10.1): an explicit override wins; otherwise the retriever embeds
    #    semantic_text inside the vector branch, under the retrieval timeout (R10.9).
    if request_data.query_vector is not None:
        query.query_vector = list(request_data.query_vector)
```

Replace the retriever construction:

```python
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
```

In `ConstructedQueryDebug(...)`, report the vector the search actually used:

```python
        query_vector_present=retrieval_result.query_vector_dimension is not None,
        query_vector_dimension=retrieval_result.query_vector_dimension,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_retrieval_debug_api.py -q && uv run pytest tests/integration/test_api_integration.py tests/integration/test_knowledge_api_integration.py -q`
Expected: PASS (the whole debug suite, plus the API integration suites).

- [ ] **Step 5: Check the OpenAPI contract is unchanged**

Run: `uv run python -m services.api.openapi --check` (the CI static-chain step).
Expected: it passes with no drift. Response fields are unchanged; only their values change.

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff format services/api tests/unit/test_retrieval_debug_api.py && uv run ruff check services/api tests/unit/test_retrieval_debug_api.py && uv run mypy services/api tests/unit/test_retrieval_debug_api.py`
Expected: `All checks passed!` and `Success: no issues found`.

```bash
git add services/api/main.py services/api/routers/search.py tests/unit/test_retrieval_debug_api.py
git commit -m "fix(api): embed debug queries in the retriever under the retrieval timeout [task 3.16] [R10.9, R9.11]"
```

---

### Task 4: Documentation, live-stack proof and task record

**Files:**
- Create: `scripts/retrieval_gate.py`
- Modify: `Makefile` (`.PHONY` line 1; a new target after `phase4-gate`)
- Modify: `docs/configuration.md` §2.5 (lines 84–95) and §2.7 (lines 135–145)
- Modify: `specs/tasks.md` (3.16 entry; the R9 and R10 coverage rows)

**Interfaces:**
- Consumes: Tasks 1–3; `scripts/stack_smoke.py` helpers `SmokeFailure`, `check_services`, `seed_tenant`, `inject_email`, `wait_for_state`; API `POST /v1/knowledge/documents` (multipart `file`, `title`, `category`; header `X-Organization-ID`; 202 with `document.id`), `GET /v1/knowledge/documents/{id}` (`status`), `POST /v1/search/debug`.
- Produces: `make retrieval-gate`, which prints `RETRIEVAL GATE OK`.

- [ ] **Step 1: Write the live-stack check**

Create `scripts/retrieval_gate.py`:

```python
"""Live-stack check for task 3.16: queries are embedded and reach the pgvector branch.

Run on the host after `make up` (reads .env like every host tool):

    make retrieval-gate

Checks, stopping at the first failure:
  1. A knowledge document uploaded through the API is ingested to `active`.
  2. /v1/search/debug with a query sharing no words with the document returns it from the
     vector branch: vector_count >= 1, lexical_count == 0, not degraded.
  3. A billing email with no word in common with the document reaches DRAFTED, and its
     CONTEXT_READY event reports retrieved_chunks_count >= 1. The ai-worker could only
     have found the chunk through the embedded query.
Creates one throwaway organization and deletes it at the end. The uploaded file stays in
the knowledge bucket (it is keyed under the deleted organization).
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from uuid import UUID

import aio_pika
import asyncpg
import httpx
from stack_smoke import (
    SmokeFailure,
    build_mime,
    check_services,
    inject_email,
    seed_tenant,
    wait_for_state,
)

from packages.core.settings import AppSettings
from packages.core.storage import get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.domain.state_machine import JobState

API = "http://localhost:8000/v1"
TIMEOUT_S = 60.0
DOCUMENT = (
    "Aurora teapot calibration: rotate the blue valve clockwise until the gauge settles, "
    "then log the reading in the maintenance ledger."
)
QUERY = "Which colour valve adjusts the Aurora gauge?"


async def upload_and_wait(http: httpx.AsyncClient, org_id: UUID) -> str:
    headers = {"X-Organization-ID": str(org_id)}
    resp = await http.post(
        f"{API}/knowledge/documents",
        headers=headers,
        files={"file": ("aurora.txt", DOCUMENT.encode(), "text/plain")},
        data={"title": "Aurora teapot calibration", "category": "billing"},
    )
    if resp.status_code != 202:
        raise SmokeFailure(f"upload returned {resp.status_code}: {resp.text}")
    doc_id = str(resp.json()["document"]["id"])
    deadline = time.monotonic() + TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        got = await http.get(f"{API}/knowledge/documents/{doc_id}", headers=headers)
        status = got.json().get("status") if got.status_code == 200 else None
        if status == "active":
            return doc_id
        if status == "failed":
            break
        await asyncio.sleep(1.0)
    raise SmokeFailure(f"document {doc_id} ended in {status!r}, expected 'active'")


async def check_debug(http: httpx.AsyncClient, org_id: UUID) -> None:
    resp = await http.post(
        f"{API}/search/debug",
        headers={"X-Organization-ID": str(org_id)},
        json={"query": QUERY, "category": "billing", "apply_rerank": False},
    )
    if resp.status_code != 200:
        raise SmokeFailure(f"/search/debug returned {resp.status_code}: {resp.text}")
    body = resp.json()
    ex = body["explanation"]
    if ex["retrieval_degraded"] or ex["vector_count"] < 1:
        raise SmokeFailure(f"vector branch found nothing: {ex}")
    print(
        f"ok   /search/debug -> vector_count {ex['vector_count']}, lexical_count "
        f"{ex['lexical_count']}, query vector dim "
        f"{body['constructed_query']['query_vector_dimension']}"
    )


async def check_ai_worker(
    settings: AppSettings, pool: asyncpg.Pool[Any], channel: Any, org_id: UUID, mailbox: UUID
) -> None:
    storage = get_storage_client(settings.object_storage)
    raw_keys: list[str] = []
    job_id = await inject_email(
        settings,
        pool,
        storage,
        channel,
        org_id,
        mailbox,
        build_mime(
            "client@enterprise.example.com",
            "Overdue payment failure on account",
            "Our account balance is past due after a payment failure. Please advise.",
        ),
        raw_keys,
    )
    await wait_for_state(pool, org_id, job_id, {JobState.DRAFTED.value})
    count = await pool.fetchval(
        "SELECT (payload->>'retrieved_chunks_count')::int FROM processing_event "
        "WHERE job_id = $1 AND state_to = 'CONTEXT_READY' ORDER BY created_at LIMIT 1",
        job_id,
    )
    for key in raw_keys:
        try:
            await storage.delete_object(settings.object_storage.bucket_raw_mime, key)
        except Exception as err:
            print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
    if not count:
        raise SmokeFailure(f"job {job_id} retrieved {count!r} chunks, expected >= 1")
    print(f"ok   billing email -> ai-worker retrieved {count} chunk(s) via the embedded query")


async def run() -> None:
    settings = AppSettings()
    await check_services(settings)
    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    org_id: UUID | None = None
    try:
        channel = await connection.channel(on_return_raises=True)
        org_id, mailbox_id = await seed_tenant(pool)
        async with httpx.AsyncClient(timeout=10.0) as http:
            doc_id = await upload_and_wait(http, org_id)
            print(f"ok   document {doc_id} ingested -> active")
            await check_debug(http, org_id)
        await check_ai_worker(settings, pool, channel, org_id, mailbox_id)
    finally:
        if org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", org_id)
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("RETRIEVAL GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`Makefile`: append ` retrieval-gate` to the `.PHONY` line, and add after the `phase4-gate` target:

```make
retrieval-gate:
	$(UV) run python scripts/retrieval_gate.py
```

- [ ] **Step 2: Check the script statically**

Run: `uv run ruff format scripts/retrieval_gate.py && uv run ruff check scripts/retrieval_gate.py && uv run mypy --strict scripts/retrieval_gate.py scripts/stack_smoke.py && (cd scripts && uv run python -c "import retrieval_gate")`
Expected: `All checks passed!`, `Success: no issues found in 2 source files`, and no import error.

- [ ] **Step 3: Update the configuration docs**

`docs/configuration.md` §2.5: under the section heading's italic description line, add:

```markdown
The ai-worker and the API embed retrieval queries with this model; the knowledge worker embeds the corpus with it. Every service must use the same `EMBEDDING__MODEL_NAME` and `EMBEDDING__DIMENSION` (R5.10; the ai-worker and knowledge worker refuse to start on a dimension mismatch). Under Docker Compose, `EMBEDDING__MOCK`, `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL` and `EMBEDDING__API_KEY` are forwarded into the app containers. Query and ingestion tokens are both counted in `embedding_tokens_total{model}` (R9.11).
```

§2.7: replace the description cell of `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` with:

```markdown
Per-branch retrieval timeout in milliseconds (R10.9). The vector branch's budget covers query embedding plus the ANN search; a branch that exceeds it is dropped and retrieval continues on the other branch (`retrieval_degraded=true`, R10.6). Used by the ai-worker and `/v1/search/debug`.
```

- [ ] **Step 4: Run the full suite**

Run: `make ci > /tmp/ci-3.16.log 2>&1; echo "exit $?"; grep -E "passed|failed|Success|All checks" /tmp/ci-3.16.log | tail -4`
Expected: `exit 0`; unit and integration counts are at least the previous 1322 / 156, plus the new tests.

- [ ] **Step 5: Live-stack proof (the user runs these commands)**

The executing agent cannot restart the stack. Hand these commands to the user and wait for the output:

1. `make up`: rebuilds images with Tasks 1–3.
2. `make retrieval-gate`. Expected output:

```
ok   api ready; consumers on mail.sync.requested, email.normalize, email.triage, knowledge.ingest
ok   document <uuid> ingested -> active
ok   /search/debug -> vector_count 1, lexical_count 0, query vector dim 1536
ok   billing email -> ai-worker retrieved 1 chunk(s) via the embedded query
RETRIEVAL GATE OK
```

3. `make smoke` and `make phase4-gate`: regression. Both must still end `SMOKE OK` and `PHASE 4 GATE (default) OK`.

After the user's run, verify read-only: `docker compose exec -T ai-worker curl -s localhost:8004/metrics | grep '^embedding_tokens_total'` must show a positive value for the configured model.

- [ ] **Step 6: Record the task**

`specs/tasks.md`, in the 3.16 entry: change `- [ ]` to `- [~]` and add, above its `_Requirements` line:

```markdown
  - Implemented: `HybridRetriever` embeds `semantic_text` inside the vector branch when a query has no vector. Embedding and the ANN search share the vector-branch timeout (`RETRIEVAL__RETRIEVAL_TIMEOUT_MS`, now honoured by the ai-worker and `/v1/search/debug`). An embedder error, timeout, or unusable vector (wrong length or all zeros) fails only the vector branch, and retrieval degrades to lexical (R10.6). The ai-worker and the API use the configured embedder (the corpus model, R5.10) with metrics, so query and ingestion tokens are counted (R9.11). The ai-worker checks the vector dimension at startup, and compose forwards the embedding model, URL and key.
  - Proof: unit tests for embed, timeout, error, unusable vector, empty text, no re-embed, no mutation and token count; live pgvector integration (vector-only hit, tenant-isolated); live stack `make retrieval-gate`.
  - Left: completion audit, then `[x]`.
```

Add `3.16` to the R9 and R10 rows of the coverage table (`grep -n "^| R9 \|^| R10 " specs/tasks.md`).

- [ ] **Step 7: Commit**

```bash
git add scripts/retrieval_gate.py Makefile docs/configuration.md specs/tasks.md
git commit -m "docs(retrieval): document query embedding and add the live retrieval gate [task 3.16] [R10.1, R10.9, R9.11]"
```

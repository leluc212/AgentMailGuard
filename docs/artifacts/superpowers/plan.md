# Implementation Plan: Task 3.15 Retrieval Debug Endpoint

## Goal
Implement and verify **Task 3.15: Retrieval debug endpoint**, fulfilling requirements **R23.3, R23.1, R23.6, R10.8**, and satisfying the **Phase 3 gate**:
- Implement `POST /v1/search/debug` returning:
  - Constructed query (synthesized semantic text, lexical text with identifiers, filters) (R12.1–R12.6, R23.3).
  - Both branch candidate lists with ranks and raw branch scores (lexical and vector) (R10.8, R23.3).
  - RRF fused results with fused scores (R10.3, R23.3).
  - Rerank results with semantic cross-encoder scores (R11.1, R23.3).
  - Final selected candidate chunks (R11.3, R23.3).
  - Diagnostic explanation (degradation flags, surviving branch, rerank fallbacks, and component latencies).
- Integrate into versioned REST API (`/v1/search/debug`) with OpenAPI 3.1 documentation (R23.1).
- Enforce mandatory tenant scoping via `X-Organization-ID` (R23.6, R5.3).

---

## Architecture & Data Flow

```
POST /v1/search/debug (Header: X-Organization-ID)
              │
              ▼
   RetrievalQueryBuilder.build()
              │
              ├──► constructed_query (semantic_text, lexical_text, identifiers, filters)
              ▼
   Embedder.embed_query(semantic_text)
              │
              ├──► query_vector (dense embedding)
              ▼
   HybridRetriever.retrieve()
              │
              ├──► lexical_candidates (lexical_rank, lexical_score)
              ├──► vector_candidates (vector_rank, vector_score)
              └──► fused_candidates (fused_score, RRF fusion)
              │
              ▼
   RerankService.rerank()
              │
              ├──► rerank_candidates (rerank_score)
              └──► selected_chunks (top-K chunks)
              │
              ▼
   RetrievalDebugResponse (Full diagnostic explanation & ranking breakdown)
```

---

## Assumptions
1. `services/api` is the authoritative FastAPI REST API service mounted with `/v1` prefix.
2. All `/v1` endpoints enforce tenant organization UUID via `services.api.dependencies.get_organization_id` (R23.6).
3. The debug endpoint operates on either structured email context (`subject`, `body_text`, `intent`, `thread_summary`) or direct search queries (`query`), synthesizing the exact `RetrievalQuery` using `RetrievalQueryBuilder`.
4. Dependencies (`SearchBackend`, `Embedder`, `RerankService`, `RetrievalQueryBuilder`) resolve from `request.app.state` or default to hermetic implementations (`PostgresSearchBackend` or `FakeSearchBackend`, `FakeEmbedder`, `StubReranker`) so no live credentials or external systems are required in CI (CLAUDE.md §8, R24.5).
5. Architecture boundaries are preserved: `services/api` imports from `packages/*`, and `packages/*` never imports `services/*`.

---

## Plan

### Step 1: Define Debug Schemas
- **Files:** `services/api/schemas/search.py`, `services/api/schemas/__init__.py`
- **Changes:**
  - Create `services/api/schemas/search.py`:
    - `RetrievalDebugRequest`: input payload supporting `query`, `subject`, `body_text`, `intent`, `category`, `thread_summary`, `identifiers`, `lexical_terms`, `filters`, `query_vector`, `top_n` (default 20), `top_k` (default 5), `apply_rerank` (default True), `lexical_weight`, `vector_weight`, `rrf_k`.
    - `ConstructedQueryDebug`: representation of synthesized query with identifiers and lexical text.
    - `CandidateDebugItem`: chunk candidate carrying `chunk_id`, `document_id`, `content`, `metadata`, `lexical_rank`, `vector_rank`, `lexical_score`, `vector_score`, `fused_score`, `rerank_score`.
    - `RetrievalExplanation`: diagnostic metadata (`retrieval_degraded`, `surviving_branch`, `rerank_applied`, `rerank_fallback_recorded`, `rerank_fallback_reason`, candidate counts, stage latencies).
    - `RetrievalDebugResponse`: complete response schema containing `organization_id`, `constructed_query`, `lexical_results`, `vector_results`, `fused_results`, `rerank_results`, `selected_chunks`, and `explanation`.
  - Export new schemas in `services/api/schemas/__init__.py`.
- **Verify:** `uv run python -c "from services.api.schemas.search import RetrievalDebugRequest, RetrievalDebugResponse; print('Schemas imported successfully')"`

### Step 2: Add Retrieval Dependencies
- **Files:** `services/api/dependencies.py`
- **Changes:**
  - Add dependency providers for `SearchBackend`, `Embedder`, `RerankService`, and `RetrievalQueryBuilder`:
    - `get_search_backend(request: Request) -> SearchBackend` (resolves `app.state.search_backend`, `app.state.db_pool`, or defaults to `FakeSearchBackend()`).
    - `get_embedder(request: Request) -> Embedder` (resolves `app.state.embedder` or defaults to `FakeEmbedder()`).
    - `get_rerank_service(request: Request) -> RerankService` (resolves `app.state.rerank_service` or defaults to `RerankService(StubReranker())`).
    - `get_retrieval_query_builder(request: Request) -> RetrievalQueryBuilder`.
  - Define annotated dependency aliases: `SearchBackendDep`, `EmbedderDep`, `RerankServiceDep`, `QueryBuilderDep`.
- **Verify:** `uv run ruff check services/api/dependencies.py && uv run mypy services/api/dependencies.py`

### Step 3: Implement Search Router & Mount in V1
- **Files:** `services/api/routers/search.py`, `services/api/routers/v1.py`, `services/api/routers/__init__.py`
- **Changes:**
  - Create `services/api/routers/search.py`:
    - Define `search_router = APIRouter(prefix="/search", tags=["search"])`.
    - Implement `@search_router.post("/debug", response_model=RetrievalDebugResponse, summary="Retrieval Debug Endpoint")`:
      1. Build `RetrievalQuery` using `RetrievalQueryBuilder` and request body.
      2. If `query_vector` is not provided, generate dense vector using `Embedder.embed_query(query.semantic_text)`.
      3. Execute `HybridRetriever(backend=search_backend, metrics=metrics).retrieve(query, limit=top_n)`.
      4. Optionally execute `RerankService.rerank(query=query.semantic_text, candidates=retrieval_result.candidates, organization_id=str(org_id), category=query.category, top_k=top_k)`.
      5. Construct and return `RetrievalDebugResponse` with all branch results, ranks, fused scores, rerank scores, final selection, and explanation.
  - Mount `search_router` in `services/api/routers/v1.py`: `v1_router.include_router(search_router)`.
  - Export `search_router` in `services/api/routers/__init__.py`.
- **Verify:** `uv run ruff check services/api/ && uv run mypy services/api/`

### Step 4: Write Comprehensive Unit & Integration Tests
- **Files:** `tests/unit/test_retrieval_debug_api.py`
- **Changes:**
  - Test tenant scoping: 400 Bad Request if `X-Organization-ID` is missing or invalid UUID (R23.6).
  - Test constructed query: validates exact identifier extraction (`INV-2026-01829`), lexical keywords, and filter binding (R12.3, R23.3).
  - Test branch results and ranks: validates lexical results have `lexical_rank` and `lexical_score`, vector results have `vector_rank` and `vector_score` (R10.8, R23.3).
  - Test fusion and reranking: validates `fused_results` have `fused_score`, and `rerank_results` have `rerank_score` (R10.3, R11.1, R23.3).
  - Test final selection: top-K candidates returned in `selected_chunks` (R11.3, R23.3).
  - Test degradation handling: when one branch fails/times out, endpoint returns 200 OK with `retrieval_degraded=True` and surviving branch candidates (R10.6).
  - Test reranker fallback: when reranker fails, endpoint returns 200 OK with `rerank_fallback_recorded=True` and fallback reason (R11.5).
  - Test OpenAPI spec inclusion: verify `/v1/search/debug` appears in `/openapi.json` with valid OpenAPI 3.1 schema (R23.1).
- **Verify:** `uv run pytest tests/unit/test_retrieval_debug_api.py -v`

### Step 5: Verify Phase 3 Gate & Commit
- **Files:** `specs/tasks.md`
- **Changes:**
  - Run architectural boundary check: `uv run pytest tests/unit/test_dependency_rules.py -v`.
  - Run full test suite: `uv run pytest -m "not slow" -q`.
  - Run linters: `uv run ruff check packages/ services/ tests/ && uv run mypy services/api/ tests/unit/test_retrieval_debug_api.py`.
  - Mark Task 3.15 `[x]` in `specs/tasks.md`.
  - Commit: `feat(api): retrieval debug endpoint [task 3.15] [R23.3]`.
- **Verify:** `git log -1 --stat`

---

## Risks & Mitigations
- **Risk:** Missing dense vector when calling `/v1/search/debug` with text query.
  - *Mitigation:* The endpoint automatically generates embeddings using the injected `Embedder` if `query_vector` is not explicitly supplied in the request.
- **Risk:** Slow execution of debug endpoint during large candidate evaluations.
  - *Mitigation:* Debug queries default to `top_n=20` and `top_k=5`, and branch execution is strictly bounded by per-branch retriever timeouts.
- **Risk:** Leaking internal traceback on branch failure.
  - *Mitigation:* Branch errors degrade gracefully per R10.6, capturing the failure reason in structured `explanation` without crashing the HTTP request.

---

## Rollback Plan
If issues arise, `services/api/routers/search.py`, `services/api/schemas/search.py`, and `tests/unit/test_retrieval_debug_api.py` can be removed, and `services/api/routers/v1.py` and `dependencies.py` reverted via `git checkout`.

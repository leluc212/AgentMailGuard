# Implementation Plan — Phase 3, Task 3.8: PostgresSearchBackend

### Goal
Implement `PostgresSearchBackend` fulfilling requirements **R10.1, R10.2, R10.4, R10.10, R10.11** with inside-branch filtering, configurable top-N, filtered-ANN under-fill mitigation (`ef_search` widening + metrics), hybrid SQL CTE execution, and multi-tenant integration tests on $\ge 3$ tenants.

### Assumptions
- PostgreSQL container runs on port 5433 with database `rag_email` and pgvector extension (v0.8.6).
- `PostgresSearchBackend` implements the `SearchBackend` protocol (`lexical` and `vector`) from Task 3.7.
- `Candidate` carries both ranks and both scores, fused score, content, and metadata per R10.8.

### Plan
1. Implement `PostgresSearchBackend`
   - Files: `packages/retrieval/postgres.py`, `packages/retrieval/__init__.py`
   - Change: Implement `PostgresSearchBackend` with `lexical()`, `vector()`, and `hybrid()` methods. Implement inside-branch filtering (R10.4), under-fill detection with metric export (`retrieval_underfilled_total`), and dynamic search widening (`SET hnsw.ef_search = 200`, `SET hnsw.iterative_scan = 'relaxed_order'`) (R10.10).
   - Verify: `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/`

2. Live PostgreSQL Contract Test Suite Conformance
   - Files: `tests/integration/test_postgres_search_backend.py`
   - Change: Subclass `SearchBackendContractSuite` using `PostgresSearchBackend` against real PostgreSQL container (port 5433).
   - Verify: `uv run pytest tests/integration/test_postgres_search_backend.py -k "TestPostgresSearchBackendContract" -v`

3. Hybrid SQL & Filtered-ANN Under-fill Integration Tests
   - Files: `tests/integration/test_postgres_search_backend.py`
   - Change: Implement tests for `hybrid()` CTE and multi-tenant corpus test with $\ge 3$ tenants with overlapping chunks, asserting target tenant receives full top-N (R10.11) and underfill mitigation activates.
   - Verify: `uv run pytest tests/integration/test_postgres_search_backend.py -v`

4. Verification and Regression Run
   - Files: `specs/tasks.md`
   - Change: Run dependency rules test, linter, and full test suite.
   - Verify: `uv run pytest tests/unit/test_dependency_rules.py -v && uv run pytest -m "not slow" -q`

### Risks & mitigations
- **Risk:** Unrecognized configuration parameter `hnsw.ef_search` before `vector` type is referenced in a connection.
  - **Mitigation:** Execute vector priming query `SELECT '[0]'::vector` or ensure query executes vector operation prior to GUC alteration.
- **Risk:** Multi-tenant vector search returning underfilled results on small `ef_search`.
  - **Mitigation:** Automatic retry with `SET hnsw.ef_search = 200` and `SET hnsw.iterative_scan = 'relaxed_order'` on count < top_n, incrementing `retrieval_underfilled_total`.

### Rollback plan
Revert changes in `packages/retrieval/postgres.py` and `tests/integration/test_postgres_search_backend.py`.

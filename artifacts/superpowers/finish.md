# Finish Summary — Phase 3, Task 3.4: Chunk Persistence & Indexing

## Review Pass
- **Blocker:** None.
- **Major:** None.
- **Minor:** None.
- **Nit:** None.

## Verification Commands & Results
1. Unit Tests:
   `uv run pytest tests/unit/test_chunk_store.py -v`
   Result: **PASS** (7/7 tests passed in 0.13s)
2. Live PostgreSQL Integration Tests:
   `uv run pytest tests/integration/test_chunk_persistence_postgres.py -v`
   Result: **PASS** (6/6 tests passed in 1.72s against PostgreSQL on port 5433)
3. Full Project Test Suite:
   `uv run pytest -m "not slow" -q`
   Result: **PASS** (700 tests passed, 0 failures, 0 regressions)
4. Code Quality & Type Check:
   `uv run ruff check .`
   `uv run mypy packages/db/ packages/domain/ tests/unit/test_chunk_store.py tests/integration/test_chunk_persistence_postgres.py`
   Result: **PASS** (All checks passed, 0 lint/mypy issues across 28 files)
5. Architectural Boundaries:
   `uv run pytest tests/unit/test_dependency_rules.py -v`
   Result: **PASS** (4/4 dependency rules passed)

## Summary of Changes
- **Domain Layer (`packages/domain/knowledge.py`, `packages/domain/__init__.py`):**
  - Added pure dataclass `EmbeddingRecord` entity (`chunk_id`, `organization_id`, `model`, `dim`, `embedding`, `created_at`) aligned with `design.md §6.1` (R5.7).
- **Database Persistence (`packages/db/knowledge.py`, `packages/db/__init__.py`):**
  - Implemented `KnowledgeStore` protocol defining document CRUD, single-transaction chunk + embedding persistence (R9.7), tenant isolation (R5.3), and lexical/vector retrieval.
  - Implemented `PostgresKnowledgeStore` executing chunk insertion with write-time GIN `content_tsv` generation (`setweight(section, 'A') || setweight(content, 'B')`, R9.7, R5.6) and `embedding_record` with `VECTOR(1536)` in one transaction.
  - Implemented `_parse_embedding()` converting `pgvector.Vector` objects seamlessly to Python `list[float]`.
  - Implemented `InMemoryKnowledgeStore` test double with transaction rollback simulation, lexical matching, and vector cosine distance.
- **Test Harness (`tests/unit/test_chunk_store.py`, `tests/integration/test_chunk_persistence_postgres.py`):**
  - 7 unit tests verifying store protocol, document lifecycle, atomic validation, version-scoped deletions, tenant isolation, and search.
  - 6 integration tests verifying real PostgreSQL transactions, rollback on failure, GIN tsvector ranking, HNSW `<=>` similarity, multi-tenant isolation across >=3 tenants, and FK cascading deletion.

## Follow-ups
- Ready to proceed to **Task 3.5: Ingestion pipeline & versioning** (`knowledge.ingest` worker running parse -> chunk -> enrich -> embed -> persist -> `active`, re-ingestion version N+1 atomic flip, checksum deduplication).

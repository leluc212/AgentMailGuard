# Finish Summary — Phase 3, Task 3.6: Knowledge Upload API

## Review Pass
- **Blocker:** None.
- **Major:** None.
- **Minor:** None.
- **Nit:** None.

## Verification Commands & Results
1. Unit Tests:
   `uv run pytest tests/unit/test_knowledge_api.py -v`
   Result: **PASS** (10/10 tests passed in 2.01s)
2. Live PostgreSQL & MinIO Integration Tests:
   `uv run pytest tests/integration/test_knowledge_api_integration.py -v`
   Result: **PASS** (2/2 tests passed in 1.50s against live PostgreSQL and MinIO)
3. Full Project Test Suite:
   `uv run pytest -m "not slow" -q`
   Result: **PASS** (728 tests passed, 0 failures, 0 regressions)
4. OpenAPI 3.1 Specification Validation:
   `uv run python -m services.api.openapi --check`
   Result: **PASS** (21 OpenAPI paths valid)
5. Code Quality & Type Check:
   `uv run ruff check packages/db/knowledge.py services/api/ tests/unit/test_knowledge_api.py tests/integration/test_knowledge_api_integration.py`
   `uv run mypy packages/db/knowledge.py services/api/`
   Result: **PASS** (All checks passed, 0 lint/mypy issues)
6. Architectural Boundaries:
   `uv run pytest tests/unit/test_dependency_rules.py -v`
   Result: **PASS** (4/4 dependency rules passed)

## Summary of Changes
- **Dependency Update (`pyproject.toml`, `uv.lock`):**
  - Added `python-multipart>=0.0.9` for FastAPI multipart form upload handling.
- **Database Store Layer (`packages/db/knowledge.py`):**
  - Added `list_documents(...)` and `delete_document(...)` to `KnowledgeStore` protocol and implementations (`InMemoryKnowledgeStore`, `PostgresKnowledgeStore`).
  - Added tenant-scoped pagination (`limit`, `offset`) and filtering (`status`, `category`) adhering strictly to `WHERE organization_id = $1` (R5.3, R23.7).
- **FastAPI Endpoints & Schemas (`services/api/`):**
  - `services/api/schemas/knowledge.py`: Pydantic V2 response models for document metadata, ingestion status, failure reasons, and pagination.
  - `services/api/dependencies.py`: Registered `KnowledgeStoreDep`.
  - `services/api/routers/knowledge.py`:
    - `POST /v1/knowledge/documents`: Multipart form upload, stores file in MinIO (`bucket_knowledge`), inserts pending record in PostgreSQL, and enqueues ingestion job to `knowledge.ingest` AMQP queue via `MessagePublisher` and `JobEnvelope`, returning HTTP 202 Accepted (R23.7, R5.8).
    - `GET /v1/knowledge/documents`: Paginated list of documents with ingestion status (R23.7, R23.2).
    - `GET /v1/knowledge/documents/{id}`: Single document status inspection.
    - `DELETE /v1/knowledge/documents/{id}`: Document, chunk, and MinIO storage object cleanup.
  - `services/api/routers/v1.py`: Mounted `knowledge_router` under `/v1`.
- **Test Harness (`tests/unit/test_knowledge_api.py`, `tests/integration/test_knowledge_api_integration.py`):**
  - 10 unit tests covering upload validation, unsupported formats, missing headers, queue publishing, pagination, status inspection, and deletion.
  - 2 live PostgreSQL + MinIO integration tests verifying full upload-to-deletion lifecycle and multi-tenant isolation across >=3 tenants (GEMINI.md §8).

## Follow-ups
- Ready to proceed to **Task 3.7: SearchBackend interface** (`Protocol with lexical() and vector() returning Candidate objects carrying both ranks and both scores; contract test suite`).

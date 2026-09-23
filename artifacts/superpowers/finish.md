# Finish Summary — Phase 3, Task 3.5: Ingestion Pipeline & Versioning

## Review Pass
- **Blocker:** None.
- **Major:** None.
- **Minor:** None.
- **Nit:** None.

## Verification Commands & Results
1. Unit Tests:
   `uv run pytest tests/unit/test_knowledge_pipeline.py tests/unit/test_knowledge_consumer.py -v`
   Result: **PASS** (14/14 tests passed in 0.95s)
2. Live PostgreSQL & MinIO Integration Tests:
   `uv run pytest tests/integration/test_knowledge_worker_e2e.py -v`
   Result: **PASS** (2/2 tests passed in 1.40s against live PostgreSQL and MinIO)
3. Full Project Test Suite:
   `uv run pytest -m "not slow" -q`
   Result: **PASS** (716 tests passed, 0 failures, 0 regressions)
4. Code Quality & Type Check:
   `uv run ruff check packages/knowledge/ services/knowledge_worker/ tests/unit/test_knowledge_pipeline.py tests/unit/test_knowledge_consumer.py tests/integration/test_knowledge_worker_e2e.py`
   `uv run mypy packages/knowledge/pipeline.py services/knowledge_worker/`
   Result: **PASS** (All checks passed, 0 lint/mypy issues)
5. Architectural Boundaries:
   `uv run pytest tests/unit/test_dependency_rules.py -v`
   Result: **PASS** (4/4 dependency rules passed)

## Summary of Changes
- **Pipeline Orchestration (`packages/knowledge/pipeline.py`, `packages/knowledge/__init__.py`):**
  - Implemented `KnowledgeIngestionPipeline` orchestrating full document lifecycle:
    1. Parse raw bytes or MinIO object (`bucket_knowledge`) into structured elements (R9.1, R9.2).
    2. Structural chunking with version assignment (R9.3–R9.5).
    3. Checksum deduplication across versions carrying forward unchanged vectors (R9.9).
    4. Batch embedding generation via `Embedder.embed_texts` for new/modified chunks only (R9.6, R9.11).
    5. Single-transaction chunk, GIN `content_tsv`, and vector persistence (R9.7).
    6. Atomic status flip to `active` followed by pruning of superseded version chunks (R9.8).
  - Implemented per-document status tracking (`pending`, `parsing`, `chunking`, `embedding`, `active`, `failed`) with failure reasons and rollback on failure (R9.10).
  - Implemented `IngestionResult` dataclass and domain exceptions (`DocumentNotFoundError`, `UnsupportedDocumentTypeError`, `IngestionError`).
- **Worker Consumer & Service Entrypoint (`services/knowledge_worker/`):**
  - Implemented `KnowledgeIngestConsumer` consuming from AMQP queue `knowledge.ingest` with mandatory tenant isolation check (R23.6), fatal error routing for unrecoverable failures (R3.5), and transient error retries.
  - Implemented `services/knowledge_worker/main.py` daemon service with graceful shutdown coordination, database readiness check, and HTTP health server exposing `/healthz`, `/readyz`, and `/metrics` on port 8003.
- **Test Harness (`tests/unit/test_knowledge_pipeline.py`, `tests/unit/test_knowledge_consumer.py`, `tests/integration/test_knowledge_worker_e2e.py`):**
  - 7 pipeline unit tests covering status lifecycle, version N+1 re-ingest, checksum deduplication, and error rollback.
  - 7 consumer unit tests verifying AMQP envelope handling, tenant enforcement, DLX routing for fatal errors, and retry handling.
  - 2 live PostgreSQL + MinIO integration tests verifying end-to-end ingestion, version N+1 atomic switch, deduplication, and strict multi-tenant isolation across >=3 tenants (GEMINI.md §8).

## Follow-ups
- Ready to proceed to **Task 3.6: Knowledge upload API** (`POST /v1/knowledge/documents` upload to object storage + enqueue, `GET /v1/knowledge/documents` with ingestion status).

# Implementation Plan — Phase 3, Task 3.5: Ingestion Pipeline & Versioning

Implement the asynchronous document ingestion engine and `knowledge.ingest` worker orchestrating `parse → chunk → enrich → embed → persist → active`, satisfying criteria **R9.1**, **R9.8**, **R9.9**, and **R9.10**.

## Architecture & Data Flow

```
knowledge.ingest Queue
         │
         ▼
 KnowledgeIngestConsumer
         │
         ▼
 KnowledgeIngestionPipeline
         │
 ┌───────┴──────────────────────────────────────────────────┐
 │ 1. Status: 'parsing'                                     │
 │    Fetch bytes (MinIO/raw) ──▶ DocumentParserRegistry    │
 │                                                          │
 │ 2. Status: 'chunking'                                    │
 │    ParsedDocument ──▶ StructuralChunker (R9.3, R9.4)     │
 │    Enrich chunks with metadata + content_checksum (R9.5) │
 │                                                          │
 │ 3. Status: 'embedding' (R9.8, R9.9)                      │
 │    Check prev version: if checksum matches ──▶ REUSE EMB │
 │    Unchanged checksums skip embedder (zero cost!)        │
 │    New/modified chunks ──▶ Embedder.embed_batch()        │
 │                                                          │
 │ 4. Persist Chunks (R9.7, R5.6, R5.7)                     │
 │    PostgresKnowledgeStore.persist_chunks_with_embeddings │
 │    Single transaction: chunks + tsvector + embeddings    │
 │                                                          │
 │ 5. Status: 'active' (R9.8, R9.10)                        │
 │    Atomically flip doc.version = N+1, status = 'active'  │
 │    Delete superseded version N chunks (no downtime!)     │
 └──────────────────────────────────────────────────────────┘
```

## User Review Required

- **Zero-Downtime Re-ingestion (R9.8):** Version $N+1$ chunks and vectors are persisted and committed in the database before `knowledge_document.version` and `status` flip to active. Only then are superseded version $N$ chunks pruned, ensuring no window where the document is unsearchable.
- **Embedding Deduplication (R9.9):** Chunks with unchanged `content_checksum` across versions carry forward their existing vector embedding from PostgreSQL, skipping embedder API invocations.
- **Status Progression (R9.10):** Document status updates sequentially through `pending → parsing → chunking → embedding → active`, or `failed` with detailed `failure_reason` if an error occurs.

## Proposed Changes

### Knowledge Domain & Pipeline (`packages/knowledge`)
- [pipeline.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/knowledge/pipeline.py): Implement `KnowledgeIngestionPipeline` and `IngestionResult`.
- [__init__.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/knowledge/__init__.py): Export `KnowledgeIngestionPipeline` and `IngestionResult`.

### Knowledge Worker Service (`services/knowledge_worker`)
- [consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/services/knowledge_worker/consumer.py): Implement `KnowledgeIngestConsumer(BaseConsumer)`.
- [main.py](file:///home/ple/Documents/antigravity/dazzling-bose/services/knowledge_worker/main.py): Implement worker entrypoint with health/metrics server and shutdown coordinator.

### Test Suite
- [test_knowledge_pipeline.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_knowledge_pipeline.py): Unit tests for pipeline orchestration, versioning, deduplication, error handling.
- [test_knowledge_consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_knowledge_consumer.py): Unit tests for worker consumer.
- [test_knowledge_worker_e2e.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/integration/test_knowledge_worker_e2e.py): Live integration tests with PostgreSQL, MinIO, and RabbitMQ.

## Verification Plan
1. `uv run pytest tests/unit/test_knowledge_pipeline.py -v`
2. `uv run pytest tests/unit/test_knowledge_consumer.py -v`
3. `uv run pytest tests/integration/test_knowledge_worker_e2e.py -v`
4. `uv run pytest -m "not slow" -q`
5. `uv run ruff check . && uv run mypy packages/knowledge/ services/knowledge_worker/`

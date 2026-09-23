# Implementation Plan — Phase 3, Task 3.4: Chunk Persistence & Indexing

Implement atomic chunk and vector persistence for the Knowledge RAG ingestion pipeline, satisfying criteria **R9.7**, **R5.6**, **R5.7**, and **R5.3**.

## Architecture & Data Flow

```
KnowledgeChunk[] + Embeddings[]
       │
       ▼
 ┌──────────────────────────────────────────────────────────┐
 │  BEGIN TRANSACTION (PostgresKnowledgeStore)              │
 │                                                          │
 │  1. INSERT INTO knowledge_chunk                          │
 │     - Content, section, heading_path, metadata           │
 │     - content_tsv: setweight(section, 'A')               │
 │                    || setweight(content, 'B') (R9.7)     │
 │     - GIN indexed (R5.6)                                 │
 │                                                          │
 │  2. INSERT INTO embedding_record                         │
 │     - chunk_id (FK CASCADE), model, dim                  │
 │     - embedding: VECTOR(1536) (R5.7)                     │
 │     - HNSW cosine indexed                                │
 │                                                          │
 │  [FAIL on any error? ROLLBACK entire batch cleanly]      │
 │  [SUCCESS? COMMIT both chunk and vector together]        │
 └──────────────────────────────────────────────────────────┘
```

## User Review Required

- All writes execute within a single PostgreSQL transaction (`async with conn.transaction():`) ensuring no orphaned chunks without embeddings or vice-versa.
- Every query carries `organization_id` matching strict multi-tenant requirements (R5.3).
- `content_tsv` is computed dynamically in SQL at write time using `setweight(..., 'A') || setweight(..., 'B')`, giving section headings higher ranking.

## Proposed Changes

### Domain Entities (`packages/domain`)
- [knowledge.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/domain/knowledge.py): Define `EmbeddingRecord` entity.

### Database Persistence Layer (`packages/db`)
- [knowledge.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/db/knowledge.py): Implement `KnowledgeStore` Protocol, `PostgresKnowledgeStore`, and `InMemoryKnowledgeStore`.
- [__init__.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/db/__init__.py): Export `KnowledgeStore`, `PostgresKnowledgeStore`, and `InMemoryKnowledgeStore`.

### Test Suite
- [test_chunk_store.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_chunk_store.py): Unit test suite for `InMemoryKnowledgeStore`.
- [test_chunk_persistence_postgres.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/integration/test_chunk_persistence_postgres.py): Live PostgreSQL integration test suite for `PostgresKnowledgeStore`.

## Verification Plan
1. `uv run pytest tests/unit/test_chunk_store.py -v`
2. `uv run pytest tests/integration/test_chunk_persistence_postgres.py -v`
3. `uv run pytest tests/`
4. `uv run ruff check packages/db/ packages/domain/ tests/`
5. `uv run mypy packages/db/knowledge.py packages/domain/knowledge.py`

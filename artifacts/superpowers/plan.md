# Implementation Plan — Phase 3, Task 3.6: Knowledge Upload API

Implement the REST API endpoints for knowledge document upload, asynchronous ingestion dispatch, and status querying, satisfying criteria **R23.7**, **R23.2**, and **R5.8**.

## Architecture & Data Flow

```
Client (HTTP Multipart Upload)
         │
         │  POST /v1/knowledge/documents
         │  Headers: X-Organization-ID: <org_id>
         │  Form: file=<bytes>, title="...", category="..."
         ▼
[Knowledge Router (FastAPI)]
         │
         ├─▶ 1. Validate tenant & document MIME/extension (R9.2, R23.6)
         │
         ├─▶ 2. Upload file bytes to MinIO (R5.8)
         │      Key: knowledge/{org_id}/{doc_id}/v{version}/{filename}
         │      Bucket: knowledge-docs
         │
         ├─▶ 3. Insert record in PostgreSQL knowledge_document
         │      status = 'pending', version = 1 (or N+1 for re-ingest)
         │
         ├─▶ 4. Enqueue JobEnvelope to RabbitMQ (R3.1, R9.1)
         │      Exchange: knowledge.ingest
         │      Queue: knowledge.ingest
         │      Payload: {document_id, filename, content_type}
         │
         └─▶ 5. Return HTTP 202 Accepted with DocumentUploadResponse
```

```
Client (HTTP Status Query)
         │
         │  GET /v1/knowledge/documents?status=active&category=policy
         │  GET /v1/knowledge/documents/{id}
         │  Headers: X-Organization-ID: <org_id>
         ▼
[Knowledge Router (FastAPI)]
         │
         ├─▶ Query PostgreSQL knowledge_document (R5.3, R23.6)
         │   Filtered by organization_id, status, category
         │
         └─▶ Return PaginatedResponse[KnowledgeDocumentResponse] (R23.7, R9.10)
             Showing per-document ingestion status and failure_reason
```

## User Review Required

> [!IMPORTANT]
> - **Multipart Upload (`POST /v1/knowledge/documents`):** Accepts standard multipart form uploads (`UploadFile`) supported by all HTTP clients. Filename and MIME type are extracted and validated against supported formats (PDF, DOCX, HTML, Markdown, Plain Text per R9.2).
> - **Object Storage Key Convention (R5.8):** Files are uploaded to MinIO bucket `knowledge-docs` using the established `ObjectKeyBuilder.knowledge_doc(org_id, doc_id, version, filename)` convention before enqueuing.
> - **Tenant Scoping (R23.6, R5.3):** Every endpoint strictly enforces `X-Organization-ID` scoping. Cross-tenant access attempts return 404 or 400.
> - **Async 202 Accepted Workflow (R23.7):** Upload returns HTTP 202 Accepted with the pending document record and `job_id`. Ingestion progresses asynchronously through the `knowledge.ingest` worker built in Task 3.5.
> - **Ingestion Status Polling (R23.7, R9.10):** `GET /v1/knowledge/documents` and `GET /v1/knowledge/documents/{id}` expose live status (`pending`, `parsing`, `chunking`, `embedding`, `active`, `failed`) and `failure_reason`.

## Proposed Changes

### Database Layer (`packages/db`)

#### [MODIFY] [`packages/db/knowledge.py`](file:///home/ple/Documents/antigravity/dazzling-bose/packages/db/knowledge.py)
- Add `list_documents` to `KnowledgeStore` protocol:
  `async def list_documents(self, organization_id: UUID | str, status: str | None = None, category: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[KnowledgeDocument], int]: ...`
- Add `delete_document` to `KnowledgeStore` protocol:
  `async def delete_document(self, organization_id: UUID | str, document_id: UUID | str) -> bool: ...`
- Implement both methods in `PostgresKnowledgeStore` with parameterized SQL (`LIMIT`, `OFFSET`, `COUNT(*)`).
- Implement both methods in `InMemoryKnowledgeStore` for unit test mocking.

---

### API Schemas (`services/api/schemas`)

#### [NEW] [`services/api/schemas/knowledge.py`](file:///home/ple/Documents/antigravity/dazzling-bose/services/api/schemas/knowledge.py)
- `KnowledgeDocumentResponse`: Complete Pydantic V2 schema exposing `id`, `organization_id`, `title`, `source_uri`, `mime_type`, `category`, `version`, `checksum`, `object_key`, `status`, `failure_reason`, `created_at`, `updated_at`.
- `DocumentUploadResponse`: Schema returning `document: KnowledgeDocumentResponse`, `job_id: str`, `message: str`.
- Helper factory `from_domain(doc: KnowledgeDocument) -> KnowledgeDocumentResponse`.

#### [MODIFY] [`services/api/schemas/__init__.py`](file:///home/ple/Documents/antigravity/dazzling-bose/services/api/schemas/__init__.py)
- Re-export knowledge schemas.

---

### API Dependencies & Router (`services/api`)

#### [MODIFY] [`services/api/dependencies.py`](file:///home/ple/Documents/antigravity/dazzling-bose/services/api/dependencies.py)
- Add `get_knowledge_store(request: Request) -> KnowledgeStore` dependency returning `app.state.knowledge_store`, `PostgresKnowledgeStore(db_pool)`, or `InMemoryKnowledgeStore()`.
- Add `KnowledgeStoreDep = Annotated[Any, Depends(get_knowledge_store)]`.

#### [NEW] [`services/api/routers/knowledge.py`](file:///home/ple/Documents/antigravity/dazzling-bose/services/api/routers/knowledge.py)
- `knowledge_router = APIRouter(prefix="/knowledge/documents", tags=["knowledge"])`:
  - `POST /`: Multipart file upload, MinIO persistence, DB record creation, AMQP job publishing to `knowledge.ingest`, returns HTTP 202.
  - `GET /`: Paginated list of documents with optional `status` and `category` filters, returns `PaginatedResponse[KnowledgeDocumentResponse]`.
  - `GET /{id}`: Single document lookup by ID, returns `KnowledgeDocumentResponse` or 404.
  - `DELETE /{id}`: Document deletion, returns HTTP 204 or 404.

#### [MODIFY] [`services/api/routers/v1.py`](file:///home/ple/Documents/antigravity/dazzling-bose/services/api/routers/v1.py)
- Include `knowledge_router` under `/v1` prefix.

---

### Test Suite (`tests`)

#### [NEW] [`tests/unit/test_knowledge_api.py`](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_knowledge_api.py)
- Test multipart file upload happy path (`POST /v1/knowledge/documents` -> HTTP 202, job enqueued, file stored in fake storage).
- Test unsupported file type rejection (`.exe`, unknown MIME) -> HTTP 400.
- Test empty file rejection -> HTTP 400.
- Test re-ingestion upload with `document_id` -> version increments to 2.
- Test `GET /v1/knowledge/documents` listing with pagination, status filtering, category filtering.
- Test `GET /v1/knowledge/documents/{id}` lookup and 404 behavior.
- Test strict multi-tenant isolation (`X-Organization-ID` mismatch cannot access or list other tenants' documents).
- Test missing `X-Organization-ID` header -> HTTP 400.

#### [NEW] [`tests/integration/test_knowledge_api_integration.py`](file:///home/ple/Documents/antigravity/dazzling-bose/tests/integration/test_knowledge_api_integration.py)
- Live integration tests against real PostgreSQL (port 5433) and MinIO (port 9010):
  - Upload real Markdown and PDF files via `TestClient`.
  - Verify file in MinIO bucket `knowledge-docs`.
  - Verify PostgreSQL `knowledge_document` row and status.
  - Query list and detail endpoints.

---

## Verification Plan

### Automated Tests
1. Unit tests:
   ```bash
   uv run pytest tests/unit/test_knowledge_api.py -v
   ```
2. Integration tests (PostgreSQL + MinIO):
   ```bash
   uv run pytest tests/integration/test_knowledge_api_integration.py -v
   ```
3. Full regression suite:
   ```bash
   uv run pytest -m "not slow" -q
   ```
4. Code quality & type checks:
   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run mypy services/api/ packages/db/
   ```
5. OpenAPI spec validation:
   ```bash
   python -m services.api.openapi --check
   ```

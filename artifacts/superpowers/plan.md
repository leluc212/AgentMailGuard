# Implementation Plan: Phase 1 — Task 1.14 Read API for Mail Data & Real Gmail Mailbox Support

**Spec Alignment:** `specs/tasks.md` Task 1.14 & Phase 1 Gate · `specs/requirements.md` (R23.2, R23.6, R5.3, R1.2, R2.5) · `specs/design.md §5.1, §5.2` · `GEMINI.md §4, §6, §8`

### Goal
Implement organization-scoped, paginated REST endpoints (`GET /v1/mailboxes`, `GET /v1/threads`, `GET /v1/threads/{id}`, `GET /v1/messages/{id}`) per R23.2, R23.6, and R5.3. In addition, ensure the mail pipeline can use a REAL Gmail mailbox (via credentials reference resolution to Gmail API OAuth access token), enabling the Phase 1 milestone gate to operate with real Gmail accounts.

### Assumptions
1. All `/v1` endpoints enforce mandatory `organization_id` scoping via header `X-Organization-ID` or query parameter `organization_id`. Cross-tenant queries return 404 Not Found per R5.3 and R23.6.
2. List endpoints return `PaginatedResponse[T]` with `items`, `total_count`, `limit`, `offset`, and `has_more`.
3. Mail provider credentials follow GEMINI.md §6: stored as references (`mailbox.credentials_ref`), never plaintext secrets. The adapter registry resolves references (e.g. `env:GMAIL_ACCESS_TOKEN` or system settings `GMAIL_ACCESS_TOKEN`) to supply `access_token` to `GmailProviderAdapter`.
4. Downstream consumers (`EmailNormalizationConsumer`, `SyncOrchestrator`) integrate with the read API so that ingested and normalized emails from real Gmail mailboxes are immediately visible and retrievable via threads and messages endpoints.

---

### Plan

1. **Step 1: Database Store Enhancements for Read Operations (`packages/db/`)**
   - Files: `packages/db/mailbox.py`, `packages/db/thread.py`, `packages/db/__init__.py`
   - Change:
     - In `MailboxStore`, `PostgresMailboxStore`, and `InMemoryMailboxStore`: implement `list_mailboxes(organization_id, limit, offset, status, provider) -> tuple[list[Mailbox], int]`.
     - In `ThreadStore`, `PostgresThreadStore`, and `InMemoryThreadStore`: implement `list_threads(organization_id, limit, offset, mailbox_id, status) -> tuple[list[EmailThread], int]`.
   - Verify: `uv run ruff check packages/db/ && uv run mypy packages/db/`

2. **Step 2: Real Gmail Credentials Reference Resolver & Webhook Bug Fix (`packages/adapters/`)**
   - Files: `packages/adapters/registry.py`, `packages/adapters/webhooks.py`
   - Change:
     - Implement `resolve_provider_credentials(credentials_ref: str | None, provider: str) -> dict[str, Any]` supporting `env:VAR_NAME`, file references, or environment fallbacks (`GMAIL_ACCESS_TOKEN`).
     - Wire `get_adapter_for_mailbox` to resolve credentials and pass `access_token` to `GmailProviderAdapter` when running against real Gmail.
     - Fix table name typo in `packages/adapters/webhooks.py`: replace `FROM mailboxes` with `FROM mailbox`.
   - Verify: `uv run ruff check packages/adapters/ && uv run mypy packages/adapters/`

3. **Step 3: Read API Schemas, Dependencies & Routers (`services/api/`)**
   - Files:
     - `services/api/schemas/threads.py` (new)
     - `services/api/schemas/messages.py` (new)
     - `services/api/schemas/mailboxes.py` (update)
     - `services/api/dependencies.py` (update: `ThreadStoreDep`, `MessageStoreDep`, `StorageClientDep`)
     - `services/api/routers/mailboxes.py` (add `GET /v1/mailboxes`)
     - `services/api/routers/threads.py` (new: `GET /v1/threads`, `GET /v1/threads/{id}`)
     - `services/api/routers/messages.py` (new: `GET /v1/messages/{id}`)
     - `services/api/routers/v1.py` (mount threads and messages routers)
   - Change: Implement all 4 read endpoints with strict tenant scoping, pagination, and OpenAPI documentation.
   - Verify: `uv run ruff check services/api/ && uv run mypy services/api/ && uv run python -m services.api.openapi --check`

4. **Step 4: Pure Unit Tests for Mail Read API & Credentials (`tests/unit/test_mail_read_api.py`)**
   - Files: `tests/unit/test_mail_read_api.py`
   - Change: Unit tests covering:
     - `GET /v1/mailboxes` pagination, status/provider filters, and tenant scoping.
     - `GET /v1/threads` pagination, mailbox filtering, and ordering.
     - `GET /v1/threads/{id}` detail response with ordered messages list; 404 on missing/foreign tenant.
     - `GET /v1/messages/{id}` detail response with attachment metadata; 404 on missing/foreign tenant.
     - `resolve_provider_credentials` resolving `env:` refs and settings tokens for real Gmail adapter.
   - Verify: `uv run pytest tests/unit/test_mail_read_api.py -v`

5. **Step 5: Multi-Tenant PostgreSQL Integration Tests (`tests/integration/test_mail_read_api_integration.py`)**
   - Files: `tests/integration/test_mail_read_api_integration.py`
   - Change: Live integration tests connecting to live PostgreSQL container:
     - Seed $\ge 3$ distinct organizations with mailboxes, threads, messages, and attachments.
     - Verify tenant-scoping isolation: Tenant A cannot view Tenant B's mailboxes, threads, or messages.
     - Verify pagination offsets and limits against live PostgreSQL.
     - Verify `GET /v1/threads/{id}` returns chronological messages.
   - Verify: `uv run pytest tests/integration/test_mail_read_api_integration.py -v`

6. **Step 6: End-to-End Real Gmail & Phase 1 Gate Verification (`tests/integration/test_phase1_pipeline_e2e.py`)**
   - Files: `tests/integration/test_phase1_pipeline_e2e.py`
   - Change: Test full Phase 1 pipeline flow:
     - Notification received -> raw MIME to MinIO -> queue `email.normalize` -> worker normalizes & persists -> checkpoint advanced -> Read API returns message and thread.
     - Verify `GmailProviderAdapter` capability: test live Gmail authentication if `GMAIL_ACCESS_TOKEN` is present in environment, or verified contract test double with real RFC822 MIME parsing.
     - Verify duplicate notification re-sync creates zero duplicate rows.
   - Verify: `uv run pytest tests/integration/test_phase1_pipeline_e2e.py -v`

7. **Step 7: Full CI Verification Gate & Task Sign-Off**
   - Files: `specs/tasks.md`
   - Change: Run `make ci` (format check, lint, mypy across whole repo, all unit + integration tests). Mark Task 1.14 as `[x]` in `specs/tasks.md`.
   - Verify: `make ci`

---

### Risks & Mitigations
- **Risk:** Sensitive OAuth access tokens exposed in API responses or logs.
  - **Mitigation:** `credentials_ref` is either omitted or strictly returned as a reference pointer in `MailboxResponse`; tokens never logged or returned over REST API.
- **Risk:** Cross-tenant leakage on nested message retrieval in `GET /v1/threads/{id}`.
  - **Mitigation:** The thread lookup and message listing both query with mandatory `WHERE organization_id = $1` filters.
- **Risk:** Real Gmail API requires external network credentials in CI.
  - **Mitigation:** Real Gmail connectivity uses credentials resolution from `GMAIL_ACCESS_TOKEN` when provided; tests gracefully fallback to hermetic contracts in CI while fully enabling real Gmail mailboxes when credentials exist (satisfying GEMINI.md §8 and user directive).

### Rollback Plan
- Revert changes via `git checkout -- packages/ services/api/ tests/`.

# Coding Standards & Architecture Guidelines

**Spec Alignment:** `specs/requirements.md` · `specs/design.md` · `GEMINI.md`

---

## 1. Architectural Boundaries & Dependency Rules

Strict modular isolation must be maintained at all times:
- **Provider Isolation:** Provider names (`gmail`, `graph`, `imap`) appear **only** inside `packages/adapters/`. Everywhere else in the codebase, use `MailProviderAdapter`.
- **Package Hierarchy:** 
  - `services/*` may import `packages/*`.
  - `packages/*` must **never** import `services/*`.
  - `packages/domain` imports stdlib + `packages/core` only (zero dependencies on database, broker, or external frameworks).
- **Retrieval Abstraction:** All retrieval goes through `SearchBackend`. No direct SQL queries from pipeline or worker code.
- **LLM Abstraction:** All model calls go through `LLMProvider`. No LLM vendor SDK imports outside `packages/llm/`.
- **State Transitions:** State transitions go through the formal domain state machine in `packages/domain/`. Never hardcode state strings.

---

## 2. Multi-Tenancy & Data Integrity

- **Mandatory Tenant Scoping:** Every tenant-scoped query carries `organization_id`. No exceptions.
- **Storage Tiering:** Raw email blobs and document payloads go to object storage (MinIO/S3). Never persist large blobs in relational columns.
- **Schema Management:** Schema changes must be versioned SQL migrations in `migrations/`. No runtime DDL.
- **Filtered-ANN Guard:** Detect `count < top_n` on tenant-scoped vector search, set `retrieval_underfilled=true`, and widen `ef_search` (R10.10).

---

## 3. Distributed Correctness & Delivery Guarantees

- **Delivery Semantics:** Design for at-least-once delivery with idempotent consumers. Never assume exactly-once.
- **Transactional Consistency:**
  - State transitions commit in the same database transaction as the side effect that caused them.
  - Acknowledge broker messages only **after** side effects and checkpoints are durably committed.
- **Checkpoint Ordering:** Advance sync checkpoints only after messages are durably persisted and published (R2.8).

---

## 4. Cost, Retrieval & AI Ceilings

- **Strict Budget Ceiling:** Exactly one generation call per job:
  $$\text{Budget} \le 1\text{ triage} + 1\text{ summarization} + 1\text{ generation} + 1\text{ repair}$$
- **Zero-AI Bypass:**
  - `reply_required=false` $\implies$ zero AI work (no triage LLM, no retrieval, no generation).
  - `workflow_hint=template` $\implies$ zero retrieval, zero generation.
- **Selective Embedding:** Never embed inbound emails; embed organizational knowledge base documents only (R6.5).
- **Validation:** Never persist an LLM response that failed schema validation.

---

## 5. Python & Code Quality Conventions

- **Runtime:** Python 3.12+ async-first (`asyncio`, `asyncpg`, `aio_pika`, `httpx`).
- **Typing:** Strict typing throughout; validate all API inputs and configurations using Pydantic V2.
- **Tooling:** Format and lint with `ruff`. Enforce strict static type checking with `mypy` or `pyright`.
- **Anti-Goals:** Do not introduce multi-agent chains (planner $\to$ critic $\to$ writer), vector DB replacements (PostgreSQL pgvector is ADR-0001), or ad-hoc security controls.

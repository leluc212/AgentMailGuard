# Testing Strategy & Test Isolation

**Spec Alignment:** `specs/requirements.md` · `GEMINI.md §8` · `specs/design.md`

---

## 1. Core Testing Directives

- **Zero Live Dependencies:** No automated test requires live credentials, internet access, or external mail provider services. Ever.
- **Fakes & Stubs:** All external providers and heavy models must use test doubles:
  - Mail providers: `FakeProviderAdapter`
  - LLMs: Deterministic stub `LLMProvider`
  - Embeddings: Stub embedder producing constant or mock vector dimensions.

---

## 2. Multi-Tenant Fixture Mandate

- **3-Tenant Overlap Standard:** Multi-tenant test fixtures must seed **$\ge 3$ tenants with overlapping lexical content**.
- **Rationale:** Single-tenant fixtures fail to catch the filtered-ANN under-fill bug in `pgvector` (where tenant-scoped HNSW returns fewer than `top_n` candidate chunks).

---

## 3. Pure Component Unit Testing

Every pure domain and algorithmic component requires isolated, deterministic unit tests:
- Chunker & text splitting logic
- Reciprocal Rank Fusion (RRF) & reranking scoring
- Rules engine & evaluation conditions
- Domain state machines & transition guards
- SQL and vector query builders
- Idempotency key extractors & dedup filters
- Subject normalizer & quoted-history parser
- Token & cost calculation counters

---

## 4. Test Execution & Coverage Targets

- **Framework:** `pytest` with `pytest-asyncio` and `pytest-mock`.
- **Containers:** Ephemeral PostgreSQL (with `pgvector`) and RabbitMQ instances for integration testing.
- **Coverage:** Aim for 100% coverage on pure domain logic, verified using the `pytest-coverage` skill.

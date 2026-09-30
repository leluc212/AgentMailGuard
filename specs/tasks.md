# Tasks — Enterprise RAG-Based Intelligent Email Management and Response System

**Spec ID:** `rag-email`
**This is the working file.** Read `requirements.md` for the acceptance contract and `design.md` for the blueprint before starting any task.

---

## How to work this file

1. **One task at a time, in order.** Phases build on each other; tasks inside a phase are ordered by dependency.
2. **Before starting:** open the `_Requirements:_` IDs and read them. They are the definition of done, not decoration.
3. **Before marking `[x]`:** the Definition of Done in `requirements.md §0.3` must hold — criteria implemented, tests written, stack still boots, config documented, logs/metrics emitted, CI green.
4. **If a task seems to need something out of scope** (auth, encryption, DLP, prompt-injection defence, compliance — see `requirements.md §0.5`), **stop and raise it**. Do not improvise a security design.
5. **If reality contradicts the design**, update `design.md` and record an ADR in `docs/adr/`. Do not silently diverge.
6. **Never skip the phase gate.** Each phase ends with a gate that must pass before the next phase starts.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked (add a note)

---

## Phase map

| Phase | Theme | Deliverable |
|---|---|---|
| 0 | Foundation & infrastructure | `docker compose up` boots a healthy empty stack |
| 1 | Core mail pipeline | Real Gmail → internal database |
| 2 | Triage & queue architecture | Email → category queue, with early exit |
| 3 | Knowledge RAG | Email → relevant organizational knowledge |
| 4 | Context & generation | Email + thread + RAG → generated draft |
| 5 | Business data integration | Knowledge + live operational data → response |
| 6 | Mail dispatch & review UI | Real email → pipeline → reply in provider mailbox |
| 7 | Observability & evaluation | Quantitative evidence for H1–H5 |
| 8 | Hardening & scale | Burst absorption, recovery, migration path documented |

---

# Phase 0 — Foundation & Infrastructure

*Goal: a skeleton that boots, migrates, tests, and observes itself — before any business logic exists.*

- [x] **0.1 Repository scaffold**
  - Create the layout from `design.md §4` (`services/`, `packages/`, `migrations/`, `evaluation/`, `tests/`, `docs/`).
  - One dependency manifest per service language; pinned versions.
  - Enforce the dependency rule: `services/*` → `packages/*` only; `packages/domain` imports nothing but stdlib + `packages/core`.
  - `Makefile` targets: `up`, `down`, `migrate`, `seed`, `test`, `lint`, `eval`, `load`.
  - _Requirements: R24.1_

- [x] **0.2 Configuration & settings**
  - Validated settings object per service, loaded from environment, fails fast on missing/invalid values.
  - Groups: database, broker, object storage, provider credential refs, embedding model + dimension, LLM tiers + price table, retrieval params, triage thresholds, summarization thresholds, retry ladder, worker concurrency.
  - Write `.env.example` and `docs/configuration.md` with every key and its default.
  - _Requirements: R20.6_

- [x] **0.3 Docker Compose stack**
  - Services: `postgres` (with `pgvector`), `rabbitmq` (management UI), `minio`, `prometheus`, `grafana`, plus placeholder containers for `api` and each worker.
  - Healthchecks and `depends_on` ordering so `make up` yields a healthy stack.
  - _Requirements: R20.1_

- [x] **0.4 Database migrations & core schema**
  - Versioned, reversible migration tooling; no runtime DDL.
  - Enable `vector` and `pg_trgm`; create all entities from `design.md §6.1`.
  - Uniqueness on `(organization_id, mailbox_id, provider_message_id)` and `(organization_id, mailbox_id, provider_thread_id)`.
  - GIN index on `tsvector` columns; HNSW index on `embedding_record.embedding`.
  - Startup assertion: configured embedding dimension == `VECTOR(n)` column width, else refuse to start.
  - _Requirements: R5.1, R5.2, R5.3, R5.4, R5.5, R5.6, R5.7, R5.10_

- [x] **0.5 Object storage client**
  - MinIO/S3 wrapper with bucket bootstrap and key conventions for raw MIME, attachments, and source documents.
  - _Requirements: R5.8_

- [x] **0.6 Domain layer: entities & processing state machine**
  - Pure dataclasses for `NormalizedMessage`, `Classification`, `ContextPackage`, `Candidate`, `Job`.
  - Transition table from `design.md §8`; illegal transitions raise.
  - `processing_event` written on every transition, in the same transaction as the side effect.
  - Unit tests covering every legal transition and a representative set of illegal ones.
  - _Requirements: R18.1, R18.2, R18.3, R18.4, R18.5, R24.3_

- [x] **0.7 Broker foundation**
  - Topology declaration in code (idempotent) per `design.md §7.1`: exchanges, queues, DLX, retry queues.
  - Publisher with persistent delivery; consumer base class with manual ack and configurable prefetch.
  - Retry ladder (30s / 5m / 30m via TTL + DLX) and terminal dead-lettering with failure headers.
  - _Requirements: R3.1, R3.2, R3.3, R3.4, R3.5, R3.8_

- [x] **0.8 Idempotency utility**
  - Deterministic key `sha256(org:mailbox:provider_message_id:operation_type)`.
  - `execute_once()` helper with app-level short-circuit plus `UNIQUE` constraint as the real guarantee; handles the concurrent-race `IntegrityError` path.
  - Unit tests: repeat execution, concurrent execution, partial failure.
  - _Requirements: R19.1, R19.2, R19.3, R19.4_

- [x] **0.9 Observability skeleton**
  - Structured JSON logging with `trace_id, message_id, thread_id, job_id, organization_id`.
  - OpenTelemetry initialization; trace context injected into the job envelope and restored on consume.
  - Prometheus registry and `/metrics` endpoint on every service.
  - `/healthz` and `/readyz` on every service; graceful shutdown (stop consuming → drain → exit).
  - _Requirements: R21.1, R21.2, R21.3, R20.7, R20.8_

- [x] **0.10 API skeleton**
  - FastAPI app, `/v1` router prefix, generated OpenAPI, pagination helper, mandatory `organization_id` scoping.
  - _Requirements: R23.1, R23.6_

- [x] **0.11 CI pipeline & test harness**
  - Format, lint, static type check; unit + integration jobs.
  - Ephemeral PostgreSQL and RabbitMQ containers for integration tests.
  - All external calls (provider, LLM, embedding) stubbed — no live credentials anywhere in CI.
  - _Requirements: R24.2, R24.4, R24.5_

- [x] **0.12 Seed & fixture loader**
  - Demo organization, mailboxes, a small knowledge corpus, and business records.
  - Fixture email set (varied: support, billing, newsletter, auto-reply, long thread, identifier-bearing) reused by tests and evaluation.
  - _Requirements: R5.9_

- [x] **0.13 Benchmark dataset construction (seed versions)**
  - **This must exist before Phase 2.** Task 2.3 trains the ML classifier and the Phase 3 gate measures retrieval — neither is possible without labelled data. Building it in Phase 7 is a circular dependency.
  - Classification seed set: ≥300 labelled emails (email → gold category/intent/`reply_required`/`workflow_hint`) with a frozen train/test split, covering every category in R6.4 and every rule in 2.2.
  - Retrieval seed set: ≥100 queries → gold chunk ids, deliberately split between natural-language questions and identifier-bearing queries (`INV-…`, `ORDER-…`, `INC…`) so the hybrid-vs-vector comparison has signal.
  - Version both sets and record the construction method in `evaluation/README.md`.
  - Phase 7 (task 7.5) **expands and re-annotates** these sets; it does not create them.
  - _Requirements: R22.1, R22.2, R5.9_

> **Phase 0 gate:** `make up && make migrate && make seed && make test` succeeds from a clean checkout. Every service reports ready, exposes `/metrics`, and logs a trace id. No business logic yet.

---

# Phase 1 — Core Mail Pipeline

*Deliverable: real Gmail → internal database.*

- [x] **1.1 Provider adapter interface & registry**
  - `MailProviderAdapter` protocol with all seven methods from `design.md §5.1`.
  - Registry keyed by `mailbox.provider`; assert no provider name appears outside `packages/adapters/`.
  - Common error taxonomy: `RateLimited`, `AuthExpired`, `NotFound`, `Transient`, `Permanent`.
  - Shared contract test suite every adapter must pass.
  - _Requirements: R1.1, R1.3, R1.4, R1.5_

- [x] **1.2 FakeProviderAdapter**
  - Fixture-driven, offline, deterministic; supports sync windows, expired checkpoints, rate limiting, and failure injection.
  - This is the adapter CI and the load harness use — build it before the real ones.
  - _Requirements: R1.7, R24.5_

- [x] **1.3 Gmail adapter**
  - Pub/Sub push notification handling; `history.list()` incremental sync from stored `historyId`; message/thread fetch; expired-history detection.
  - Honour retry-after on rate limiting.
  - _Requirements: R1.2, R1.6, R2.5_

- [x] **1.4 Microsoft Graph adapter**
  - Change-notification subscription handling; delta query following `@odata.nextLink` to the terminal `@odata.deltaLink`; invalid-delta-token detection.
  - _Requirements: R1.2, R2.6_

- [x] **1.5 Webhook receivers**
  - Endpoints for Gmail and Graph, completing each provider's validation handshake.
  - Acknowledge within 5 s; enqueue a sync job; perform **no** provider fetch inside the request.
  - Treat payload as a signal only — never as authoritative state.
  - _Requirements: R2.1, R2.2, R2.3_

- [x] **1.6 Checkpoint store & sync orchestration**
  - `mailbox_checkpoint` read/write; the sync loop from `design.md §5.1`.
  - Checkpoint advanced **only after** fetched messages are durably persisted and published.
  - Bounded full re-sync when the checkpoint is rejected, recording `sync_state='full_resync'`.
  - In-flight coalescing: further notifications for a syncing mailbox collapse to one pending follow-up.
  - _Requirements: R2.4, R2.7, R2.8, R2.9_

- [x] **1.7 Subscription renewal job**
  - Scheduled renewal before expiry for both providers; outcomes recorded; `AuthExpired` marks the mailbox `needs_reauth` and stops syncing rather than spinning.
  - _Requirements: R2.10, R1.5_

- [x] **1.8 Manual re-sync endpoint**
  - `POST /v1/mailboxes/{id}/resync` with an optional time window.
  - _Requirements: R2.11, R23.2_

- [x] **1.9 Raw payload archival**
  - Store raw provider payload / raw MIME in object storage before normalization, keyed for replay.
  - _Requirements: R4.10, R5.8_

- [x] **1.10 MIME normalization**
  - Parse MIME → part selection → HTML-to-text → produce the normalized contract from `design.md §5.2`.
  - Quoted-history separation into `body_text` and `body_text_clean`; signature detection with a recorded success flag.
  - Attachment metadata extraction; binaries to object storage only.
  - Unit tests over a corpus of awkward real-world MIME (multipart/alternative, inline images, nested forwards, non-UTF8 charsets, missing text part).
  - _Requirements: R4.1, R4.2, R4.3, R4.4, R4.7, R24.3_

- [x] **1.11 Subject normalization & thread association**
  - Strip reply/forward prefixes; association order: provider thread id → `In-Reply-To`/`References` → normalized subject + participants → new thread.
  - Maintain `email_thread` counters and timestamps.
  - _Requirements: R4.5, R4.6_

- [x] **1.12 Deduplication & persistence**
  - `ON CONFLICT DO NOTHING` insert; zero rows ⇒ ack as success, emit no new job.
  - Populate `email_message.search_tsv` at write time.
  - _Requirements: R4.8, R5.4, R5.6_

- [x] **1.13 Normalization failure handling**
  - Parse failure ⇒ persist with `normalization_failed`, retain raw key, dead-letter the job. Never discard a message.
  - _Requirements: R4.9, R3.5_

- [x] **1.14 Read API for mail data**
  - `GET /v1/mailboxes`, `/v1/threads`, `/v1/threads/{id}`, `/v1/messages/{id}`, all paginated and organization-scoped.
  - _Requirements: R23.2, R23.6_

> **Phase 1 gate:** a real Gmail mailbox (or the fake adapter in CI) delivers a notification; the message appears in `email_message`, associated with a thread, with attachments in MinIO, the checkpoint advanced, and a single trace covering the path. Replaying the same notification creates no duplicate row.

---

# Phase 2 — Triage & Queue Architecture

*Deliverable: email → category queue, with the early-exit gate working.*

- [x] **2.1 Job envelope & publication**
  - Envelope per `design.md §7.3` including `trace_id`, `idempotency_key`, `attempt`, and the classification snapshot.
  - `processing_job` row created at ingestion in state `RECEIVED`.
  - _Requirements: R7.3, R18.1, R19.2_

- [x] **2.2 Rule engine (triage stage 1)**
  - Declarative, hot-reloadable rule config (sender patterns, `List-Unsubscribe`, `Auto-Submitted`, subject and body patterns) — no hard-coded conditionals.
  - Returns category/intent/priority/flags/confidence.
  - Unit tests per rule plus a regression suite of fixture emails.
  - _Requirements: R6.1, R6.8, R24.3_

- [x] **2.3 Lightweight ML classifier (triage stage 2)**
  - **Depends on task 0.13** — trains on the classification seed set built in Phase 0.
  - TF-IDF or embedding features + linear head; export as a loadable, versioned artifact.
  - Inference path budgeted at 20–50 ms; report held-out macro-F1 at this stage, not only in Phase 7.
  - _Requirements: R6.1, R22.1, NFR3_

- [x] **2.4 Small-LLM fallback (triage stage 3)**
  - Structured-output classification call through `LLMProvider`; strict schema; short prompt.
  - _Requirements: R6.1, R6.3_

- [x] **2.5 Cascade orchestration & thresholds**
  - Stop at the first stage meeting its threshold; never invoke later stages after a confident answer.
  - Thresholds configurable per organization and per category without redeploy.
  - Persist every result with stage, latency, model, raw output.
  - Safe default + review flag if all stages fail or return malformed output.
  - _Requirements: R6.2, R6.7, R6.9, R6.11_

- [x] **2.6 Category taxonomy**
  - Implement support, sales, billing, administration, scheduling, general_inquiry, automated_notification, acknowledgement, no_response.
  - _Requirements: R6.4_

- [x] **2.7 Early-exit gate — the cost lever**
  - `reply_required == false` ⇒ transition straight to `COMPLETED`; assert in tests that **no** embedding, retrieval, rerank, or generation call is made.
  - `retrieval_required == false` ⇒ RAG is skipped downstream.
  - _Requirements: R6.5, R6.6_

- [x] **2.8 Deterministic template reply path — the missing 20%**
  - Emit `workflow_hint ∈ {template, ai, none}` from the cascade alongside `reply_required` and `retrieval_required`.
  - Template registry keyed by `(category, intent)` with variable substitution from message and business fields; versioned template files.
  - `workflow_hint='template'` ⇒ render and go to `DRAFTED` with **zero** retrieval and **zero** generation calls; assert this in tests.
  - No matching template ⇒ fall back to `workflow_hint='ai'`. The template path must never block a reply.
  - Verify the three outcomes (early exit / template / AI) are mutually exclusive and exhaustive over actionable mail.
  - _Requirements: R6.12, R6.13, R6.14, R6.15_

- [x] **2.9 Funnel instrumentation**
  - Counters for all three outcomes so the realized funnel reconciles against the 45% / 20% / 35% assumption with no residual bucket, plus the ~70% RAG share of AI traffic.
  - `emails_templated_total` exported alongside `emails_generated_total`.
  - _Requirements: R6.10, R6.15, R21.4, NFR14_

- [x] **2.10 Category-aware routing**
  - Publish to `email.<category>.<priority>`; `normal` and `priority` lanes with independent consumer scaling.
  - New categories addable by configuration; queues declared at startup.
  - Startup warning naming any category queue with no configured consumer.
  - _Requirements: R7.1, R7.2, R7.4, R7.6_

- [x] **2.11 Worker micro-batching**
  - Pull N jobs together; each job still gets its own independent inference call.
  - Add an explicit test asserting no prompt ever contains two distinct emails.
  - _Requirements: R3.6, R3.7_

- [x] **2.12 Retry, backoff & dead-letter**
  - `GENERATING → RETRY_PENDING → GENERATING` with exponential backoff and jitter; attempts exhausted ⇒ `FAILED → DEAD_LETTER`.
  - Failure reason and original routing key preserved in DLQ headers.
  - _Requirements: R19.5, R19.6, R3.5, R18.2_

- [x] **2.13 Lease reaper**
  - Reclaim jobs stuck in a non-terminal state past `lease_expires_at`.
  - _Requirements: R19.8_

- [x] **2.14 Job timeline API & replay**
  - `GET /v1/messages/{id}/timeline` returning ordered `processing_event` history.
  - `POST /v1/jobs/{id}/replay` to re-run a dead-lettered job from its last good state.
  - _Requirements: R18.6, R18.7, R23.2_

- [x] **2.15 Queue metrics**
  - Per-queue depth and wait time exported to Prometheus.
  - _Requirements: R7.5, R21.4, R20.5_

> **Phase 2 gate:** a newsletter fixture terminates at `COMPLETED` with zero AI calls; an acknowledgement fixture produces a template reply with zero retrieval and zero generation calls; a support fixture lands in `email.support.normal`; killing a worker mid-job results in redelivery and exactly one logical result; a poisoned job reaches the DLQ with its reason intact and can be replayed.

---

# Phase 3 — Knowledge RAG

*Deliverable: email → relevant organizational knowledge.*

- [x] **3.1 Document parsers**
  - PDF, DOCX, HTML, Markdown, plain text → text plus document structure (headings, sections, lists).
  - _Requirements: R9.2_

- [x] **3.2 Structural chunker**
  - Split on semantic boundaries in preference to fixed character counts; target 350–700 tokens with configurable overlap.
  - Emit `heading_path`, `section`, `chunk_index`, `token_count`, `content_checksum`.
  - Unit tests: heading-heavy doc, table-heavy doc, one long unbroken paragraph, tiny doc.
  - _Requirements: R9.3, R9.4, R9.5, R24.3_

- [x] **3.3 Embedding service**
  - Provider-abstracted embedder; batching; retry; records model name and dimension; counts `embedding_tokens_total`.
  - _Requirements: R9.6, R9.11, R21.4_

- [x] **3.4 Chunk persistence & indexing**
  - Write chunk + `content_tsv` in one transaction; write `embedding_record` with the HNSW-indexed vector.
  - _Requirements: R9.7, R5.6, R5.7_

- [x] **3.5 Ingestion pipeline & versioning**
  - `knowledge.ingest` worker running parse → chunk → enrich → embed → persist → `active`.
  - Re-ingestion writes version N+1 then flips status atomically — no window where the document is unsearchable.
  - Unchanged `content_checksum` ⇒ carry the embedding forward, skip re-embedding.
  - Per-document status with failure reason.
  - _Requirements: R9.1, R9.8, R9.9, R9.10_

- [x] **3.6 Knowledge upload API**
  - `POST /v1/knowledge/documents` (upload to object storage + enqueue), `GET /v1/knowledge/documents` with ingestion status.
  - _Requirements: R23.7, R23.2, R5.8_

- [x] **3.7 SearchBackend interface**
  - Protocol with `lexical()` and `vector()` returning `Candidate` objects carrying both ranks and both scores.
  - Contract test suite the PostgreSQL implementation must pass — and any future backend.
  - _Requirements: R10.7, R10.8_

- [x] **3.8 PostgresSearchBackend**
  - Implement the hybrid SQL from `design.md §5.5`; filters (organization, category, document status) applied **inside** each branch.
  - Configurable top-N per branch, default 20.
  - **Filtered-ANN under-fill (read `design.md §5.5` first).** HNSW post-filters, so a tenant-scoped vector query can silently return far fewer than top-N. Detect `count < top_n` ⇒ set `retrieval_underfilled=true` and export the metric; widen via `hnsw.ef_search` / iterative scans before degrading.
  - Integration test must seed **≥3 tenants with overlapping content** and assert the target tenant's full top-N is returned. A single-tenant fixture will pass while production under-retrieves.
  - _Requirements: R10.1, R10.2, R10.4, R10.10, R10.11_

- [x] **3.9 RRF fusion**
  - `score(d) = Σ 1/(k + rank_r(d))`, configurable `k` (default 60).
  - Pure-function unit tests: disjoint lists, identical lists, single-branch, ties.
  - _Requirements: R10.3, R24.3_

- [x] **3.10 Concurrent branch execution & degradation**
  - Run lexical and vector concurrently; per-branch timeout.
  - One branch failing or timing out ⇒ continue on the survivor, record `retrieval_degraded=true`.
  - _Requirements: R10.5, R10.6, R10.9_

- [x] **3.11 Cross-encoder reranker**
  - Optional rerank over the fused candidate set; disable-able per organization and per category.
  - Unavailable ⇒ fall back to RRF order, record the fallback.
  - _Requirements: R11.1, R11.2, R11.5_

- [x] **3.12 Context packing**
  - Configurable Top-K (default 4–6); hard token budget; truncate only at chunk boundaries.
  - _Requirements: R11.3, R11.4_

- [x] **3.13 Retrieval query builder**
  - **Ships degraded in Phase 3.** `thread_state` does not exist until task 4.1, so build from *current email + classification intent* now, behind a `thread_summary: str | None` parameter that is wired up in task 4.4. Do not block Phase 3 on Phase 4.
  - Build `RetrievalQuery` from current email + thread summary + classification intent — no extra LLM call in the default path.
  - Separate semantic text and lexical terms; regex-configurable identifier extraction (invoice, order, ticket, SKU, container, incident).
  - Derive category/metadata filters from classification; persist the query with the job.
  - Test explicitly that an identifier-bearing email (e.g. `INV-2026-01829`) retrieves the right chunk where a vector-only query would not.
  - _Requirements: R12.1, R12.2, R12.3, R12.4, R12.5, R12.6_

- [x] **3.14 Retrieval latency metrics**
  - `retrieval_latency_ms` and `rerank_latency_ms` recorded separately, as histograms.
  - _Requirements: R11.6, R21.4, R21.5, NFR5, NFR6_

- [x] **3.15 Retrieval debug endpoint**
  - `POST /v1/search/debug` returning the constructed query, both branch result lists with ranks, fused scores, rerank scores, and the final selection.
  - _Requirements: R23.3_

- [x] **3.16 Dense query embedding in the production retrieval path** *(discovered 2026-09-27, CLAUDE.md §7)*
  - Nothing in the production path fills `RetrievalQuery.query_vector`, so the pgvector branch returns no candidates (`packages/retrieval/postgres.py:192`) and hybrid retrieval runs lexical-only. Evidence: `grep -rn "query_vector" packages services` finds no producer.
  - Embed the query's semantic text with the configured embedder (`packages/knowledge/embedder.py`, the same model and dimension as the corpus, R5.10) in `RetrievalQueryBuilder` or the `ContextBuilder`, guarded by a timeout so a slow embedder degrades to lexical-only (R10.9). Count embedding tokens (`embedding_tokens_total`).
  - Implemented: `HybridRetriever` embeds `semantic_text` inside the vector branch when a query has no vector. Embedding and the ANN search share the vector-branch timeout (`RETRIEVAL__RETRIEVAL_TIMEOUT_MS`, now honoured by the ai-worker and `/v1/search/debug`). An embedder error, timeout, or unusable vector (wrong length or all zeros) fails only the vector branch, and retrieval degrades to lexical (R10.6). The ai-worker and the API use the configured embedder (the corpus model, R5.10); the ai-worker and the knowledge worker count its tokens (R9.11). The ai-worker checks the vector dimension at startup, both workers close their embedder after their consumers drain, and compose forwards the embedding model, URL and key.
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1350, integration 158). Every bullet and R10.1, R10.9 and R9.11 was traced to code and to a test that fails without it. The embedding sits in `HybridRetriever`, so it shares the vector-branch budget and `RetrievalQueryBuilder` stays synchronous. The user ran the live gate: `make up` rebuilt the images, and the ai-worker logged `configured=1536, database=1536`. `make retrieval-gate` found the uploaded document through the vector branch alone (not degraded, `lexical_count` 0, dimension 1536), both for a probe query and for the billing email's own query, and the ai-worker retrieved 1 chunk for that email. `embedding_tokens_total` moved on the ai-worker (queries) and the knowledge worker (ingestion). `make smoke` and `make phase4-gate` still pass. The stack embeds with `FakeEmbedder`, so this proves wiring, not semantic quality. The fix pass before flipping:
    - `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` is forwarded into the app containers.
    - Embedder errors carry the HTTP status only, never the provider body, which can echo email text.
    - The gate asserts the vector-only hit instead of arguing it.
  - Deferred:
    - `/v1/search/debug` query tokens are not counted in mock mode (the API gives `FakeEmbedder` no metrics); real embeddings are counted.
    - The debug API does not validate a caller-supplied `query_vector` (wrong length, all zeros, or empty).
    - The gate leaks raw MIME objects when the email never reaches `DRAFTED`.
    - A hosted embedding request cut off by the timeout is billed but not counted.
    - No hosted embedder has run live. NFR5 with one is unmeasured, and the 500 ms per-branch default may need raising (docs/configuration.md §2.5).
    - Compose does not forward `EMBEDDING__DIMENSION`: every container uses the default 1536, and changing it in `.env` has no effect under compose.
  - _Requirements: R10.1, R10.9, R9.11_

> **Phase 3 gate:** a support email retrieves the correct procedure chunk; an invoice-identifier email retrieves the correct billing chunk via the lexical branch; disabling either branch degrades gracefully; the debug endpoint explains every ranking decision; hybrid retrieval measurably beats vector-only on the **seed** benchmark set from task 0.13 (first evidence for H1; the full comparison is exp02 in Phase 3's successor phase); and filtered vector search returns full top-N across ≥3 seeded tenants.

---

# Runtime Assembly & Delivery Safety (inserted 2026-09-26, before resuming Phase 4)

*Discovered missing work (CLAUDE.md §7). Phases 0–4 built components that no running process hosts, and several broker and API paths drop messages silently. Evidence: `artifacts/superpowers/2026-09-26-project-scouting-audit.md`. Plan: `docs/superpowers/plans/2026-09-26-runtime-assembly-and-delivery-safety.md`.*

> **Status (2026-09-27): RA.1–RA.14 done.** `make ci` is green (evidence below); RA.14 removed the pre-existing lint/format baseline that had held RA.1–RA.13 at `[~]`.

- [x] **RA.1 Production import path free of dev-only modules**
  - No production module imports pytest, `tests`, or `evaluation`; a subprocess import walk proves it.
  - _Requirements: R20.1_

- [x] **RA.2 Integration tests isolated from the running stack**
  - Dedicated vhost and database reset per session; guards reject vhost `/` and database `rag_email`.
  - _Requirements: R24.4 (partial: dedicated vhost/database on the shared dev servers, not ephemeral containers)_

- [x] **RA.3 Retry ladder returns messages to their origin exchange**
  - `retry.return` headers exchange with alternate exchange `dlx.email`; one-time migration target for existing retry queues.
  - _Requirements: R3.5, R19.5 (partial: fixed retry tiers, no jitter), R19.6_

- [x] **RA.4 Unparseable deliveries dead-lettered verbatim**
  - _Requirements: R3.5, R3.3_

- [x] **RA.5 Unroutable publishes raise; consumers resume after reconnect; failed acks never duplicate**
  - _Requirements: R3.1, R3.3, R7.1_

- [x] **RA.6 Graceful drain: stop consuming → drain in-flight → close**
  - _Requirements: R20.8, R3.3_

- [x] **RA.7 API publish paths never report lost work as success**
  - Replay routes via the queue→exchange resolver or refuses; upload returns 503 and compensates when it cannot enqueue.
  - _Requirements: R18.7, R23.7, R9.1_

- [x] **RA.8 Shared worker runtime; topology declared at startup; R5.10 check hosted**
  - _Requirements: R3.2, R20.7, R20.8, R5.10_

- [x] **RA.9 Triage worker entrypoint**
  - _Requirements: R6.1, R6.2, R6.5, R7.1, R3.4_

- [x] **RA.10 Mail connector entrypoint and background jobs**
  - Sync consumer, subscription renewal, queue monitor; lease reaper hosted but disabled by default.
  - _Requirements: R2.1, R2.10, R2.11, R7.5, R19.8 (partial: hosted but disabled by default), R23.6_

- [x] **RA.11 Production image from the lockfile with every runtime asset**
  - _Requirements: R20.1, R24.1_

- [x] **RA.12 Compose wiring: commands, init job, readiness healthchecks, drain grace**
  - _Requirements: R20.1, R20.7, R20.8, R3.2_

- [x] **RA.13 Live-stack smoke check and documentation**
  - _Requirements: R24.7 (partial: ingestion → triage), R20.1_

- [x] **RA.14 Restore a green lint baseline (unblocks `[x]` on RA.1–RA.13)**
  - Pre-existing failures (audit finding A09 in `artifacts/superpowers/2026-09-26-audit-register.md`), re-measured 2026-09-27; none were introduced by RA.1–RA.13.
  - `uv run mypy packages services tests evaluation` → 14 errors in 6 test files: `tests/unit/test_queue_metrics.py` (4), `tests/integration/test_queue_metrics_integration.py` (3), `tests/unit/test_draft_repair_orchestration.py` (3), `tests/unit/test_citation_verification_generation.py` (2), `tests/unit/test_thread_context_assembly.py` (1), `tests/integration/test_thread_context_assembly_postgres.py` (1).
  - `uv run ruff format --check .` → 40 files: 31 Python files (`packages/` 15, `services/api` 1, `tests/` 15) and 9 Markdown files under `docs/` whose code blocks ruff also formats. List them with `uv run ruff format --check .`.
  - Fix type errors at their cause (no blanket `# type: ignore`); formatting changes must not alter behaviour. Decide whether `docs/**/*.md` belongs in ruff's scope (exclude it in `pyproject.toml`, or format it).
  - **Done when:** `make ci` (`fmt-check lint test-unit test-integration`) passes. Then flip RA.1–RA.13 to `[x]` and add the date to the gate evidence note below.
  - _Requirements: R24.2; requirements.md §0.3 DoD #6 (CI green)_

> **RA gate:** `make up` builds images from HEAD, and api, mail-connector, email-worker, triage-worker and knowledge-worker all report ready. `mail.sync.requested`, `email.normalize`, `email.triage` and `knowledge.ingest` each have ≥1 consumer. `make smoke` does three things: it drives a billing email through normalize → triage into `email.billing.*`; it drives a no-reply newsletter to `COMPLETED` with zero AI work; and it sends a cross-tenant sync request to `email.dead_letter` with its reason. `uv run pytest tests/integration` passes without touching vhost `/` or database `rag_email`.
>
> **Gate evidence (2026-09-27):** `make smoke` → SMOKE OK; `uv run pytest tests/unit` → 1202 passed; `uv run pytest tests/integration` → 140 passed (vhost/database `rag_email_test`). **DoD #6 caveat:** repository-wide `make lint` was already red before this block (14 mypy errors in 6 pre-existing test files; 40 files fail `ruff format --check`, re-measured 2026-09-27). RA tasks added no new errors (targeted mypy/ruff on every touched file). Fixing that baseline was task **RA.14** (audit finding A09), which held RA.1–RA.13 at `[~]` until it closed. **CI green (2026-09-27, RA.14):** `make ci` → exit 0 (`ruff format --check .` clean with `*.md` out of the formatter's scope; `ruff check .` clean; strict mypy clean on packages, services, tests and evaluation; unit 1260 passed; integration 149 passed).

---

# Phase 4 — Context & Generation

*Deliverable: email + thread + RAG → generated draft.*

- [x] **4.1 Thread state store**
  - `thread_state` read/write with `topic`, `current_intent`, `summary`, `open_questions[]`, `resolved_items[]`.
  - Optimistic concurrency on `version`; concurrent workers on one thread must not lose updates (add a concurrency test).
  - _Requirements: R8.1, R8.6_

- [x] **4.2 Summarization policy**
  - Below threshold ⇒ verbatim recent messages, no summarization call.
  - `message_count > threshold` OR `estimated_context_tokens > threshold` ⇒ (re)summarize; record `summarized_through_message_id`.
  - Assert in tests that a short thread triggers zero summarization calls.
  - _Requirements: R8.2, R8.3, R8.4_

- [x] **4.3 Thread context assembly**
  - `summary + latest N relevant messages + current email` once a summary exists.
  - Record tokens saved (pre- vs post-compression estimate) for H3.
  - Keep inbound email out of the knowledge corpus by default.
  - _Requirements: R8.5, R8.7, R8.8_

- [x] **4.4 Context Builder orchestration**
  - Gather thread context, business data (stub until Phase 5), and — only when `retrieval_required` — hybrid RAG.
  - Emit a `ContextPackage` in the fixed order from `design.md §5.4`; static sections first so prompt-prefix caching can apply.
  - Transition `QUEUED → CONTEXT_READY`.
  - _Requirements: R14.8, R6.6, R18.1_

- [x] **4.5 LLMProvider abstraction**
  - `generate(messages, schema, tier, …) -> LLMResult` with content, model, tier, token counts, latency.
  - At least two implementations selectable by config (one hosted API, one OpenAI-compatible/local endpoint) plus a deterministic stub for CI.
  - Shared contract test suite.
  - _Requirements: R14.5, R14.7, R24.5_

- [x] **4.6 Agent profile registry**
  - Profiles specifying `profile, knowledge_domain, response_style, model_tier, context_policy`, prompt template, output schema.
  - Selected by classification category with a configured default fallback.
  - Versioned prompt templates; `prompt_version` recorded on every draft.
  - _Requirements: R14.1, R14.2, R14.6_

- [x] **4.7 Single-pass generation path & call budget**
  - One retrieval + one **generation** call for a normal email. No planner/critic/writer chain.
  - Assert **exactly one generation call per job** — not "one LLM call per job", which would contradict threshold summarization (4.2) and the triage-LLM fallback (2.4).
  - Enforce the full budget from `design.md §5.7`: ≤1 triage + ≤1 summarization + exactly 1 generation + ≤1 repair. Ceiling 4, common case 1.
  - Tier escalation **replaces** the generation call; it never adds one.
  - Export `llm_calls_total{kind}` and the `llm_calls_per_job` histogram so budget drift is visible rather than assumed.
  - _Requirements: R14.3, R14.4, R14.9, R14.10_

- [x] **4.8 Complexity router & model cascade**
  - Tiers `routine` and `high_capability` bound to models by config; routine by default.
  - Escalation on: low classification confidence, complex/long thread, insufficient retrieval evidence, multiple requested actions, oversized context.
  - Max one escalation per job; record tier and escalation reason (or `none`).
  - Config switch forcing single-tier operation for the H4 comparison.
  - _Requirements: R15.1, R15.2, R15.3, R15.4, R15.5, R15.6_

- [x] **4.9 Structured output & validation**
  - Enforce the schema `{action, draft, confidence, knowledge_chunks[], thread_summary_updated, model_tier}`.
  - Validate → one repair retry → fail into retry/DLQ. Never persist an unvalidated draft.
  - Done: schema enforced (`schemas/reply.v1.json`, `packages/llm/validation.py`); validation and the one repair retry wired into `SinglePassGenerator`; `UnvalidatedDraftError` raised on second failure or spent repair budget, so no unvalidated draft can reach a caller. Covers R16.1, R16.2 and the generator half of R16.3.
  - Left: the retry/DLQ hop itself is not observable end to end — `services/ai_worker/` has no consumer yet, so nothing nacks the message. `UnvalidatedDraftError` derives from `LLMError`, so the existing base-consumer retry ladder will dead-letter it once a consumer exists; the **Phase 4 gate must verify that hop** rather than assume it. Decide at that point whether it should be a `FatalError` (straight to DLQ) instead of climbing the 3-tier ladder, since a deterministic schema failure at `temperature=0.0` will likely fail all three redeliveries. The same decision applies to `DraftSchemaContractError`, introduced by this task: it is a pure deployment error (a profile's `output_schema` declaring what the code cannot enforce), so retrying it would climb all three redeliveries for every message of an affected profile — it is the stronger candidate of the two for `FatalError`. **Resolved 2026-09-27 by 4.13a:** the deterministic failures (`UnvalidatedDraftError`, `DraftSchemaContractError`) are dead-lettered at once rather than climbing the ladder (option 1), and the hop is proven on a real broker. 4.9 flips to `[x]` after the completion audit. Closed 2026-09-27 after the completion audit (PASS WITH NOTES): R16.3's dead-letter hop is proven on a real broker, with job `DEAD_LETTER` and no persisted draft.
  - _Requirements: R16.1, R16.2, R16.3_

- [x] **4.10 Citation verification**
  - Reject citations naming chunks that were not supplied in the context; set `citation_mismatch` and export its rate as a metric.
  - Done: `packages/llm/citations.py` verifies every cited id against the context's chunk-level aliases (`external_id`, falling back to `chunk_id` as `prompts/*.j2` renders them); `SinglePassGenerator` attaches a `CitationVerdict` to `GenerationResult` and flags `citation_mismatch` without failing the job. `citations_verified_total{category}` and `citation_mismatches_total{category}` are exported, with the rate's PromQL documented in `docs/observability.md`.
  - Note: the flag is produced but not yet persisted — `generated_draft.citation_mismatch` and `.citations` are written by task 4.11, which must persist `verdict.citations` rather than `content["knowledge_chunks"]`. The Grafana panel for the rate belongs to task 7.4.
  - _Requirements: R16.5_

- [x] **4.11 Draft persistence**
  - Persist body, citations, model, tier, escalation reason, prompt version, token counts, estimated cost.
  - Transition `GENERATING → DRAFTED`.
  - Done: `DraftingService` (`services/ai_worker/drafting.py`) moves `CONTEXT_READY → GENERATING`, makes the one generation call, builds the record (`packages/llm/drafts.py`: verified citations from `CitationVerdict`, escalation reason or `none`, `Re:` subject, cost from `packages/core/pricing.py`) and persists it with `GENERATING → DRAFTED` in one transaction (`packages/db/draft_persistence.py`). Migration `0004` enforces one draft per job; a redelivered `DRAFTED` job returns its draft without a second generation. An unpriced model stores `cost_estimate = NULL`.
  - Closed 2026-09-27 after a completion audit (`make ci` green via RA.14). The audit verdict was PASS WITH NOTES: every requirement was traced to code and a test (unit 1260, integration 149). DoD #3 and #5 were checked statically only, because no running worker calls `DraftingService` until 4.13b. The audit added a `draft_persisted` JSON log line on save and documented `LLM__PRICE_TABLE` plus the unpriced-model `NULL` rule (DoD #4, #5).
  - Owned elsewhere: the `ai-worker` broker consumer that calls `DraftingService`, and with it the retry/DLQ hop from 4.9, is task 4.13a; its service wiring is 4.13b. Per-draft cost *aggregation* per email / category / day (R21.6) is a query over `generated_draft.cost_estimate` and belongs to 7.3, with its panel in 7.4. The "Draft persistence < 50 ms" target (NFR9) is recorded in 7.2.
  - Before the next `make up` on an existing dev DB: migration `0004` adds a one-draft-per-job unique index, which fails if an old template-path crash ever left two drafts for one job. Run `SELECT job_id, count(*) FROM generated_draft WHERE job_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1` once. On 2026-09-27 the DB held 3 drafts and no duplicates.
  - _Requirements: R16.4, R18.1, R21.6_

- [x] **4.12 Generation metrics**
  - `generation_latency_ms`, `input_tokens_total`, `output_tokens_total`, `emails_generated_total`, `estimated_ai_cost`, with low-cardinality labels.
  - Record the final assembled context token count on **every** inference request, so context length can be correlated with quality, latency, and cost.
  - Done: `record_inference` (`packages/llm/inference_metrics.py`) records `llm_context_tokens{kind, tier}` (R11.7), input/output tokens and priced cost on every request through `BudgetedLLMProvider` (generate, repair) and `InstrumentedLLMProvider` (triage wired in `services/triage_worker/main.py`), plus one `llm_inference` JSON log line. `DraftingService` counts `emails_generated_total{organization, category, model_tier}` and `generated_draft_cost_total{category, model_tier}` once per created draft. The generator no longer counts tokens itself. PromQL in `docs/observability.md`.
  - Closed 2026-09-27 after a completion audit (`make ci` green via RA.14). The audit verdict was PASS WITH NOTES, and it added a 5000 ms `generation_latency_ms` bucket so the NFR8 1–5 s limit is read at a real bucket edge. "Every inference request" (R11.7) is fully true only once 4.13b wires the summarizer and generator. 4.13b carries that wiring and a test proving it.
  - Owned elsewhere: dashboards are 7.4. Tokens of an unparseable first response that a repair then fixes are uncounted (7.3). `llm_calls_total` does not count triage calls yet (7.2).
  - _Requirements: R11.7, R21.4, R21.5, R21.6, NFR8_

- [x] **4.13a AI-worker consumer core & generation failure routing**
  - Discovered missing work (CLAUDE.md §7): `services/ai_worker/` hosted no consumer, so no actionable job moved past `QUEUED`, and 4.9's retry/DLQ hop could not be observed.
  - `AIWorkerConsumer` (`services/ai_worker/consumer.py`) consumes one lane queue `email.<category>.<priority>`. Per job, in process (`design.md` §3.2): Context Builder (`QUEUED → CONTEXT_READY`, 4.4) → Complexity Router (4.8) → `DraftingService` (`CONTEXT_READY → GENERATING → DRAFTED`, 4.11). The classification comes from the envelope snapshot, never a re-classification (`design.md` §7.3).
  - Failure policy (`services/ai_worker/failure_policy.py`, decided 2026-09-27, option 1):
    - `UnvalidatedDraftError`, `DraftSchemaContractError` and `UnpersistableDraftError` → DLQ at once, with the reason. A `max_tokens` truncation is named.
    - A delivery for a job already `DRAFTED` / `DISPATCHED` / `COMPLETED` is acked and dropped.
    - Transient and unknown errors → retry ladder.
  - Proven on a real broker (scratch vhost) and Postgres: a timeout retries then drafts once; invalid output twice → DLQ with `x-failure-reason`, job `DEAD_LETTER`, no draft, 2 model calls; a drafted-job redelivery is acked with no call.
  - Closed 2026-09-27 after a final review and a completion audit, both on opus. Both tasks passed with notes; `make ci` is green (unit 1297, integration 152). The fix pass before flipping:
    - A state error for a job still in flight (`QUEUED`/`CONTEXT_READY`/`GENERATING`/`RETRY_PENDING`) or with an unreadable state now retries instead of dead-lettering. That stops a duplicate delivery from killing a live job, and a skipped recovery write from dead-lettering a transient failure.
    - A stale delivery for a `DEAD_LETTER` job is dropped.
    - `LLMSchemaValidationError` now carries the provider's finish reason, so a truncated, unparseable response is named in the DLQ reason. `packages/llm/client.py` and `packages/llm/anthropic.py` set it.
    - The redelivery proof now asserts the second message was consumed, and the DLQ-empty checks fetch instead of trusting a passive-declare count. Both were shown to fail on a deliberate break.
  - Deferred: `failed_jobs_total{error_type}` labels policy dead-letters as `FatalError`, and the reason header is double-prefixed (label with the cause). The integration tests assert final states, not the `processing_event` trails. There is no unit test for re-entry at `GENERATING` across two deliveries, or for a triage snapshot round-trip. DoD #3 and #5 (live stack, scrapeable metrics) are shown by 4.13b.
  - _Requirements: R3.3, R3.5, R7.3, R16.3, R18.1, R19.3, R19.5, R19.6, R19.7_

- [x] **4.13b AI-worker service wiring & live gate**
  - `services/ai_worker/main.py` on the shared `WorkerRuntime` (`/healthz`, `/readyz`, graceful drain), one `AIWorkerConsumer` per lane queue in `routing.configured_consumers` with a bounded, configurable `prefetch`. Replace the `ai-worker` placeholder in `docker-compose.yml`.
  - Compose with telemetry (4.12): wrap the summarizer's provider in `InstrumentedLLMProvider(kind=CallKind.SUMMARIZE)`, and pass `metrics` and `settings.llm.price_table` to `SinglePassGenerator` and `DraftingService`, so every request in the worker is measured.
  - Prove the wiring with a composed-worker test: one actionable job through the built components moves `llm_context_tokens{kind="generate"}` and `emails_generated_total`, and `llm_context_tokens{kind="summarize"}` when the thread crosses the summarization threshold (R11.7).
  - Call `start_token_counter_warmup()` in the worker's `build_components` (as the triage worker does), so the BPE encoding never loads on the event loop. Pass the plain provider to `SinglePassGenerator`, not a pre-built `BudgetedLLMProvider`: a reused wrapper keeps its own `metrics`/`price_table`, so the generator's would be ignored.
  - Worker-kill test (`design.md` §9): kill the worker mid-generation → redelivery → exactly one `generated_draft` row.
  - Extend `make smoke` so an actionable email reaches `DRAFTED` through the live stack.
  - Clear the job's lease at `DRAFTED` (or exclude `DRAFTED` from the lease reaper), so enabling `LEASE_REAPER__ENABLED` never reclaims and regenerates a drafted job (4.13a final review).
  - Implemented: `services/ai_worker/main.py` composes one pipeline for all lanes:
    - `ThreadSummarizer`, instrumented as `summarize` and threshold-triggered;
    - `ContextBuilder` with `HybridRetriever` over Postgres;
    - `ComplexityRouter`;
    - `DraftingService` with a plain provider, metrics and price table.

    One `AIWorkerConsumer` runs per configured lane, with lane prefetch. The compose `ai-worker` runs it with a `/readyz` healthcheck and a 45 s stop grace. The fake provider answers the draft and summary schemas, so the default stack drafts. The lease reaper skips `DRAFTED`. The composed-worker telemetry and worker-kill proofs pass on a real broker.
  - Closed 2026-09-27 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1316, integration 156). The user ran the live gate: `make up` rebuilt the images, ai-worker was healthy with consumers started about 3 s after container start, and `make smoke` showed the billing email reaching `email.billing.priority` → ai-worker → `DRAFTED` with one draft. `/metrics` exposes `llm_context_tokens` and `emails_generated_total` (4.13a DoD #3, #5). The fix pass before flipping:
    - The `cl100k_base` encoding is baked into the image (`TIKTOKEN_CACHE_DIR`), because a download at first use took the first live start to 56 s. The image smoke now runs offline.
    - R8.4 / design §5.4 `LAG`: a thread already summarized is re-summarized only after more than `SUMMARIZATION__RESUMMARIZE_LAG_MESSAGES` (default 2) new messages.
    - `persist_drafted` clears the claim lease in the `DRAFTED` transaction. The reaper's `DRAFTED` exclusion stays as a backstop.
    - The worker-kill test is an in-process broker-connection drop. It proves kill → redelivery → one draft. It does not prove a slow live worker racing a redelivery. The `docker kill` run belongs to 7.13.
  - Deferred:
    - The drain window is `TELEMETRY__DRAIN_TIMEOUT_S` (15 s) under a 45 s stop grace, so a longer job is cut and re-billed on deploy.
    - A worker with zero resolved lanes starts silent and reports ready.
    - Default prefetch gives priority lanes 5 and normal lanes 10.
    - Smoke step 1 does not check the ai-worker lane consumers.
    - LLM API keys are not forwarded into compose.
    - Nothing validates `RESUMMARIZE_LAG_MESSAGES` ≤ `KEEP_LATEST_MESSAGES`; a larger lag silently drops the messages between the summary point and the verbatim window.
    - The lag rule's result may depend on arrival timing: `make phase4-gate`'s 12-message thread ended at summary v2 on 2026-09-27 and v3 on 2026-09-28 (v3 matches refreshes at messages 5, 8, 11). Suspected cause: messages that share a 1-second `Date` header sort ambiguously.
    - Query embedding is 3.16.
  - _Requirements: R3.4, R11.7, R19.7, R20.1, R20.7, R20.8, R22.8, R24.7_

> **Phase 4 gate:** a support email with a 12-message thread produces a schema-valid, citation-verified draft in `DRAFTED` through the `ai-worker` consumer (4.13a, 4.13b); a draft that fails validation twice reaches the DLQ with its reason (4.13a policy) with no draft persisted; a short thread triggers no summarization; a low-confidence job escalates exactly once; forcing single-tier mode still works end to end.
>
> **Gate evidence (2026-09-27, live stack, `scripts/phase4_gate.py` / `make phase4-gate MODE=…`; the user ran `make up` and the ai-worker restarts):**
> - `default`: 12 replies chained by `In-Reply-To`/`References` through normalize → triage → ai-worker. The last job reached `DRAFTED` with one draft on a 12-message thread, the thread had a summary (version 2), and the draft recorded its citations with no mismatch. A one-message thread reached `DRAFTED` with no `thread_state` row (no summarization). This was re-run on the fixed image.
> - `low-confidence` (`ROUTER_CONFIDENCE_THRESHOLD=1.0`): the job escalated to `high_capability` for `low_classification_confidence` with exactly one escalated `GENERATING` event.
> - `single-tier` (`ROUTER_FORCE_SINGLE_TIER=true`): the job was drafted on `high_capability` for `single_tier_forced`, with no escalation counted.
> - Not live: "fails validation twice → DLQ". The offline model always answers validly, and forcing bad output would need a test hook in production code. That hop is proven on a real broker by `tests/integration/test_ai_worker_failure_routing_integration.py` (4.13a).
> - Citation grounding: the offline model cites nothing, so the live draft shows 0 citations. Grounding itself is proven by the 4.10 tests. A live grounded draft needs a real provider key and an ingested knowledge document.
> - Defects the gate found and fixed:
>   - The ai-worker built `ComplexityRouter()` with its own defaults, so every `ROUTER_*` setting was ignored, including the R15.6 switch. It now reads `settings.complexity_router` and the LLM tiers (`test_router_follows_the_configured_cascade_settings` RED→GREEN).
>   - The R15.5 per-job cap was never given a count. The consumer now counts escalations recorded on the job's earlier `GENERATING` events, so a redelivered job does not escalate again (`test_redelivered_job_escalates_at_most_once` RED→GREEN).
>   - Compose did not forward `ROUTER_FORCE_SINGLE_TIER`, `ROUTER_CONFIDENCE_THRESHOLD` or the LLM API keys. It does now (`test_compose_forwards_the_switch_into_app_containers` RED→GREEN).
>
>   `make ci` passed afterwards (unit 1322, integration 156).

---

# Phase 5 — Business Data Integration

*Deliverable: RAG knowledge + live operational data → response.*

> **Design change (2026-09-28, ADR-0008):** the fetch is decided by a code-side plan from typed IDs, the profile's `context_policy` and the intent — not by the intent alone, because the ML triage stage emits no intent. See `specs/design.md` §5.4 "Business data (R13)" and `artifacts/superpowers/2026-09-28-phase5-business-data-trigger-research.md`. Live runs use the Google Gemini API (Gemma 4 and Gemini 3.1 Flash-Lite, chosen as the cheapest working models on 2026-09-28) through the `openai` provider's OpenAI-compatible base URL. Local models are not run on the owner's laptop.

- [x] **5.0 Hosted OpenAI-compatible provider wiring (Gemini) & live smoke check**
  - Discovered missing work (CLAUDE.md §7): compose forwards `LLM__PROVIDER` and the API keys, but not `LLM__OPENAI_BASE_URL` or `LLM__FAST_MODEL` / `LLM__STRONG_MODEL` / `LLM__FALLBACK_MODEL`, so containers would send a Gemini key to the default OpenAI URL with OpenAI model names.
  - Forward those four settings and `LLM__PRICE_TABLE` through the shared compose environment. Document them in `.env.example` and `docs/configuration.md` with the Gemini configuration:
    - base URL `https://generativelanguage.googleapis.com/v1beta/openai`;
    - `LLM__FAST_MODEL=gemma-4-26b-a4b-it`, `LLM__STRONG_MODEL=gemma-4-31b-it`, `LLM__FALLBACK_MODEL=gemini-3.1-flash-lite`;
    - a price-table example with those three models: Gemma is free of charge (free tier only), and `gemini-3.1-flash-lite` costs $0.25 / $1.50 per 1M input / output tokens (Google pricing page, updated 2026-09-24) (R21.6).
  - Settings validation fails fast when `LLM__PROVIDER=openai` names a Gemini or Gemma model but `LLM__OPENAI_BASE_URL` is still the OpenAI default, so the key is never sent to the wrong host (R20.6).
  - Add a one-call-per-schema live smoke script the owner runs by hand (not part of CI, R24.5): one triage call and one draft call through the configured provider. It reports whether each response parses and validates, the `finish_reason`, and the token counts, and it never prints the key.
  - Pre-check (2026-09-28, throwaway probe through `OpenAILLMProvider` with the real triage schema at 250 tokens and the reply schema at 1000 tokens): `gemma-4-26b-a4b-it`, `gemma-4-31b-it`, `gemini-3.1-flash-lite` and `gemini-3.5-flash-lite` all returned schema-valid triage and draft JSON, and each draft stated the given order status and cited the given chunk. `gemini-2.5-flash-lite` returned 404 "no longer available to new users".
  - _Requirements: R14.7, R20.6, R21.6, R24.5_
  - Audit 2026-09-28: PASS WITH NOTES (`make ci` green, unit 1522, integration 191). The forwarded keys, the Gemini block in `.env.example` and `docs/configuration.md`, the fail-fast check (`tests/unit/test_settings.py`) and `scripts/llm_smoke.py` (`tests/unit/test_llm_smoke.py`: parse/validate, `finish_reason`, tokens, key never printed) are in place. Findings: none.
  - Closed 2026-09-28 after the owner's live runs. `make llm-smoke` printed `LLM SMOKE OK`: triage `billing` / `order_status_inquiry` (confidence 0.95) and a `support.v2` draft citing the given chunk, both on `gemma-4-26b-a4b-it` with `finish_reason stop`. After `make up`, the ai-worker container reported `LLM__PROVIDER=openai`, the Gemini base URL and `gemma-4-26b-a4b-it`, so the compose forwarding works in the real stack.

- [x] **5.1 Business schema & seed data**
  - `customer`, `product`, `order`, `order_item`, `ticket` with realistic seed records tied to the fixture emails. The tables already exist (migration 0001) and match design §6.2.
  - Seed order `ORD-82915` for Alice (`alice.smith@clientcorp.com`), and add a fixture email from Alice asking "What is the status of order 82915?".
  - Keep the existing cross-customer case: Edward's `identifier_order_ticket` email asks about Dana's `ORD-9901` (expected `NOT_FOUND` under R13.4 scoping).
  - Test fixtures for the business provider seed ≥3 tenants with overlapping customer emails and order numbers (CLAUDE.md §8).
  - _Requirements: R13.1, R5.9_
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1522, integration 191). `ORD-82915` with two items is on Alice, her order-status email is a fixture, Edward still asks about Dana's `ORD-9901`, and `packages/db/fixtures/business_tenants.py` seeds three tenants with overlapping emails and order numbers (`tests/unit/test_seed_fixtures.py`, `tests/integration/test_business_seed.py`). Note: `product` and `order_item` are seeded but not looked up (owner decision, ADR-0008). The fix pass before flipping:
    - none

- [x] **5.2 BusinessDataProvider interface & implementations**
  - Move the protocol from `packages/context/builder.py` into `packages/business/`, with typed models (`FetchPlan`, `BusinessFact`, `BusinessContext` with `customer_status`, the two status enums) and `get_business_context(organization_id, sender_email, plan)`.
  - The provider's first step is sender → customer resolution (design §5.4); its rules and proofs are 5.3.
  - A local PostgreSQL implementation using fixed, parameterised queries that always carry `organization_id`. An in-memory implementation for unit tests.
  - One shared contract test suite that both implementations pass: typed-ID lookups, snapshot rows, `NOT_FOUND`, `NOT_LOOKED_UP`, and each `customer_status`.
  - Not wired into the ai-worker yet; that is 5.4.
  - _Requirements: R13.2_
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1522, integration 191). Both providers pass `BusinessDataProviderContractSuite` (typed-ID lookups, snapshot rows, `NOT_FOUND`, `NOT_LOOKED_UP`, and the provider-level statuses `FOUND`, `UNKNOWN_SENDER`, `AMBIGUOUS_CUSTOMER`). `UNAVAILABLE` is set by the caller, `fetch_business_context`, and is covered by `tests/unit/test_business_fetch.py` and the `statement_timeout` integration tests in `tests/integration/test_business_provider_postgres.py`. The old protocol and stub are gone from `packages/context/`. The fix pass before flipping:
    - none

- [x] **5.3 Sender → customer resolution & scoping proof**
  - Case-insensitive match on the whole sender address within the organization. 0 rows ⇒ `customer_status=UNKNOWN_SENDER`, >1 rows ⇒ `AMBIGUOUS_CUSTOMER`; in both cases every planned order and ticket fact is `NOT_LOOKED_UP` with reason `unknown_sender` / `ambiguous_customer`. `INV-` references keep `unsupported_entity`, because no invoice is looked up for any sender (owner decision 2026-09-28).
  - Every order and ticket lookup is scoped to the resolved customer: another customer's order is `NOT_FOUND`.
  - Integration tests on real Postgres over the ≥3-tenant fixtures from 5.1, with overlapping customer emails and order numbers.
  - The identity assumption is recorded in ADR-0008. Add no verification control (CLAUDE.md §6).
  - _Requirements: R13.4_
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1522, integration 191). The five queries in `packages/business/postgres.py` all start with `organization_id = $1`, and the order and ticket queries add `customer_id = $2`. The contract suite runs on real Postgres over the three-tenant fixtures (same email in each tenant, another customer's order and ticket `NOT_FOUND`, ambiguous in one tenant and found in another). `INV-` references keep `unsupported_entity` for an unresolved sender, as the owner decided. The fix pass before flipping:
    - none

- [x] **5.4 Fetch plan, Context Builder wiring & timeout**
  - A pure `FetchPlan` builder in `packages/business/plan.py`, as in design §5.4. Typed IDs are always planned, the snapshot follows `context_policy` or `INTENT_ENTITIES`, `INV-` references get `NOT_LOOKED_UP` (`unsupported_entity`), and nothing planned ⇒ no provider call.
  - A typed-ID extractor in the forms of design §5.4, run on every job, independent of `retrieval_required`.
  - Unit tests for the builder and the extractor, including the negatives "in order to", "an order 2 days ago" and "ticket 3 of 5", `ORD`/`TICK` normalisation, and typed IDs overriding `context_policy`.
  - Wiring:
    - `ContextBuilder` takes the `AgentProfileRegistry` and reads the routed profile's `context_policy` (design §5.4). The builder's call site changes to `get_business_context(org_id, sender_email, plan)`.
    - `services/ai_worker/main.py` passes the registry it already builds for `SinglePassGenerator` and the Postgres provider, replacing the stub.
  - The provider call runs under `BUSINESS_DATA__TIMEOUT_MS`. On timeout or error, statuses become `UNAVAILABLE`, `business_data_degraded=true` is recorded, and the draft is still produced.
  - Settings: add a `business_data` group (`BUSINESS_DATA__TIMEOUT_MS=500`, `BUSINESS_DATA__SNAPSHOT_ORDERS=3`, `BUSINESS_DATA__SNAPSHOT_TICKETS=3`) to the validated settings, forward it through compose, and document it in `.env.example` and `docs/configuration.md`.
  - Observability:
    - The plan, `customer_status`, fact statuses and `business_data_degraded` go into the `CONTEXT_READY` payload.
    - Emit the `business.fetch` span, `business_lookups_total{entity, status}` and the `business_lookup_latency_ms` histogram.
    - Log one structured `business_fetch` line per job with `trace_id`, `job_id` and `organization_id`.
  - _Requirements: R13.3, R13.7, R20.6, R21.3_
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1522, integration 191). The extractor and plan tests cover the negatives and typed IDs overriding `context_policy`. `ContextBuilder` reads the routed profile from the shared `AgentProfileRegistry`, the ai-worker passes `PostgresBusinessDataProvider`, a timeout or error degrades every planned fact to `UNAVAILABLE` and the job still reaches `CONTEXT_READY`, and the `CONTEXT_READY` payload, the `business.fetch` span, both metrics and the `business_fetch` log line (with `trace_id`, `job_id`, `organization_id`) are asserted in `tests/unit/test_business_fetch.py` and `tests/unit/test_context_builder_business.py`. `BUSINESS_DATA__*` is in `.env.example`, `docs/configuration.md` and compose `x-app-env`. The fix pass before flipping:
    - none

- [x] **5.5 Fact labelling & missing-entity handling**
  - Business facts render as one `[BUSINESS DATA]` section (with `source` and `as_of`) in every profile template, replacing the four different headers the templates use today.
  - `customer_status` and every planned entity appear with their status: a missing entity is an explicit `NOT_FOUND` fact, never an omission. `NOT_FOUND` and `UNAVAILABLE` stay distinct.
  - Add the precedence rule (design §5.4) to the agent instructions the live path sends, `DefaultInstructionProvider` in `packages/context/builder.py`, and the same line to `DEFAULT_ENTERPRISE_INSTRUCTIONS` in `packages/llm/profile.py`.
  - Bump all four profiles to `*.v2`: new `prompts/*.v2.j2` files, `prompt_template` and `prompt_version` in `config/agent_profiles.yaml`, and the tests that assert `v1`.
  - _Requirements: R13.5, R13.6_
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1522, integration 191). All four profiles point at `prompts/*.v2.j2`, each renders one `[BUSINESS DATA]` block through `business_data.render()` (with `source` and `as_of`). `DefaultInstructionProvider` sends `DEFAULT_ENTERPRISE_INSTRUCTIONS`, which ends with `BUSINESS_DATA_PRECEDENCE_RULE`, so the live path and the registry default carry the same rule. `NOT_FOUND` and `UNAVAILABLE` stay distinct in the rendered block. The fix pass before flipping:
    - none

- [x] **5.6 End-to-end business-data scenario test & live gate**
  - Integration test with the stub LLM, covering both fixture emails:
    - Alice's "What is the status of order 82915?" email reaches `DRAFTED`. The context carries `ORD-82915` as `FOUND` with its seeded status in `[BUSINESS DATA]`, and the order-status procedure chunk is among the retrieved knowledge.
    - Edward's email yields `NOT_FOUND` for `ORD-9901` (Dana's order) and `FOUND` for `TICK-4402` (seeded with `customer_id = CUST_EDWARD_ID`, status `open`).
  - A `make phase5-gate` script, run live by the owner with Gemini, checks that the draft text contains the exact seeded status and cites the procedure chunk.
  - The live gate runs with `EMBEDDING__MOCK=true`. The procedure chunk reaches the prompt through the vector branch with the mock embedder: this proves wiring, not semantic retrieval quality. The lexical branch cannot carry it, because `websearch_to_tsquery` ANDs every term and the procedure must not contain the order number (R13.3). A live Gemini embedder is out of Phase 5 scope.
  - Seeded knowledge is filed under `source_type` categories that no ai-worker lane retrieves (pre-existing, `packages/retrieval/query_builder.py`). The test and the gate file the procedure under the lane category as a workaround; the real fix is task 7.18.
  - _Requirements: R13.3, R13.5, R16.1_
  - Audit 2026-09-28: PASS WITH NOTES (`make ci` green, unit 1522, integration 191). `tests/integration/test_business_data_e2e.py` drives the composed ai-worker with the stub LLM: Alice's email carries `ORD-82915` `FOUND` with its seeded status and the procedure chunk's `[CITATION: …]` line, Edward's carries `ORD-9901` `NOT_FOUND` with no status leak and `TICK-4402` `FOUND` `open`, and `business_lookups_total` moves for each. `scripts/phase5_gate.py` and `make phase5-gate` exist and are unit-tested (`tests/unit/test_phase5_gate.py`); they have not been run live. Findings: none.
  - Live gate passed on 2026-09-28 (`PHASE 5 GATE OK`, evidence below).
  - Closed 2026-09-28 after the regression runs on the Gemini stack, all run by the owner:
    - `make smoke` (`SMOKE OK`): the billing email reached `DRAFTED` with one draft, the newsletter exited early, and the cross-tenant sync was dead-lettered.
    - `make retrieval-gate` (`RETRIEVAL GATE OK`): vector hits with a 1536-dim query, and the ai-worker retrieved one chunk via the embedded query.
    - `make phase4-gate` default mode (`PHASE 4 GATE (default) OK`): the 12-message thread reached `DRAFTED` with summary v3, escalated to `high_capability` for `insufficient_retrieval_evidence`, with 0 citations and no mismatch. The one-message thread drafted with no summarization.

> **Phase 5 gate:** an order-status email produces a draft containing the actual order status from the business tables, with a knowledge citation for the procedure — demonstrating the knowledge/transactional distinction. The live draft comes from a real model (Gemini API through the OpenAI-compatible endpoint), because the stub LLM cannot state a status or cite a chunk. The stub-LLM integration test in 5.6 proves the context side in CI.
>
> **Gate evidence (2026-09-28, live stack, Gemini API, `make phase5-gate`; the owner ran `make up`, `make seed` and the gate):**
> - Setup: provider `openai` at `https://generativelanguage.googleapis.com/v1beta/openai`, models `gemma-4-26b-a4b-it` / `gemma-4-31b-it`, mock embedder. All app consumers were ready, and `alice.smith@clientcorp.com` was seeded with `ORD-82915` (`dispatched`). The procedure was active for every lane category (the 7.18 workaround).
> - Path: Alice's "Order status question" went normalize → triage → ai-worker, and the job reached `DRAFTED`.
> - Context: `CONTEXT_READY` recorded customer `FOUND`, `ORD-82915` `FOUND`, not degraded, and 1 chunk retrieved.
> - Draft: generated on `high_capability` (`gemma-4-31b-it`, escalation reason `insufficient_retrieval_evidence`). It reads: "Our records indicate that order ORD-82915, placed on 2026-09-28, has been dispatched. Please note that automated courier tracking numbers are provided once an order is packed at our central warehouse, and tracking links typically become active within 12 hours [060bc50c-…-01]." The status comes from `[BUSINESS DATA]`, and the procedure sentence is cited to the knowledge chunk filed under `billing`. The status is stated positively; the owner read the draft because the check alone would also accept a negation.
> - Not shown live: the cross-customer case (Edward asking about Dana's `ORD-9901`). It is proven by `tests/integration/test_business_data_e2e.py` with the stub LLM.

---

# Phase 6 — Mail Dispatch & Review Interface

*Deliverable: real incoming email → pipeline → generated reply → provider mailbox.*

> **Design (2026-09-28, ADR-0009):** dispatch sends exactly once through claim → provider draft → send → confirm (`specs/design.md` §5.8, §9). Mode is per category, default `create_draft`. The review UI is server-rendered (FastAPI + Jinja2 + htmx), local only, with no login (accepted scope limit). The live gate uses a Gmail test account with a short-lived token (`docs/demo-runbook.md` §3). Research: `artifacts/superpowers/2026-09-28-phase6-dispatch-review-research.md`.

- [x] **6.1 Draft management API**
  - `GET /v1/drafts` (filter by `status`, `category`, `mailbox`; cursor pagination), `GET /v1/drafts/{id}` (with the original email, thread summary, cited chunks and `[BUSINESS DATA]` facts), `PATCH /v1/drafts/{id}` (edit while `status=draft`), `POST …/approve`, `POST …/reject`. Every query is org-scoped.
  - Approve commits first, then publishes the dispatch job. A repeated approve re-publishes while the job is not `COMPLETED` (dispatch is idempotent), so a lost publish cannot strand an approved draft; it writes no second `feedback` row. Reject moves the job `DRAFTED → COMPLETED` with no send; a repeated reject returns the first result.
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1837, integration 232, e2e 7). Every bullet and R16.6, R23.2, R23.6 was traced to code and to a test. The repeated-approve re-publish is the reading the owner approved (D2); design §5.8's Review API line was rewritten to match. `GET /v1/drafts` uses keyset cursors (`{items, next_cursor, limit}`), not `PaginatedResponse`. Two fast approves write one `feedback` row (`tests/integration/test_drafts_api_integration.py::test_two_fast_approves_write_one_feedback_row`). The fix pass before flipping:
    - none
  - _Requirements: R16.6, R23.2, R23.6_

- [x] **6.2 Feedback capture**
  - Every decision writes one `feedback` row: `decision` (`accepted` / `edited` / `rejected`), `edited_body`, character-level `edit_distance`, optional `rating`, `reviewer` (free-text label) and `review_ms`. Migration 0005 adds `feedback.review_ms` and `UNIQUE (draft_id)`, plus the dispatch columns of 6.5.
  - Export `draft_decisions_total{decision, category}`; document the acceptance-rate and approved-without-edits PromQL in `docs/observability.md` (SC3).
  - Closed 2026-09-28 after a completion audit (PASS; `make ci` green, unit 1837, integration 232, e2e 7). Every bullet and R16.7, R21.4 was traced to code and to a test. `draft_decisions_total{decision, category}` is exported (`packages/observability/metrics.py`) and documented with the acceptance-rate and approved-without-edits PromQL in `docs/observability.md`. The fix pass before flipping:
    - none
  - _Requirements: R16.7, R21.4_

- [x] **6.3 Outbound reply construction**
  - Pure `build_outbound_reply(draft, original, thread)`: `In-Reply-To` = original `Message-ID`; `References` = original References + its `Message-ID`; a new `Message-ID` (Gmail/MIME only; Graph's `createReply` sets its own); exactly one `Re: ` before the original subject; the provider thread id (never our UUID); quoted original below the reply. Unit-tested, including subjects that already start with `Re:`/`RE:` and originals without References.
  - Entity changes: `OutboundReply` gains `message_id`, and its thread field carries the provider thread id as a string. A null `email_thread.provider_thread_id` (allowed since migration 0003) makes the dispatch fail permanently (dead-letter).
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1837, integration 232, e2e 7). R17.2 was traced to `packages/dispatch/reply.py` and `tests/unit/test_outbound_reply.py`, including `test_original_without_message_id`. The signature is `build_outbound_reply(*, draft, original, provider_thread_id, message_id_domain)` (the plan's contract). Graph drafts also carry our `Message-ID` as `internetMessageId` (owner decision D3, design §5.8), so the bullet's "Graph's `createReply` sets its own" is superseded. A missing recipient dead-letters too (`MissingRecipientError`). The fix pass before flipping:
    - none
  - _Requirements: R17.2_

- [x] **6.3a Adapter fixes & draft operations**
  - Discovered missing work (CLAUDE.md §7, Phase 6 research): the Graph adapter posts new messages (`/messages`, `/sendMail`) instead of replies and stores a request id as the message id; the Gmail reply has no `Message-ID`; both map every 403 to an expired token.
  - Graph: `createReply` + `send` with `Prefer: IdType="ImmutableId"`, real message ids. Gmail: set the reply's `Message-ID`. Both: 429, 5xx and rate-limit 403s are retryable with `Retry-After`; 400/404/auth are permanent.
  - Add `send_draft(mailbox, provider_draft_id)` and `get_draft_status(mailbox, provider_draft_id) -> DRAFT | SENT | MISSING` to `MailProviderAdapter`, the fake and both adapters, with the shared contract suite extended (recorded HTTP responses, no live calls).
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1837, integration 232, e2e 7). Every bullet and R1.1, R17.1, R17.2, R17.5 was traced to code and to the shared contract suite (recorded HTTP responses, run against the fake, Gmail and Graph). Graph is verified by recorded responses only, as ADR-0009 states; the owner accepted, as known limits, that Graph's `createReply` may add its own quoted original and that Graph ids stored at ingestion are not immutable ids (a moved original 404s and dead-letters). The adapters also gained `find_sent_message` and `find_draft` (orphan-draft lookup, 6.5). The fix pass before flipping:
    - none
  - _Requirements: R1.1, R17.1, R17.2, R17.5_

- [x] **6.4 Dispatch modes**
  - `dispatch_mode: create_draft | send_reply` per category in `config/categories.yaml`, default `create_draft` for every category.
  - `send_reply` requires an explicit approval unless the category's `auto_send_eligible` is true (false for every category); human-in-the-loop is the default posture.
  - Closed 2026-09-28 after a completion audit (PASS; `make ci` green, unit 1837, integration 232, e2e 7). `dispatch_mode` and `auto_send_eligible` were traced to `config/categories.yaml` (every category `create_draft` and `false`), `packages/domain/taxonomy.py` and their tests; the mode is read when the dispatch runs (`test_dispatch_mode_is_read_when_the_dispatch_runs`). Only approve publishes a dispatch job; the owner confirmed no automatic trigger, so R17.6 holds trivially. The fix pass before flipping:
    - none
  - _Requirements: R17.1, R17.6, R16.8_

- [x] **6.5 Idempotent dispatch**
  - The `dispatch-worker` consumes `email.dispatch` and runs design §5.8's five steps on the existing job: claim the R19.2 key `key(org, mailbox, original provider_message_id, "dispatch")` into `generated_draft.dispatch_idempotency_key` (UNIQUE) with `DRAFTED → DISPATCHED`; create or reuse the provider draft (`provider_draft_id`, `provider_draft_message_id`); in `create_draft` mode complete there; in `send_reply` mode send, confirm with `get_draft_status` after an ambiguous failure (MISSING ⇒ look for our sent message in the provider thread, else dead-letter), then finish in one transaction.
  - State machine: add `RETRY_PENDING → DISPATCHED` (operator replay of a dead-lettered dispatch) to `packages/domain/state_machine.py` and design §8. The lease reaper skips `DISPATCHED` jobs.
  - Forced-redelivery tests that crash the worker after each step and assert exactly one provider send and one provider draft.
  - Replace the `dispatch-worker` placeholder in `docker-compose.yml`.
  - Closed 2026-09-28 after a completion audit (PASS; `make ci` green, unit 1837, integration 232, e2e 7). Every bullet and R17.3, R18.3, R18.7, R19.2, R19.3 was traced to code and to a test. `tests/integration/test_dispatch_worker_integration.py::test_worker_killed_after_each_step_sends_exactly_once` kills the worker around the claim, the provider draft and the send in both modes and asserts one provider draft and at most one send; the finish and confirm steps are covered by `test_worker_killed_before_finish_resumes_without_a_second_draft` and `test_republished_dispatch_after_completion_sends_nothing`. The claim (`test_claim_sets_key_queue_and_dispatched_in_one_transaction`), the `RETRY_PENDING → DISPATCHED` edge (`tests/unit/test_state_machine.py`), the reaper skip (`test_reaper_skips_dispatched_jobs` and `_postgres`, which fails with `DISPATCHED` removed from the reaper query) and the no-lease rule (`tests/unit/test_dispatch_worker.py`) are each pinned. The fix pass before flipping:
    - none
  - _Requirements: R17.3, R18.3, R18.7, R19.2, R19.3_

- [x] **6.6 Dispatch completion & failure**
  - Success ⇒ persist the provider ref, draft `dispatched`, `DISPATCHED → COMPLETED`.
  - Transient failure (429, 5xx, Gmail rate-limit 403) ⇒ the job stays `DISPATCHED` and the broker retry ladder redelivers; a `Retry-After` picks the first ladder tier ≥ its value, capped at the last tier. Permanent (400, 404 on send, auth, null provider thread id) ⇒ `DISPATCHED → FAILED → DEAD_LETTER` with the provider error retained on the job.
  - Closed 2026-09-28 after a completion audit (PASS; `make ci` green, unit 1837, integration 232, e2e 7). R17.4, R17.5 were traced to `services/dispatch_worker/failure_policy.py` and `packages/broker/backoff.py`: a `Retry-After` picks the first ladder tier at least as long, capped at 30 m (`tests/unit/test_retry_after_tier.py`), and 400, 404 on send, auth and a null provider thread id dead-letter with the provider error kept on the job (`test_failure_policy`, `test_permanent_failure_dead_letters_with_the_provider_error`). The fix pass before flipping:
    - none
  - _Requirements: R17.4, R17.5_

- [x] **6.7 Outbound message write-back**
  - In `send_reply` mode, record the sent reply into `email_message` as `direction='outbound'` in step 5's transaction and update the thread, so subsequent inbound messages see the full conversation.
  - In `create_draft` mode nothing is recorded at dispatch (the customer has received nothing). Check whether mailbox sync ingests the mailbox's own sent mail and does not re-triage it; if it does not, add the gap to this task's notes rather than recording unsent drafts.
  - Check result (2026-09-28, by code reading and recorded-response tests): Gmail sync did ingest the mailbox's own mail. `history.list` and the initial `messages.list` had no label filter (`packages/adapters/gmail.py`), the normalizer marks everything `inbound`, and triage does not look at `direction`. So in `create_draft` mode our own provider draft came back as a new inbound email, and after a `send_reply` dispatch the draft deleted by `drafts.send` made the next sync fail on a 404. Fixed in 6.7: sync reads `labelId=INBOX` only, and a message deleted between listing and fetch is skipped (`tests/unit/test_gmail_adapter.py::test_incremental_sync_asks_only_for_inbox_messages`, `::test_sync_skips_a_message_deleted_after_history_listed_it`). A sent copy is also deduplicated by `provider_message_id` because step 5 records it. Left as a gap: an email the account sends to itself carries `INBOX` and would still be triaged; the Graph adapter's delta sync was not checked (verified by recorded responses only, ADR-0009).
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1837, integration 232, e2e 7). R17.7 was traced to `PostgresDispatchStore.finish`, which inserts the outbound `email_message` and touches the thread in step 5's transaction (`test_finish_send_reply_writes_one_outbound_row_and_updates_the_thread`, `test_finish_create_draft_writes_no_outbound_row`). The owner approved E1 (INBOX-only Gmail sync, skip a message deleted before fetch); the check result above matches the code. The two gaps it names stay open. The fix pass before flipping:
    - none
  - _Requirements: R17.7_

- [x] **6.8 Review UI**
  - Server-rendered pages in the `frontend` service (FastAPI + Jinja2 + htmx) calling only `/v1`:
    - pending-draft queue: original email, thread summary, citations next to the sentences they support, `[BUSINESS DATA]` facts highlighted, edit / approve / reject;
    - job timeline and current state per message (from `processing_event`);
    - knowledge upload with per-document ingestion status.
  - Local only, no login (ADR-0009): bind the `frontend` and `api` ports to `127.0.0.1` in `docker-compose.yml`. New settings `FRONTEND__API_BASE_URL` and `FRONTEND__ORGANIZATION_ID` (sent as the `X-Organization-Id` header, R23.6) in `.env.example` and `docs/configuration.md`. WCAG 2.2 AA basics: visible focus, 24 px targets, live-region status messages. Playwright tests for the approve and edit flows.
  - Closed 2026-09-28 after a completion audit (PASS WITH NOTES; `make ci` green, unit 1837, integration 232, e2e 7). R23.4, R23.5, R23.7 were traced to `services/frontend/` (it calls only `/v1` and imports no DB or broker code), `docker-compose.yml` (`frontend` and `api` on `127.0.0.1`), `FRONTEND__*` in `.env.example` and `docs/configuration.md`, and `tests/e2e` (approve, edit and accessibility flows on Chromium). The UI lives in `services/frontend/` (owner decision D1, design tree updated). Business facts show statuses, not values (owner accepted). The fix pass before flipping:
    - none
  - _Requirements: R23.4, R23.5, R23.7_

- [x] **6.9 Full end-to-end smoke test**
  - Fixture email → ingest → normalize → triage → context → RAG → generate → approve → dispatch, asserted in CI against fakes, ending with exactly one fake-provider draft (or send) and an outbound `email_message`.
  - Closed 2026-09-28 after a completion audit (PASS; `make ci` green, unit 1837, integration 232, e2e 7). R24.7 was traced to `tests/integration/test_phase6_pipeline_e2e.py`, which runs every hop through the production builders (sync orchestrator, email, triage, ai and dispatch workers, and the API's approve) with no hand-inserted job: `test_create_draft_mode_ends_with_one_provider_draft_and_no_outbound_message` and `test_send_reply_mode_ends_with_one_send_and_one_outbound_message`. The fix pass before flipping:
    - none
  - _Requirements: R24.7_

- [~] **6.10 Connect a real Gmail mailbox & live gate**
  - `make connect-gmail ADDRESS=…` registers the test account as a watched mailbox with `credentials_ref=env:GMAIL_ACCESS_TOKEN`; compose forwards `GMAIL_ACCESS_TOKEN` to the services that call Gmail; a blank `GMAIL_ACCESS_TOKEN=` and the `dispatch_mode` key go into `.env.example` / `docs/configuration.md`.
  - `make phase6-gate`, run live by the owner: a real email to the test inbox becomes a reviewable draft; approving it (with that category set to `send_reply` for the gate) delivers a correctly threaded reply in Gmail; the reply appears in our thread; replaying the dispatch job sends nothing.
  - Complete the **[after Phase 6]** sections of `docs/demo-runbook.md` (§3.4, §5.3, §6).
  - Completion audit 2026-09-28 (PASS WITH NOTES; `make ci` green, unit 1837, integration 232, e2e 7): `make connect-gmail`, `make phase6-gate`, the compose token forwarding, the `.env.example` / `docs/configuration.md` keys and runbook §3.4, §5.3 and §6 are in place; no test needs `GMAIL_ACCESS_TOKEN`, and `tests/conftest.py` strips it. Nothing has run live yet.
  - Left: the owner runs the live gate against the Gmail test account (runbook §3 and §5.3): put a fresh `GMAIL_ACCESS_TOKEN` in `.env`; set `billing` to `dispatch_mode: send_reply`; `make up` (its bootstrap applies migration 0005, still pending on the host database), `make seed`, `make connect-gmail ADDRESS=<test account>`; `make phase6-gate`, sending the email it asks for from another address, until it prints `PHASE 6 GATE OK`; set `billing` back to `create_draft` and `make up`; then `make smoke` and `make phase5-gate`. The Phase 6 gate evidence block is written from that output afterwards.
  - _Requirements: R17.1–R17.7_

> **Phase 6 gate:** a real email sent to a connected mailbox produces a reviewable draft; approving it delivers a correctly threaded reply to the provider; the reply is visible in the thread; replaying the dispatch job sends nothing further.

---

# Phase 7 — Observability & Evaluation

*Deliverable: quantitative evidence supporting or rejecting H1–H5.*

- [ ] **7.1 Complete span coverage**
  - Spans for every stage in `design.md §10`, with the documented attributes; one email = one trace across all queue hops.
  - _Requirements: R21.1, R21.2_

- [ ] **7.2 Complete metric coverage**
  - Every metric named in R21.4, with latency as histograms supporting p50/p95/p99 and low-cardinality labels only.
  - Include the "Draft persistence < 50 ms" target (NFR9, proposal latency table): a draft-persistence latency histogram, or the `draft.persist` span from 7.1, measured against it. Count triage calls in `llm_calls_total{kind="triage"}` (today only generate and repair are counted there).
  - _Requirements: R21.4, R21.5, NFR9_

- [ ] **7.3 Cost accounting**
  - Config price table per model; per-inference cost, aggregated per email, per category, per day.
  - Count the billed tokens of a first generation whose response could not be parsed (`LLMSchemaValidationError` carries no usage today; carry it from `packages/llm/client.py` and `packages/llm/anthropic.py`), so a repaired draft's cost includes both calls.
  - _Requirements: R21.6_

- [ ] **7.4 Grafana dashboards**
  - Funnel (with the 45/20/35 comparison), latency breakdown vs NFR targets, queue health, retrieval quality, cost.
  - Provisioned as code so `make up` yields working dashboards.
  - _Requirements: R21.7, R21.8_

- [ ] **7.5 Benchmark dataset expansion & re-annotation**
  - The seed sets were built in task 0.13. This task **expands** them to evaluation scale and hardens the labels.
  - Grow the classification set with real traffic collected during Phases 1–6, preserving the frozen test split so earlier numbers stay comparable.
  - Grow the retrieval set against the real knowledge corpus; add hard negatives and near-miss cases.
  - Second-annotator pass with inter-annotator agreement reported; document the process in `evaluation/README.md`.
  - _Requirements: R22.1, R22.2_

- [ ] **7.6 Experiment runner framework**
  - Common runner: load dataset → sweep one axis → write `manifest.json` (git SHA, config hash, dataset version, models, timestamp) + `metrics.csv` + `report.md`.
  - _Requirements: R22.12_

- [ ] **7.7 exp01 — triage cascade (H2)**
  - Rules-only vs rules+ML vs full cascade; accuracy, precision, recall, macro-F1, confusion matrix, cost per 1k emails.
  - _Requirements: R22.1, R22.4_

- [ ] **7.8 exp02 — retrieval modes (H1)**
  - Vector-only vs FTS-only vs hybrid vs hybrid+rerank; Recall@K, Precision@K, MRR, nDCG@K.
  - _Requirements: R22.2, R22.3_

- [ ] **7.9 exp03 — context size**
  - Top-3 / Top-5 / Top-10; quality, tokens, latency, cost. Settle the production Top-K from this.
  - _Requirements: R22.9_

- [ ] **7.10 exp04 — thread context (H3)**
  - Full thread vs summarized thread; token consumption and response quality.
  - _Requirements: R22.5_

- [ ] **7.11 exp05 — model cascade (H4)**
  - Single high-capability model vs cascade; cost and quality.
  - _Requirements: R22.6_

- [ ] **7.12 exp06 — load & scalability (H5)**
  - 1× / 5× / 10× / 20× the 1.16 msg/s reference rate; throughput, queue depth, p50/p95/p99 latency.
  - Demonstrate throughput increase when worker replicas are added.
  - _Requirements: R22.7, R20.4, NFR12_

- [ ] **7.13 exp07 — failure recovery**
  - Kill a worker mid-job; assert redelivery, completion, and exactly one logical result.
  - _Requirements: R22.8, R19.7, R19.9_

- [ ] **7.14 exp08 — corpus growth**
  - Retrieval latency and recall as the corpus grows; identify the PostgreSQL → dedicated-search migration threshold.
  - _Requirements: R22.11_

- [ ] **7.15 exp09 — RAG vs non-RAG baseline**
  - Response quality with and without retrieved knowledge.
  - _Requirements: R22.10_

- [ ] **7.16 Response-quality rubric & review round**
  - Rubric: factual correctness, relevance, completeness, evidence consistency, clarity, thread awareness, redundancy.
  - Human review round producing acceptance rate, edit rate, edit distance, and ratings.
  - _Requirements: R22.10, R16.7_

- [ ] **7.18 Knowledge category ↔ lane mapping**
  - Discovered missing work (CLAUDE.md §7, 2026-09-28, Phase 5 planning): retrieval filters `d.category` on the classification category, but the seed files knowledge under `source_type` categories (`fulfillment`, `policy`, …) that no lane uses, so seeded knowledge is unreachable from every ai-worker lane. Map document categories to lane categories (or index documents under the lanes that may cite them) so evaluation runs on reachable knowledge. Must land before 7.15–7.17 produce results.
  - _Requirements: R9.5, R10.4, R12.4_

- [ ] **7.17 Success-criteria report**
  - Single report measuring SC1–SC10 against targets, with the funnel, latency percentiles, and cost per generated email.
  - Report measured values honestly; do not tune the dataset to hit a target.
  - _Requirements: R22.12_

- [ ] **7.19 AgentMailGuard prompt-injection benchmark (C0 vs C3)**
  - Spec: `docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md`; decision: ADR-0010. AgentMailGuard (separate branch, editable worktree install) wraps rag-email's real `ContextBuilder` output and one `reply.v1` generation call through its own integration adapters; rag-email adds no defence logic (CLAUDE.md §6, requirements §0.5), so no R-requirement covers the defence itself and the task is traced to the evaluation requirements it exercises.
  - Configs: `C0` = rag-email exactly as it runs (`generate_draft`, its own profile template, no AgentMailGuard code); `C0T` = `MailGuardPipeline.run` with preset `C0` (guard template, no layer active); `C3` = every layer on. C0, C0T and C3 are required over the full case set; McNemar compares C0 vs C3 (headline) and C0T vs C3.
  - Cases: 300 LLMail-Inject phase-2 attacks from the benchmark half (stratified by scenario, seed 20260930) + 150 benign emails; RAG-vector attacks ingested into an isolated evaluation organization; `C1`/`C2` optional reduced ablation on a fixed 100-attack subset. Case ids are fixed in a case manifest.
  - Scorecard: ASR (headline; target C3 ≤ 5 % on LLMail-Inject, stated as "met / not met" with the Wilson interval), DER, TMR N/A, ASR by scenario and by vector, McNemar exact p, FPR, benign utility, latency p50/p95/p99 split guard vs generation, tokens, model calls and cost per email; no-API analyses: TF-IDF leakage check, first catching layer, worked examples, threat model and limitations.
  - Resilience: each case is written as it finishes; reruns skip recorded cases; HTTP 429 gets back-off; failed cases are reported as errors, never as defended.
  - Artifacts: `evaluation/results/mailguard_bench/<run_id>/{manifest.json,metrics.csv,report.md,analyses.md}`. Code is unit-tested on the fake provider; live runs are owner-run (`make mailguard-bench`, runbook §9) and never in `make ci`.
  - _Requirements: R22.12, R21.5, R21.6, R24.5, SC4, SC5, SC9_

- [~] **7.20 AgentMailGuard live pipeline benchmark, v2 (every service live)**
  - Spec: `docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md`; decision: ADR-0011 (builds on ADR-0010). v1 (7.19) stays as it is: the reply path in-process on a mock embedder. v2 sends each case through rag-email's own services (MinIO, email-worker, triage, the lane queues, then the ai-worker or the guard-worker, with Gemini embeddings and the cross-encoder reranker), one benchmarked model per run in every LLM role. v2 runs use new `RUN` names (`2026-09-29-<model>-live`) and a `transport: services-v2` fingerprint key, so they never mix with v1. rag-email adds no defence logic (CLAUDE.md §6, ADR-0010): the guard runs in an evaluation guard-worker outside rag-email, through one generic hook. Nothing is ever approved or sent.
  - A. rag-email fixes and hooks: triage stage-3 schema meets OpenAI Structured Outputs (R6.1, R6.3); `SUMMARIZATION__SUMMARIZER_MODEL` honoured (R8.3); retrieval budget 500 → 3000 ms (R10.9); `html` bucket, and bucket names from settings (R4.1); embedder tolerates items without `index` and a response without `usage` (R5.10, R9.6); `drafting_factory` on `build_consumers`; a `context_built` event (R21); `create_eval_mailbox` in `packages/adapters/evaluation.py`, its only caller the feeder.
  - B. Cross-encoder reranker in the reply path: CPU-only torch, the model baked into the image, RRF order kept on unavailability or timeout (R11.1–R11.5).
  - C. Guard-worker `evaluation/mailguard_bench/live/guard_worker.py`: a `GuardedDraftingService` with the ai-worker's interface, one audit line per job, every guard LLM stage (L1, L2, L3b, L4) on in C3.
  - D. Live runner and feeder `live/run.py`: each case KB through the API, the email as a MIME message archived and enqueued as the mail-connector does after a fetch, polled to a terminal state; `mailguard-bench-result.v3` rows; MinIO and organization cleanup; fingerprint keys for the transport, embedding, reranker, triage, images and Ollama.
  - E. Scoring and report: pipeline ASR and guard ASR with Wilson intervals (the C3 target is judged on the guard ASR), a triage table per config, guard FPR, pipeline benign utility, and a meaning-based second column (`meaning.py`; rubric v1 pre-registered 2026-09-29, before any v2 run; a reader model that is not a benchmarked model, recorded before the runs).
  - F. Ops and docs: compose host alias and forwarded settings, `live/stack_env.py` (per-model container settings in a git-ignored `.env.stack`; it refuses a host `.env` that disagrees with them, since the guard-worker and the runner read `.env`, not the stack env), runbook §9.9, ADR-0011, `docs/configuration.md`, `.env.example`.
  - Live runs are owner-run (runbook §9.9), one model and one config at a time (the containers hold one model's settings, the lane queues one drafting consumer), and never in `make ci`; unit tests use fakes (R24.5).
  - **Config scheme (work package R5; ADR-0012 decision 11; pre-registered as Amendment 2 of the v2 design, written before any v2 run).** The names C0 to C7 have two meanings, recorded as `scheme` (`"v1"` or `"v2"`) in every run's meta and settings fingerprint: new runs are v2, a run folder never mixes the two (runners, guard-worker and report refuse), and a meta without the key is v1 and keeps its presets, case selection (C1 and C2 on the reduced subset), C3-L1..C3-L5 ablation and report byte for byte. Scheme v2: C0 no guard, C0T the guard's template with no layer, C1 L1+L5, C2 L2+L5, C3 L3+L5, C4 L3b+L5, C5 L4+L5, C6 L5 alone (a control), C7 every layer; every config on all 550 pinned cases per model, built from explicit layer flags (no guard preset) by the in-process runner, the live runner and the guard-worker, with the live AI stages C1 the L1 judge, C2 L2's AI step, C4 L3b's, C5 L4's, C7 all four, none for C0, C0T, C3 and C6. The report judges the target on C7's guard ASR (at most 5 %) and adds paired exact McNemar tests of C1..C6 against C0T and C7 against C0, and a control check of C6 against C0T. The sentences of A–F above that say "C3", "C0T/C1/C2/C3" or "the C3 target" describe scheme v1, the vocabulary they were built in; in scheme v2 they read C7 and the configs of Amendment 2.
  - Status: A–F and R1–R3 are merged on the integration branch (R1 reporting: AI-step fallbacks, the two benign-utility rules, the fail-closed sensitivity line, template-path successes, the L2 recount; R2: the guarded prompt carries rag-email's reply-format rules, guard pin `1a3ef62`, the prompt version in every meta; R3: the lexical branch ORs the query terms, the default lane consumers come from the category taxonomy, and 7.21's router relevance check). R5, the config scheme, the v2 configs, their report, the Make target and runbook §9.9, and the pre-registration, is built in this change. Left: (1) the owner-run v2 runs and their reports for the three models, the teammate's main run on Friday 2026-10-02 with the kit of ADR-0012 decision 9, after live smoke runs through all nine configs on the desktop (runbook §9.9 step 5; ADR-0012 decision 1); (2) the v2-config cases of `tests/integration/test_mailguard_bench_guarded_run.py` are written but need Postgres and were not run when they were written; (3) the model route for Qwen and Llama, decided at the owner's meeting on 2026-10-01 at 20:00 (OpenRouter support, if chosen, is on branches not merged here); (4) task 7.23, the no-API analyses for scheme v2; (5) decision 6, one repository on `main`, once decision 1 holds and a final audit finds nothing open.
  - _Requirements: R22.12, R4.1, R5.10, R6.1, R6.3, R8.3, R9.6, R10.9, R11.1–R11.5, R20.6, R21.3, R21.4, R21.6, R24.5, SC4, SC5, SC9_

- [x] **7.21 Router relevance check when the rerank falls back**
  - Found in 7.20 B: trigger 3 of `ComplexityRouter` (`packages/llm/router.py`, `_extract_chunk_score`) counted the chunks whose best score is at least `ROUTER_MIN_RELEVANCE_SCORE` (0.50, a probability). A reranked chunk carries a cross-encoder probability, but a chunk that was not reranked carries only its RRF `fused_score`, at most 2/61 (about 0.03), which never reaches 0.50. Every RAG job whose rerank was off, unavailable or over `RETRIEVAL__RERANK_TIMEOUT_MS` therefore escalated to `high_capability` with `insufficient_retrieval_evidence`, so the tier of an otherwise identical job depended on whether the reranker answered in time, and the escalation counted toward the R15.5 cap.
  - **Decision (owner, ADR-0012 decision 5): option (a).** The relevance check applies only when `ContextPackage.rerank_applied` is true. Otherwise (false, or None when unknown) only the chunk-count check applies: fewer retrieved chunks than `ROUTER_MIN_RETRIEVED_CHUNKS`, and no chunks at all, still escalate. R15.3's wording says so (no ID renumbered or removed).
  - Built: the check in `packages/llm/router.py` (the escalation details carry `rerank_applied`, and `min_relevance_score` only when the bar was applied); `tests/unit/test_complexity_router.py` (new cases for a reranked, a fallback and an unknown pool) and `tests/unit/test_context_builder_rerank.py::test_after_a_fallback_the_router_applies_only_the_chunk_count_check`, which replaces the test that pinned the old behaviour; the `ROUTER_MIN_RELEVANCE_SCORE` description in `docs/configuration.md`, `.env.example` and the setting.
  - _Requirements: R15.3, R15.5, R11.5_

- [~] **7.22 AgentMailGuard layer ablation (C3 minus one layer)**
  - Pre-registration: `docs/superpowers/specs/2026-09-30-mailguard-layer-ablation-design.md`. Extends 7.19: `C3-L1` ... `C3-L5` run the guard's own "C3 minus one layer" presets over the same 550 pinned cases, with same-run C0 and C3 as baselines. rag-email adds no defence logic (CLAUDE.md §6); this is evaluation code only.
  - Scorecard: ASR per vector with Wilson 95 % intervals, FPR, benign drafts of at least 40 characters that were not blocked, exact McNemar of each config against the same-run C3 with the pre-registered necessity test ("removing the layer raises the ASR, p < 0.05"), and per config which layer first stopped and which layers flagged each attack.
  - Done: runner, guard build, report and tests (unit-tested on fakes, runbook §9.6a). The `C3-L1` ... `C3-L5` configs are scheme v1 only (ADR-0012 decision 11): run them with `SCHEME=v1`.
  - Left: the live runs (owner-run, gpt-4o-mini) and the write-up of the results, including null results.
  - _Requirements: R22.12_

- [ ] **7.23 No-API analyses for scheme v2 (C7 as the full guard)**
  - Found in 7.20 R5: `analyses.py` (leakage restatement, first catching layer, worked examples, threat model) and the headline lines it feeds read `C3` as the full guard. In scheme v2 (ADR-0012 decision 11) the full guard is `C7` and `C3` is channel isolation, so `make mailguard-analyses` refuses a v2 folder (`analyses.run_analyses`) and the v2 report has no `analyses.md`; reading C3 instead would be a wrong number, so it is refused, not adapted on the side.
  - Build: take the full-guard config from `scheme.target_config(scheme)` in `analyses.py`, `examples.py`, `first_layer.py` and the report's restated lines; decide with the owner which v2 analyses make sense per layer (first catching layer of C7, near-duplicate-free C7 guard ASR, worked examples of C7 against C0); tests first, on fake rows; v1 folders keep byte-identical output (the goldens).
  - _Requirements: R22.12_

- [~] **7.24 Single-repository layout (AgentMailGuard under `agentmailguard/`)**
  - Decision: ADR-0012 decision 6 (after the final merge the guard is committed inside this repository as a git subtree with full history). The benchmark kit (7.23) and the Make targets must work in that layout and in today's separate-worktree layout with the same names, so the owner's teammate clones one repository and needs no sibling checkout.
  - Scope: `guard_env.worktree_info` / `require_pinned_worktree` verify a subtree by tree id (`git rev-parse HEAD:agentmailguard` equals the pinned commit's tree, `git status --porcelain -- agentmailguard` empty, ignored files not counted) and a worktree by HEAD as before; the v1 and v2 tree ids sit next to the pinned commits, so a shallow clone verifies; the commit that reaches run metas and manifests is the pinned guard commit (any commit whose tree the subdirectory holds: `MAILGUARD_COMMIT` or the explicit argument, not only the v1 and v2 pins), never the enclosing repository's HEAD; `make mailguard-prep` writes to `MAILGUARD_PREP_OUT`, never into the pinned directory; `amg.resolve_mailguard_dir` uses the Makefile's default (`guard_env.default_guard_dir`); the Makefile defaults `MAILGUARD_DIR` to `./agentmailguard` and `MAILGUARD_ARTIFACTS` to `evaluation/mailguard_bench/pinned` when they exist, and `mailguard-worktree` only verifies the pin in the subtree layout; `ruff`, `mypy`, `pytest`, `.dockerignore` and uv never see the subtree; the guard's top-level `services` and `evaluation` never shadow rag-email's (`require_module_origins` now demands the origin under rag-email's own `<name>/` directory, because a subtree copy is under the repository root too).
  - Done: the code, the Make defaults, the tooling excludes, `tests/unit/test_mailguard_single_repo_layout.py`, runbook section 9, and a rehearsal of the merge in a disposable clone (`git subtree add`, then ruff, mypy, the unit suite with and without the overlay, `guard_smoke`).
  - Left: the final merge itself (owner decision, ADR-0012 decision 6), and then removing the worktree layout text once no machine uses it.
  - _Requirements: R22.12, R24.5_

- [~] **7.25 Teammate benchmark kit (Windows 11 Home, WSL2 first)**
  - Decision: ADR-0012 decision 9 (the teammate runs the full v2 benchmark from a fresh download, with their own keys; results come back as ours do), decision 15 (the L1 classifier is not redistributed) and decision 16 (Docker Engine inside WSL2 Ubuntu is the primary route). The teammate's laptop: Windows 11 Home, Intel Core i7-13700HX, 24 GB RAM, NVIDIA RTX 4050 Laptop GPU with 6 GB, about 63 GB of free disk; `gpt-4o-mini` by API, Qwen2.5-7B and Llama-3.1-8B locally on Ollama, one model completely before the next. The kit automates `docs/demo-runbook.md` section 9.9 (steps 3 to 7) and never hard-codes a route or a model list: it takes `--model-profile` and uses the profile's own settings. It adds no defence logic (CLAUDE.md section 6): it is evaluation tooling.
  - Done, runner side (work package R6a): `evaluation/mailguard_bench/kit/campaign.py` (`setup`, `run`, `report`, `package`) automating runbook §9.9 steps 3 to 7 for one model, the default config list taken from `scheme.configs_for("v2")`; `make bench-setup`, `bench-run`, `bench-report`, `bench-package` (R7 added `bench-doctor` and the `MAILGUARD_ARTIFACTS` default: the pinned folder when the joblib is in it, else `../AgentMailGuard-bench-artifacts`); a Windows-safe guard-worker liveness check and stop (`live/process.py`).
  - Done, environment side (work package R6b, cherry-picked onto the integrated code and changed for decisions 15 and 16 in work package D4):
    - [x] `.gitattributes`: LF in every working tree (`* text=auto eol=lf`), explicit binary types, byte-exact MIME fixtures and result CSVs; `tests/unit/test_kit_gitattributes.py`.
    - [x] The pinned inputs: `evaluation/datasets/mailguard/cases.jsonl` (sha256 `c00dddca...`) and `manifest.json`, the classifier's metrics file, `SHA256SUMS` and `NOTICE.md` are **committed**; the teammate needs neither `make mailguard-prep` nor `make mailguard-cases`. `NOTICE.md` names each source dataset with its license and link, says why the classifier is not in git, and carries verbatim the LICENSE files of microsoft/llmail-inject-challenge (MIT, Copyright (c) Microsoft Corporation) and sleeepeer/PoisonedRAG (MIT, Copyright (c) 2024 Runpeng Geng), read on 2026-10-01.
    - [x] Decision 15: `l1_injection_clf_v1.joblib` (sha256 `8fc1cbe7...`, scikit-learn 1.9.1) is **in no commit**: its exact path is in `.gitignore`, the owner sends it privately, the teammate puts it in `evaluation/mailguard_bench/pinned/`, and `SHA256SUMS` still lists its sha256. `kit/pinned.py` (`verify`, `verify_committed`, `classifier_problem`) and the doctor report a missing file as FAIL with the fix "ask the owner for l1_injection_clf_v1.joblib (it is not in git, see NOTICE.md), put it in evaluation/mailguard_bench/pinned/; its sha256 must be <sha>", and a file with another sha256 stays a FAIL; `make bench-setup` refuses both through `pinned.verify`. `tests/unit/test_kit_pinned.py` (also runs on a clone without the file).
    - [x] `kit/doctor.py` (`make bench-doctor`, `python -m evaluation.mailguard_bench.kit.doctor [--model-profile M] [--reader R]`): one `ok|WARN|FAIL` line and a fix hint per check, exit 1 on any FAIL; platform, repository not under `/mnt/<drive>`, systemd, no CRLF, docker CLI, daemon and Compose v2, Docker memory (8 GB), disk (25 GB, and the Windows C: drive under WSL2), uv, make, Python 3.12, `.env`, keys by name only, settings against the stack env, no benchmark setting exported in the shell, the two old `.env` lines, the pinned guard (fix: `make mailguard-worktree`), the inputs in git, the L1 classifier, the classifier folder the runners read (the shell's `MAILGUARD_ARTIFACTS`, else the Makefile's conditional default, resolved the way make does), scikit-learn, published ports, for a local profile Ollama and its model, whether the containers can reach it (a loopback Ollama is OK when `<docker0 address>:11434` answers, e.g. behind the socat forwarder) and whether the model is loaded, nvidia-smi, and a reader that is not a benchmarked model; no value that may be a key is ever printed; `tests/unit/test_kit_doctor.py` (docker, ports and the network are faked).
    - [x] `docs/BENCHMARK.md` (the WSL2 guide: Docker Engine inside Ubuntu, `.wslconfig`, power settings, get the project, `.env`, step D1 places the classifier file, doctor, setup, run per model, resume, local models with Ollama: load the model before each run because the runner refuses an unloaded one, containers reaching a loopback Ollama through the systemd override (sudo) or a socat forwarder bound to the docker0 address, run times estimated from the owner's 7-case smoke on an RTX 3060 (about 3 s per case and config for `gpt-4o-mini` at concurrency 2, 10 s for Qwen2.5-7B and 9 s for Llama-3.1-8B at concurrency 1: roughly 4 to 6 hours for `gpt-4o-mini` and 12 to 15 hours per local model, longer on 6 GB), sending results back, troubleshooting) and `docs/benchmark-windows-native.md` (Docker Desktop on Pro, PowerShell commands); `tests/unit/test_kit_docs.py` keeps both in step with the code (env block, profiles, ports, flags, Make targets and variables against the real Makefile, campaign arguments, the order of D1 before the doctor, the load command against the doctor's hint, the run-time arithmetic).
  - Left: a live run of the kit from a fresh download (ADR-0012 decision 1), which also needs the owner's private copy of the classifier and a teammate who has it; measured durations of full runs on the laptop (the guide's hours are an estimate from a smoke run; the kit log will show real ones); Windows itself was never exercised.
  - Not exercised: Windows. Everything was written and tested on Linux; Docker, Ollama, the network and the Makefile's shell are faked or read as text in the tests (R24.5). No live model, embedding or Docker call was made for this work package.
  - Numbering: this task's early commits say `[task 7.23]`; the number collided with 7.23 (v2 analyses) and was changed to 7.25 when the work packages were integrated.
  - _Requirements: R22.12, R24.5_

- [ ] **7.26 Rows that a live-service failure changed are retried error rows**
  - Decision: ADR-0012 decision 13 (owner, 2026-10-01). Found in the live smoke: a DNS stall made triage fall back to the safe default and the query embedding time out, and both rows were scored as normal.
  - Build: the live collector records such a case as an error row of its own kind (triage safe default after stage errors, not an abstention; retrieval degraded), so the retry pass runs it again; the report counts them per config; the v2 design gains Amendment 3 (pre-registered before any v2 run).
  - _Requirements: R6.11, R10.9, R22.12_

- [ ] **7.27 Reply prompts say they draft the reply to the customer**
  - Decision: ADR-0012 decision 14 (owner, 2026-10-01). Found in the live smoke: Llama-3.1-8B answered the general prompt's "concise response" with "Your email draft is ready.".
  - Build: every reply prompt whose task line does not say it drafts the reply email to the customer gets that wording as a new prompt version; the old versions stay for the v1 runs, whose recorded prompts and goldens do not change.
  - _Requirements: R14.6, R16.1_

- [ ] **7.28 Category retrieval floor and the benchmark's category-filter switch**
  - Decision: ADR-0012 decision 12 and ADR-0013 (owner, 2026-10-01). Found in the live smoke: no case reached retrieval (triage's model never asked for it, and case documents filed under `support` were filtered out by the live category).
  - Build: `retrieval_required` raised to the category's `default_retrieval_required` for replies routed to AI (setting, on by default); a setting that disables the category filter (on by default), turned off by the v2 benchmark's stack env and host check, recorded in the fingerprint; design §5.3 updated.
  - _Requirements: R6.6, R6.9, R12.4, R22.12_

> **Phase 7 gate:** every hypothesis H1–H5 has a reproducible artifact with a run manifest, and SC1–SC10 are reported with measured values.

---

# Phase 8 — Hardening, Scale & Migration Path

*Deliverable: the architecture's scaling and evolution claims are demonstrated, not asserted.*

- [ ] **8.1 Scale compose profile**
  - `docker-compose.scale.yml` with replica counts per worker class; verify N replicas coordinate through broker + database only.
  - _Requirements: R20.2, R20.3_

- [ ] **8.2 Autoscaling signal**
  - Queue-depth metrics exported in a form usable as an autoscaling input; document the mapping from signal to worker class.
  - _Requirements: R20.5, R7.5_

- [ ] **8.3 Quorum queue profile**
  - Configuration switch enabling quorum queues for the HA profile; verify the retry/DLQ ladder still behaves.
  - _Requirements: R3.9_

- [ ] **8.4 Resilience test suite**
  - Broker restart, PostgreSQL failover/restart, provider 429 and 5xx, LLM timeout, malformed LLM output, object-storage unavailability.
  - Assert: no lost jobs, no duplicate side effects, graceful degradation flags recorded.
  - _Requirements: R19.7, R19.8, R10.6, R11.5, R13.7, R16.3_

- [ ] **8.5 Backpressure & graceful shutdown under load**
  - Verify prefetch limits hold, shutdown drains cleanly mid-burst, and nothing is acked without its side effect committed.
  - _Requirements: R3.3, R3.4, R20.8_

- [ ] **8.6 OpenSearch migration path documentation**
  - Write the migration runbook for implementing `OpenSearchBackend` behind `SearchBackend`: dual-write, re-run exp02 against both, switch by config, with no pipeline change.
  - Do **not** implement it unless exp08 shows PostgreSQL retrieval is insufficient.
  - _Requirements: R10.7, R20.9, R24.6_

- [ ] **8.7 ADR completion**
  - ADRs 0001–0007 from `design.md §12.2` written, plus any decision made during implementation.
  - _Requirements: R24.6_

- [ ] **8.8 Operational runbook**
  - `docs/runbook.md`: mailbox re-auth, stuck sync, DLQ triage and replay, knowledge re-index, cost spike investigation, latency regression triage.
  - _Requirements: R20.9, R18.7_

- [ ] **8.9 Enterprise evolution documentation**
  - Document the scaled architecture (RabbitMQ cluster, managed PostgreSQL, S3, Kubernetes) and the per-service scaling signals, without requiring implementation.
  - _Requirements: R20.9_

- [ ] **8.10 Per-mailbox Gmail credentials only**
  - Discovered missing work (CLAUDE.md §7, Phase 6 planning, owner decision E5 on 2026-09-29): `resolve_provider_credentials` in `packages/adapters/registry.py` falls back to the `GMAIL_ACCESS_TOKEN` environment variable for any Gmail mailbox without a resolvable `credentials_ref`, so one token reaches every Gmail mailbox, including seeded demo mailboxes. Drop the fallback so a mailbox only gets the token its own `credentials_ref` names (`make connect-gmail` already sets `env:GMAIL_ACCESS_TOKEN`), and update the adapter-registry tests.
  - Until this lands: do not approve drafts of seeded demo mailboxes while `GMAIL_ACCESS_TOKEN` is set (`docs/demo-runbook.md`).
  - _Requirements: R1.1, R23.6_

> **Phase 8 gate:** a 20× burst is absorbed without job loss; adding AI-worker replicas measurably raises throughput; every failure scenario recovers with zero duplicates; the migration path is documented and the decision record is complete.

---

## Requirement coverage index

Use this to confirm nothing was dropped. Every requirement ID in `requirements.md` appears in at least one task.

| Requirement group | Tasks |
|---|---|
| R1 Provider abstraction | 1.1, 1.2, 1.3, 1.4, 1.7, 6.3a, 8.10 |
| R2 Ingestion & sync | 1.3, 1.4, 1.5, 1.6, 1.7, 1.8 |
| R3 Async distribution | 0.7, 2.11, 2.12, 4.13a, 4.13b, 8.3, 8.5 |
| R4 Normalization | 1.9, 1.10, 1.11, 1.12, 1.13, 7.20 |
| R5 Data platform | 0.4, 0.5, 0.12, 1.12, 3.4, 5.1, 7.20 |
| R6 Triage | 2.2–2.8, 2.9, 7.20, 7.26, 7.28 |
| R7 Routing | 2.1, 2.10, 2.15, 4.13a, 8.2 |
| R8 Thread state | 4.1, 4.2, 4.3, 4.13b, 7.20 |
| R9 Knowledge ingestion | 3.1–3.6, 3.16, 7.18, 7.20 |
| R10 Hybrid retrieval | 3.7, 3.8, 3.9, 3.10, 3.13, 3.16, 7.18, 7.20, 7.26, 8.6 |
| R11 Rerank & packing | 3.11, 3.12, 3.14, 4.12, 4.13b, 7.20, 7.21 |
| R12 Query construction | 3.13, 7.18, 7.28 |
| R13 Business data | 5.1–5.6, 8.4 |
| R14 Agent & LLM abstraction | 4.4, 4.5, 4.6, 4.7, 4.12, 5.0, 7.27 |
| R15 Model cascade | 4.8, 4.13a, 4.13b, 7.21 |
| R16 Structured output & drafts | 4.9, 4.10, 4.11, 4.13a, 6.1, 6.2, 6.4, 7.27 |
| R17 Dispatch | 6.3, 6.3a, 6.4–6.7, 6.10 |
| R18 State machine | 0.6, 2.1, 2.12, 2.14, 4.4, 4.11, 4.13a, 6.5 |
| R19 Idempotency & recovery | 0.8, 2.1, 2.12, 2.13, 4.13a, 4.13b, 6.5, 7.13, 8.4 |
| R20 Deployment & scale | 0.2, 0.3, 0.9, 4.13b, 5.0, 5.4, 7.12, 7.20, 8.1, 8.2, 8.6, 8.8, 8.9 |
| R21 Observability | 0.9, 2.8, 2.15, 3.14, 4.12, 5.0, 5.4, 6.2, 7.1–7.4, 7.19, 7.20 |
| R22 Evaluation | 0.13, 4.13b, 7.5–7.17, 7.19, 7.20, 7.22, 7.23, 7.24, 7.25, 7.26, 7.28 |
| R23 API & UI | 0.10, 1.8, 1.14, 2.14, 3.6, 3.15, 6.1, 6.8 |
| R24 Engineering baseline | 0.1, 0.6, 0.11, 1.2, 4.5, 4.13b, 5.0, 6.9, 8.6, 8.7, 7.19, 7.20, 7.24 |
| NFR1–NFR14 | 2.3, 3.14, 4.12, 7.2, 7.4, 7.12 |
| SC1–SC10 | 7.7, 7.8, 7.12, 7.13, 7.16, 7.17, 7.19, 7.20 |
| H1–H5 | 7.8 (H1), 7.7 (H2), 7.10 (H3), 7.11 (H4), 7.12 (H5) |

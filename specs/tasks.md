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

- [ ] **2.3 Lightweight ML classifier (triage stage 2)**
  - **Depends on task 0.13** — trains on the classification seed set built in Phase 0.
  - TF-IDF or embedding features + linear head; export as a loadable, versioned artifact.
  - Inference path budgeted at 20–50 ms; report held-out macro-F1 at this stage, not only in Phase 7.
  - _Requirements: R6.1, R22.1, NFR3_

- [ ] **2.4 Small-LLM fallback (triage stage 3)**
  - Structured-output classification call through `LLMProvider`; strict schema; short prompt.
  - _Requirements: R6.1, R6.3_

- [ ] **2.5 Cascade orchestration & thresholds**
  - Stop at the first stage meeting its threshold; never invoke later stages after a confident answer.
  - Thresholds configurable per organization and per category without redeploy.
  - Persist every result with stage, latency, model, raw output.
  - Safe default + review flag if all stages fail or return malformed output.
  - _Requirements: R6.2, R6.7, R6.9, R6.11_

- [ ] **2.6 Category taxonomy**
  - Implement support, sales, billing, administration, scheduling, general_inquiry, automated_notification, acknowledgement, no_response.
  - _Requirements: R6.4_

- [ ] **2.7 Early-exit gate — the cost lever**
  - `reply_required == false` ⇒ transition straight to `COMPLETED`; assert in tests that **no** embedding, retrieval, rerank, or generation call is made.
  - `retrieval_required == false` ⇒ RAG is skipped downstream.
  - _Requirements: R6.5, R6.6_

- [ ] **2.8 Deterministic template reply path — the missing 20%**
  - Emit `workflow_hint ∈ {template, ai, none}` from the cascade alongside `reply_required` and `retrieval_required`.
  - Template registry keyed by `(category, intent)` with variable substitution from message and business fields; versioned template files.
  - `workflow_hint='template'` ⇒ render and go to `DRAFTED` with **zero** retrieval and **zero** generation calls; assert this in tests.
  - No matching template ⇒ fall back to `workflow_hint='ai'`. The template path must never block a reply.
  - Verify the three outcomes (early exit / template / AI) are mutually exclusive and exhaustive over actionable mail.
  - _Requirements: R6.12, R6.13, R6.14, R6.15_

- [ ] **2.9 Funnel instrumentation**
  - Counters for all three outcomes so the realized funnel reconciles against the 45% / 20% / 35% assumption with no residual bucket, plus the ~70% RAG share of AI traffic.
  - `emails_templated_total` exported alongside `emails_generated_total`.
  - _Requirements: R6.10, R6.15, R21.4, NFR14_

- [ ] **2.10 Category-aware routing**
  - Publish to `email.<category>.<priority>`; `normal` and `priority` lanes with independent consumer scaling.
  - New categories addable by configuration; queues declared at startup.
  - Startup warning naming any category queue with no configured consumer.
  - _Requirements: R7.1, R7.2, R7.4, R7.6_

- [ ] **2.11 Worker micro-batching**
  - Pull N jobs together; each job still gets its own independent inference call.
  - Add an explicit test asserting no prompt ever contains two distinct emails.
  - _Requirements: R3.6, R3.7_

- [ ] **2.12 Retry, backoff & dead-letter**
  - `GENERATING → RETRY_PENDING → GENERATING` with exponential backoff and jitter; attempts exhausted ⇒ `FAILED → DEAD_LETTER`.
  - Failure reason and original routing key preserved in DLQ headers.
  - _Requirements: R19.5, R19.6, R3.5, R18.2_

- [ ] **2.13 Lease reaper**
  - Reclaim jobs stuck in a non-terminal state past `lease_expires_at`.
  - _Requirements: R19.8_

- [ ] **2.14 Job timeline API & replay**
  - `GET /v1/messages/{id}/timeline` returning ordered `processing_event` history.
  - `POST /v1/jobs/{id}/replay` to re-run a dead-lettered job from its last good state.
  - _Requirements: R18.6, R18.7, R23.2_

- [ ] **2.15 Queue metrics**
  - Per-queue depth and wait time exported to Prometheus.
  - _Requirements: R7.5, R21.4, R20.5_

> **Phase 2 gate:** a newsletter fixture terminates at `COMPLETED` with zero AI calls; an acknowledgement fixture produces a template reply with zero retrieval and zero generation calls; a support fixture lands in `email.support.normal`; killing a worker mid-job results in redelivery and exactly one logical result; a poisoned job reaches the DLQ with its reason intact and can be replayed.

---

# Phase 3 — Knowledge RAG

*Deliverable: email → relevant organizational knowledge.*

- [ ] **3.1 Document parsers**
  - PDF, DOCX, HTML, Markdown, plain text → text plus document structure (headings, sections, lists).
  - _Requirements: R9.2_

- [ ] **3.2 Structural chunker**
  - Split on semantic boundaries in preference to fixed character counts; target 350–700 tokens with configurable overlap.
  - Emit `heading_path`, `section`, `chunk_index`, `token_count`, `content_checksum`.
  - Unit tests: heading-heavy doc, table-heavy doc, one long unbroken paragraph, tiny doc.
  - _Requirements: R9.3, R9.4, R9.5, R24.3_

- [ ] **3.3 Embedding service**
  - Provider-abstracted embedder; batching; retry; records model name and dimension; counts `embedding_tokens_total`.
  - _Requirements: R9.6, R9.11, R21.4_

- [ ] **3.4 Chunk persistence & indexing**
  - Write chunk + `content_tsv` in one transaction; write `embedding_record` with the HNSW-indexed vector.
  - _Requirements: R9.7, R5.6, R5.7_

- [ ] **3.5 Ingestion pipeline & versioning**
  - `knowledge.ingest` worker running parse → chunk → enrich → embed → persist → `active`.
  - Re-ingestion writes version N+1 then flips status atomically — no window where the document is unsearchable.
  - Unchanged `content_checksum` ⇒ carry the embedding forward, skip re-embedding.
  - Per-document status with failure reason.
  - _Requirements: R9.1, R9.8, R9.9, R9.10_

- [ ] **3.6 Knowledge upload API**
  - `POST /v1/knowledge/documents` (upload to object storage + enqueue), `GET /v1/knowledge/documents` with ingestion status.
  - _Requirements: R23.7, R23.2, R5.8_

- [ ] **3.7 SearchBackend interface**
  - Protocol with `lexical()` and `vector()` returning `Candidate` objects carrying both ranks and both scores.
  - Contract test suite the PostgreSQL implementation must pass — and any future backend.
  - _Requirements: R10.7, R10.8_

- [ ] **3.8 PostgresSearchBackend**
  - Implement the hybrid SQL from `design.md §5.5`; filters (organization, category, document status) applied **inside** each branch.
  - Configurable top-N per branch, default 20.
  - **Filtered-ANN under-fill (read `design.md §5.5` first).** HNSW post-filters, so a tenant-scoped vector query can silently return far fewer than top-N. Detect `count < top_n` ⇒ set `retrieval_underfilled=true` and export the metric; widen via `hnsw.ef_search` / iterative scans before degrading.
  - Integration test must seed **≥3 tenants with overlapping content** and assert the target tenant's full top-N is returned. A single-tenant fixture will pass while production under-retrieves.
  - _Requirements: R10.1, R10.2, R10.4, R10.10, R10.11_

- [ ] **3.9 RRF fusion**
  - `score(d) = Σ 1/(k + rank_r(d))`, configurable `k` (default 60).
  - Pure-function unit tests: disjoint lists, identical lists, single-branch, ties.
  - _Requirements: R10.3, R24.3_

- [ ] **3.10 Concurrent branch execution & degradation**
  - Run lexical and vector concurrently; per-branch timeout.
  - One branch failing or timing out ⇒ continue on the survivor, record `retrieval_degraded=true`.
  - _Requirements: R10.5, R10.6, R10.9_

- [ ] **3.11 Cross-encoder reranker**
  - Optional rerank over the fused candidate set; disable-able per organization and per category.
  - Unavailable ⇒ fall back to RRF order, record the fallback.
  - _Requirements: R11.1, R11.2, R11.5_

- [ ] **3.12 Context packing**
  - Configurable Top-K (default 4–6); hard token budget; truncate only at chunk boundaries.
  - _Requirements: R11.3, R11.4_

- [ ] **3.13 Retrieval query builder**
  - **Ships degraded in Phase 3.** `thread_state` does not exist until task 4.1, so build from *current email + classification intent* now, behind a `thread_summary: str | None` parameter that is wired up in task 4.4. Do not block Phase 3 on Phase 4.
  - Build `RetrievalQuery` from current email + thread summary + classification intent — no extra LLM call in the default path.
  - Separate semantic text and lexical terms; regex-configurable identifier extraction (invoice, order, ticket, SKU, container, incident).
  - Derive category/metadata filters from classification; persist the query with the job.
  - Test explicitly that an identifier-bearing email (e.g. `INV-2026-01829`) retrieves the right chunk where a vector-only query would not.
  - _Requirements: R12.1, R12.2, R12.3, R12.4, R12.5, R12.6_

- [ ] **3.14 Retrieval latency metrics**
  - `retrieval_latency_ms` and `rerank_latency_ms` recorded separately, as histograms.
  - _Requirements: R11.6, R21.4, R21.5, NFR5, NFR6_

- [ ] **3.15 Retrieval debug endpoint**
  - `POST /v1/search/debug` returning the constructed query, both branch result lists with ranks, fused scores, rerank scores, and the final selection.
  - _Requirements: R23.3_

> **Phase 3 gate:** a support email retrieves the correct procedure chunk; an invoice-identifier email retrieves the correct billing chunk via the lexical branch; disabling either branch degrades gracefully; the debug endpoint explains every ranking decision; hybrid retrieval measurably beats vector-only on the **seed** benchmark set from task 0.13 (first evidence for H1; the full comparison is exp02 in Phase 3's successor phase); and filtered vector search returns full top-N across ≥3 seeded tenants.

---

# Phase 4 — Context & Generation

*Deliverable: email + thread + RAG → generated draft.*

- [ ] **4.1 Thread state store**
  - `thread_state` read/write with `topic`, `current_intent`, `summary`, `open_questions[]`, `resolved_items[]`.
  - Optimistic concurrency on `version`; concurrent workers on one thread must not lose updates (add a concurrency test).
  - _Requirements: R8.1, R8.6_

- [ ] **4.2 Summarization policy**
  - Below threshold ⇒ verbatim recent messages, no summarization call.
  - `message_count > threshold` OR `estimated_context_tokens > threshold` ⇒ (re)summarize; record `summarized_through_message_id`.
  - Assert in tests that a short thread triggers zero summarization calls.
  - _Requirements: R8.2, R8.3, R8.4_

- [ ] **4.3 Thread context assembly**
  - `summary + latest N relevant messages + current email` once a summary exists.
  - Record tokens saved (pre- vs post-compression estimate) for H3.
  - Keep inbound email out of the knowledge corpus by default.
  - _Requirements: R8.5, R8.7, R8.8_

- [ ] **4.4 Context Builder orchestration**
  - Gather thread context, business data (stub until Phase 5), and — only when `retrieval_required` — hybrid RAG.
  - Emit a `ContextPackage` in the fixed order from `design.md §5.4`; static sections first so prompt-prefix caching can apply.
  - Transition `QUEUED → CONTEXT_READY`.
  - _Requirements: R14.8, R6.6, R18.1_

- [ ] **4.5 LLMProvider abstraction**
  - `generate(messages, schema, tier, …) -> LLMResult` with content, model, tier, token counts, latency.
  - At least two implementations selectable by config (one hosted API, one OpenAI-compatible/local endpoint) plus a deterministic stub for CI.
  - Shared contract test suite.
  - _Requirements: R14.5, R14.7, R24.5_

- [ ] **4.6 Agent profile registry**
  - Profiles specifying `profile, knowledge_domain, response_style, model_tier, context_policy`, prompt template, output schema.
  - Selected by classification category with a configured default fallback.
  - Versioned prompt templates; `prompt_version` recorded on every draft.
  - _Requirements: R14.1, R14.2, R14.6_

- [ ] **4.7 Single-pass generation path & call budget**
  - One retrieval + one **generation** call for a normal email. No planner/critic/writer chain.
  - Assert **exactly one generation call per job** — not "one LLM call per job", which would contradict threshold summarization (4.2) and the triage-LLM fallback (2.4).
  - Enforce the full budget from `design.md §5.7`: ≤1 triage + ≤1 summarization + exactly 1 generation + ≤1 repair. Ceiling 4, common case 1.
  - Tier escalation **replaces** the generation call; it never adds one.
  - Export `llm_calls_total{kind}` and the `llm_calls_per_job` histogram so budget drift is visible rather than assumed.
  - _Requirements: R14.3, R14.4, R14.9, R14.10_

- [ ] **4.8 Complexity router & model cascade**
  - Tiers `routine` and `high_capability` bound to models by config; routine by default.
  - Escalation on: low classification confidence, complex/long thread, insufficient retrieval evidence, multiple requested actions, oversized context.
  - Max one escalation per job; record tier and escalation reason (or `none`).
  - Config switch forcing single-tier operation for the H4 comparison.
  - _Requirements: R15.1, R15.2, R15.3, R15.4, R15.5, R15.6_

- [ ] **4.9 Structured output & validation**
  - Enforce the schema `{action, draft, confidence, knowledge_chunks[], thread_summary_updated, model_tier}`.
  - Validate → one repair retry → fail into retry/DLQ. Never persist an unvalidated draft.
  - _Requirements: R16.1, R16.2, R16.3_

- [ ] **4.10 Citation verification**
  - Reject citations naming chunks that were not supplied in the context; set `citation_mismatch` and export its rate as a metric.
  - _Requirements: R16.5_

- [ ] **4.11 Draft persistence**
  - Persist body, citations, model, tier, escalation reason, prompt version, token counts, estimated cost.
  - Transition `GENERATING → DRAFTED`.
  - _Requirements: R16.4, R18.1, R21.6_

- [ ] **4.12 Generation metrics**
  - `generation_latency_ms`, `input_tokens_total`, `output_tokens_total`, `emails_generated_total`, `estimated_ai_cost`, with low-cardinality labels.
  - Record the final assembled context token count on **every** inference request, so context length can be correlated with quality, latency, and cost.
  - _Requirements: R11.7, R21.4, R21.5, R21.6, NFR8_

> **Phase 4 gate:** a support email with a 12-message thread produces a schema-valid, citation-verified draft in `DRAFTED`; a short thread triggers no summarization; a low-confidence job escalates exactly once; forcing single-tier mode still works end to end.

---

# Phase 5 — Business Data Integration

*Deliverable: RAG knowledge + live operational data → response.*

- [ ] **5.1 Business schema & seed data**
  - `customer`, `product`, `order`, `order_item`, `ticket` with realistic seed records tied to the fixture emails.
  - _Requirements: R13.1, R5.9_

- [ ] **5.2 BusinessDataProvider interface**
  - Protocol with a local PostgreSQL implementation, structured so a CRM/ERP adapter can replace it without touching the Context Builder.
  - Contract test suite.
  - _Requirements: R13.2_

- [ ] **5.3 Sender → customer resolution**
  - Resolve the sender address to a customer record; scope all business lookups to that customer.
  - _Requirements: R13.4_

- [ ] **5.4 Intent-driven fact fetching**
  - When the intent names transactional facts (order status, ticket state, invoice), fetch them from the business subsystem — never answer from RAG chunks.
  - Timeout-bounded; degrade with a recorded flag on failure.
  - _Requirements: R13.3, R13.7_

- [ ] **5.5 Fact labelling & missing-entity handling**
  - Business facts labelled distinctly from retrieved knowledge in the assembled context.
  - Missing entity ⇒ explicit "not found" fact passed to the agent, never silent omission.
  - _Requirements: R13.5, R13.6_

- [ ] **5.6 End-to-end business-data scenario test**
  - "What is the status of order 82915?" ⇒ the draft states the real seeded status, and the procedural framing comes from RAG.
  - _Requirements: R13.3, R13.5, R16.1_

> **Phase 5 gate:** an order-status email produces a draft containing the actual order status from the business tables, with a knowledge citation for the procedure — demonstrating the knowledge/transactional distinction.

---

# Phase 6 — Mail Dispatch & Review Interface

*Deliverable: real incoming email → pipeline → generated reply → provider mailbox.*

- [ ] **6.1 Draft management API**
  - List, read, edit, approve, reject drafts; filter by status, category, mailbox.
  - _Requirements: R16.6, R23.2, R23.6_

- [ ] **6.2 Feedback capture**
  - Reviewer action writes a `feedback` row with decision, edited body, edit distance, optional rating.
  - Export draft acceptance rate as a metric — this is the headline quality signal (SC3).
  - _Requirements: R16.7, R21.4_

- [ ] **6.3 Outbound reply construction**
  - Build `OutboundReply` with correct threading (`In-Reply-To`, `References`, or provider thread id) and quoted-original handling.
  - _Requirements: R17.2_

- [ ] **6.4 Dispatch modes**
  - `create_draft` and `send_reply` through the adapter interface; mode configurable per category.
  - Explicit approval required before `send` unless auto-send is enabled for that category; human-in-the-loop is the default posture.
  - _Requirements: R17.1, R17.6, R16.8_

- [ ] **6.5 Idempotent dispatch**
  - Guard with the `dispatch` idempotency key; redelivery cannot send a second copy.
  - Forced-redelivery test asserting exactly one provider send.
  - _Requirements: R17.3, R19.2, R19.3_

- [ ] **6.6 Dispatch completion & failure**
  - Success ⇒ persist provider ref, `DISPATCHED → COMPLETED`.
  - Transient failure ⇒ backoff retry; permanent ⇒ dead-letter with the provider error retained.
  - _Requirements: R17.4, R17.5_

- [ ] **6.7 Outbound message write-back**
  - Record the sent reply into `email_message` as `direction='outbound'` and update the thread, so subsequent inbound messages see the full conversation.
  - _Requirements: R17.7_

- [ ] **6.8 Review UI**
  - Pending-draft queue showing the original email, thread summary, cited chunks, and approve/edit/reject actions.
  - Job timeline and current state per message.
  - Knowledge upload view with per-document ingestion status.
  - _Requirements: R23.4, R23.5, R23.7_

- [ ] **6.9 Full end-to-end smoke test**
  - Fixture email → ingest → normalize → triage → context → RAG → generate → approve → dispatch, asserted in CI against fakes.
  - _Requirements: R24.7_

> **Phase 6 gate:** a real email sent to a connected mailbox produces a reviewable draft; approving it delivers a correctly threaded reply to the provider; the reply is visible in the thread; replaying the dispatch job sends nothing further.

---

# Phase 7 — Observability & Evaluation

*Deliverable: quantitative evidence supporting or rejecting H1–H5.*

- [ ] **7.1 Complete span coverage**
  - Spans for every stage in `design.md §10`, with the documented attributes; one email = one trace across all queue hops.
  - _Requirements: R21.1, R21.2_

- [ ] **7.2 Complete metric coverage**
  - Every metric named in R21.4, with latency as histograms supporting p50/p95/p99 and low-cardinality labels only.
  - _Requirements: R21.4, R21.5_

- [ ] **7.3 Cost accounting**
  - Config price table per model; per-inference cost, aggregated per email, per category, per day.
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

- [ ] **7.17 Success-criteria report**
  - Single report measuring SC1–SC10 against targets, with the funnel, latency percentiles, and cost per generated email.
  - Report measured values honestly; do not tune the dataset to hit a target.
  - _Requirements: R22.12_

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

> **Phase 8 gate:** a 20× burst is absorbed without job loss; adding AI-worker replicas measurably raises throughput; every failure scenario recovers with zero duplicates; the migration path is documented and the decision record is complete.

---

## Requirement coverage index

Use this to confirm nothing was dropped. Every requirement ID in `requirements.md` appears in at least one task.

| Requirement group | Tasks |
|---|---|
| R1 Provider abstraction | 1.1, 1.2, 1.3, 1.4, 1.7 |
| R2 Ingestion & sync | 1.3, 1.4, 1.5, 1.6, 1.7, 1.8 |
| R3 Async distribution | 0.7, 2.11, 2.12, 8.3, 8.5 |
| R4 Normalization | 1.9, 1.10, 1.11, 1.12, 1.13 |
| R5 Data platform | 0.4, 0.5, 0.12, 1.12, 3.4, 5.1 |
| R6 Triage | 2.2–2.8, 2.9 |
| R7 Routing | 2.1, 2.10, 2.15, 8.2 |
| R8 Thread state | 4.1, 4.2, 4.3 |
| R9 Knowledge ingestion | 3.1–3.6 |
| R10 Hybrid retrieval | 3.7, 3.8, 3.9, 3.10, 3.13, 8.6 |
| R11 Rerank & packing | 3.11, 3.12, 3.14 |
| R12 Query construction | 3.13 |
| R13 Business data | 5.1–5.6, 8.4 |
| R14 Agent & LLM abstraction | 4.4, 4.5, 4.6, 4.7, 4.12 |
| R15 Model cascade | 4.8 |
| R16 Structured output & drafts | 4.9, 4.10, 4.11, 6.1, 6.2, 6.4 |
| R17 Dispatch | 6.3–6.7 |
| R18 State machine | 0.6, 2.1, 2.12, 2.14, 4.4, 4.11 |
| R19 Idempotency & recovery | 0.8, 2.1, 2.12, 2.13, 6.5, 7.13, 8.4 |
| R20 Deployment & scale | 0.2, 0.3, 0.9, 7.12, 8.1, 8.2, 8.6, 8.8, 8.9 |
| R21 Observability | 0.9, 2.8, 2.15, 3.14, 4.12, 6.2, 7.1–7.4 |
| R22 Evaluation | 0.13, 7.5–7.17 |
| R23 API & UI | 0.10, 1.8, 1.14, 2.14, 3.6, 3.15, 6.1, 6.8 |
| R24 Engineering baseline | 0.1, 0.6, 0.11, 1.2, 4.5, 6.9, 8.6, 8.7 |
| NFR1–NFR14 | 2.3, 3.14, 4.12, 7.2, 7.4, 7.12 |
| SC1–SC10 | 7.7, 7.8, 7.12, 7.13, 7.16, 7.17 |
| H1–H5 | 7.8 (H1), 7.7 (H2), 7.10 (H3), 7.11 (H4), 7.12 (H5) |

# Requirements — Enterprise RAG-Based Intelligent Email Management and Response System

**Spec ID:** `rag-email`
**Source of truth:** `Technical_Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md`, `enterprise-rag-email.architecture.json`
**Companion files:** `design.md` (how to build it), `tasks.md` (what to build, in order)

---

## 0. How to use this document

This file is the **acceptance contract**. Every task in `tasks.md` carries one or more requirement IDs. A task is not "done" until every referenced criterion is demonstrably satisfied by code, a test, or a recorded measurement.

### 0.1 ID scheme

```
R<group>.<criterion>        e.g. R6.3
NFR<n>                      non-functional / SLO
SC<n>                       success criterion (final evaluation gate)
```

IDs are **immutable**. If a requirement is dropped, mark it `WITHDRAWN` — never renumber.

### 0.2 Notation (EARS)

| Pattern | Form |
|---|---|
| Event-driven | WHEN `<trigger>`, THE SYSTEM SHALL `<response>` |
| State-driven | WHILE `<state>`, THE SYSTEM SHALL `<response>` |
| Conditional | IF `<condition>`, THEN THE SYSTEM SHALL `<response>` |
| Unwanted behaviour | IF `<failure>`, THEN THE SYSTEM SHALL `<recovery>` |
| Ubiquitous | THE SYSTEM SHALL `<property>` |

`SHALL` = mandatory. `SHOULD` = strong default, deviation must be recorded in an ADR. `MAY` = optional.

### 0.3 Definition of Done (applies to every task)

A sub-task is complete when **all** hold:

1. Referenced criteria are implemented.
2. Automated tests cover the referenced criteria (unit and/or integration).
3. `docker compose up` still brings the stack to a healthy state.
4. New/changed config keys are documented in `.env.example` and `docs/configuration.md`.
5. Structured logs and metrics named in R21 are emitted for any new processing step.
6. No requirement outside the task's scope regressed (CI green).

### 0.4 Glossary

| Term | Meaning |
|---|---|
| **Job** | One unit of asynchronous work tracked in `processing_job` with an explicit state |
| **Triage** | Cascading classification: rules → ML classifier → LLM fallback |
| **Early exit** | A message classified as `reply_required=false`, terminated before RAG/LLM |
| **Hybrid retrieval** | PostgreSQL FTS + pgvector ANN, fused by RRF, optionally reranked |
| **RRF** | Reciprocal Rank Fusion, `score(d) = Σ 1/(k + rank_r(d))` |
| **Thread state** | Persistent, compressed conversation summary + open/resolved items |
| **Agent profile** | Named configuration bundle (prompt, domain, style, model tier, context policy) |
| **Model cascade** | Routine-tier model by default, high-capability tier on escalation |
| **Idempotency key** | `(organization_id, mailbox_id, provider_message_id, operation_type)` |
| **Reference workload** | 10,000 mailboxes, 100,000 inbound emails/day |

### 0.5 Explicitly out of scope

Authentication/authorization architecture, access-control enforcement, encryption architecture, DLP, prompt-injection defences, malware/phishing detection, secret detection, content security classification, security auditing, regulatory compliance, threat modelling. Provider OAuth credentials are assumed to exist as an integration prerequisite; storing them securely is a deployment concern, not an architectural deliverable here.

Any task that appears to require one of the above SHALL be stopped and raised rather than improvised.

---

## R1 — Provider Abstraction

**User story:** As a platform engineer, I want every provider-specific detail confined behind one interface, so that adding IMAP or swapping Gmail for Graph never touches pipeline code.

| ID | Criterion |
|---|---|
| R1.1 | THE SYSTEM SHALL define a single `MailProviderAdapter` interface exposing `subscribe()`, `renew_subscription()`, `synchronize()`, `get_message()`, `get_thread()`, `create_draft()`, `send_reply()`. |
| R1.2 | THE SYSTEM SHALL provide `GmailProviderAdapter` and `MicrosoftGraphProviderAdapter` implementations; an `IMAPProviderAdapter` MAY be provided. |
| R1.3 | THE SYSTEM SHALL resolve adapters through a registry keyed by `mailbox.provider`, with no provider name appearing in any module outside `adapters/`. |
| R1.4 | WHEN an adapter method is called, THE SYSTEM SHALL return provider-neutral domain objects (`NormalizedMessage`, `ThreadRef`, `DraftRef`) and never raw provider payloads. |
| R1.5 | THE SYSTEM SHALL translate provider errors into a common taxonomy: `RateLimited`, `AuthExpired`, `NotFound`, `Transient`, `Permanent`. |
| R1.6 | IF an adapter raises `RateLimited`, THEN THE SYSTEM SHALL honour any provider-supplied retry-after hint before retrying. |
| R1.7 | THE SYSTEM SHALL ship a `FakeProviderAdapter` driven by fixture files, usable in CI with no network access. |

## R2 — Event-Driven Ingestion & Incremental Synchronization

**User story:** As an operator, I want mailbox changes to push work into the system rather than being polled for, so that provider quota and compute stay proportional to real traffic.

| ID | Criterion |
|---|---|
| R2.1 | THE SYSTEM SHALL expose provider webhook endpoints that accept change notifications and complete the provider handshake/validation exchange. |
| R2.2 | WHEN a change notification is received, THE SYSTEM SHALL acknowledge it within 5 seconds and perform no synchronous provider fetch inside the request. |
| R2.3 | THE SYSTEM SHALL treat a notification as a *signal only* and SHALL NOT trust notification payload contents as authoritative message state. |
| R2.4 | THE SYSTEM SHALL persist a per-mailbox checkpoint: `history_id` (Gmail), `delta_link` (Graph), `last_sync_at`, `sync_state`. |
| R2.5 | WHEN synchronizing Gmail, THE SYSTEM SHALL call `history.list()` from the stored `historyId` and fetch only changed messages. |
| R2.6 | WHEN synchronizing Microsoft Graph, THE SYSTEM SHALL follow `@odata.nextLink` pages and persist the terminal `@odata.deltaLink`. |
| R2.7 | IF the provider reports the stored checkpoint as expired or invalid, THEN THE SYSTEM SHALL fall back to a bounded full synchronization and record `sync_state='full_resync'`. |
| R2.8 | THE SYSTEM SHALL update the checkpoint **only after** all fetched messages from that window are durably persisted. |
| R2.9 | WHILE a mailbox has an in-flight sync, THE SYSTEM SHALL coalesce further notifications for that mailbox into at most one queued follow-up sync. |
| R2.10 | THE SYSTEM SHALL renew provider subscriptions before expiry on a scheduled job and record renewal outcomes. |
| R2.11 | THE SYSTEM SHALL provide a manual re-sync endpoint accepting `mailbox_id` and an optional time window. |

## R3 — Asynchronous Work Distribution

**User story:** As an operator, I want ingestion decoupled from inference, so that a slow LLM never blocks mail arrival.

| ID | Criterion |
|---|---|
| R3.1 | THE SYSTEM SHALL use RabbitMQ as the broker, with durable exchanges, durable queues, and persistent messages. |
| R3.2 | THE SYSTEM SHALL declare topology in code at startup (idempotent declaration), never by manual console configuration. |
| R3.3 | THE SYSTEM SHALL use manual consumer acknowledgement; a message SHALL be acked only after its side effects are committed. |
| R3.4 | THE SYSTEM SHALL set a bounded `prefetch` per consumer, configurable per worker class. |
| R3.5 | THE SYSTEM SHALL route unrecoverable messages to a dead-letter exchange with the original routing key and failure reason preserved in headers. |
| R3.6 | THE SYSTEM SHALL support worker-level micro-batching (N jobs pulled together). |
| R3.7 | THE SYSTEM SHALL NOT concatenate multiple distinct emails into one LLM prompt; each email SHALL receive an independent inference call. |
| R3.8 | THE SYSTEM SHALL keep a single logical broker with separate queues per stage; queue names SHALL be config-driven. |
| R3.9 | THE SYSTEM SHOULD support quorum queues via configuration for the HA deployment profile. |

## R4 — Email Normalization

**User story:** As a downstream service, I want one canonical message shape, so that classification and generation never parse MIME.

| ID | Criterion |
|---|---|
| R4.1 | WHEN a raw message is consumed, THE SYSTEM SHALL parse MIME and produce the normalized schema: `message_id, thread_id, mailbox_id, sender, recipients, cc, subject, body_text, body_html, received_at, attachments, provider`. |
| R4.2 | THE SYSTEM SHALL extract plain text, converting HTML bodies to text when no text part exists. |
| R4.3 | THE SYSTEM SHALL separate quoted reply history from the new content and persist both `body_text` and `body_text_clean`. |
| R4.4 | THE SYSTEM SHOULD detect and strip signature blocks, recording whether detection succeeded. |
| R4.5 | THE SYSTEM SHALL normalize subjects by stripping reply/forward prefixes for thread matching. |
| R4.6 | THE SYSTEM SHALL associate each message with a thread using provider thread id when available, else `In-Reply-To`/`References`, else normalized subject + participant set. |
| R4.7 | THE SYSTEM SHALL extract attachment metadata (filename, MIME type, size, checksum) and store binary content in object storage, never in a relational column. |
| R4.8 | IF a message with the same `(organization_id, mailbox_id, provider_message_id)` already exists, THEN THE SYSTEM SHALL treat the operation as a no-op success and SHALL NOT create a second job. |
| R4.9 | IF MIME parsing fails, THEN THE SYSTEM SHALL persist the message with a `normalization_failed` flag, retain the raw object reference, and route the job to dead-letter rather than discarding it. |
| R4.10 | THE SYSTEM SHALL store the raw provider payload / raw MIME in object storage keyed for later replay. |

## R5 — Data Platform & Model

**User story:** As a developer, I want PostgreSQL to be the single authoritative operational store, so that the system stays deployable by a small team.

| ID | Criterion |
|---|---|
| R5.1 | THE SYSTEM SHALL use PostgreSQL with the `pgvector` extension as the authoritative operational and search store. |
| R5.2 | THE SYSTEM SHALL implement the entities: `organization`, `mailbox`, `mailbox_checkpoint`, `email_thread`, `email_message`, `attachment`, `classification_result`, `processing_job`, `thread_state`, `generated_draft`, `knowledge_document`, `knowledge_chunk`, `embedding_record`, `feedback`, `processing_event`. |
| R5.3 | THE SYSTEM SHALL carry `organization_id` on every tenant-scoped table and SHALL include it in every query predicate. |
| R5.4 | THE SYSTEM SHALL enforce uniqueness on `(organization_id, mailbox_id, provider_message_id)` for messages and `(organization_id, mailbox_id, provider_thread_id)` for threads. |
| R5.5 | THE SYSTEM SHALL manage schema exclusively through versioned, reversible migrations; no runtime DDL. |
| R5.6 | THE SYSTEM SHALL maintain a GIN index over the message and chunk `tsvector` columns. |
| R5.7 | THE SYSTEM SHALL maintain an HNSW index over `embedding_record.embedding` using the configured distance operator class. |
| R5.8 | THE SYSTEM SHALL store large blobs (raw MIME, attachments, source documents) in MinIO/S3-compatible storage and reference them by object key. |
| R5.9 | THE SYSTEM SHALL provide a seed/fixture loader creating a demo organization, mailboxes, knowledge documents, and business records for local development. |
| R5.10 | THE SYSTEM SHALL keep vector dimensionality a configuration value and SHALL fail fast at startup if the configured embedding model's dimension disagrees with the column definition. |

## R6 — Cascading Triage

**User story:** As a cost owner, I want most emails classified without an LLM, so that inference spend tracks actionable mail rather than total mail.

| ID | Criterion |
|---|---|
| R6.1 | THE SYSTEM SHALL classify every normalized message in three ordered stages: deterministic rules → lightweight ML classifier → small-LLM fallback. |
| R6.2 | WHEN a stage returns confidence ≥ its configured threshold, THE SYSTEM SHALL stop and SHALL NOT invoke later stages. |
| R6.3 | THE SYSTEM SHALL emit a classification object: `category, intent, priority, reply_required, retrieval_required, confidence`, plus `decided_by ∈ {rule, ml, llm}`. |
| R6.4 | THE SYSTEM SHALL support at minimum the categories: support, sales, billing, administration, scheduling, general_inquiry, automated_notification, acknowledgement, no_response. |
| R6.5 | IF `reply_required = false`, THEN THE SYSTEM SHALL transition the job directly to `COMPLETED` and SHALL NOT perform embedding, retrieval, reranking, or generation. |
| R6.6 | IF `retrieval_required = false`, THEN THE SYSTEM SHALL skip the hybrid RAG call and build context from thread and business data only. |
| R6.7 | THE SYSTEM SHALL persist every classification result with its stage, latency, model identifier (if any), and raw output. |
| R6.8 | THE SYSTEM SHALL express rules as declarative, hot-reloadable configuration (sender patterns, header signals such as `List-Unsubscribe`/`Auto-Submitted`, subject patterns), not hard-coded conditionals. |
| R6.9 | THE SYSTEM SHALL make every threshold configurable per organization and per category without redeploy. |
| R6.10 | THE SYSTEM SHALL record counters allowing the realized early-exit rate to be compared against the 45% / 20% / 35% design assumption. |
| R6.11 | IF all three stages fail or return malformed output, THEN THE SYSTEM SHALL assign a safe default (`category=general_inquiry`, `priority=normal`, `reply_required=true`, `confidence=0`) and flag the job for review rather than dropping it. |
| R6.12 | THE SYSTEM SHALL emit a third routing signal `workflow_hint ∈ {template, ai, none}` alongside `reply_required` and `retrieval_required`. |
| R6.13 | IF `workflow_hint = 'template'`, THEN THE SYSTEM SHALL render a deterministic approved-template reply and SHALL NOT invoke retrieval or generation. |
| R6.14 | THE SYSTEM SHALL support a template registry keyed by `(category, intent)` with variable substitution from message and business fields, and SHALL fall back to `workflow_hint='ai'` when no template matches. |
| R6.15 | THE SYSTEM SHALL account for all actionable mail across exactly three mutually exclusive outcomes — early exit, template reply, AI generation — so the measured funnel reconciles against NFR14 without a residual bucket. |

## R7 — Category-Aware Routing & Scheduling

**User story:** As an operator, I want urgent billing mail to overtake newsletters, so that queue depth doesn't flatten business priority.

| ID | Criterion |
|---|---|
| R7.1 | WHEN a message is classified as actionable, THE SYSTEM SHALL publish a job to a queue named by `email.<category>.<priority>`. |
| R7.2 | THE SYSTEM SHALL define at least `normal` and `priority` lanes, with independent consumer scaling per lane. |
| R7.3 | THE SYSTEM SHALL carry the routing decision, correlation ids, and classification snapshot in the job payload so workers need no re-classification. |
| R7.4 | THE SYSTEM SHALL allow new categories to be added by configuration, creating queues on startup without code change. |
| R7.5 | THE SYSTEM SHALL expose per-queue depth and per-queue wait time as metrics. |
| R7.6 | IF a category queue has no configured consumer, THEN THE SYSTEM SHALL log a startup warning naming the queue rather than silently accumulating messages. |

## R8 — Thread & Conversation State

**User story:** As a reply agent, I want compressed conversation state, so that long threads don't repeatedly consume the full context window.

| ID | Criterion |
|---|---|
| R8.1 | THE SYSTEM SHALL maintain one `thread_state` row per thread containing `topic`, `current_intent`, `summary`, `open_questions[]`, `resolved_items[]`. |
| R8.2 | WHILE a thread is below the configured size threshold, THE SYSTEM SHALL supply recent messages verbatim and SHALL NOT summarize. |
| R8.3 | WHEN `message_count > threshold` OR `estimated_context_tokens > threshold`, THE SYSTEM SHALL (re)generate the thread summary. |
| R8.4 | THE SYSTEM SHALL NOT regenerate a summary on every message; summarization SHALL be threshold-triggered and recorded via `summarized_through_message_id`. |
| R8.5 | WHEN a summary exists, THE SYSTEM SHALL assemble thread context as `summary + latest N relevant messages + current email`. |
| R8.6 | THE SYSTEM SHALL version thread state and update it optimistically, tolerating concurrent workers on the same thread without lost updates. |
| R8.7 | THE SYSTEM SHALL record tokens saved by summarization (pre- vs post-compression estimate) to support hypothesis H3. |
| R8.8 | THE SYSTEM SHALL keep inbound email out of the organizational knowledge corpus by default; email content belongs to the thread subsystem. |

## R9 — Knowledge Ingestion Pipeline

**User story:** As a knowledge owner, I want documents indexed offline, so that retrieval is fresh without paying embedding cost per email.

| ID | Criterion |
|---|---|
| R9.1 | THE SYSTEM SHALL ingest documents asynchronously: parse → structure extraction → chunk → metadata enrichment → embed → persist. |
| R9.2 | THE SYSTEM SHALL support at minimum PDF, DOCX, HTML, Markdown, and plain text sources. |
| R9.3 | THE SYSTEM SHALL chunk on semantic boundaries (headings, sections, paragraphs, list groups) in preference to fixed character splits. |
| R9.4 | THE SYSTEM SHALL target a configurable chunk size defaulting to 350–700 tokens, with configurable overlap. |
| R9.5 | THE SYSTEM SHALL attach metadata to every chunk: `document_id, chunk_id, title, heading_path, section, category, version`. |
| R9.6 | THE SYSTEM SHALL generate and store one embedding per chunk, recording the embedding model name and dimension. |
| R9.7 | THE SYSTEM SHALL populate the chunk `tsvector` at write time in the same transaction as the chunk row. |
| R9.8 | WHEN a document is re-ingested, THE SYSTEM SHALL create a new version and atomically switch retrieval to it, leaving no window where the document is unsearchable. |
| R9.9 | THE SYSTEM SHALL skip re-embedding when a chunk's content checksum is unchanged across versions. |
| R9.10 | THE SYSTEM SHALL expose ingestion status per document (`pending, parsing, chunking, embedding, indexed, failed`) with failure reason. |
| R9.11 | THE SYSTEM SHALL track `embedding_tokens_total` for cost accounting. |

## R10 — Hybrid Retrieval

**User story:** As a reply agent, I want both exact-identifier and conceptual matching, so that `INV-2026-01829` and "my account is locked" both retrieve correctly.

| ID | Criterion |
|---|---|
| R10.1 | WHEN retrieval is required, THE SYSTEM SHALL execute a PostgreSQL FTS query and a pgvector ANN query against the same chunk corpus. |
| R10.2 | THE SYSTEM SHALL take a configurable top-N from each branch, defaulting to 20. |
| R10.3 | THE SYSTEM SHALL fuse the two ranked lists using RRF with a configurable `k` (default 60). |
| R10.4 | THE SYSTEM SHALL apply metadata filters (organization, category, document version/status) inside both branches, not after fusion. |
| R10.5 | THE SYSTEM SHALL execute the two branches concurrently. |
| R10.6 | IF one branch fails or times out, THEN THE SYSTEM SHALL degrade to the surviving branch, record `retrieval_degraded=true`, and continue. |
| R10.7 | THE SYSTEM SHALL hide retrieval behind a `SearchBackend` interface with a `PostgresSearchBackend` implementation, so an `OpenSearchBackend` can be added without touching pipeline code. |
| R10.8 | THE SYSTEM SHALL return, for every candidate, its lexical rank, vector rank, fused score, and source metadata. |
| R10.9 | THE SYSTEM SHALL enforce a configurable retrieval timeout and SHALL NOT let a slow query block the worker indefinitely. |
| R10.10 | IF a filtered ANN query returns fewer than the requested top-N candidates, THEN THE SYSTEM SHALL record `retrieval_underfilled=true`, export it as a metric, and widen the search (raised `ef_search` / iterative scan / tenant-partitioned index) before falling back to the lexical branch alone. |
| R10.11 | THE SYSTEM SHALL verify under integration test that filtered vector search on a multi-tenant corpus returns the full requested top-N for the target tenant. |

## R11 — Reranking & Context Packing

**User story:** As a cost owner, I want the smallest sufficient context, so that quality doesn't degrade and tokens don't inflate.

| ID | Criterion |
|---|---|
| R11.1 | THE SYSTEM SHALL support an optional cross-encoder / semantic reranker over the fused candidate set. |
| R11.2 | THE SYSTEM SHALL allow reranking to be disabled by configuration, per organization and per category. |
| R11.3 | THE SYSTEM SHALL pass a configurable top-K to generation, defaulting to 4–6 chunks. |
| R11.4 | THE SYSTEM SHALL enforce a maximum retrieved-context token budget and SHALL truncate at chunk boundaries, never mid-chunk. |
| R11.5 | IF the reranker is unavailable, THEN THE SYSTEM SHALL fall back to RRF order, record the fallback, and continue. |
| R11.6 | THE SYSTEM SHALL record `retrieval_latency_ms` and `rerank_latency_ms` separately. |
| R11.7 | THE SYSTEM SHALL record the final context token count on every inference request. |

## R12 — Query Construction

**User story:** As a retrieval engine, I want a purpose-built query rather than the raw email, so that follow-up messages like "I already did that" still retrieve usefully.

| ID | Criterion |
|---|---|
| R12.1 | THE SYSTEM SHALL construct the retrieval query from `current email + thread summary + classification intent`, not from the raw body alone. |
| R12.2 | THE SYSTEM SHALL produce a semantic query string and a lexical keyword set as separate outputs. |
| R12.3 | THE SYSTEM SHALL extract structured identifiers (invoice, order, ticket, SKU, container, incident patterns) via configurable regex and pass them to the lexical branch verbatim. |
| R12.4 | THE SYSTEM SHALL derive category and metadata filters from the classification result. |
| R12.5 | THE SYSTEM SHALL construct the query without an additional autonomous LLM agent call in the default path. |
| R12.6 | THE SYSTEM SHALL persist the constructed query with the job for debugging and evaluation replay. |

## R13 — Business Data vs Knowledge

**User story:** As a customer, I want "what's the status of order 82915?" answered with the real status, not with a procedure document.

| ID | Criterion |
|---|---|
| R13.1 | THE SYSTEM SHALL maintain a transactional business subsystem in PostgreSQL with `customer`, `product`, `order`, `order_item`, `ticket`. |
| R13.2 | THE SYSTEM SHALL define a `BusinessDataProvider` interface so the local implementation can later be replaced by CRM/ERP adapters. |
| R13.3 | WHEN the classified intent requires transactional facts, THE SYSTEM SHALL fetch them from the business subsystem and SHALL NOT attempt to answer from RAG chunks. |
| R13.4 | THE SYSTEM SHALL resolve the sender to a customer record where possible and scope business lookups to that customer. |
| R13.5 | THE SYSTEM SHALL label business facts distinctly from retrieved knowledge inside the assembled context. |
| R13.6 | IF a referenced business entity is not found, THEN THE SYSTEM SHALL pass an explicit "not found" fact to the agent rather than omitting it silently. |
| R13.7 | THE SYSTEM SHALL bound business-data lookups with a timeout and degrade with a recorded flag on failure. |

## R14 — Agent Profiles & LLM Abstraction

**User story:** As a developer, I want specialization by configuration, so that we don't build a multi-agent chain that multiplies latency and cost.

| ID | Criterion |
|---|---|
| R14.1 | THE SYSTEM SHALL implement an agent-profile registry; profiles specify `profile, knowledge_domain, response_style, model_tier, context_policy`, plus prompt template and output schema. |
| R14.2 | THE SYSTEM SHALL select the profile from the classification category, with a configured default profile as fallback. |
| R14.3 | THE SYSTEM SHALL process a normal email with approximately **one retrieval operation and one generation operation**. |
| R14.4 | THE SYSTEM SHALL NOT implement a chained planner/critic/writer multi-agent pipeline in the default path. |
| R14.5 | THE SYSTEM SHALL access all models through an `LLMProvider` interface exposing `generate(messages, schema, tier, **params)` and returning content plus token usage. |
| R14.6 | THE SYSTEM SHALL version prompt templates and record `prompt_version` on every generated draft. |
| R14.7 | THE SYSTEM SHALL support at least two provider implementations (one hosted API, one OpenAI-compatible/local endpoint) selectable by configuration. |
| R14.8 | THE SYSTEM SHALL assemble the final context in the fixed order: agent instructions, category instructions, thread summary, recent messages, current email, retrieved knowledge, business data. |
| R14.9 | THE SYSTEM SHALL bound total model invocations per email job to: ≤1 triage-LLM call (stage 3 only), ≤1 thread-summarization call (threshold-triggered only), exactly 1 generation call, and ≤1 schema-repair retry. Any additional call is a defect. |
| R14.10 | THE SYSTEM SHALL export `llm_calls_per_job` by call kind (`triage`, `summarize`, `generate`, `repair`) so the budget in R14.9 is continuously observable rather than assumed. |

## R15 — Model Cascading

**User story:** As a cost owner, I want the cheap model to handle the easy majority, so that spend tracks difficulty rather than volume.

| ID | Criterion |
|---|---|
| R15.1 | THE SYSTEM SHALL define at least two model tiers (`routine`, `high_capability`) bound to concrete models by configuration. |
| R15.2 | THE SYSTEM SHALL route to the routine tier by default. |
| R15.3 | THE SYSTEM SHALL escalate to the high-capability tier when any configured criterion holds: low classification confidence, complex/long thread, insufficient retrieval evidence, multiple requested actions, or context length above threshold. |
| R15.4 | THE SYSTEM SHALL record the tier used, the escalation reason (or `none`), and token usage on every draft. |
| R15.5 | THE SYSTEM SHALL cap escalations per job (default 1) to prevent unbounded retry-escalate loops. |
| R15.6 | THE SYSTEM SHALL expose a configuration switch forcing single-tier operation, so the cascade can be A/B measured against a single model (H4). |

## R16 — Structured Output & Draft Store

**User story:** As an integrator, I want machine-readable generation results, so that persistence, evaluation, and UI are not prose-parsing exercises.

| ID | Criterion |
|---|---|
| R16.1 | THE SYSTEM SHALL require the model to return a schema-conformant object: `action, draft, confidence, knowledge_chunks[], thread_summary_updated, model_tier`. |
| R16.2 | THE SYSTEM SHALL validate the response against the schema before persistence. |
| R16.3 | IF validation fails, THEN THE SYSTEM SHALL retry once with a repair instruction, and on second failure SHALL fail the job into the retry/DLQ path — never persist an unvalidated draft. |
| R16.4 | THE SYSTEM SHALL persist every draft with its citations, model, tier, prompt version, token counts, and estimated cost. |
| R16.5 | THE SYSTEM SHALL reject citations that do not correspond to chunks actually supplied in the context, recording a `citation_mismatch` flag. |
| R16.6 | THE SYSTEM SHALL expose drafts through an API supporting list, read, edit, approve, and reject. |
| R16.7 | WHEN a reviewer acts on a draft, THE SYSTEM SHALL record a `feedback` row with decision, edited body, edit distance, and optional rating. |
| R16.8 | THE SYSTEM SHALL support a human-in-the-loop mode (draft only, never auto-send) as the default operating posture. |

## R17 — Mail Dispatch

**User story:** As a user, I want the approved reply to land in the real mailbox, in the right thread, exactly once.

| ID | Criterion |
|---|---|
| R17.1 | THE SYSTEM SHALL support two dispatch modes: create provider draft, and send reply. |
| R17.2 | WHEN dispatching, THE SYSTEM SHALL set correct threading headers (`In-Reply-To`, `References`) or the provider's thread identifier. |
| R17.3 | THE SYSTEM SHALL guard dispatch with an idempotency key so a redelivered job cannot send a second copy. |
| R17.4 | WHEN dispatch succeeds, THE SYSTEM SHALL persist the provider-returned identifier and transition the job to `DISPATCHED` then `COMPLETED`. |
| R17.5 | IF dispatch fails transiently, THEN THE SYSTEM SHALL retry with backoff; IF it fails permanently, THEN THE SYSTEM SHALL dead-letter the job with the provider error retained. |
| R17.6 | THE SYSTEM SHALL require explicit approval before `send` mode dispatch unless auto-send is explicitly enabled for that category. |
| R17.7 | THE SYSTEM SHALL record the outbound message back into the thread so subsequent inbound messages see the full conversation. |

## R18 — Processing State Machine

**User story:** As an operator, I want every email's position in the pipeline to be a queryable fact, not an inference from logs.

| ID | Criterion |
|---|---|
| R18.1 | THE SYSTEM SHALL model every job with states: `RECEIVED, NORMALIZED, CLASSIFIED, QUEUED, CONTEXT_READY, GENERATING, DRAFTED, DISPATCHED, COMPLETED`. |
| R18.2 | THE SYSTEM SHALL support failure states: `RETRY_PENDING, FAILED, DEAD_LETTER`. |
| R18.3 | THE SYSTEM SHALL permit only declared transitions and SHALL reject illegal transitions with an error rather than overwriting state. |
| R18.4 | THE SYSTEM SHALL write a `processing_event` row for every transition, carrying `trace_id`, from-state, to-state, and timestamp. |
| R18.5 | THE SYSTEM SHALL persist state transitions in the same transaction as the side effect that caused them. |
| R18.6 | THE SYSTEM SHALL expose a job-timeline API returning the ordered event history for a message. |
| R18.7 | THE SYSTEM SHALL support replaying a `DEAD_LETTER` job from its last good state via an operator endpoint. |

## R19 — Delivery Semantics, Idempotency & Recovery

**User story:** As an operator, I want redelivery to be harmless, so that at-least-once queues don't produce duplicate replies.

| ID | Criterion |
|---|---|
| R19.1 | THE SYSTEM SHALL adopt at-least-once delivery with idempotent consumers, and SHALL NOT rely on exactly-once semantics. |
| R19.2 | THE SYSTEM SHALL derive a deterministic idempotency key from `(organization_id, mailbox_id, provider_message_id, operation_type)`. |
| R19.3 | BEFORE any state-changing step, THE SYSTEM SHALL check whether the logical operation already completed and SHALL short-circuit with the prior result if so. |
| R19.4 | THE SYSTEM SHALL enforce idempotency with a database uniqueness constraint, not only application logic. |
| R19.5 | IF a transient failure occurs, THEN THE SYSTEM SHALL transition `GENERATING → RETRY_PENDING → GENERATING` with exponential backoff and jitter. |
| R19.6 | WHEN the retry limit is exceeded, THE SYSTEM SHALL transition `FAILED → DEAD_LETTER` and stop consuming capacity. |
| R19.7 | IF a worker is killed mid-job, THEN THE SYSTEM SHALL redeliver the unacked message and complete processing without duplicate side effects. |
| R19.8 | THE SYSTEM SHALL reclaim jobs stuck in a non-terminal state beyond a configured lease timeout. |
| R19.9 | THE SYSTEM SHALL demonstrate zero duplicate logical processing under a forced-redelivery test (see SC6). |

## R20 — Deployment & Horizontal Scalability

**User story:** As a student team, I want the whole system on one laptop, and the same architecture to scale on a cluster.

| ID | Criterion |
|---|---|
| R20.1 | THE SYSTEM SHALL run completely via `docker compose` with services: frontend, api, mail-connector, email-worker, triage-worker, ai-worker, knowledge-worker, dispatch-worker, postgres+pgvector, rabbitmq, minio, prometheus, grafana. |
| R20.2 | THE SYSTEM SHALL keep every worker stateless; all durable state SHALL live in PostgreSQL, RabbitMQ, or object storage. |
| R20.3 | THE SYSTEM SHALL support running N replicas of any worker class with no coordination beyond the broker and database. |
| R20.4 | THE SYSTEM SHALL demonstrate throughput increase when worker replicas are added (see SC8). |
| R20.5 | THE SYSTEM SHALL expose queue-depth metrics suitable as an autoscaling signal. |
| R20.6 | THE SYSTEM SHALL read all configuration from environment variables with documented defaults and a validated settings object that fails fast on misconfiguration. |
| R20.7 | THE SYSTEM SHALL provide `/healthz` (liveness) and `/readyz` (dependency readiness) on every service. |
| R20.8 | THE SYSTEM SHALL shut down gracefully: stop consuming, finish in-flight jobs or nack them, then exit. |
| R20.9 | THE SYSTEM SHALL document the enterprise evolution path (RabbitMQ cluster/quorum queues, managed PostgreSQL, S3, Kubernetes) without requiring it for the FYP deliverable. |

## R21 — Observability & Cost Accounting

**User story:** As an evaluator, I want objective evidence, so that architectural claims are measured rather than asserted.

| ID | Criterion |
|---|---|
| R21.1 | THE SYSTEM SHALL assign a correlation id (`trace_id`) at ingestion and propagate it through every stage, including across queue hops. |
| R21.2 | THE SYSTEM SHALL emit OpenTelemetry spans for: mail connector, email processor, triage, queue publish/consume, retrieval, rerank, LLM call, draft persist, dispatch. |
| R21.3 | THE SYSTEM SHALL emit structured JSON logs including `trace_id, message_id, thread_id, job_id, organization_id`. |
| R21.4 | THE SYSTEM SHALL expose Prometheus metrics: `emails_received_total, emails_classified_total, emails_generated_total, classification_latency_ms, retrieval_latency_ms, generation_latency_ms, end_to_end_latency_ms, queue_depth, queue_wait_ms, retrieval_hit_rate, retrieval_top_k, input_tokens_total, output_tokens_total, embedding_tokens_total, estimated_ai_cost, failed_jobs_total, retry_jobs_total`. |
| R21.5 | THE SYSTEM SHALL label latency metrics as histograms enabling p50/p95/p99 computation. |
| R21.6 | THE SYSTEM SHALL compute estimated cost per inference from a configurable per-model price table and aggregate cost per email, per category, and per day. |
| R21.7 | THE SYSTEM SHALL ship provisioned Grafana dashboards for: pipeline funnel, latency breakdown, queue health, retrieval quality, and cost. |
| R21.8 | THE SYSTEM SHALL record the early-exit funnel (received → no-reply → simple → AI → RAG) as a first-class dashboard panel. |

## R22 — Evaluation Harness

**User story:** As a researcher, I want each hypothesis independently testable, so that the project produces evidence rather than a demo.

| ID | Criterion |
|---|---|
| R22.1 | THE SYSTEM SHALL provide a labelled classification benchmark set and compute accuracy, precision, recall, macro-F1, and a confusion matrix. |
| R22.2 | THE SYSTEM SHALL provide a retrieval benchmark set (query → known relevant chunks) and compute Recall@K, Precision@K, MRR, and nDCG@K. |
| R22.3 | THE SYSTEM SHALL support running retrieval evaluation in four modes: vector-only, FTS-only, hybrid, hybrid+rerank, and emit a comparison table (H1). |
| R22.4 | THE SYSTEM SHALL support a triage comparison: rules-only vs rules+ML vs full cascade, reporting quality and cost (H2). |
| R22.5 | THE SYSTEM SHALL support a thread-context comparison: full thread vs summarized thread, reporting tokens and quality (H3). |
| R22.6 | THE SYSTEM SHALL support a model comparison: single high-capability model vs cascade, reporting cost and quality (H4). |
| R22.7 | THE SYSTEM SHALL provide a load-test harness at 1×, 5×, 10×, and 20× the reference arrival rate, reporting throughput, queue depth, and latency percentiles (H5). |
| R22.8 | THE SYSTEM SHALL provide a fault-injection scenario (kill a worker mid-job) proving recovery and no duplication. |
| R22.9 | THE SYSTEM SHALL support a context-size sweep (Top-3 / Top-5 / Top-10) reporting quality, tokens, latency, cost. |
| R22.10 | THE SYSTEM SHALL support a RAG vs non-RAG response-quality baseline comparison. |
| R22.11 | THE SYSTEM SHALL support a corpus-growth retrieval experiment identifying the PostgreSQL-to-dedicated-search migration threshold. |
| R22.12 | THE SYSTEM SHALL write all experiment outputs to versioned, reproducible artifacts (CSV/JSON + run manifest with config hash and git SHA). |

## R23 — Operator API & Review Interface

**User story:** As a reviewer, I want to see the queue of drafts, the evidence behind each, and act on them.

| ID | Criterion |
|---|---|
| R23.1 | THE SYSTEM SHALL expose a versioned REST API (`/v1/...`) with OpenAPI documentation generated from code. |
| R23.2 | THE SYSTEM SHALL provide endpoints for mailboxes, threads, messages, jobs, drafts, knowledge documents, and evaluation runs. |
| R23.3 | THE SYSTEM SHALL provide a retrieval-debug endpoint returning the constructed query, both branch results with ranks, fused scores, rerank scores, and the final selected chunks. |
| R23.4 | THE SYSTEM SHALL provide a review UI listing pending drafts with the original email, thread summary, cited chunks, and approve/edit/reject actions. |
| R23.5 | THE SYSTEM SHALL display the job timeline and current state for any message in the UI. |
| R23.6 | THE SYSTEM SHALL paginate all list endpoints and require `organization_id` scoping on every request. |
| R23.7 | THE SYSTEM SHALL provide a knowledge-upload flow showing per-document ingestion status. |

## R24 — Engineering Quality Baseline

| ID | Criterion |
|---|---|
| R24.1 | THE SYSTEM SHALL be developed in a single repository with a documented layout and one dependency manifest per service language. |
| R24.2 | THE SYSTEM SHALL enforce formatting, linting, and static type checking in CI. |
| R24.3 | THE SYSTEM SHALL maintain unit tests for every pure component (chunker, RRF, rules, state machine, query builder, idempotency). |
| R24.4 | THE SYSTEM SHALL maintain integration tests using ephemeral PostgreSQL and RabbitMQ containers. |
| R24.5 | THE SYSTEM SHALL stub all external LLM, embedding, and provider calls in CI; no test SHALL require live credentials. |
| R24.6 | THE SYSTEM SHALL record every significant technology decision as a short ADR in `docs/adr/`. |
| R24.7 | THE SYSTEM SHALL keep an end-to-end smoke test that drives a fixture email from ingestion to draft. |

---

## Non-functional requirements (SLO targets)

These are **project SLO targets**, measured under the reference workload, not guarantees about third-party providers.

| ID | Stage | Target |
|---|---|---:|
| NFR1 | Notification → ingestion | < 1 s |
| NFR2 | Parse + persistence | < 100 ms |
| NFR3 | Classification | 20–300 ms |
| NFR4 | Queue scheduling | < 100 ms |
| NFR5 | Hybrid retrieval | 50–250 ms |
| NFR6 | Reranking | 20–200 ms |
| NFR7 | Context assembly | < 50 ms |
| NFR8 | LLM generation | 1–5 s |
| NFR9 | Draft persistence | < 50 ms |
| NFR10 | **End-to-end typical** | **2–6 s** |
| NFR11 | **End-to-end p95** | **< 10 s** |
| NFR12 | Sustained ingest throughput | ≥ 25 msg/s (20× burst of the 1.16 msg/s reference average) |
| NFR13 | Reference workload | 10,000 mailboxes / 100,000 emails per day |
| NFR14 | Expected funnel | ≈45% no-reply, ≈20% deterministic, ≈35% AI-generated, ≈70% of AI needing RAG (~24,500/day) |

---

## Success criteria (final evaluation gate)

| ID | Metric | Target |
|---|---|---:|
| SC1 | Email category macro-F1 | ≥ 0.90 |
| SC2 | Retrieval Recall@5 | ≥ 0.85 |
| SC3 | Useful-draft acceptance rate | ≥ 80% |
| SC4 | Typical end-to-end latency | ≤ 6 s |
| SC5 | p95 end-to-end latency | ≤ 10 s |
| SC6 | Duplicate logical processing | 0 |
| SC7 | Recovery from temporary worker failure | successful |
| SC8 | Throughput increase when workers added | demonstrable |
| SC9 | Cost per generated email | measured and reported |
| SC10 | RAG improvement over non-RAG baseline | statistically observable |

Report measured values as measured. Do not tune the dataset to hit a target.

---

## Hypotheses under test

| ID | Hypothesis | Primary evidence |
|---|---|---|
| H1 | Hybrid lexical+semantic retrieval beats vector-only for email knowledge | R22.2, R22.3 |
| H2 | Cascaded triage cuts unnecessary generation without hurting routing accuracy | R22.1, R22.4, R6.10 |
| H3 | Thread summarization cuts tokens while preserving reply quality | R22.5, R8.7 |
| H4 | Model cascading cuts cost vs a single strong model at acceptable quality | R22.6, R15.4 |
| H5 | The async queue architecture absorbs bursts and scales with stateless workers | R22.7, R20.4 |

---

## Traceability index (requirement group → implementation phase)

| Group | Phase(s) in `tasks.md` |
|---|---|
| R24 Engineering baseline | Phase 0 |
| R5 Data platform | Phase 0, 1 |
| R1, R2, R4 Ingestion | Phase 1 |
| R3, R6, R7, R18, R19 Triage & async | Phase 2 |
| R9, R10, R11 Knowledge & retrieval | Phase 3 |
| R8, R12, R14, R15, R16 Context & generation | Phase 4 |
| R13 Business data | Phase 5 |
| R17 Dispatch | Phase 6 |
| R21, R22 Observability & evaluation | Phase 7 |
| R20, R23 Deployment & UI | Phase 0, 6, 8 |

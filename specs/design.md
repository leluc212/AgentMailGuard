# Design — Enterprise RAG-Based Intelligent Email Management and Response System

**Spec ID:** `rag-email`
**Read this before writing any code.** `requirements.md` says *what must be true*; this document says *what the system looks like when it is true*; `tasks.md` says *in what order to build it*.

---

## 1. Overview

The system turns inbound enterprise email into grounded, reviewable replies while spending inference only where it creates value. The governing sentence:

> **Classify first. Retrieve only when required. Generate only when necessary.**

Everything in this design exists to make that sentence operational and measurable.

**Shape of the system:** a set of stateless Python workers connected by a RabbitMQ broker, sharing one PostgreSQL (with `pgvector`) as the authoritative operational *and* search store, with MinIO/S3 for blobs and OpenTelemetry/Prometheus/Grafana for evidence.

**The single most important flow property:** of 100,000 daily emails, ~45,000 terminate at triage with zero AI cost, ~20,000 follow deterministic workflows, ~35,000 reach generation, and only ~24,500 trigger retrieval. Cost tracks *actionable* mail, not *total* mail.

---

## 2. Design principles → where they are enforced

| Principle | Enforcement point in this design |
|---|---|
| Event-driven, never poll | §5.1 webhook + checkpoint sync (R2) |
| Routing ≠ retrieval | Triage Engine and Hybrid RAG Engine are separate services (R6, R10) |
| Selective AI execution | `reply_required` / `retrieval_required` gates in §7.3 (R6.5, R6.6) |
| Async workload decoupling | Broker between every stage; webhook returns before any fetch (R3) |
| Stateful conversations, stateless workers | `thread_state` in PostgreSQL; workers hold nothing (R8, R20.2) |
| Progressive infrastructure complexity | `SearchBackend` interface, PostgreSQL implementation only (R10.7) |

**Anti-goals** — if a design choice moves toward any of these, stop and raise it:

- Multi-agent chains (planner → critic → writer) in the default path.
- Batching unrelated emails into one prompt.
- Embedding every inbound email.
- Introducing OpenSearch, MongoDB, or a dedicated vector DB before measurement justifies it.
- Adding blobs to relational columns.

---

## 3. System architecture

### 3.1 Component map

Twelve logical components. This matches `enterprise-rag-email.architecture.json` exactly — that file is the canonical diagram source.

```
┌──────────────────┐   change notification    ┌────────────────────────┐
│ Email Providers  │ ───────────────────────► │ Mail Connector Service │
│ Gmail·Graph·IMAP │ ◄─────────────────────── │ Adapter·Sync·Checkpoint│
└──────────────────┘   incremental fetch      └───────────┬────────────┘
         ▲                                                │ publish inbound work
         │ create draft / send reply                      ▼
┌────────┴───────────────┐                    ┌────────────────────────┐
│ Draft Store &          │                    │   RabbitMQ  Ingest     │
│ Dispatcher             │                    │ inbound queue·ack·DLQ  │
└────────▲───────────────┘                    └───────────┬────────────┘
         │ structured draft                               │ consume
┌────────┴───────────────┐                    ┌───────────▼────────────┐
│ Reply Agent &          │                    │ Email Processing Svc   │
│ Model Cascade          │                    │ MIME·HTML clean·dedupe │
└────────▲───────────────┘                    └───────────┬────────────┘
         │ assembled context                              │ normalized email
┌────────┴───────────────┐                    ┌───────────▼────────────┐
│    Context Builder     │                    │ Cascading Triage Engine│
│  thread·business·RAG   │ ◄──────────────────┤ rules → ML → LLM       │
└────────┬───────────────┘  scheduled job     └───────────┬────────────┘
         │ selective query               ┌────────────────┴──── no reply ──► COMPLETED
         ▼                               ▼
┌────────────────────────┐    ┌────────────────────────┐
│  Hybrid RAG Engine     │    │  RabbitMQ Categories   │
│  FTS+HNSW·RRF·rerank   │    │ priority·micro-batch   │
└────────┬───────────────┘    └────────────────────────┘
         │ FTS + ANN
┌────────▼───────────────┐    ┌────────────────────────┐
│ PostgreSQL Platform    │ ◄──┤ Knowledge Ingestion    │
│ relational·FTS·pgvector│    │ parse·350-700t·embed   │
└────────────────────────┘    └────────────────────────┘
```

### 3.2 Runtime processes (what actually gets deployed)

| Process | Hosts components | Scales on |
|---|---|---|
| `api` | REST + webhook receivers | request rate |
| `mail-connector` | Mail Connector Service | mailbox count, provider events |
| `email-worker` | Email Processing Service | inbound message rate |
| `triage-worker` | Cascading Triage Engine | classification queue depth |
| `ai-worker` | Context Builder + Hybrid RAG + Reply Agent | generation queue depth |
| `knowledge-worker` | Knowledge Ingestion | document ingestion volume |
| `dispatch-worker` | Draft Store & Dispatcher (send side) | dispatch queue depth |
| `frontend` | Review UI | — |

Context Builder, Hybrid RAG, and Reply Agent co-locate in `ai-worker` to avoid three network hops on the hot path. They remain **separate modules with separate interfaces** so they can be split into their own processes later without refactoring callers.

### 3.3 The five canonical views

Keep these in mind when reasoning about a change; they come from the architecture JSON and are how the system gets explained.

1. **End-to-end email path** — providers → connector → ingest queue → processor → triage → category queue → context → agent → dispatcher.
2. **Selective hybrid RAG** — context builder → hybrid RAG → PostgreSQL → back to context builder.
3. **Knowledge ingestion** — offline pipeline → PostgreSQL. Never on the hot path.
4. **Async reliability & recovery** — at-least-once, idempotent jobs, durable checkpoints, backoff, dead-letter.
5. **Scale, deployment & operations** — Docker Compose → worker pools scaling on queue depth, OTel tracing throughout.

---

## 4. Repository layout

```
rag-email/
├── docker-compose.yml
├── docker-compose.scale.yml           # replica overrides for load tests
├── .env.example
├── Makefile                           # up, down, migrate, seed, test, eval, load
├── docs/
│   ├── adr/                           # ADR-0001..N  (R24.6)
│   ├── configuration.md
│   └── runbook.md
├── specs/                             # requirements.md · design.md · tasks.md
├── migrations/                        # versioned SQL (R5.5)
├── services/
│   ├── api/                           # FastAPI app, routers, OpenAPI
│   ├── mail_connector/
│   ├── email_worker/
│   ├── triage_worker/
│   ├── ai_worker/
│   ├── knowledge_worker/
│   ├── dispatch_worker/
│   └── frontend/                      # review UI: FastAPI + Jinja2 + htmx (ADR-0009)
├── packages/
│   ├── core/                          # settings, logging, tracing, errors, ids
│   ├── domain/                        # entities, value objects, state machine
│   ├── db/                            # engine, repositories, unit of work
│   ├── broker/                        # topology, publisher, consumer base
│   ├── adapters/                      # gmail/ graph/ imap/ fake/
│   ├── llm/                           # LLMProvider impls, router, schemas
│   ├── retrieval/                     # SearchBackend, rrf, rerank, query builder
│   ├── knowledge/                     # parsers, chunker, embedder
│   ├── business/                      # BusinessDataProvider
│   ├── dispatch/                      # build_outbound_reply, DispatchService (design §5.8)
│   └── observability/                 # metrics registry, span helpers, cost table
├── evaluation/
│   ├── datasets/                      # classification & retrieval benchmarks
│   ├── experiments/                   # one runner per experiment in R22
│   └── results/                       # versioned run artifacts
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

**Dependency rule:** `services/*` may import `packages/*`. `packages/*` must not import `services/*`. `packages/domain` imports nothing but stdlib and `packages/core`.

---

## 5. Service designs

### 5.1 Mail Connector Service — *R1, R2*

**Responsibility:** be the only component that knows a provider exists.

**Inputs:** webhook notifications (via `api`), scheduled subscription renewal, manual re-sync requests.
**Outputs:** `mail.sync.requested` → `email.normalize` jobs; `mailbox_checkpoint` writes.

```python
class MailProviderAdapter(Protocol):
    async def subscribe(self, mailbox: Mailbox) -> Subscription: ...
    async def renew_subscription(self, sub: Subscription) -> Subscription: ...
    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult: ...
    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage: ...
    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread: ...
    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef: ...
    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef: ...


@dataclass
class SyncResult:
    messages: list[RawMessage]
    new_checkpoint: Checkpoint
    requires_full_resync: bool
    has_more: bool  # caller loops until False
```

**Sync algorithm (provider-neutral):**

```
on sync_requested(mailbox_id):
    if in_flight(mailbox_id):        # R2.9
        mark_pending_followup(mailbox_id); return
    cp = load_checkpoint(mailbox_id)
    loop:
        result = adapter.synchronize(mailbox, cp)
        if result.requires_full_resync:
            cp = bounded_full_sync(mailbox); continue       # R2.7
        for raw in result.messages:
            store_raw_blob(raw)                             # R4.10
            publish(email.normalize, job(raw))              # persistent
        commit()                                            # side effects durable
        cp = result.new_checkpoint
        save_checkpoint(cp)                                 # R2.8 — only now
        if not result.has_more: break
```

The checkpoint write is **after** the publish commit. Re-running a window is safe (idempotency, R19); losing a window is not.

**Provider specifics (confined to adapters):**

| | Gmail | Microsoft Graph |
|---|---|---|
| Notification | Cloud Pub/Sub push | webhook subscription |
| Checkpoint | `historyId` | `@odata.deltaLink` |
| Incremental call | `users.history.list(startHistoryId=…)` | delta query, follow `@odata.nextLink` |
| Expiry signal | 404 / history too old | invalid delta token |
| Subscription life | watch expires ~7 days | subscription expires, must renew |

**Failure modes:** `RateLimited` → honour retry-after, backoff (R1.6). `AuthExpired` → mark mailbox `needs_reauth`, stop syncing, alert; do not spin. `Transient` → retry. `Permanent` → dead-letter.

---

### 5.2 Email Processing Service — *R4*

**Responsibility:** one canonical message shape; nothing downstream touches MIME.

**Pipeline:**

```
raw blob → MIME parse → part selection → HTML→text → quoted-history split
        → signature strip → subject normalize → thread association
        → dedupe check → persist (message + attachments metadata) → emit CLASSIFY job
```

**Thread association order (first match wins, R4.6):**
1. provider `thread_id`
2. `In-Reply-To` / `References` → existing `email_message.rfc822_message_id`
3. normalized subject + overlapping participant set within a time window
4. otherwise: new thread

**Dedupe:** insert with `ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING`. Zero rows affected ⇒ already processed ⇒ ack and return success, emitting no new job (R4.8).

**Normalized message contract:**

```json
{
  "message_id": "uuid", "thread_id": "uuid", "mailbox_id": "uuid",
  "organization_id": "uuid", "provider": "gmail",
  "provider_message_id": "...", "rfc822_message_id": "<...>",
  "sender": {"name": "...", "email": "..."},
  "recipients": [], "cc": [], "subject": "...", "subject_normalized": "...",
  "body_text": "full text incl. quoted history",
  "body_text_clean": "new content only",
  "body_html_ref": "s3://raw/...",
  "received_at": "ISO-8601", "direction": "inbound",
  "attachments": [{"filename":"...","mime_type":"...","size_bytes":0,"object_key":"..."}],
  "flags": {"normalization_failed": false, "signature_stripped": true}
}
```

Parsing failure does not discard: persist with `normalization_failed`, keep the raw key, dead-letter the job (R4.9).

---

### 5.3 Cascading Triage Engine — *R6*

**Responsibility:** decide cheaply whether this email deserves expensive treatment.

```
                     normalized email
                            │
                 ┌──────────▼──────────┐
                 │ Stage 1: Rules      │  ~1ms, free
                 │ sender/header/subject
                 └──────────┬──────────┘
                    conf ≥ τ₁ ? ──yes──► emit
                            │ no
                 ┌──────────▼──────────┐
                 │ Stage 2: ML classifier │  ~20-50ms, ~free
                 │ embedding/TF-IDF + linear head
                 └──────────┬──────────┘
                    conf ≥ τ₂ ? ──yes──► emit
                            │ no
                 ┌──────────▼──────────┐
                 │ Stage 3: small LLM  │  ~200-300ms, cheap
                 └──────────┬──────────┘
                            ▼ emit (or safe default on failure, R6.11)
```

**Output contract:**

```json
{
  "category": "technical_support",
  "intent": "password_reset",
  "priority": "normal",
  "reply_required": true,
  "workflow_hint": "ai",
  "retrieval_required": true,
  "confidence": 0.96,
  "decided_by": "ml",
  "latency_ms": 34,
  "model": null
}
```

**Rule configuration** (declarative, hot-reloadable — R6.8):

```yaml
rules:
  - id: auto-submitted
    when: {header.Auto-Submitted: {exists: true}}
    then: {category: automated_notification, reply_required: false, confidence: 0.99}
  - id: newsletter
    when: {any: [{header.List-Unsubscribe: {exists: true}},
                 {sender.email: {matches: "^(no-?reply|newsletter)@"}}]}
    then: {category: automated_notification, reply_required: false, confidence: 0.97}
  - id: invoice-reference
    when: {body.matches: "\\bINV-\\d{4}-\\d{5}\\b"}
    then: {category: billing, retrieval_required: true, confidence: 0.85}
```

**The three gates that define the system's economics:**

- `reply_required == false` → job goes straight to `COMPLETED`. No embedding, no retrieval, no rerank, no generation (R6.5). This is the ~45%.
- `workflow_hint == 'template'` → a deterministic approved-template reply is rendered and the job goes to `DRAFTED` without retrieval or generation (R6.12, R6.13). This is the ~20%.
- `retrieval_required == false` → context is thread + business data only; hybrid RAG is not called (R6.6). For a reply that reaches AI generation this flag is the stage's own answer **or** its category's default (next paragraph).

**The category retrieval floor (ADR-0013, proposed).** A triage stage can answer `retrieval_required=false` for a question only the knowledge base can answer: in the live v2 smoke of 2026-09-30 the stage-3 model did so for 9 of 9 company-policy questions (warranty period, refund fee, password reset, shipping redirect, discount, policy), so hybrid retrieval and the reranker never ran. `config/categories.yaml` already declares `default_retrieval_required` per category (true for support, sales, billing, administration and general_inquiry; false for scheduling and the no-reply categories), and nothing applied it to a stage's result. The gate applies it where a required reply is finally routed to AI generation, for every stage (rule, ML, LLM and the R6.11 safe default):

```
routed.retrieval_required = stage.retrieval_required  OR  category.default_retrieval_required
```

It runs only in the third outcome, after the no-reply exit (R6.5) and the matched-template reply (R6.13) have been taken, so those two stay zero-AI. A template-hinted message with no matching template falls back to AI (R6.14), and then the floor applies. A category the taxonomy does not know has no default, so the stage's answer stands. The classification row persisted by the cascade keeps the stage's own answer (R6.7); the gate's `QUEUED` event payload records `retrieval_required_from_category` (true when the floor raised the flag) and the routed classification's `raw` carries the same key when it is true. `TRIAGE__CATEGORY_RETRIEVAL_FLOOR=false` switches the floor off and restores the stage's own answer.

**Why the template gate exists.** The reference funnel is 45% no-reply / 20% deterministic / 35% AI. Without a template path there is nowhere for the middle 20% to go, the funnel cannot reconcile against NFR14, and 20,000 daily emails that need no reasoning are paid for at generation prices. The template registry is keyed by `(category, intent)` with variable substitution from message and business fields; **no match ⇒ fall back to `workflow_hint='ai'`** (R6.14) — the template path never blocks a reply, it only makes cheap replies cheap.

```yaml
templates:
  - match: {category: acknowledgement, intent: receipt_confirmation}
    subject: "Re: {{ subject }}"
    body: prompts/templates/acknowledgement.v1.txt
  - match: {category: scheduling, intent: meeting_accepted}
    body: prompts/templates/scheduling_ack.v1.txt
```

Three mutually exclusive outcomes — early exit, template, AI — so every actionable email is accounted for with no residual bucket (R6.15).

**Category set:** support, sales, billing, administration, scheduling, general_inquiry, automated_notification, acknowledgement, no_response.

---

### 5.4 Context Builder — *R8, R12, R13, R14.8*

**Responsibility:** assemble the smallest sufficient context. This is where "selective" becomes real.

```
                       Context Builder
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   Thread state        Business data         Hybrid RAG
 (summary + recent)   (customer/order/       (only if
                       ticket facts)      retrieval_required)
        └────────────────────┼────────────────────┘
                             ▼
                    ContextPackage (ordered)
```

**Fixed assembly order (R14.8) — do not reorder:**

```
1. Agent instructions          (from agent profile, static → cacheable prefix)
2. Category instructions       (from agent profile)
3. Thread summary              (if exists)
4. Recent thread messages      (last N)
5. Current email               (body_text_clean)
6. Retrieved knowledge         (Top 4–6 chunks, each with citation id)
7. Business data               (labelled distinctly from knowledge)
```

Sections 1–2 are static per profile so provider prompt-prefix caching can apply (cost lever).

**Thread state policy (R8):**

```
if message_count <= SUMMARY_MSG_THRESHOLD and est_tokens <= SUMMARY_TOKEN_THRESHOLD:
    context = recent_messages_verbatim
else:
    if thread_state.summarized_through_message_id < latest_message_id - LAG:
        thread_state = summarize(thread_state, new_messages)   # one LLM call
    context = thread_state.summary + last_N_messages
```

Summarization is threshold-triggered, **not** per-message (R8.4). Optimistic concurrency on `thread_state.version` (R8.6).

**Query construction (R12):**

```python
@dataclass
class RetrievalQuery:
    semantic_text: str  # email intent + thread topic, natural language
    lexical_terms: list[str]  # keywords
    identifiers: list[str]  # INV-…, ORDER-…, INC…, SKU-…, extracted by regex
    filters: dict  # organization_id, category, doc status=active
```

Built from `current email + thread summary + classification intent` — no extra LLM call in the default path (R12.5). Persisted with the job for replay (R12.6).

The category filter is derived from the classification (R12.4) and `RETRIEVAL__CATEGORY_FILTER_ENABLED=false` switches it off; the organization and status filters always stay (R10.4). Production keeps it on. Only the live benchmark turns it off, because its knowledge documents are filed under the case's own category while live triage picks the query's (ADR-0013, proposed).

**Business data (R13) — code-side fetch plan (ADR-0008):** the decision to fetch is made in code, before the one generation call, from signals the pipeline already has. The model never calls a lookup tool: a tool call needs a second model request, which breaks the one-generation budget (§5.7). An intent-only trigger is not enough, because the ML triage stage always emits `intent=None`.

```
inputs: organization_id, sender address, category → profile context_policy, intent, typed IDs
        │
        ▼
FetchPlan (pure function, packages/business/plan.py, no I/O, no model)
  1. typed IDs in subject + body:  ORD-… / "order 82915" → order lookup
                                   TICK-… / "ticket 4402" → ticket lookup
                                   INV-…                 → NOT_LOOKED_UP (unsupported_entity)
  2. snapshot:
       routed profile's context_policy == thread_plus_rag_plus_business ⇒ orders + tickets
       else intent in INTENT_ENTITIES (constant in plan.py)            ⇒ only the mapped entities
            invoice_inquiry, receipt_lookup, payment_failure, refund_request → orders
  3. nothing planned ⇒ no provider call, no customer resolution
        │
        ▼
BusinessDataProvider.get_business_context(org_id, sender_email, plan)   under BUSINESS_DATA__TIMEOUT_MS
  • resolve sender → customer first (R13.4): lower(email) match within organization_id
      → customer_status FOUND | UNKNOWN_SENDER (0 rows) | AMBIGUOUS_CUSTOMER (>1 rows)
      → not FOUND ⇒ no lookups run
  • every lookup is a fixed, parameterised query scoped to (organization_id, customer_id)
        │
        ▼
BusinessContext: customer_status (once) + one BusinessFact per planned lookup or snapshot row
```

- **Typed IDs.** A separate extractor keeps the entity type (the retrieval query builder's untyped regex stays as it is). It accepts two forms, case-insensitive:
  - prefixed: `ORD-<digits>` / `ORDER-<digits>`, `TICK-<digits>` / `TICKET-<digits>`, `INV-<digits>[-<digits>…]`;
  - bare: the word `order` or `ticket`, optionally followed by `#`, `no.` or `number`, then **at least 4 digits** ("order 82915", "order #82915", "ticket number 4402").

  "in order to", "an order 2 days ago" and "ticket 3 of 5" do not match. Orders normalise to `ORD-<digits>` and tickets to `TICK-<digits>`, the stored `order_number` / `ticket_number` format. The extractor runs on every job, independent of `retrieval_required`.
- **Typed IDs override `context_policy`.** An order number typed into a `general_inquiry` email is still looked up. `context_policy` gates only the full snapshot. Which categories get it is configuration (`config/agent_profiles.yaml`), not code.
- **Where the policy comes from.** The Context Builder takes the `AgentProfileRegistry` as a constructor dependency; the ai-worker passes the same instance it gives `SinglePassGenerator`. `build_context` resolves the profile with `registry.resolve_profile(category)` — the default profile when there is no classification, the same rule the generator uses — and reads `profile.context_policy`. The instruction source is not changed by this.
- **Snapshot.** The customer's most recent orders by `placed_at DESC` (`BUSINESS_DATA__SNAPSHOT_ORDERS`, default 3) and tickets whose status is not `closed` or `resolved`, newest `opened_at` first (`BUSINESS_DATA__SNAPSHOT_TICKETS`, default 3). One `FOUND` fact per row; one `NOT_FOUND` fact per entity type when the customer has none.
- **Statuses (R13.6).** Two levels, so each case has exactly one representation:
  - `customer_status`, recorded once: `FOUND` · `UNKNOWN_SENDER` · `AMBIGUOUS_CUSTOMER` · `UNAVAILABLE`.
  - per fact: `FOUND` · `NOT_FOUND` (lookup ran, no row for this customer) · `NOT_LOOKED_UP` with a reason (`unsupported_entity` for `INV-`; `unknown_sender` / `ambiguous_customer` for orders and tickets when resolution did not give one customer; `INV-` keeps `unsupported_entity`) · `UNAVAILABLE` (timeout or error).

  `NOT_FOUND` and `UNAVAILABLE` are never merged: a timeout must not become "we have no such order". A missing entity is always an explicit fact, never an omission.
- **Timeout and degradation (R13.7).** The provider call runs under its own deadline (`BUSINESS_DATA__TIMEOUT_MS`, default 500), and the Postgres implementation also sets `statement_timeout` for its transaction. On timeout or error, `customer_status` and every planned fact become `UNAVAILABLE`, `business_data_degraded=true` is recorded, and the draft is still written. The three `BUSINESS_DATA__*` keys form a `business_data` settings group.
- **Label and precedence (R13.3, R13.5).** Facts render as one `[BUSINESS DATA]` section (section 7 above) in every profile's template, with a `source` and `as_of` header and one `key: value` line per fact — never a JSON array. The agent instructions the live path sends (today `DefaultInstructionProvider` in `packages/context/builder.py`; `DEFAULT_ENTERPRISE_INSTRUCTIONS` in `packages/llm/profile.py` carries the same line so the two cannot drift) carry one precedence rule: *order, ticket and invoice status, dates and amounts come only from `[BUSINESS DATA]`; if a fact is `NOT_FOUND` or `UNAVAILABLE`, say so; knowledge chunks explain procedure only.* Knowledge chunks stay in the context, because the reply still cites the procedure.
- **Replaceability (R13.2).** The plan is built outside the provider, so a CRM/ERP adapter only executes lookups and replaces the Postgres implementation without touching the Context Builder.
- **Replay and observability.** The plan, each fact's status and `business_data_degraded` go into the `CONTEXT_READY` payload, next to `retrieval_performed`. The step emits the `business.fetch` span (§10), a `business_lookups_total{entity, status}` counter and a `business_lookup_latency_ms` histogram.
- **Identity assumption.** The sender address is treated as the customer's identity, as R13.4 requires. This is an accepted trust assumption, not a verification control: sender verification is out of scope (CLAUDE.md §6) and recorded in ADR-0008.
- **Not in scope here.** Business facts in template replies (`workflow_hint=template` stays zero-lookup in Phase 5); an invoice table (there is none, so `INV-` references are `NOT_LOOKED_UP`); model-written SQL (never).

---

### 5.5 Hybrid RAG Engine — *R10, R11*

**Responsibility:** the right 4–6 chunks, fast.

```
RetrievalQuery
     │
 ┌───┴────────────────┬─────────────────┐
 ▼ (concurrent)       ▼                 
PostgreSQL FTS    pgvector HNSW         
 top 20               top 20            
 └───────────┬────────┘                 
             ▼                          
     RRF fusion (k=60)  ──►  ~20 candidates
             ▼
     Cross-encoder rerank (optional)
             ▼
          Top 4–6  ──► ContextPackage
```

**Interface (R10.7) — the migration seam:**

```python
class SearchBackend(Protocol):
    async def lexical(self, q: RetrievalQuery, top_n: int) -> list[Candidate]: ...
    async def vector(self, q: RetrievalQuery, top_n: int) -> list[Candidate]: ...


@dataclass
class Candidate:
    chunk_id: str
    document_id: str
    content: str
    metadata: dict
    lexical_rank: int | None
    vector_rank: int | None
    lexical_score: float | None
    vector_score: float | None
    fused_score: float | None
    rerank_score: float | None
```

`PostgresSearchBackend` ships now. `OpenSearchBackend` is a later drop-in — nothing above this interface changes (R10.7, §12.2).

**Reference hybrid SQL** (filters applied *inside* each branch, R10.4):

```sql
WITH lexical AS (
  SELECT c.id AS chunk_id,
         ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.query) DESC) AS rnk
  FROM knowledge_chunk c
  JOIN knowledge_document d ON d.id = c.document_id
  CROSS JOIN to_tsquery('english', :or_tsquery) AS q(query)
  WHERE c.organization_id = :org
    AND d.status = 'active'
    AND (:category IS NULL OR d.category = :category)
    AND c.content_tsv @@ q.query
  ORDER BY rnk LIMIT :top_n
),
vector AS (
  SELECT c.id AS chunk_id,
         ROW_NUMBER() OVER (ORDER BY e.embedding <=> :query_vec) AS rnk
  FROM embedding_record e
  JOIN knowledge_chunk c    ON c.id = e.chunk_id
  JOIN knowledge_document d ON d.id = c.document_id
  WHERE c.organization_id = :org
    AND d.status = 'active'
    AND (:category IS NULL OR d.category = :category)
  ORDER BY e.embedding <=> :query_vec
  LIMIT :top_n
)
SELECT COALESCE(l.chunk_id, v.chunk_id) AS chunk_id,
       l.rnk AS lexical_rank, v.rnk AS vector_rank,
       COALESCE(1.0/(:k + l.rnk), 0) + COALESCE(1.0/(:k + v.rnk), 0) AS fused_score
FROM lexical l FULL OUTER JOIN vector v USING (chunk_id)
ORDER BY fused_score DESC
LIMIT :fuse_limit;
```

`:or_tsquery` is built by `build_or_tsquery` (`packages/retrieval/query_builder.py`): the query's terms joined by OR (a chunk that holds any term matches; `ts_rank_cd` ranks the chunks with more matching terms higher), stop words and one-character terms removed, each term single-quoted so it cannot inject tsquery syntax, at most 64 terms. An empty result skips the lexical branch and hybrid retrieval is vector-only. `websearch_to_tsquery` was used before Amendment 1 G.1 and is not used any more: it ANDs every word, so a ~20-word email query matched almost no chunk.

Identifiers from `RetrievalQuery.identifiers` are injected into the lexical branch verbatim — this is why exact-match cases like `INV-2026-01829` work where vector-only retrieval fails (R12.3).

**RRF:** `score(d) = Σ_r 1/(k + rank_r(d))`, `k = 60` default. Used because BM25-style and cosine scores are not on a comparable scale.

**Filtered ANN under-fill — read this before tuning retrieval (R10.10, R10.11).**

The vector branch applies `WHERE organization_id = :org AND d.status='active'` *and* an HNSW `ORDER BY … LIMIT 20`. HNSW post-filters: the index walk returns its `ef_search` nearest neighbours, and only then are non-matching tenants/versions discarded. On a multi-tenant corpus this can return **far fewer than 20 rows — sometimes zero — while reporting success**. RRF then fuses a short list without complaint, recall collapses, and nothing in the pipeline raises an error. This is the single most likely cause of quietly bad retrieval in this design, and it will not show up in a single-tenant dev database.

Mitigation, in order of preference:

1. **Detect it.** If the vector branch returns `< top_n`, set `retrieval_underfilled=true` and export it as a metric. Never let an under-filled branch pass silently.
2. **Widen the walk.** Raise `hnsw.ef_search` (session-scoped, e.g. `SET LOCAL hnsw.ef_search = 200`) or enable pgvector's iterative index scans so the walk continues until the filtered `LIMIT` is satisfied.
3. **Partition the index.** For a small number of large tenants, partial indexes per `organization_id` — or a partitioned `embedding_record` — turn the filter into index selection rather than post-filtering.
4. **Only then** degrade to the lexical branch.

Integration tests must seed **at least three tenants with overlapping content** and assert the target tenant's full top-N is returned (R10.11). A single-tenant fixture will pass while production silently under-retrieves.

**Degradation (R10.6):** one branch failing or timing out does not fail the request — the surviving branch's ranking is used and `retrieval_degraded=true` is recorded. Reranker unavailable ⇒ fall back to RRF order (R11.5).

**Context packing (R11):** Top-K default 4–6, hard token budget, truncation only at chunk boundaries. More chunks ≠ better answers; the Top-3/5/10 sweep (R22.9) is what settles K for this corpus.

---

### 5.6 Knowledge Ingestion Pipeline — *R9*

Offline. Never on the email hot path.

```
document → parser → structure extraction → chunker → metadata enrichment
        → embedder → persist(chunk + tsvector + embedding) → mark indexed
```

**Chunking (R9.3, R9.4):** respect heading/section/paragraph/list boundaries; target 350–700 tokens (configurable), configurable overlap. Never split mid-sentence at a fixed character count.

**Chunk record:**

```json
{
  "document_id": "DOC-125", "chunk_id": "DOC-125-08", "chunk_index": 8,
  "title": "Account Recovery Procedure", "heading_path": ["Support","Accounts","Locked Account"],
  "section": "Locked Account", "category": "technical_support", "version": 4,
  "content": "...", "token_count": 512, "content_checksum": "sha256:…"
}
```

**Re-ingestion (R9.8, R9.9):** write version N+1 chunks, then flip `knowledge_document.status` in one transaction. Chunks whose `content_checksum` is unchanged carry their embedding forward — no re-embedding, no cost.

**Cost note:** embedding is a one-time-per-version cost. At `$0.02/M` input tokens, a 20M-token corpus indexes for ~$0.40. Generation, not embedding, is the recurring cost — which is why R6's gates matter more than embedding optimization.

---

### 5.7 Reply Agent & Model Cascade — *R14, R15, R16*

**Agent profiles (R14.1):** specialization by configuration, not by chaining agents.

```yaml
profiles:
  technical_support:
    knowledge_domain: support
    response_style: professional
    model_tier: routine
    context_policy: thread_plus_rag
    prompt_template: prompts/support.v3.j2
    output_schema: schemas/reply.v1.json
  billing:
    knowledge_domain: billing
    response_style: precise_formal
    model_tier: routine
    context_policy: thread_plus_rag_plus_business
    prompt_template: prompts/billing.v2.j2
```

**The default path is one retrieval + one generation (R14.3).** No planner, no critic, no writer chain (R14.4).

**Per-job model call budget (R14.9) — the number that must not drift:**

| Call | When | Max |
|---|---|---|
| Triage LLM | stage 3 only, after rules and ML both abstain | 1 |
| Thread summarization | threshold-triggered only (§5.4) | 1 |
| **Generation** | always, for AI-path emails | **exactly 1** |
| Schema repair | only on validation failure | 1 |

So the *budget ceiling* is 4 and the *common case* is 1. "One generation call per job" is the invariant to assert in tests — not "one LLM call per job", which would contradict summarization and triage fallback. Escalation to the high-capability tier **replaces** the generation call; it does not add one (R15.5).

**Model cascade (R15):**

```
ContextPackage ──► Complexity Router ──┬─ routine tier          (default, ~90%)
                                       └─ high-capability tier  (escalated, ~10%)
```

Escalation triggers (any, all configurable): `classification.confidence < τ`, thread message count or token estimate above threshold, fewer than *m* retrieved chunks above a relevance floor, multiple distinct requested actions detected, total context tokens above threshold. Max one escalation per job (R15.5). A config switch forces single-tier so the cascade can be measured against a single strong model (R15.6, H4).

**LLM abstraction (R14.5):**

```python
class LLMProvider(Protocol):
    async def generate(
        self,
        *,
        messages: list[Message],
        schema: dict,
        tier: ModelTier,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult: ...


@dataclass
class LLMResult:
    content: dict  # schema-validated
    model: str
    tier: ModelTier
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw_finish_reason: str
```

**Structured output (R16.1):**

```json
{
  "action": "reply",
  "draft": "Dear Customer, ...",
  "confidence": 0.94,
  "knowledge_chunks": ["DOC-125-08", "DOC-772-02"],
  "thread_summary_updated": false,
  "model_tier": "routine"
}
```

Validate → repair once → fail to retry/DLQ. An unvalidated draft is never persisted (R16.3). Citations naming chunks that were not in the context are flagged `citation_mismatch` (R16.5) — this is the hallucinated-citation detector, and its rate is a reportable quality metric.

**Generation failure routing (4.13a).** The AI worker classifies every generation failure before `BaseConsumer` acts:

| Failure | Route |
|---|---|
| Timeouts, 429/5xx, connection errors, unknown errors | retry ladder, then DLQ at `max_retries` |
| `UnvalidatedDraftError` (invalid after the one repair), `DraftSchemaContractError`, `UnpersistableDraftError` | DLQ at once with `x-failure-reason`; a `max_tokens` truncation is named in the reason |
| Missing or malformed job, missing message (`FatalError`) | DLQ at once with the reason |
| `IllegalStateTransitionError`, job already `DRAFTED`/`DISPATCHED`/`COMPLETED` or `DEAD_LETTER` | ack and drop, no model call (a redelivery or a stale copy) |
| `IllegalStateTransitionError`, job `QUEUED`/`CONTEXT_READY`/`GENERATING`/`RETRY_PENDING`, or state unreadable | retry ladder (another delivery owns the job, or a recovery write was skipped; the next attempt drafts or drops) |
| `IllegalStateTransitionError`, job `RECEIVED`/`NORMALIZED`/`CLASSIFIED`/`FAILED` | DLQ at once (the job was never routable to generation) |

Retrying a failure that does not go away only multiplies cost (each redelivery re-runs generate + repair). A human replays dead-lettered jobs through the replay endpoint after fixing the cause.

---

### 5.8 Draft Store & Dispatcher — *R16.6, R17*

**Draft lifecycle:** `draft → (edited) → approved | rejected → dispatched`.

Human-in-the-loop is the default posture (R16.8): a reviewer approves in the review UI, and only then does the system create the provider draft (default mode) or send the reply (`send_reply` mode). Auto-send is opt-in per category.

**Review API (R16.6, R23.2, R23.6):** `GET /v1/drafts` (filter by `status`, `category`, `mailbox`; cursor-paginated, org-scoped), `GET /v1/drafts/{id}` (draft with the original email, thread summary, cited chunks and the `[BUSINESS DATA]` facts), `PATCH /v1/drafts/{id}` (edit the body while `status=draft`), `POST /v1/drafts/{id}/approve`, `POST /v1/drafts/{id}/reject`. Approve and reject are idempotent: a repeated approve returns the first result and re-publishes the dispatch job while the job is not `COMPLETED` (dispatch is idempotent, so a lost publish cannot strand an approved draft); a repeated reject returns the first result.

**Feedback (R16.7, R21.4):** every decision writes one `feedback` row: `decision` (`accepted` = approved unchanged, `edited`, `rejected`), `edited_body`, character-level `edit_distance`, optional `rating`, `reviewer` (a free-text label; there is no login, see ADR-0009) and `review_ms` (time from opening the draft to deciding). `draft_decisions_total{decision, category}` is exported; the acceptance rate and the approved-without-edits rate are reported separately, because an unchanged approval can also mean an unread draft.

**Dispatch mode (R17.1, R17.6, R16.8):** set per category in `config/categories.yaml` (`dispatch_mode: create_draft | send_reply`), default `create_draft` for every category. `send_reply` needs an approval unless that category's `auto_send_eligible` is true; it is false for every category.

**Outbound reply (R17.2):** a pure `build_outbound_reply(draft, original, thread)` sets `In-Reply-To` to the original `Message-ID`, `References` to the original References plus its `Message-ID`, a new `Message-ID` of our own (Gmail: the MIME header; Graph: the draft's `internetMessageId`, so an orphaned draft can be found again), exactly one `Re: ` before the original subject, the **provider** thread id (`email_thread.provider_thread_id`, never our UUID; a null provider thread id dead-letters the dispatch), and the quoted original below the reply. Gmail keeps a reply in the thread only when `threadId`, the headers and the subject all match. The Graph adapter uses `createReply` + `send` with `Prefer: IdType="ImmutableId"`, never `sendMail` (which starts a new conversation).

**Dispatch — exactly once without a transactional send (R17.3–R17.5, R19.2, R19.3; ADR-0009):**

One job per email carries the whole lifecycle; dispatch drives the same `processing_job` that generation moved to `DRAFTED`. The dispatch idempotency key follows R19.2, `key(organization_id, mailbox_id, provider_message_id of the original email, "dispatch")`, and is stored `UNIQUE` on `generated_draft.dispatch_idempotency_key` (one draft per job, migration 0004), so no second job row is needed.

```
approve (commit first, then publish) ─▶ queue email.dispatch ─▶ dispatch-worker
  1 claim    one txn: set generated_draft.dispatch_idempotency_key, job DRAFTED → DISPATCHED
             job already COMPLETED ⇒ ack, send nothing · already DISPATCHED ⇒ resume at 2
  2 draft    adapter.create_draft(reply) → store provider_draft_id and the draft's provider message id
             (already stored ⇒ reuse it, never create a second draft)
  ── create_draft mode stops here: job DISPATCHED → COMPLETED, draft status dispatched,
     no outbound email_message (the customer has received nothing yet)
  3 send     send_reply mode only: adapter.send_draft(provider_draft_id)
  4 confirm  after an ambiguous failure (timeout, crash, unknown 5xx, Graph 202 not yet visible) the retry
             first asks adapter.get_draft_status(provider_draft_id):
               DRAFT   ⇒ send (Graph: re-check once after a short delay before resending)
               SENT    ⇒ go to 5 without sending
               MISSING ⇒ look in the provider thread for a sent message with the stored message id:
                         found ⇒ go to 5 · not found (a person deleted the draft) ⇒ dead-letter for an operator
  5 finish   one transaction: provider ref, draft status dispatched, DISPATCHED → COMPLETED,
             outbound email_message (direction='outbound'), thread updated
errors: 429, 5xx, Gmail rate-limit 403s ⇒ job stays DISPATCHED, the broker retry ladder redelivers
          (a Retry-After picks the first ladder tier ≥ Retry-After, capped at the last tier)
        400, 404 on send, auth failures, a null provider thread id ⇒ DISPATCHED → FAILED → DEAD_LETTER,
          provider error kept on the job; operator replay DEAD_LETTER → RETRY_PENDING → DISPATCHED
```

Approve is published only after its transaction commits. A repeated approve re-publishes the dispatch job while the job is not `COMPLETED` (dispatch is idempotent), so a lost publish cannot strand an approved draft; a repeated approve writes no second `feedback` row (`UNIQUE (draft_id)`). The lease reaper skips `DISPATCHED` jobs: the dispatch-worker does not take leases, and redelivery is the broker's.

A provider send cannot be rolled back, so it never runs inside the database transaction: the provider draft is the durable handle that makes a retry safe.

The outbound message is written back into `email_message` with `direction='outbound'` (R17.7) so the next inbound reply sees a complete conversation.

**Review UI (R23.4, R23.5, R23.7):** server-rendered pages (FastAPI + Jinja2 + htmx) in the `frontend` service, calling the `/v1` API only: the pending-draft queue (original email, thread summary, citations shown next to the sentences they support, business facts highlighted, edit / approve / reject), a job timeline per message from `processing_event`, and knowledge upload with per-document ingestion status. Local use only, with no login (ADR-0009): compose binds the `frontend` and `api` ports to `127.0.0.1`. The UI reads `FRONTEND__API_BASE_URL` and `FRONTEND__ORGANIZATION_ID` and sends the organization as the `X-Organization-Id` header the API already requires (R23.6). Meets the WCAG 2.2 AA basics: visible focus, 24 px targets, status changes announced through a live region.

---

## 6. Data model

PostgreSQL is authoritative. Blobs live in MinIO/S3 and are referenced by key.

### 6.1 Core schema (abridged DDL — migrations are the source of truth)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE organization (
  id UUID PRIMARY KEY, name TEXT NOT NULL,
  settings JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE mailbox (
  id UUID PRIMARY KEY,
  organization_id UUID NOT NULL REFERENCES organization(id),
  provider TEXT NOT NULL CHECK (provider IN ('gmail','graph','imap')),
  address TEXT NOT NULL, display_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',      -- active|paused|needs_reauth
  credentials_ref TEXT,                        -- pointer only, never a secret
  UNIQUE (organization_id, address));

CREATE TABLE mailbox_checkpoint (
  mailbox_id UUID PRIMARY KEY REFERENCES mailbox(id),
  history_id TEXT, delta_link TEXT,
  sync_state TEXT NOT NULL DEFAULT 'idle',     -- idle|syncing|full_resync|error
  last_sync_at TIMESTAMPTZ, last_full_sync_at TIMESTAMPTZ,
  pending_followup BOOLEAN NOT NULL DEFAULT false);

CREATE TABLE email_thread (
  id UUID PRIMARY KEY,
  organization_id UUID NOT NULL, mailbox_id UUID NOT NULL REFERENCES mailbox(id),
  provider_thread_id TEXT, subject_normalized TEXT,
  participants TEXT[] NOT NULL DEFAULT '{}',
  first_message_at TIMESTAMPTZ, last_message_at TIMESTAMPTZ,
  message_count INT NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
  UNIQUE (organization_id, mailbox_id, provider_thread_id));

CREATE TABLE email_message (
  id UUID PRIMARY KEY,
  organization_id UUID NOT NULL, mailbox_id UUID NOT NULL REFERENCES mailbox(id),
  thread_id UUID NOT NULL REFERENCES email_thread(id),
  provider_message_id TEXT NOT NULL, rfc822_message_id TEXT,
  in_reply_to TEXT, references_ids TEXT[],
  direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  sender_email TEXT, sender_name TEXT,
  recipients JSONB NOT NULL DEFAULT '[]', cc JSONB NOT NULL DEFAULT '[]',
  subject TEXT, subject_normalized TEXT,
  body_text TEXT, body_text_clean TEXT, snippet TEXT,
  raw_object_key TEXT, html_object_key TEXT,
  received_at TIMESTAMPTZ NOT NULL, has_attachments BOOLEAN NOT NULL DEFAULT false,
  normalization_failed BOOLEAN NOT NULL DEFAULT false,
  search_tsv TSVECTOR,
  UNIQUE (organization_id, mailbox_id, provider_message_id));      -- R4.8 / R5.4

CREATE INDEX ON email_message USING GIN (search_tsv);
CREATE INDEX ON email_message (organization_id, thread_id, received_at DESC);

CREATE TABLE attachment (
  id UUID PRIMARY KEY, message_id UUID NOT NULL REFERENCES email_message(id),
  filename TEXT, mime_type TEXT, size_bytes BIGINT,
  object_key TEXT NOT NULL, checksum TEXT);

CREATE TABLE classification_result (
  id UUID PRIMARY KEY, message_id UUID NOT NULL REFERENCES email_message(id),
  category TEXT NOT NULL, intent TEXT, priority TEXT NOT NULL,
  reply_required BOOLEAN NOT NULL, retrieval_required BOOLEAN NOT NULL,
  confidence NUMERIC(4,3) NOT NULL,
  decided_by TEXT NOT NULL CHECK (decided_by IN ('rule','ml','llm','default')),
  model_name TEXT, latency_ms INT, raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE processing_job (
  id UUID PRIMARY KEY, organization_id UUID NOT NULL,
  message_id UUID REFERENCES email_message(id), thread_id UUID,
  job_type TEXT NOT NULL, state TEXT NOT NULL,
  attempt INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 5,
  idempotency_key TEXT NOT NULL UNIQUE,                            -- R19.4
  result_ref JSONB,                          -- prior result for idempotent short-circuit (§9)
  queue_name TEXT, priority TEXT, lease_expires_at TIMESTAMPTZ,
  last_error TEXT, next_retry_at TIMESTAMPTZ, trace_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX ON processing_job (state, next_retry_at);

CREATE TABLE thread_state (
  thread_id UUID PRIMARY KEY REFERENCES email_thread(id),
  topic TEXT, current_intent TEXT, summary TEXT,
  open_questions JSONB NOT NULL DEFAULT '[]',
  resolved_items JSONB NOT NULL DEFAULT '[]',
  summarized_through_message_id UUID, token_estimate INT,
  version INT NOT NULL DEFAULT 1,                                  -- R8.6
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE generated_draft (
  id UUID PRIMARY KEY, job_id UUID REFERENCES processing_job(id),
  message_id UUID NOT NULL REFERENCES email_message(id), thread_id UUID NOT NULL,
  action TEXT NOT NULL, subject TEXT, body TEXT NOT NULL,
  confidence NUMERIC(4,3), citations JSONB NOT NULL DEFAULT '[]',
  citation_mismatch BOOLEAN NOT NULL DEFAULT false,                -- R16.5
  model_name TEXT, model_tier TEXT, escalation_reason TEXT,
  prompt_version TEXT, input_tokens INT, output_tokens INT,
  cost_estimate NUMERIC(10,6),
  status TEXT NOT NULL DEFAULT 'draft',        -- draft|approved|rejected|dispatched
  provider_ref TEXT,
  provider_draft_id TEXT, provider_draft_message_id TEXT,        -- ADR-0009 dispatch handle
  dispatch_idempotency_key TEXT UNIQUE,                          -- R19.2 key, operation "dispatch"
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE knowledge_document (
  id UUID PRIMARY KEY, organization_id UUID NOT NULL,
  title TEXT NOT NULL, source_uri TEXT, mime_type TEXT, category TEXT,
  version INT NOT NULL DEFAULT 1, checksum TEXT, object_key TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|parsing|chunking|embedding|active|superseded|failed
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE knowledge_chunk (
  id UUID PRIMARY KEY, document_id UUID NOT NULL REFERENCES knowledge_document(id),
  organization_id UUID NOT NULL, chunk_index INT NOT NULL,
  external_id TEXT,                              -- "DOC-125-08", used in citations
  heading_path TEXT[], section TEXT, category TEXT,
  content TEXT NOT NULL, token_count INT, content_checksum TEXT,
  metadata JSONB NOT NULL DEFAULT '{}', version INT NOT NULL,
  content_tsv TSVECTOR NOT NULL);
CREATE INDEX ON knowledge_chunk USING GIN (content_tsv);           -- R5.6
CREATE INDEX ON knowledge_chunk (organization_id, category);

CREATE TABLE embedding_record (
  chunk_id UUID PRIMARY KEY REFERENCES knowledge_chunk(id) ON DELETE CASCADE,
  organization_id UUID NOT NULL, model TEXT NOT NULL, dim INT NOT NULL,
  embedding VECTOR(1536) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX ON embedding_record USING hnsw (embedding vector_cosine_ops);  -- R5.7

CREATE TABLE feedback (
  id UUID PRIMARY KEY, draft_id UUID NOT NULL REFERENCES generated_draft(id),
  reviewer TEXT, decision TEXT NOT NULL,       -- accepted|edited|rejected
  edited_body TEXT, edit_distance INT, rating INT, comment TEXT,
  review_ms INT,                               -- time from opening the draft to deciding (ADR-0009)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (draft_id));                          -- one decision per draft

CREATE TABLE processing_event (
  id BIGSERIAL PRIMARY KEY, organization_id UUID NOT NULL,
  job_id UUID, message_id UUID, event_type TEXT NOT NULL,
  state_from TEXT, state_to TEXT, payload JSONB, trace_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX ON processing_event (message_id, created_at);
```

### 6.2 Business subsystem (R13.1)

```sql
CREATE TABLE customer  (id UUID PK, organization_id UUID, email TEXT, name TEXT,
                        account_status TEXT, tier TEXT);
CREATE TABLE product   (id UUID PK, organization_id UUID, sku TEXT, name TEXT,
                        price NUMERIC(12,2), status TEXT);
CREATE TABLE "order"   (id UUID PK, organization_id UUID, customer_id UUID,
                        order_number TEXT, status TEXT, total NUMERIC(12,2),
                        placed_at TIMESTAMPTZ, shipped_at TIMESTAMPTZ);
CREATE TABLE order_item(id UUID PK, order_id UUID, product_id UUID,
                        quantity INT, unit_price NUMERIC(12,2));
CREATE TABLE ticket    (id UUID PK, organization_id UUID, customer_id UUID,
                        ticket_number TEXT, status TEXT, priority TEXT,
                        subject TEXT, opened_at TIMESTAMPTZ);
```

This exists to demonstrate the architectural distinction: RAG answers *"how do we handle order-status questions?"*; the business subsystem answers *"where is order 82915?"*.

---

## 7. Messaging design

### 7.1 Topology (declared in code at startup, R3.2)

| Exchange | Type | Queue | Consumer |
|---|---|---|---|
| `mail.ingest` | direct | `mail.sync.requested` | mail-connector |
| `email.process` | direct | `email.normalize` | email-worker |
| `email.triage` | direct | `email.triage` | triage-worker |
| `email.route` | topic | `email.<category>.<priority>` | ai-worker |
| `email.dispatch` | direct | `email.dispatch` | dispatch-worker |
| `knowledge.ingest` | direct | `knowledge.ingest` | knowledge-worker |
| `retry.email` | direct | `email.retry.30s` / `.5m` / `.30m` | (TTL → retry.return → origin exchange) |
| `retry.return` | headers (alternate-exchange `dlx.email`) | — (exchange-to-exchange binding to each origin exchange, matched on header `retry-origin-exchange`) | returns expired retries to their origin |
| `dlx.email` | topic | `email.dead_letter` | operator inspection |

All durable; messages persistent; manual ack; per-consumer `prefetch` (R3.1–R3.4).

### 7.2 Retry ladder

Failed job → publish to `email.retry.<delay>` with original routing key in headers → queue TTL expires → dead-letters into `retry.return`, which routes it back to the origin exchange named in the `retry-origin-exchange` header (unmatched → `dlx.email`). Attempts: 30s, 5m, 30m (configurable). Exhausted → `dlx.email` with `x-failure-reason`, `x-original-routing-key`, `attempt` (R3.5, R19.5, R19.6).

### 7.3 Job envelope (every message)

```json
{
  "job_id": "uuid", "idempotency_key": "org:mbx:msgid:generate",
  "job_type": "generate_reply", "organization_id": "uuid",
  "message_id": "uuid", "thread_id": "uuid",
  "trace_id": "hex", "attempt": 0,
  "classification": { "...": "snapshot, so the worker never re-classifies (R7.3)" },
  "enqueued_at": "ISO-8601"
}
```

### 7.4 Micro-batching (R3.6, R3.7)

A worker may pull N jobs together to amortize DB round-trips and keep provider connections warm. Each job still gets its **own** LLM call. Never:

```
Prompt: Email A… Email B… Email C… "Respond to all three."
```

---

## 8. Processing state machine — *R18*

```
RECEIVED ──► NORMALIZED ──► CLASSIFIED ──┬── reply_required=false ──► COMPLETED
                                         │
                                         └──► QUEUED ──► CONTEXT_READY ──► GENERATING
                                                                              │
                                    ┌─────────────────────────────────────────┤
                                    ▼                                         ▼
                              RETRY_PENDING ◄──── transient failure       DRAFTED
                                    │                                         │
                          attempts exhausted                                  ▼
                                    ▼                                    DISPATCHED
                                 FAILED ──► DEAD_LETTER ──(operator replay)──►│
                                                                              ▼
                                                                         COMPLETED
```

**Rules:**
- Only declared transitions are permitted; an illegal transition raises, it does not overwrite (R18.3).
- Every transition writes a `processing_event` **in the same transaction as its side effect** (R18.4, R18.5).
- The state machine lives in `packages/domain/state_machine.py` as a pure, unit-testable transition table. No service hand-writes state strings.

```python
TRANSITIONS: dict[JobState, set[JobState]] = {
    RECEIVED: {NORMALIZED, FAILED},
    NORMALIZED: {CLASSIFIED, FAILED},
    CLASSIFIED: {QUEUED, COMPLETED, FAILED},
    QUEUED: {CONTEXT_READY, FAILED},
    CONTEXT_READY: {GENERATING, FAILED},
    GENERATING: {DRAFTED, RETRY_PENDING, FAILED},
    RETRY_PENDING: {GENERATING, DISPATCHED, FAILED},  # DISPATCHED: replay of a dead-lettered dispatch (ADR-0009)
    DRAFTED: {DISPATCHED, COMPLETED, FAILED},
    DISPATCHED: {COMPLETED, FAILED},
    FAILED: {DEAD_LETTER, RETRY_PENDING},
    DEAD_LETTER: {RETRY_PENDING},  # operator replay (R18.7)
    COMPLETED: set(),
}
```

---

## 9. Idempotency & recovery — *R19*

**Key:** `sha256(f"{organization_id}:{mailbox_id}:{provider_message_id}:{operation_type}")`.

**Enforcement is two-layered:**
1. Application check before the side effect (fast path, R19.3).
2. `UNIQUE` constraint on `processing_job.idempotency_key` (truth, R19.4).

```python
async def execute_once(key: str, op: Callable[[], Awaitable[T]]) -> T:
    if prior := await repo.find_completed(key):
        return prior.result  # short-circuit, ack, done
    try:
        async with uow:  # side effect + state in one txn
            result = await op()
            await repo.mark_completed(key, result)
        return result
    except IntegrityError:  # lost a concurrent race
        return (await repo.find_completed(key)).result
```

**Non-transactional side effects (dispatch, ADR-0009):** `execute_once` above fits side effects that live in our own database. A provider send cannot join the transaction, so dispatch claims the key first, creates a provider draft as a durable handle, and on retry asks the provider whether that draft was already sent before sending (§5.8). Exactly one provider send under redelivery is asserted by a test that crashes the worker after each step.

**Stuck jobs:** a lease (`lease_expires_at`) is taken on claim; a reaper transitions expired leases back to a retryable state (R19.8).

**Worker kill test (R19.7, R22.8, SC6/SC7):** kill `ai-worker` mid-generation → message is unacked → redelivered → idempotency check finds no completed draft → regenerates → exactly one draft exists. Assert `COUNT(generated_draft) == 1`.

---

## 10. Observability — *R21*

**Trace:** `trace_id` minted at webhook receipt, carried in the job envelope, restored into the OTel context on every consume, so one email is one trace across all queue hops (R21.1, R21.2).

**Span tree per email:**

```
email.lifecycle (root)
├── connector.sync
├── broker.publish → broker.consume
├── processor.normalize
├── triage.classify   [attr: decided_by, confidence, reply_required]
├── context.build
│   ├── thread.load / thread.summarize
│   ├── business.fetch
│   └── rag.retrieve
│       ├── rag.lexical
│       ├── rag.vector
│       ├── rag.rrf
│       └── rag.rerank
├── llm.generate      [attr: model, tier, input_tokens, output_tokens]
├── draft.persist
└── dispatch.send
```

**Metrics (R21.4):** counters `emails_received_total`, `emails_classified_total`, `emails_templated_total`, `emails_generated_total`, `failed_jobs_total`, `retry_jobs_total`, `input_tokens_total`, `output_tokens_total`, `embedding_tokens_total`, `estimated_ai_cost_total`, `generated_draft_cost_total`, `llm_calls_total{kind}`, `retrieval_underfilled_total`; histograms `classification_latency_ms`, `retrieval_latency_ms`, `rerank_latency_ms`, `generation_latency_ms`, `end_to_end_latency_ms`, `queue_wait_ms`, `llm_calls_per_job`, `llm_context_tokens`; gauges `queue_depth`, `retrieval_hit_rate`, `retrieval_top_k`.

Cost is a **monotonic counter**, not a gauge — a gauge cannot be summed over a window, which is exactly what SC9 (cost per generated email) requires. Same for `llm_calls_total`; the per-job histogram is what proves the R14.9 budget holds.

Label sets stay low-cardinality: `{organization, category, priority, model_tier, decided_by}`. Never label with `message_id`.

`llm_context_tokens{kind, tier}` is observed on every inference request, failed ones included, from the messages actually sent (R11.7); the per-request `llm_inference` log line carries the same size with the job's correlation ids so it can be joined to draft quality.

**Cost accounting (R21.6):** a config price table `{model: {input_per_m, output_per_m}}` converts token counts to `cost_estimate` per draft, aggregated per email / category / day. This is what makes SC9 answerable.

A model missing from the price table yields `cost_estimate = NULL`, never `0`: an unknown cost is not a free call, and SQL aggregates then exclude it visibly instead of under-counting. The draft row and the job's `GENERATING → DRAFTED` transition commit in one transaction (`packages/db/draft_persistence.py`), and a partial unique index on `generated_draft(job_id)` keeps one draft per job across redeliveries (§9).

**Dashboards (R21.7, R21.8):**
1. **Funnel** — received → no-reply → simple → AI → RAG, as absolute counts and percentages against the 45/20/35 design assumption.
2. **Latency** — stacked stage breakdown with p50/p95/p99 against NFR1–NFR11.
3. **Queue health** — depth and wait per queue, retry and DLQ rates.
4. **Retrieval** — hit rate, top-K distribution, degradation rate, rerank on/off.
5. **Cost** — cost/email, cost/category, tier mix, daily spend.

---

## 11. Evaluation harness design — *R22*

Each experiment is a runner under `evaluation/experiments/` that loads a dataset, sweeps one configuration axis, and writes a versioned artifact.

```
evaluation/
├── datasets/
│   ├── classification/{train,test}.jsonl        # email → gold category/intent
│   └── retrieval/queries.jsonl                  # query → gold chunk ids
├── experiments/
│   ├── exp01_triage_cascade.py       # rules vs +ML vs full cascade      (H2)
│   ├── exp02_retrieval_modes.py      # vector | fts | hybrid | +rerank   (H1)
│   ├── exp03_context_size.py         # Top-3 / 5 / 10
│   ├── exp04_thread_context.py       # full vs summarized                (H3)
│   ├── exp05_model_cascade.py        # single model vs cascade           (H4)
│   ├── exp06_load.py                 # 1× / 5× / 10× / 20×               (H5)
│   ├── exp07_failure_recovery.py     # kill worker mid-job
│   ├── exp08_corpus_growth.py        # PostgreSQL retrieval vs corpus size
│   └── exp09_rag_vs_norag.py         # response quality baseline
├── mailguard_bench/                  # AgentMailGuard prompt-injection benchmark, C0 vs C3 (7.19, ADR-0010)
└── results/<experiment>/<run_id>/{manifest.json,metrics.csv,report.md}
```

**Run manifest (R22.12):** git SHA, config hash, dataset version, model names, timestamp, hardware note. An experiment without a manifest is not a result.

**Metrics:** classification → accuracy, precision, recall, macro-F1, confusion matrix (R22.1). Retrieval → Recall@K, Precision@K, MRR, nDCG@K (R22.2). Response → factual correctness, relevance, completeness, evidence consistency, clarity, thread awareness, redundancy; plus acceptance rate, edit rate, edit distance, reviewer rating from the `feedback` table. **Draft acceptance rate is the headline practical metric** (SC3).

**Load harness:** a generator replays fixture emails through the `FakeProviderAdapter` at a multiple of the 1.16 msg/s reference average, up to 20× ≈ 23 msg/s (NFR12), while Prometheus records throughput, queue depth, and latency percentiles.

**AgentMailGuard prompt-injection benchmark (task 7.19, ADR-0010):** `evaluation/mailguard_bench/` measures AgentMailGuard, the separate prompt-injection subsystem that §0.5 of the requirements and CLAUDE.md §6 keep out of rag-email, running inside rag-email's real reply path: `ContextBuilder`, then one `reply.v1` generation call through `SinglePassGenerator`. The same sampled cases (300 LLMail-Inject phase-2 attacks from the half not used to train the guard's classifier, stratified by scenario, seed 20260930; 150 benign emails; about 100 RAG-vector attacks ingested into an isolated evaluation organization) run under three required configs: `C0` is rag-email exactly as it runs (`SinglePassGenerator.generate_draft` on the `ContextPackage`, rendered by rag-email's own profile template; no AgentMailGuard code runs); `C0T` is AgentMailGuard's `MailGuardPipeline.run` with preset `C0` (its prompt template with no layer active; its task line still tells the model to use only trusted sections for instructions); `C3` is `MailGuardPipeline.run` with every layer on. In `C0T` and `C3` the generation call is `SinglePassGenerator.generate_from_messages`, one additive public method, on the prompt AgentMailGuard's L3 layer built from rag-email's `ContextPackage` (`GuardedReplyAgent` is not used because it calls the model with its own schema); `generate_draft` delegates to it, so the live path is unchanged. `C1`/`C2` are an optional ablation on a fixed 100-attack subset. rag-email adds no defence logic. AgentMailGuard is a git worktree of its branch installed editable for the run (`uv run --with-editable`); the branches are not merged. Scoring uses AgentMailGuard's own rule and metrics (`goal_achieved`, Wilson 95 % intervals, exact McNemar). The headline is "C3 ASR ≤ 5 %: met / not met" on LLMail-Inject, always with FPR, benign utility and the C0 ASR; McNemar compares C0 vs C3 (the headline pair: rag-email without vs with the guard) and C0T vs C3 (the guard's layers alone, same template). The report adds latency p50/p95/p99 (guard vs generation), tokens, calls and cost per email, a TF-IDF leakage check, the first catching layer, worked examples and a threat-model section. Artifacts: `evaluation/results/mailguard_bench/<run_id>/{manifest.json,metrics.csv,report.md,analyses.md}`; the manifest records both branches' commit SHAs. Live runs are owner-run Make targets and never part of `make ci` (R24.5). Spec: `docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md`.

---

## 12. Technology decisions

### 12.1 Chosen stack

| Layer | FYP implementation | Enterprise evolution |
|---|---|---|
| API/backend | Python + FastAPI | same, horizontally replicated |
| Email | Gmail API + Microsoft Graph | same |
| Database | PostgreSQL + pgvector | managed / clustered PostgreSQL |
| Vector search | pgvector HNSW | pgvector or dedicated search |
| Lexical search | PostgreSQL FTS | PostgreSQL FTS / OpenSearch |
| Queue | RabbitMQ | RabbitMQ cluster, quorum queues |
| Object storage | MinIO | S3-compatible |
| Cache | Redis *where justified* | Redis cluster |
| LLM | provider abstraction | multi-provider routing |
| Embedding | embedding API or local model | hosted inference |
| Containers | Docker Compose | Kubernetes / ECS |
| Metrics / dashboards / tracing | Prometheus / Grafana / OpenTelemetry | same |

### 12.2 Decisions worth recording as ADRs (R24.6)

| ADR | Decision | Why |
|---|---|---|
| 0001 | PostgreSQL-first retrieval; **no OpenSearch initially** | fewer components, fewer hops, transactional metadata, easier local test; migrate only when measurement (exp08) says so |
| 0002 | RabbitMQ over Kafka | work-queue semantics, per-message ack, native DLQ and retry, lower operational weight for this workload |
| 0003 | At-least-once + idempotent consumers | exactly-once across a broker and a database is not achievable cheaply; idempotency is |
| 0004 | Agent profiles, not multi-agent chains | latency and cost multiply with each hop; specialization is a config problem |
| 0005 | RRF over score normalization | BM25 and cosine scores aren't comparable; rank fusion needs no calibration |
| 0006 | Thread state as compressed summary | prevents long threads from re-consuming full history every message |
| 0007 | Human-in-the-loop default | drafts are reviewable; auto-send is opt-in per category |
| 0008 | Business data fetched by a code-side plan, not by intent alone or model tool calls | the ML stage emits no intent; tool calls need a second generation call; typed IDs and profile policy are deterministic and model-independent |
| 0009 | Dispatch: claim → provider draft → send → confirm; local review UI without login | a provider send cannot be rolled back; the draft id lets a retry check instead of resend; auth is out of scope (CLAUDE.md §6) |
| 0010 | AgentMailGuard is integrated as the separate prompt-injection subsystem, first for evaluation; rag-email adds no defence logic | CLAUDE.md §6 keeps defences out of rag-email; the guard is the thesis contribution and needs a real host; consumed as an editable worktree install pinned by commit, no branch merge |

**Migration seam** (when exp08 shows PostgreSQL retrieval is insufficient): implement `OpenSearchBackend` behind `SearchBackend`, dual-write the chunk index, run exp02 against both, switch by config. The email pipeline does not change.

---

## 13. Deployment

### 13.1 FYP — Docker Compose (R20.1)

```
docker-compose.yml
├── frontend          ├── postgres (pgvector)
├── api               ├── rabbitmq (management UI)
├── mail-connector    ├── minio
├── email-worker      ├── prometheus
├── triage-worker     └── grafana (provisioned dashboards)
├── ai-worker
├── knowledge-worker
└── dispatch-worker
```

External: Gmail / Microsoft Graph, embedding provider, LLM provider. All three must be stubbable — CI runs with `FakeProviderAdapter` and stub LLM/embedding (R24.5).

Every service: `/healthz`, `/readyz`, graceful shutdown (stop consuming → drain in-flight → exit) (R20.7, R20.8).

### 13.2 Scaled profile (R20.3, R20.9)

```
Load Balancer → API replicas
                     │
              RabbitMQ cluster (quorum queues)
                     │
   ┌─────────────────┼─────────────────┐
mail workers    triage workers     AI workers
   └─────────────────┼─────────────────┘
              Managed PostgreSQL
                     │
               Object storage
```

Each worker class scales on its own signal: mail connector on mailbox/event count, email processor on message rate, triage on classification queue depth, AI worker on generation queue depth plus external inference concurrency, knowledge worker on ingestion volume. The expensive layer scales independently of the cheap ones — this is the property that keeps 10,000 mailboxes from implying 10,000 concurrent agents.

### 13.3 Configuration (R20.6)

One validated settings object per service, loaded from environment, failing fast on missing/invalid values. Key groups: database, broker, object storage, provider credentials refs, embedding model + dimension, LLM tiers + price table, retrieval (`TOP_N`, `RRF_K`, `TOP_K`, `RERANK_ENABLED`, timeouts), triage thresholds, thread summarization thresholds, retry ladder, worker prefetch/concurrency, business-data timeout.

**Hosted OpenAI-compatible endpoints.** The `openai` provider talks to any OpenAI-compatible `/chat/completions` endpoint through `LLM__OPENAI_BASE_URL`, with tier models from `LLM__FAST_MODEL` / `LLM__STRONG_MODEL` / `LLM__FALLBACK_MODEL`. The project's live runs use the Google Gemini API this way (base URL `https://generativelanguage.googleapis.com/v1beta/openai`; routine `gemma-4-26b-a4b-it`, high-capability `gemma-4-31b-it`, fallback `gemini-3.1-flash-lite`). After task 5.0, compose forwards the base URL, the three model names and `LLM__PRICE_TABLE` into every service that merges the shared `x-app-env` block (init, api, mail-connector, email-worker, triage-worker, knowledge-worker, ai-worker). An unset base URL still falls back to `https://api.openai.com/v1`, so settings validation fails fast when `provider=openai` names a Gemini or Gemma model while the base URL is still the OpenAI default — the key is never sent to the wrong host.

**Startup assertion (R5.10):** configured embedding dimension must equal the `VECTOR(n)` column width, or the service refuses to start.

---

## 14. Testing strategy — *R24*

| Level | Scope | Notes |
|---|---|---|
| Unit | chunker, RRF, rules engine, state machine, query builder, idempotency, subject normalizer, quoted-history splitter, cost calculator | pure functions, no I/O, fast |
| Integration | repositories, hybrid SQL, broker publish/consume, retry ladder, adapters against recorded fixtures | ephemeral PostgreSQL + RabbitMQ containers |
| Contract | `MailProviderAdapter`, `LLMProvider`, `SearchBackend`, `BusinessDataProvider` | one shared suite every implementation must pass, including fakes |
| E2E | fixture email → ingest → triage → RAG → draft | one smoke test in CI (R24.7) |
| Resilience | worker kill, broker restart, provider 429/5xx, LLM timeout, malformed LLM output | assert no duplicates, no lost jobs |
| Evaluation | the R22 experiments | not CI-gated; produces artifacts |

**Golden rule for CI:** no live credentials, no network to providers. Everything external is faked (R24.5).

---

## 15. Traceability — design section → requirements

| Design § | Requirements covered |
|---|---|
| 5.1 Mail Connector | R1.1–R1.7, R2.1–R2.11 |
| 5.2 Email Processing | R4.1–R4.10 |
| 5.3 Triage | R6.1–R6.11, R7.1–R7.6 |
| 5.4 Context Builder | R8.1–R8.8, R12.1–R12.6, R13.1–R13.7, R14.8 |
| 5.5 Hybrid RAG | R10.1–R10.9, R11.1–R11.7 |
| 5.6 Knowledge Ingestion | R9.1–R9.11 |
| 5.7 Reply Agent & Cascade | R14.1–R14.7, R15.1–R15.6, R16.1–R16.5 |
| 5.8 Draft & Dispatch | R16.6–R16.8, R17.1–R17.7 |
| 6 Data model | R5.1–R5.10, R13.1 |
| 7 Messaging | R3.1–R3.9, R7.1–R7.6 |
| 8 State machine | R18.1–R18.7 |
| 9 Idempotency | R19.1–R19.9 |
| 10 Observability | R21.1–R21.8 |
| 11 Evaluation | R22.1–R22.12 |
| 12 Technology | R24.6, ADR record |
| 13 Deployment | R20.1–R20.9, R23.x (API surface) |
| 14 Testing | R24.1–R24.7 |

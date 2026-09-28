# Phase 6 — Mail Dispatch & Review Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reviewer approves an AI-drafted reply in a local review UI, and the dispatch-worker turns it into exactly one provider draft (default `create_draft` mode) or exactly one threaded sent reply (`send_reply` mode), even when messages are redelivered or the worker crashes.

**Architecture:** Approve commits the decision and a `feedback` row, then publishes a dispatch job to `email.dispatch`. The dispatch-worker runs design §5.8's five steps on the existing `processing_job`: claim the R19.2 key, create or reuse the provider draft, send, confirm after an ambiguous failure, and finish in one transaction. The adapters gain `send_draft`, `get_draft_status` and `find_sent_message`. The review UI is a separate server-rendered service. It talks only to `/v1` over HTTP and is published on loopback only.

```
 browser ─▶ frontend :3001 (127.0.0.1)  FastAPI + Jinja2 + htmx      no DB, no broker
              │  httpx, header X-Organization-Id = FRONTEND__ORGANIZATION_ID
              ▼
            api :8000 (127.0.0.1)  /v1/drafts  GET · GET {id} · PATCH · POST approve · POST reject
              │ approve: txn(lock job → lock draft → status=approved + feedback row) ─ COMMIT
              │          then publish JobEnvelope(job_type="dispatch")  ─ draft_decisions_total{decision,category}
              ▼
            email.dispatch ─▶ dispatch-worker (DispatchConsumer(BaseConsumer) → DispatchService.dispatch)
              1 claim   txn: generated_draft.dispatch_idempotency_key = key(org, mailbox, orig provider_message_id, "dispatch")
                             + job DRAFTED|RETRY_PENDING → DISPATCHED      (COMPLETED ⇒ ALREADY_DONE, nothing sent)
              2 draft   adapter.create_draft(reply) → provider_draft_id + provider_draft_message_id  (stored ⇒ reuse)
                        └─ create_draft mode: finish, no outbound email_message ─────────────────┐
              3 send    adapter.send_draft(provider_draft_id)                                    │
              4 confirm (resumed) get_draft_status: DRAFT ⇒ send · SENT ⇒ 5 ·                     │
                        MISSING ⇒ find_sent_message(draft msg id, then our Message-ID) else DLQ   │
              5 finish  txn: provider ref, draft dispatched, outbound email_message, thread, DISPATCHED → COMPLETED
              │
              ▼
            MailProviderAdapter ─▶ GmailProviderAdapter ─▶ Gmail REST (drafts.create / drafts.send / drafts.get / threads.get)
                                └▶ GraphProviderAdapter ─▶ createReply + send, Prefer: IdType="ImmutableId"
            errors: RetryableProviderError(retry_after_s) ⇒ stays DISPATCHED, ladder tier ≥ Retry-After
                    PermanentProviderError / MissingProviderThreadError ⇒ DISPATCHED → FAILED → DEAD_LETTER
```

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2 / pydantic-settings, asyncpg + PostgreSQL (migration 0005), aio-pika + RabbitMQ, httpx (+ `MockTransport` recorded responses), Jinja2 + htmx 2.0.11 (vendored), Playwright for Python 1.63.0, pytest / pytest-asyncio, ruff, mypy, uv, Docker Compose.

**Spec:**
- `specs/tasks.md` Phase 6 (6.1–6.10 and the Phase 6 gate)
- `specs/design.md` §5.8 (Draft Store & Dispatcher), §6 (data model), §8 (processing state machine), §9 (idempotency & recovery)
- `docs/adr/0009-dispatch-exactly-once-and-local-review-ui.md`
- `specs/requirements.md`: R1.1, R16.6–R16.8, R17.1–R17.7, R18.3, R18.7, R19.2, R19.3, R21.4, R23.4–R23.7, R24.5, R24.7
- Research: `artifacts/superpowers/2026-09-28-phase6-dispatch-review-research.md`
- Demo runbook: `docs/demo-runbook.md` (§3, §3.4, §5.3, §6)
- Constitution: `GEMINI.md` (§2 working loop, §3 Definition of Done, §4 rules, §6 hard stop, §7 divergence, §8 testing)

---

## Global Constraints

- Branch: work on `RAG_Email_System`. Never merge it into `main` and never open a PR into `main`. Push only when the owner asks.
- Commit format: `<type>(<area>): <what> [task 6.x] [<req IDs>]`, then a blank line, then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Queue: `email.dispatch`. The dispatch-worker is `services/dispatch_worker/main.py`, `SERVICE_NAME=dispatch_worker`, port `8006`.
- Dispatch key: `derive_idempotency_key(organization_id, mailbox_id, provider_message_id of the original email, "dispatch")` (`packages/core/idempotency.py`), stored in `generated_draft.dispatch_idempotency_key` (UNIQUE).
- Migration: `migrations/0005_dispatch_and_review.{up,down}.sql` adds `generated_draft.provider_draft_id TEXT`, `generated_draft.provider_draft_message_id TEXT`, `generated_draft.dispatch_idempotency_key TEXT UNIQUE`, `feedback.review_ms INT`, and `UNIQUE (draft_id)` on `feedback`.
- State machine: `RETRY_PENDING → {GENERATING, DISPATCHED, FAILED}`. The lease reaper skips `DISPATCHED`.
- `dispatch_mode: create_draft | send_reply` is set per category in `config/categories.yaml`. The default is `create_draft` for every category. `auto_send_eligible: false` for every category.
- Retry ladder: `30 s / 5 m / 30 m`, `max_retries=3`. A `Retry-After` picks the first ladder tier ≥ its value, capped at the last tier.
- Transient (retry, job stays `DISPATCHED`): 429, 5xx, Gmail 403 `rateLimitExceeded` / `userRateLimitExceeded`. Permanent (`DISPATCHED → FAILED → DEAD_LETTER`, error kept on the job): 400, 404 on send, auth failures, null provider thread id.
- Graph always uses `createReply` + `send` with `Prefer: IdType="ImmutableId"`, and never `sendMail`.
- Outbound reply: exactly one `Re: `. `In-Reply-To` is the original `Message-ID`. `References` is the original References plus the original `Message-ID`. The provider thread id is used, never our UUID. The quoted original goes below the reply.
- Provider names (`gmail`, `graph`, `imap`) appear only inside `packages/adapters/`. `packages/*` never imports `services/*`. `packages/domain` imports only the stdlib and `packages/core`.
- Every tenant-scoped query carries `organization_id`. State strings come from `JobState`, never written by hand.
- Tests never use live credentials or network (R24.5). `GMAIL_ACCESS_TOKEN` and `GRAPH_ACCESS_TOKEN` are stripped by `tests/conftest.py`. Integration tests run against the `rag_email_test` database and scratch RabbitMQ vhosts.
- Review UI: FastAPI + Jinja2 + htmx `2.0.11` (vendored, no CDN). It calls only `/v1`. Settings are `FRONTEND__API_BASE_URL` and `FRONTEND__ORGANIZATION_ID`, sent as the `X-Organization-Id` header. There is no login (ADR-0009). Compose publishes `frontend` (3001) and `api` (8000) on `127.0.0.1` only.
- Accessibility: WCAG 2.2 AA basics. Focus is visible, targets are ≥ 24 px, and status messages use live regions.
- New config keys go into `.env.example` **and** `docs/configuration.md` (GEMINI.md §3.4). `FRONTEND__*` is section 21 / §2.21. `GMAIL_ACCESS_TOKEN` is section 22 and the §2.4 table.
- Verification per task: `make fmt-check`, `make lint` (`ruff check .` + `mypy packages services tests evaluation`), and the task's pytest commands. Task 13 runs the full `make ci` (`fmt-check lint test-unit test-integration`, plus `test-e2e` once Task 10 adds it).

## Review Focus

These are the five inputs most likely to bite a person using the software. Each is pinned by a test in the task that owns the code.

1. **Replying to an email whose original has no `Message-ID`.** The reply still builds, with no `In-Reply-To`. `References` keeps whatever the original had, and the reply still threads by provider thread id and subject. Test: Task 2 `test_original_without_message_id`.
2. **Approve clicked twice fast.** Both calls succeed and exactly one of them is the decision (`created: true`). One `feedback` row exists, and every published dispatch envelope names the same job. Test: Task 6 Step 6 `test_two_fast_approves_write_one_feedback_row` (integration, concurrent requests on Postgres).
3. **Token expires mid-dispatch (401 between the provider draft and the send).** The job dead-letters with the auth reason and nothing is sent. After the owner refreshes the token, the operator replay sends exactly once through the recorded provider draft and never creates a second draft. Tests: Task 7 `test_token_expiry_mid_dispatch_is_permanent_and_replay_reuses_the_draft`, and Task 8 `test_failure_policy` (`AuthExpired` → `DEAD_LETTER`).
4. **A person deletes the Gmail draft before the send.** The resumed dispatch finds no draft and no sent copy. It dead-letters for an operator and never re-creates or sends a draft. Test: Task 7 `test_provider_draft_deleted_before_the_send_is_not_recreated`.
5. **Category switched from `create_draft` to `send_reply` between approve and dispatch.** The dispatch uses the mode in force when it runs, and sends once. A job that already completed in `create_draft` mode is not sent when redelivered after the switch. Tests: Task 7 `test_dispatch_mode_is_read_when_the_dispatch_runs` and `test_mode_switch_after_a_completed_draft_does_not_send`.

## Contract deviations and cross-task decisions (binding for executors)

The shared contract holds except where noted below. Each item was settled while the parts were assembled, and later tasks rely on it.

- **`OutboundReply.thread_id: str`** is the thread field. The existing name is kept and retyped from `UUID | str` to `str`, and it carries the **provider** thread id. `OutboundReply` also gains `message_id: str | None` and `reply_to_provider_message_id: str | None`. Graph `createReply` is addressed to the original's provider id, so it needs the second field. Task 1 adds both fields, and Task 2 sets them.
- **`build_outbound_reply(*, draft, original, provider_thread_id: str | None, message_id_domain: str)`.** `provider_thread_id` is typed `str | None` so that it can raise `MissingProviderThreadError`. The `Message-ID` is deterministic, `<dispatch-{draft.id.hex}@{message_id_domain}>`. `message_id_domain` is the domain of `mailbox.address`, computed by `packages.dispatch.service.message_id_domain`. Extra errors: `MissingRecipientError`. The dispatch-worker treats it as permanent.
- **Adapter errors** live in `packages/adapters/exceptions.py`:
  - retryable base `RetryableProviderError(retry_after_s: float | None)`, with subclasses `RateLimited` (which also keeps `retry_after` with the same value) and `Transient`;
  - permanent base `PermanentProviderError`, with subclasses `AuthExpired`, `NotFound` and `Permanent`.

  Dispatch reads `retry_after_s` from the retryable base.
- **`DraftRef(provider_draft_id, provider_message_id, provider_thread_id)`.** `create_draft` returns this ref, with both ids set.
- **`find_sent_message` lookup order.** The first lookup uses the stored `provider_draft_message_id`, because Graph's immutable id survives the send. The second uses our `Message-ID`, because Gmail's `drafts.send` gives the sent copy a new id. Both adapters match the key against either the provider id or the RFC 5322 `Message-ID`.
- **`DispatchOutcome` has a fourth member, `NOT_DISPATCHABLE`.** The dispatch job is acknowledged without a state change when the draft is not approved or the job is in a state the claim must not touch, for example still `GENERATING`.
- **`BaseConsumer` gains two overridable hooks.**
  - `prepare_delivery(envelope)` holds today's `RETRY_PENDING → GENERATING` recovery plus the lease, unchanged by default.
  - `retry_after_s(exc)` defaults to `None`.

  `DispatchConsumer` overrides both, because dispatch claims its own job and takes no lease. `handle_job_transient_failure` gains `retry_after_s: float | None = None`.
- **The claim transaction also writes `processing_job.queue_name = 'email.dispatch'`.** Without it, `POST /v1/jobs/{id}/replay` cannot route a dead-lettered dispatch. A failure before or during the claim (a key conflict rolls the claim back; `load()` finding nothing raises before it) would leave the generation queue there, so `DispatchConsumer` also calls `DispatchService.mark_dispatch_route` (`DispatchStore.set_dispatch_queue`, org-scoped) on **every** dispatch failure before `BaseConsumer` retries or dead-letters it (Task 7/8).
- **Dispatch is serialized per job.** A repeated approve re-publishes while the job is `DRAFTED`/`DISPATCHED`/`RETRY_PENDING`, and the consumer runs up to `dispatch_worker_concurrency` (default 5) deliveries at once, so two deliveries of one job can arrive together. `DispatchService.dispatch()` holds `DispatchStore.job_lock(org, job)` for all five steps: Postgres takes a session-level `pg_try_advisory_lock(hashtextextended('dispatch:<org>:<job>', 0))` on a dedicated pool connection (a crashed worker's session ends and releases it). A busy lock raises `DispatchJobBusyError`; `DispatchConsumer` re-queues that delivery on the first retry tier **without** using an attempt or touching the job (Task 7/8).
- **Orphan provider draft lookup (tasks.md 6.5 "exactly one provider draft").** The adapters gain `find_draft(mailbox, provider_thread_id, message_id) -> DraftRef | None`. When the job was not freshly `DRAFTED` (a redelivery or an operator replay) and no provider draft handle is stored, dispatch first looks up an unsent draft carrying our deterministic `Message-ID` and records it instead of creating a second one. Gmail: `drafts.list?q=rfc822msgid:<id>`. Graph: `createReply` sets `internetMessageId` to our `Message-ID` (updatable while `isDraft`), and the lookup matches it on `isDraft` items of the conversation (open question D3). A send on a handle this delivery did not create always runs step 4 (confirm) first (Task 5, Task 7).
- **The API loads `config/categories.yaml` into its own registry** (`app.state.taxonomy`, built in `create_app`), so `GET /v1/drafts/{id}`'s `dispatch_mode` is the mode the dispatch-worker will use. The process-default registry is left untouched (Task 6).
- **`GET /v1/drafts` uses keyset cursor pagination.** It returns `{items, next_cursor, limit}`, a new shape, and does not use `PaginatedResponse`. The `GET /v1/drafts/{id}` fields are `original`, `cited_chunks`, `business_data.facts` and `dispatch_mode`. The UI's `api_client.py` reads exactly these names.
- **The body as generated is kept in the first `draft_edited` `processing_event` payload (`original_body`).** `edit_distance` is measured from it. Migration 0005 stays exactly as the contract states.
- **`services.dispatch_worker.main.build_consumer(res, *, service=None, adapter_resolver=None)`.** Task 11 injects the fake adapter through `adapter_resolver`.
- **The review UI lives in `services/frontend/`,** not in the root `frontend/` of design §4. The Docker image only packages `services/`. Task 9 updates the design tree, which is open question D1.
- **`GMAIL_ACCESS_TOKEN` is forwarded only to `dispatch-worker` (Task 8) and `mail-connector` (Task 12),** never through `x-app-env`.

Part labels inside tasks refer to the authoring split: Part A = Tasks 1–3, Part B = Tasks 4–5, Part C = Tasks 6–8, Part D = Tasks 9–10, Part E = Tasks 11–13.

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `migrations/0005_dispatch_and_review.up.sql` / `.down.sql` | Dispatch handle columns and UNIQUE dispatch key on `generated_draft`; `feedback.review_ms`, `UNIQUE (draft_id)` | 1 |
| `packages/domain/dispatch.py` | `DispatchMode`, `ProviderDraftStatus`, `DEFAULT_DISPATCH_MODE`, `parse_dispatch_mode` | 1, 3 |
| `packages/domain/state_machine.py` | `RETRY_PENDING → DISPATCHED` edge | 1 |
| `packages/domain/entities.py` | `OutboundReply.thread_id: str`, `.message_id`, `.reply_to_provider_message_id`; `GeneratedDraft` dispatch handle fields | 1 |
| `packages/db/job.py` | Reaper skips `DISPATCHED`; `JOB_SELECT_COLUMNS` | 1, 6 |
| `packages/db/draft.py` | Maps the three new draft columns | 1 |
| `packages/dispatch/reply.py` | Pure `build_outbound_reply`, `MissingProviderThreadError`, `MissingRecipientError` | 2 |
| `packages/domain/taxonomy.py`, `config/categories.yaml` | `dispatch_mode` per category, `get_dispatch_mode` | 3 |
| `packages/adapters/exceptions.py` | Retryable / permanent bases, `parse_retry_after` | 4 |
| `packages/adapters/gmail.py` | `Message-ID`, `classify_gmail_error`, `send_draft`, `get_draft_status`, `find_sent_message`; INBOX-only sync (E1) | 4, 12 |
| `packages/adapters/graph.py` | `createReply` + `send` with immutable ids, status, sent lookup | 5 |
| `packages/adapters/protocol.py`, `fake.py`, `testing.py` | Protocol methods, fake draft lifecycle and fault injection, contract suite | 5 |
| `packages/domain/review.py` | `DraftStatus`, `FeedbackDecision`, `edit_distance`, verdicts | 6 |
| `packages/core/pagination.py` | Keyset cursor helpers | 6 |
| `packages/db/review.py` | `ReviewStore` (Postgres + in-memory): list, detail, edit, approve, reject | 6 |
| `services/api/schemas/drafts.py`, `services/api/routers/drafts.py` | `/v1/drafts` API, approve-then-publish, `draft_decisions_total` | 6 |
| `packages/observability/metrics.py`, `docs/observability.md` | `draft_decisions_total{decision, category}`, SC3 PromQL | 6 |
| `packages/db/message.py`, `packages/db/thread.py` | Connection-scoped insert / thread touch for the finish transaction | 7 |
| `packages/db/dispatch.py` | `DispatchStore`: load, claim, record draft, finish (Postgres + in-memory) | 7 |
| `packages/dispatch/service.py` | `DispatchService` five steps, `DispatchOutcome`, helpers | 7 |
| `tests/stubs/dispatch_fakes.py` | `RecordingFake`, `DispatchWorld`, `build_dispatch_world` | 7 |
| `packages/broker/backoff.py`, `retry.py`, `consumer.py` | Retry-After tier, `prepare_delivery` / `retry_after_s` hooks | 8 |
| `services/dispatch_worker/{failure_policy,consumer,main}.py` | The dispatch-worker service | 8 |
| `docker-compose.yml` | dispatch-worker block, loopback ports, frontend service, token forwarding | 8, 9, 12 |
| `packages/core/settings.py` | `FrontendSettings`, `FrontendServiceSettings` | 9 |
| `services/frontend/*` (+ templates, static) | Review UI: queue, detail, decisions, timeline, knowledge upload | 9, 10 |
| `tests/e2e/*` | Playwright approve/edit flows and accessibility checks | 10 |
| `tests/integration/test_phase6_pipeline_e2e.py` | Fixture email → … → dispatch on fakes (6.9) | 11 |
| `scripts/connect_gmail.py`, `scripts/phase6_gate.py`, `Makefile` | `make connect-gmail ADDRESS=…`, `make phase6-gate` | 12 |
| `.env.example`, `docs/configuration.md`, `docs/demo-runbook.md` | `FRONTEND__*`, `GMAIL_ACCESS_TOKEN`, `dispatch_mode`, runbook §3.4/§5.3/§6/§7 | 3, 9, 12 |
| `specs/tasks.md`, `specs/design.md` | 6.7 note, design tree (D1), Phase 6 closure and gate evidence | 9, 12, 13 |

---

### Task 0: Commit this plan

The spec and ADR-0009 are already committed (`bb00bfc`). This task commits only the plan.

**Files:**
- Create: `docs/superpowers/plans/2026-09-29-phase6-dispatch-review.md` (this file)

- [ ] **Step 1: Check the working tree holds only the plan**

Run: `git status --short`
Expected: exactly one line, `?? docs/superpowers/plans/2026-09-29-phase6-dispatch-review.md`.

- [ ] **Step 2: Commit the plan**

```bash
git add docs/superpowers/plans/2026-09-29-phase6-dispatch-review.md
git commit -m "$(cat <<'EOF'
docs(plan): Phase 6 dispatch and review implementation plan [task 6.1–6.10] [R16.6–R16.8, R17.1–R17.7, R18.3, R18.7, R19.2, R19.3, R21.4, R23.4–R23.7, R24.7]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

Expected: one commit on `RAG_Email_System`, and `git status --short` prints nothing.

---

## Part A (Tasks 1–3): Foundation, outbound reply, dispatch modes

Skills applied while writing this part: `fullstack-dev-skills:python-pro` (strict typing, dataclasses, pytest layout) and `fullstack-dev-skills:postgres-pro` (migration DDL, unique indexes, reversibility). Part A has no UI and no FastAPI code, so it needs no other skill.

```
Task 1 (foundation)                      Task 2 (6.3, pure)                     Task 3 (6.4, config)
─────────────────────────────            ───────────────────────────            ──────────────────────────
migrations/0005_dispatch_and_review      packages/dispatch/reply.py             config/categories.yaml
  generated_draft + provider_draft_id      build_outbound_reply(*, draft,         every category:
                  + provider_draft_msg_id    original, provider_thread_id,          dispatch_mode: create_draft
                  + dispatch_idempotency_key  message_id_domain) -> OutboundReply    auto_send_eligible: false
                    (uq_generated_draft_dispatch_key)                                   │
  feedback        + review_ms                 │ Re: ×1, In-Reply-To, References,     ▼
                  + uq_feedback_draft          │ <dispatch-{draft.id.hex}@domain>,   packages/domain/taxonomy.py
packages/domain/dispatch.py                    │ provider thread id, quoted original   CategoryDefinition.dispatch_mode
  DispatchMode, ProviderDraftStatus ───────────┼──────────────────────────────────▶   TaxonomyRegistry.dispatch_mode_for
packages/domain/state_machine.py               │                                      get_dispatch_mode(category)
  RETRY_PENDING → {GENERATING, DISPATCHED, FAILED}
packages/db/job.py reaper: skip DISPATCHED     ▼
packages/domain/entities.py                OutboundReply(thread_id: str = PROVIDER thread id,
  OutboundReply.thread_id: str               message_id, reply_to_provider_message_id)
  OutboundReply.message_id, .reply_to_provider_message_id
  GeneratedDraft.provider_draft_id, .provider_draft_message_id, .dispatch_idempotency_key
packages/db/draft.py maps the three new columns (read + insert + in-memory twin)
```

### File Structure (part A)

- Create: `migrations/0005_dispatch_and_review.up.sql`, `migrations/0005_dispatch_and_review.down.sql`
- Create: `packages/domain/dispatch.py` — `DispatchMode`, `ProviderDraftStatus` (Task 1); `DEFAULT_DISPATCH_MODE`, `parse_dispatch_mode` (Task 3)
- Create: `packages/dispatch/__init__.py`, `packages/dispatch/reply.py` — `build_outbound_reply` and its errors (Task 2). Parts B/C add `service.py` to this package.
- Modify: `packages/domain/state_machine.py`, `packages/domain/entities.py`, `packages/domain/__init__.py`, `packages/domain/taxonomy.py`
- Modify: `packages/db/job.py` (both reaper exclusion sites), `packages/db/draft.py`
- Modify: `config/categories.yaml`, `docs/configuration.md` (§2.16)
- Tests: `tests/unit/test_state_machine.py`, `tests/unit/test_lease_reaper.py`, `tests/unit/test_dispatch_domain.py` (new), `tests/unit/test_draft_store.py`, `tests/unit/test_outbound_reply.py` (new), `tests/unit/test_category_taxonomy.py`, `tests/unit/test_category_routing.py`, `tests/integration/test_dispatch_schema_postgres.py` (new), `tests/integration/test_lease_reaper_integration.py`

---

### Task 1: Dispatch foundation — migration 0005, RETRY_PENDING → DISPATCHED, reaper skips DISPATCHED, dispatch domain types [tasks.md 6.2 (migration), 6.5 (state machine)]

**Files:**
- Create: `migrations/0005_dispatch_and_review.up.sql`
- Create: `migrations/0005_dispatch_and_review.down.sql`
- Create: `packages/domain/dispatch.py`
- Modify: `packages/domain/state_machine.py` (line 65, the `RETRY_PENDING` row of `TRANSITIONS`)
- Modify: `packages/domain/entities.py` (`OutboundReply` lines 322–337; `GeneratedDraft` lines 348–372)
- Modify: `packages/domain/__init__.py` (imports block lines 6–82 and `__all__` lines 84–148)
- Modify: `packages/db/job.py` (Postgres reaper `WHERE` lines 566–567; in-memory reaper lines 1086–1088)
- Modify: `packages/db/draft.py` (`_row_to_draft` lines 32–66, `_INSERT_DRAFT_SQL` lines 69–83, `insert_draft` lines 86–116, `InMemoryDraftStore.create_draft` lines 173–200)
- Test: `tests/unit/test_state_machine.py` (count at lines 57–58, illegal list lines 66–86, new tests), `tests/unit/test_lease_reaper.py` (append), `tests/unit/test_dispatch_domain.py` (new), `tests/unit/test_draft_store.py` (append), `tests/integration/test_dispatch_schema_postgres.py` (new), `tests/integration/test_lease_reaper_integration.py` (append)

**Interfaces:**
- Consumes: `transition_job(job, target_state, payload=None, trace_id=None) -> tuple[Job, ProcessingEvent]`, `validate_transition(current, target)` (packages/domain/state_machine.py); `InMemoryJobStore` / `PostgresJobStore.create_job(job) -> tuple[Job, bool]` (the bool says whether the job was newly created; plan code only unpacks `job, _ =`), `.reap_expired_jobs(batch_size=..., organization_id=...)`, `.get_job(organization_id, job_id)` (packages/db/job.py); `apply_migrations(dsn=...)`, `rollback_migrations(dsn=..., steps=...)`, `discover_migrations()` (packages/db/migrator.py); `create_pool_from_settings(settings)` (packages/db/connection.py); `PostgresDraftStore.create_draft/get_draft(draft_id, organization_id)`.
- Produces:
  - SQL: `generated_draft.provider_draft_id TEXT`, `generated_draft.provider_draft_message_id TEXT`, `generated_draft.dispatch_idempotency_key TEXT` + unique index `uq_generated_draft_dispatch_key`; `feedback.review_ms INT` + unique index `uq_feedback_draft` on `feedback(draft_id)` (replaces the non-unique `idx_feedback_draft`).
  - `packages.domain.dispatch.DispatchMode(StrEnum)`: `CREATE_DRAFT = "create_draft"`, `SEND_REPLY = "send_reply"`
  - `packages.domain.dispatch.ProviderDraftStatus(StrEnum)`: `DRAFT = "DRAFT"`, `SENT = "SENT"`, `MISSING = "MISSING"` (both re-exported from `packages.domain`)
  - `TRANSITIONS[JobState.RETRY_PENDING] == {GENERATING, DISPATCHED, FAILED}` (26 declared transitions)
  - Lease reaper (Postgres and in-memory) never reclaims `DISPATCHED`.
  - `OutboundReply.thread_id: str` — **the provider thread id** (exact field name kept: `thread_id`); new `OutboundReply.message_id: str | None = None` (RFC 5322 id in `<...>`, Gmail/MIME only); new `OutboundReply.reply_to_provider_message_id: str | None = None` (the original email's provider message id, which Graph `createReply` addresses).
  - `GeneratedDraft.provider_draft_id: str | None`, `.provider_draft_message_id: str | None`, `.dispatch_idempotency_key: str | None` (all default `None`), read by `_row_to_draft`, written by `insert_draft`, copied by `InMemoryDraftStore`.

Design decisions:
- The `UNIQUE` columns are unique **indexes** (`uq_<table>_<what>`), the same form migration 0004 uses. `INSERT … ON CONFLICT (dispatch_idempotency_key)` and `ON CONFLICT (draft_id)` both work with a non-partial unique index, and `CREATE UNIQUE INDEX IF NOT EXISTS` keeps the file idempotent (PostgreSQL has no `ADD CONSTRAINT IF NOT EXISTS`). Not `CONCURRENTLY`: the migrator wraps each file in one transaction.
- `uq_feedback_draft` covers every lookup the old `idx_feedback_draft` served, so 0005 drops the old index and 0005.down recreates it. Adding the unique index is safe on existing data because no code writes `feedback` yet (verified: no writer in `packages/` or `services/`).
- `OutboundReply.thread_id` keeps its name and narrows to `str`: every existing construction (`packages/adapters/testing.py:117,134`, `packages/adapters/fake.py:261`, `tests/unit/test_gmail_adapter.py:47`, `tests/unit/test_domain_entities.py:291`) already passes a string, and the adapters already send `str(reply.thread_id)` as the Gmail `threadId`. No call site changes.
- The reaper change is the only hand-written state string, and it extends the existing SQL `NOT IN` list (the accepted precedent). The dispatch-worker takes no lease (design §5.8); redelivery belongs to the broker.

- [ ] **Step 1: Write the failing state-machine tests**

In `tests/unit/test_state_machine.py`, change the count assertion at lines 57–58 to:

```python
    # Exactly 26 declared legal transitions (including CLASSIFIED -> DRAFTED and the
    # RETRY_PENDING -> DISPATCHED replay of a dead-lettered dispatch, ADR-0009)
    assert total_transitions == 26
```

Add `(JobState.DISPATCHED, JobState.RETRY_PENDING),` to the `test_illegal_transitions_raise` parametrize list, directly after the existing `(JobState.DISPATCHED, JobState.GENERATING),` row (a transient dispatch failure keeps the job `DISPATCHED`; design §5.8).

Append after `test_operator_replay_transition`:

```python
def test_retry_pending_to_dispatched_transition() -> None:
    """ADR-0009: operator replay of a dead-lettered dispatch resumes at DISPATCHED (R18.7)."""
    src, dst = validate_transition(JobState.RETRY_PENDING, JobState.DISPATCHED)
    assert src == JobState.RETRY_PENDING
    assert dst == JobState.DISPATCHED
    assert TRANSITIONS[JobState.RETRY_PENDING] == {
        JobState.GENERATING,
        JobState.DISPATCHED,
        JobState.FAILED,
    }


def test_dead_lettered_dispatch_replays_back_to_dispatched() -> None:
    """design.md §5.8: DISPATCHED -> FAILED -> DEAD_LETTER -> RETRY_PENDING -> DISPATCHED."""
    job = Job(
        organization_id=uuid4(),
        state=JobState.DISPATCHED.value,
        idempotency_key=str(uuid4()),
    )
    steps: list[tuple[str | None, str | None]] = []
    for target in (
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.RETRY_PENDING,
        JobState.DISPATCHED,
    ):
        job, event = transition_job(job, target, payload={"reason": "replay-path"})
        steps.append((event.state_from, event.state_to))

    assert steps == [
        ("DISPATCHED", "FAILED"),
        ("FAILED", "DEAD_LETTER"),
        ("DEAD_LETTER", "RETRY_PENDING"),
        ("RETRY_PENDING", "DISPATCHED"),
    ]
    assert job.state == JobState.DISPATCHED.value
```

(`Job` and `uuid4` are already imported at the top of the file.)

- [ ] **Step 2: Write the failing reaper unit test**

Append to `tests/unit/test_lease_reaper.py`:

```python
@pytest.mark.asyncio
async def test_reaper_skips_dispatched_jobs() -> None:
    """6.5 / design.md §5.8: no lease is taken; the broker redelivers DISPATCHED."""
    store = InMemoryJobStore()
    past_time = datetime.now(UTC) - timedelta(seconds=600)
    job, _ = await store.create_job(
        Job(
            organization_id=uuid4(),
            state=JobState.DISPATCHED.value,
            lease_expires_at=past_time,
            idempotency_key=str(uuid4()),
        )
    )
    job.lease_expires_at = past_time

    reaped = await store.reap_expired_jobs(batch_size=10)

    assert reaped == []
    stored = await store.get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.DISPATCHED.value
```

- [ ] **Step 3: Write the failing domain and draft-store unit tests**

Create `tests/unit/test_dispatch_domain.py`:

```python
"""Phase 6 dispatch domain types and entity changes (R17.1, R17.2, R19.2; ADR-0009)."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import packages.domain as domain
from packages.domain.dispatch import DispatchMode, ProviderDraftStatus
from packages.domain.entities import EmailAddress, GeneratedDraft, OutboundReply


def test_dispatch_mode_values() -> None:
    """R17.1: exactly two dispatch modes, spelled as config/categories.yaml spells them."""
    assert [m.value for m in DispatchMode] == ["create_draft", "send_reply"]
    assert DispatchMode("send_reply") is DispatchMode.SEND_REPLY


def test_provider_draft_status_values() -> None:
    """design.md §5.8 step 4: what get_draft_status reports after an ambiguous send."""
    assert [s.value for s in ProviderDraftStatus] == ["DRAFT", "SENT", "MISSING"]


def test_dispatch_types_are_exported_from_the_domain_package() -> None:
    assert domain.DispatchMode is DispatchMode
    assert domain.ProviderDraftStatus is ProviderDraftStatus
    assert {"DispatchMode", "ProviderDraftStatus"} <= set(domain.__all__)


def test_outbound_reply_thread_field_is_the_provider_thread_id_string() -> None:
    """6.3: the thread field carries the provider thread id as a string, never our UUID."""
    annotations = {f.name: f.type for f in dataclasses.fields(OutboundReply)}
    assert annotations["thread_id"] == "str"
    assert annotations["message_id"] == "str | None"
    assert annotations["reply_to_provider_message_id"] == "str | None"


def test_outbound_reply_new_fields() -> None:
    reply = OutboundReply(
        thread_id="18c2f0a9d1e4b7aa",
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        to=[EmailAddress(email="customer@example.com")],
        body_text="Hello",
        message_id="<dispatch-1@acme.example>",
        reply_to_provider_message_id="prov-msg-1",
    )
    assert reply.thread_id == "18c2f0a9d1e4b7aa"
    assert reply.message_id == "<dispatch-1@acme.example>"
    assert reply.reply_to_provider_message_id == "prov-msg-1"

    bare = OutboundReply(
        thread_id="th-1",
        mailbox_id="mbx-1",
        organization_id="org-1",
        to=[EmailAddress(email="customer@example.com")],
        body_text="Hello",
    )
    assert bare.message_id is None
    assert bare.reply_to_provider_message_id is None


def test_generated_draft_dispatch_handle_defaults_to_none() -> None:
    """ADR-0009: a fresh draft has no provider draft and no claimed dispatch key."""
    draft = GeneratedDraft(body="Hello")
    assert draft.provider_draft_id is None
    assert draft.provider_draft_message_id is None
    assert draft.dispatch_idempotency_key is None
```

Append to `tests/unit/test_draft_store.py`:

```python
@pytest.mark.asyncio
async def test_in_memory_draft_store_keeps_dispatch_handle() -> None:
    """6.5: the in-memory twin copies the ADR-0009 dispatch columns like Postgres does."""
    store = InMemoryDraftStore()
    org_id = uuid4()
    draft = GeneratedDraft(
        organization_id=org_id,
        job_id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        body="Hello",
        provider_draft_id="r-8123",
        provider_draft_message_id="18c2f0a9d1e4b7ab",
        dispatch_idempotency_key="a" * 64,
    )
    await store.create_draft(draft)

    stored = await store.get_draft(draft.id, org_id)

    assert stored is not None
    assert stored.provider_draft_id == "r-8123"
    assert stored.provider_draft_message_id == "18c2f0a9d1e4b7ab"
    assert stored.dispatch_idempotency_key == "a" * 64
```

- [ ] **Step 4: Run the unit tests to see them fail**

Run: `uv run pytest tests/unit/test_state_machine.py tests/unit/test_lease_reaper.py tests/unit/test_dispatch_domain.py tests/unit/test_draft_store.py -v`

Expected: FAIL — `test_dispatch_domain.py` errors at collection with `ModuleNotFoundError: No module named 'packages.domain.dispatch'`; `test_all_declared_legal_transitions` fails with `assert 25 == 26`; `test_retry_pending_to_dispatched_transition` and `test_dead_lettered_dispatch_replays_back_to_dispatched` fail with `IllegalStateTransitionError: Illegal job state transition: cannot transition from 'RETRY_PENDING' to 'DISPATCHED'.`; `test_reaper_skips_dispatched_jobs` fails because the reaper returns one reaped job; `test_in_memory_draft_store_keeps_dispatch_handle` fails with `TypeError: GeneratedDraft.__init__() got an unexpected keyword argument 'provider_draft_id'`.

- [ ] **Step 5: Write the failing Postgres tests**

Create `tests/integration/test_dispatch_schema_postgres.py`:

```python
"""Migration 0005: dispatch handle and review feedback (R5.5, R16.7, R17.3, R19.2; ADR-0009).

Runs in the isolated rag_email_test database (tests/integration/conftest.py).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.migrator import apply_migrations, discover_migrations, rollback_migrations
from packages.domain.entities import GeneratedDraft

DISPATCH_COLUMNS = ("provider_draft_id", "provider_draft_message_id", "dispatch_idempotency_key")


@pytest.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool[Any]]:
    settings = AppSettings().database
    await apply_migrations(dsn=settings.asyncpg_dsn)
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_message(conn: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert org, mailbox, thread and one inbound email; return (org, message, thread)."""
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    await conn.execute(
        "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
    )
    await conn.execute(
        "INSERT INTO mailbox (id, organization_id, provider, address)"
        " VALUES ($1, $2, 'gmail', $3)",
        mbx_id,
        org_id,
        f"box-{mbx_id.hex[:6]}@example.com",
    )
    await conn.execute(
        "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
        " VALUES ($1, $2, $3, $4)",
        thread_id,
        org_id,
        mbx_id,
        f"th-{thread_id.hex[:6]}",
    )
    await conn.execute(
        "INSERT INTO email_message (id, organization_id, mailbox_id, thread_id,"
        " provider_message_id, direction, received_at)"
        " VALUES ($1, $2, $3, $4, $5, 'inbound', now())",
        msg_id,
        org_id,
        mbx_id,
        thread_id,
        f"prov-{msg_id.hex[:6]}",
    )
    return org_id, msg_id, thread_id


async def _insert_draft(
    conn: Any, org_id: uuid.UUID, msg_id: uuid.UUID, thread_id: uuid.UUID, key: str | None
) -> uuid.UUID:
    draft_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO generated_draft (id, organization_id, message_id, thread_id, action, body,"
        " dispatch_idempotency_key) VALUES ($1, $2, $3, $4, 'reply', 'Hello', $5)",
        draft_id,
        org_id,
        msg_id,
        thread_id,
        key,
    )
    return draft_id


async def _columns(conn: Any, table: str, names: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1 AND column_name = ANY($2::text[])
        """,
        table,
        list(names),
    )
    return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}


async def _indexes(conn: Any, table: str) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = $1",
        table,
    )
    return {r["indexname"]: r["indexdef"] for r in rows}


async def test_0005_adds_the_dispatch_and_review_columns(db_pool: asyncpg.Pool[Any]) -> None:
    """design.md §6: three nullable TEXT dispatch columns and feedback.review_ms INT."""
    async with db_pool.acquire() as conn:
        draft_cols = await _columns(conn, "generated_draft", DISPATCH_COLUMNS)
        feedback_cols = await _columns(conn, "feedback", ("review_ms",))
        draft_idx = await _indexes(conn, "generated_draft")
        feedback_idx = await _indexes(conn, "feedback")

    assert draft_cols == {name: ("text", "YES") for name in DISPATCH_COLUMNS}
    assert feedback_cols == {"review_ms": ("integer", "YES")}
    assert "UNIQUE" in draft_idx["uq_generated_draft_dispatch_key"]
    assert "UNIQUE" in feedback_idx["uq_feedback_draft"]
    assert "idx_feedback_draft" not in feedback_idx  # superseded by uq_feedback_draft


async def test_dispatch_key_is_unique_but_unclaimed_drafts_coexist(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """R17.3, R19.2: one draft can hold a dispatch key; NULL (unclaimed) is not a key."""
    async with db_pool.acquire() as conn:
        org_id, msg_id, thread_id = await _seed_message(conn)
        try:
            await _insert_draft(conn, org_id, msg_id, thread_id, None)
            await _insert_draft(conn, org_id, msg_id, thread_id, None)
            key = f"dispatch-{uuid.uuid4().hex}"
            await _insert_draft(conn, org_id, msg_id, thread_id, key)
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert_draft(conn, org_id, msg_id, thread_id, key)
        finally:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_feedback_allows_one_row_per_draft(db_pool: asyncpg.Pool[Any]) -> None:
    """R16.7 / 6.1: a repeated approve cannot write a second feedback row."""
    async with db_pool.acquire() as conn:
        org_id, msg_id, thread_id = await _seed_message(conn)
        try:
            draft_id = await _insert_draft(conn, org_id, msg_id, thread_id, None)
            await conn.execute(
                "INSERT INTO feedback (id, organization_id, draft_id, decision, review_ms)"
                " VALUES ($1, $2, $3, 'accepted', 4200)",
                uuid.uuid4(),
                org_id,
                draft_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO feedback (id, organization_id, draft_id, decision)"
                    " VALUES ($1, $2, $3, 'rejected')",
                    uuid.uuid4(),
                    org_id,
                    draft_id,
                )
            review_ms = await conn.fetchval(
                "SELECT review_ms FROM feedback WHERE draft_id = $1 AND organization_id = $2",
                draft_id,
                org_id,
            )
            assert review_ms == 4200
        finally:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_draft_store_round_trips_the_dispatch_handle(db_pool: asyncpg.Pool[Any]) -> None:
    """insert_draft writes and _row_to_draft reads the three ADR-0009 columns."""
    async with db_pool.acquire() as conn:
        org_id, msg_id, thread_id = await _seed_message(conn)
    store = PostgresDraftStore(db_pool)
    try:
        plain = await store.create_draft(
            GeneratedDraft(
                organization_id=org_id, message_id=msg_id, thread_id=thread_id, body="Hi"
            )
        )
        assert plain.provider_draft_id is None
        assert plain.provider_draft_message_id is None
        assert plain.dispatch_idempotency_key is None

        key = f"dispatch-{uuid.uuid4().hex}"
        handled = await store.create_draft(
            GeneratedDraft(
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                body="Hi again",
                provider_draft_id="r-8123",
                provider_draft_message_id="18c2f0a9d1e4b7ab",
                dispatch_idempotency_key=key,
            )
        )
        stored = await store.get_draft(handled.id, org_id)
        assert stored is not None
        assert stored.provider_draft_id == "r-8123"
        assert stored.provider_draft_message_id == "18c2f0a9d1e4b7ab"
        assert stored.dispatch_idempotency_key == key
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_0005_rolls_back_cleanly_and_reapplies() -> None:
    """R5.5: 0005.down removes exactly what 0005.up added and restores idx_feedback_draft."""
    dsn = AppSettings().database.asyncpg_dsn
    await apply_migrations(dsn=dsn)
    steps = sum(1 for m in discover_migrations() if m.version >= "0005")
    rolled_back = await rollback_migrations(dsn=dsn, steps=steps)
    assert "0005" in rolled_back

    conn = await asyncpg.connect(dsn)
    try:
        assert await _columns(conn, "generated_draft", DISPATCH_COLUMNS) == {}
        assert await _columns(conn, "feedback", ("review_ms",)) == {}
        feedback_idx = await _indexes(conn, "feedback")
        assert "idx_feedback_draft" in feedback_idx
        assert "uq_feedback_draft" not in feedback_idx
        assert "uq_generated_draft_dispatch_key" not in await _indexes(conn, "generated_draft")
    finally:
        await conn.close()

    applied = await apply_migrations(dsn=dsn)
    assert "0005" in applied
    conn = await asyncpg.connect(dsn)
    try:
        assert set(await _columns(conn, "generated_draft", DISPATCH_COLUMNS)) == set(
            DISPATCH_COLUMNS
        )
    finally:
        await conn.close()
```

Append to `tests/integration/test_lease_reaper_integration.py`:

```python
@pytest.mark.asyncio
async def test_reaper_skips_dispatched_jobs_postgres(db_pool: asyncpg.Pool) -> None:
    """6.5 on Postgres: an expired lease on a DISPATCHED job is left alone (design.md §5.8)."""
    org_id = uuid4()
    await ensure_test_org(db_pool, org_id, "Dispatched Lease Org")
    store = PostgresJobStore(db_pool)
    created, _ = await store.create_job(
        Job(
            organization_id=org_id,
            state=JobState.DISPATCHED.value,
            idempotency_key=f"lease-dispatched-{uuid4()}",
        )
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE processing_job SET lease_expires_at = now() - interval '10 minutes'"
            " WHERE id = $1 AND organization_id = $2",
            created.id,
            org_id,
        )

    reaped = await store.reap_expired_jobs(batch_size=10, organization_id=org_id)

    assert reaped == []
    stored = await store.get_job(org_id, created.id)
    assert stored is not None and stored.state == JobState.DISPATCHED.value
```

- [ ] **Step 6: Run the Postgres tests to see them fail**

Run: `uv run pytest tests/integration/test_dispatch_schema_postgres.py tests/integration/test_lease_reaper_integration.py::test_reaper_skips_dispatched_jobs_postgres -v`

Expected: FAIL — `test_0005_adds_the_dispatch_and_review_columns` fails on `assert {} == {...}`; the key and feedback tests fail with `asyncpg.exceptions.UndefinedColumnError: column "dispatch_idempotency_key" of relation "generated_draft" does not exist` (and `"review_ms"`); the round-trip test fails with `TypeError ... unexpected keyword argument 'provider_draft_id'`; the rollback test fails because `"0005"` is not in the rolled-back list; the reaper test fails because one job is reaped.

- [ ] **Step 7: Write migration 0005**

Create `migrations/0005_dispatch_and_review.up.sql`:

```sql
-- Migration: 0005_dispatch_and_review.up.sql
-- Dispatch handle and review feedback (ADR-0009; design.md §5.8, §6; R16.7, R17.3, R19.2, R21.4).
-- generated_draft: the provider draft created or reused in dispatch step 2, and the R19.2
--   dispatch key (operation "dispatch") claimed in dispatch step 1. NULL means unclaimed.
-- feedback: time from opening a draft to deciding, and exactly one decision row per draft.
-- Unique indexes, not CONCURRENTLY: the migrator runs each file inside a transaction.
ALTER TABLE generated_draft ADD COLUMN IF NOT EXISTS provider_draft_id TEXT;
ALTER TABLE generated_draft ADD COLUMN IF NOT EXISTS provider_draft_message_id TEXT;
ALTER TABLE generated_draft ADD COLUMN IF NOT EXISTS dispatch_idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_draft_dispatch_key
    ON generated_draft (dispatch_idempotency_key);

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS review_ms INT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_draft ON feedback (draft_id);
-- uq_feedback_draft serves every lookup the non-unique index did.
DROP INDEX IF EXISTS idx_feedback_draft;
```

Create `migrations/0005_dispatch_and_review.down.sql`:

```sql
-- Migration: 0005_dispatch_and_review.down.sql
CREATE INDEX IF NOT EXISTS idx_feedback_draft ON feedback (draft_id);
DROP INDEX IF EXISTS uq_feedback_draft;
ALTER TABLE feedback DROP COLUMN IF EXISTS review_ms;

DROP INDEX IF EXISTS uq_generated_draft_dispatch_key;
ALTER TABLE generated_draft DROP COLUMN IF EXISTS dispatch_idempotency_key;
ALTER TABLE generated_draft DROP COLUMN IF EXISTS provider_draft_message_id;
ALTER TABLE generated_draft DROP COLUMN IF EXISTS provider_draft_id;
```

- [ ] **Step 8: Add the dispatch domain types**

Create `packages/domain/dispatch.py`:

```python
"""Dispatch value objects: per-category dispatch mode and provider draft status.

Requirements:
- R17.1: two dispatch modes, create a provider draft or send the reply.
- R16.8, R17.6: create_draft is the default posture; send_reply needs an explicit approval.
- design.md §5.8 step 4, ADR-0009: the provider draft's status after an ambiguous send.
- GEMINI.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

from enum import StrEnum


class DispatchMode(StrEnum):
    """How an approved draft leaves the system (R17.1)."""

    CREATE_DRAFT = "create_draft"
    SEND_REPLY = "send_reply"


class ProviderDraftStatus(StrEnum):
    """What the provider reports for a stored provider draft id (design.md §5.8 step 4).

    DRAFT: still an unsent draft. SENT: sent under the same id (Graph immutable ids).
    MISSING: gone — sent under a new id (Gmail drafts.send) or deleted by a person.
    """

    DRAFT = "DRAFT"
    SENT = "SENT"
    MISSING = "MISSING"
```

In `packages/domain/__init__.py`, add after the `packages.domain.business` import block:

```python
from packages.domain.dispatch import (
    DispatchMode,
    ProviderDraftStatus,
)
```

and add `"DispatchMode",` to `__all__` directly after `"DraftRef",`, and `"ProviderDraftStatus",` directly after `"ProcessingEvent",`.

- [ ] **Step 9: Add the RETRY_PENDING → DISPATCHED edge**

In `packages/domain/state_machine.py`, replace line 65:

```python
    JobState.RETRY_PENDING: {JobState.GENERATING, JobState.FAILED},
```

with:

```python
    # DISPATCHED: operator replay of a dead-lettered dispatch resumes the send (ADR-0009)
    JobState.RETRY_PENDING: {JobState.GENERATING, JobState.DISPATCHED, JobState.FAILED},
```

`specs/design.md` §8 already lists this edge (line 981); no spec change.

- [ ] **Step 10: Make both reapers skip DISPATCHED**

In `packages/db/job.py`, replace lines 566–567:

```python
            -- DRAFTED waits for a human reviewer; no worker holds it (4.13b).
            WHERE state NOT IN ('COMPLETED', 'DEAD_LETTER', 'DRAFTED')
```

with:

```python
            -- DRAFTED waits for a human reviewer; no worker holds it (4.13b).
            -- DISPATCHED: the broker redelivers it; the dispatch-worker holds no lease (6.5).
            WHERE state NOT IN ('COMPLETED', 'DEAD_LETTER', 'DRAFTED', 'DISPATCHED')
```

and replace lines 1086–1088:

```python
                # DRAFTED waits for a human reviewer; no worker holds it (4.13b).
                if j.state in ("COMPLETED", "DEAD_LETTER", "DRAFTED"):
                    continue
```

with:

```python
                # DRAFTED waits for a human reviewer; no worker holds it (4.13b).
                # DISPATCHED: the broker redelivers it; the dispatch-worker holds no lease (6.5).
                if j.state in ("COMPLETED", "DEAD_LETTER", "DRAFTED", "DISPATCHED"):
                    continue
```

- [ ] **Step 11: Change the entities**

In `packages/domain/entities.py`, replace the whole `OutboundReply` class (lines 322–337) with:

```python
@dataclass
class OutboundReply:
    """Outbound reply draft or message submission data (R1.1, R1.4, R17.1, R17.2).

    ``thread_id`` is the PROVIDER thread id (``email_thread.provider_thread_id``: the thread
    the provider groups the conversation under), never our email_thread UUID.
    ``message_id``, ``in_reply_to`` and ``references`` are RFC 5322 ids wrapped in ``<...>``,
    ready for MIME headers; ``message_id`` is set for MIME-built replies only (a provider
    that builds the reply itself sets its own). ``reply_to_provider_message_id`` is the
    original email's provider message id, for providers that create a reply from it.
    """

    thread_id: str
    mailbox_id: UUID | str
    organization_id: UUID | str
    to: list[EmailAddress]
    body_text: str
    subject: str = ""
    cc: list[EmailAddress] = field(default_factory=list)
    body_html: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    draft_id: str | None = None
    message_id: str | None = None
    reply_to_provider_message_id: str | None = None
```

In `GeneratedDraft`, replace:

```python
    status: str = "draft"  # draft | approved | rejected | dispatched
    provider_ref: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

with:

```python
    status: str = "draft"  # draft | approved | rejected | dispatched
    provider_ref: str | None = None
    provider_draft_id: str | None = None  # ADR-0009 dispatch handle (design.md §5.8 step 2)
    provider_draft_message_id: str | None = None  # the provider draft's own message id
    dispatch_idempotency_key: str | None = None  # R19.2 key, operation "dispatch" (step 1)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

- [ ] **Step 12: Map the new columns in the draft store**

In `packages/db/draft.py`, in `_row_to_draft`, replace:

```python
        status=row["status"],
        provider_ref=row["provider_ref"],
        created_at=row["created_at"],
    )
```

with:

```python
        status=row["status"],
        provider_ref=row["provider_ref"],
        provider_draft_id=row["provider_draft_id"],
        provider_draft_message_id=row["provider_draft_message_id"],
        dispatch_idempotency_key=row["dispatch_idempotency_key"],
        created_at=row["created_at"],
    )
```

Replace `_INSERT_DRAFT_SQL` (lines 69–83) with:

```python
_INSERT_DRAFT_SQL = """
    INSERT INTO generated_draft (
        id, organization_id, job_id, message_id, thread_id,
        action, subject, body, confidence, citations,
        citation_mismatch, model_name, model_tier, escalation_reason,
        prompt_version, input_tokens, output_tokens, cost_estimate,
        status, provider_ref, provider_draft_id, provider_draft_message_id,
        dispatch_idempotency_key, created_at
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8, $9, $10::jsonb,
        $11, $12, $13, $14,
        $15, $16, $17, $18,
        $19, $20, $21, $22,
        $23, $24
    )
    RETURNING *;
"""
```

In `insert_draft`, replace the tail of the argument list:

```python
        draft.status,
        draft.provider_ref,
        draft.created_at or datetime.now(UTC),
    )
```

with:

```python
        draft.status,
        draft.provider_ref,
        draft.provider_draft_id,
        draft.provider_draft_message_id,
        draft.dispatch_idempotency_key,
        draft.created_at or datetime.now(UTC),
    )
```

In `InMemoryDraftStore.create_draft`, replace:

```python
            status=draft.status,
            provider_ref=draft.provider_ref,
            created_at=draft.created_at or datetime.now(UTC),
        )
```

with:

```python
            status=draft.status,
            provider_ref=draft.provider_ref,
            provider_draft_id=draft.provider_draft_id,
            provider_draft_message_id=draft.provider_draft_message_id,
            dispatch_idempotency_key=draft.dispatch_idempotency_key,
            created_at=draft.created_at or datetime.now(UTC),
        )
```

- [ ] **Step 13: Run the unit tests**

Run: `uv run pytest tests/unit/test_state_machine.py tests/unit/test_lease_reaper.py tests/unit/test_dispatch_domain.py tests/unit/test_draft_store.py tests/unit/test_domain_entities.py tests/unit/test_dependency_rules.py tests/unit/test_mail_adapter_contract.py tests/unit/test_gmail_adapter.py tests/unit/test_graph_adapter.py tests/unit/test_fake_adapter.py -v`

Expected: PASS (the adapter suites confirm the `thread_id: str` narrowing breaks no construction site; `test_dependency_rules.py` confirms `packages/domain/dispatch.py` imports only the stdlib).

- [ ] **Step 14: Run the Postgres tests**

Run: `uv run pytest tests/integration/test_dispatch_schema_postgres.py tests/integration/test_lease_reaper_integration.py tests/integration/test_database_schema.py tests/integration/test_draft_persistence_postgres.py -v`

Expected: PASS (`test_migration_reversibility` still rolls every migration back to zero tables, now including 0005).

- [ ] **Step 15: Format, lint, type-check, full unit suite**

Run: `uv run ruff format packages tests && uv run ruff check . && uv run mypy packages services tests evaluation && uv run pytest tests/unit -q`

Expected: ruff reports no changes needed after formatting and `All checks passed!`; mypy prints `Success: no issues found in N source files`; the unit suite passes.

- [ ] **Step 16: Commit**

```bash
git add migrations/0005_dispatch_and_review.up.sql migrations/0005_dispatch_and_review.down.sql \
  packages/domain/dispatch.py packages/domain/__init__.py packages/domain/state_machine.py \
  packages/domain/entities.py packages/db/job.py packages/db/draft.py \
  tests/unit/test_state_machine.py tests/unit/test_lease_reaper.py \
  tests/unit/test_dispatch_domain.py tests/unit/test_draft_store.py \
  tests/integration/test_dispatch_schema_postgres.py tests/integration/test_lease_reaper_integration.py
git commit -m "$(cat <<'EOF'
feat(dispatch): migration 0005, RETRY_PENDING -> DISPATCHED, dispatch domain types [task 6.2, 6.5] [R16.7, R17.3, R18.3, R18.7, R19.2, R19.3]

Migration 0005 adds the ADR-0009 dispatch handle to generated_draft
(provider_draft_id, provider_draft_message_id, unique dispatch_idempotency_key)
and feedback.review_ms with one feedback row per draft. The state machine
gains the operator-replay edge RETRY_PENDING -> DISPATCHED, and both lease
reapers skip DISPATCHED jobs. OutboundReply.thread_id now holds the provider
thread id as a string and gains message_id and reply_to_provider_message_id.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Outbound reply construction [tasks.md 6.3]

**Files:**
- Create: `packages/dispatch/__init__.py`
- Create: `packages/dispatch/reply.py`
- Test: `tests/unit/test_outbound_reply.py` (new)

**Interfaces:**
- Consumes: `GeneratedDraft` (`id: UUID`, `body: str`), `NormalizedMessage` (`thread_id`, `mailbox_id`, `organization_id`, `provider_message_id`, `sender: EmailAddress`, `received_at`, `rfc822_message_id`, `references_ids`, `subject`, `body_text`, `headers` with lower-case keys), `OutboundReply`, `EmailAddress` (packages/domain/entities.py, as changed in Task 1).
- Produces (`packages.dispatch`):
  - `build_outbound_reply(*, draft: GeneratedDraft, original: NormalizedMessage, provider_thread_id: str | None, message_id_domain: str) -> OutboundReply` — pure; raises `MissingProviderThreadError` when `provider_thread_id` is `None` or blank, `MissingRecipientError` when the sender has no address, `ValueError` for an invalid `message_id_domain`.
  - `class MissingProviderThreadError(ValueError)` with `.draft_id: str`, `.thread_id: str` — permanent (dead-letter, design §5.8).
  - `class MissingRecipientError(ValueError)` with `.draft_id: str` — permanent.
  - `reply_subject(original_subject: str) -> str` — exactly one leading `"Re: "`.
  - `build_reply_message_id(draft_id: UUID | str, message_id_domain: str) -> str` — `"<dispatch-{UUID(draft_id).hex}@{domain}>"`, deterministic per draft.

Output contract (what the adapters and the dispatch service of parts B/C rely on):
- `reply.thread_id` = the stripped `provider_thread_id` (never `str(original.thread_id)`).
- `reply.in_reply_to` = `"<" + original.rfc822_message_id + ">"`, or `None` when the original has no Message-ID.
- `reply.references` = the original's `references_ids` (deduplicated, order kept, the parent id removed) followed by the original's Message-ID, each wrapped in `<...>`.
- `reply.message_id` = `build_reply_message_id(draft.id, message_id_domain)`. Deterministic, so a redelivered dispatch rebuilds the same header and the sent copy can be found by it after an ambiguous send (design §5.8 step 4). The write-back in step 5 stores it without brackets, matching `email_message.rfc822_message_id`.
- `reply.subject` = `reply_subject(original.subject)`; the draft's own `subject` is ignored, because the provider keeps a reply in the thread only when the subject matches the original.
- `reply.to` = `[original.sender]`; `cc` stays empty (reply, not reply-all). A `Reply-To` header is **not** honoured: the dispatch path loads the original through `PostgresMessageStore`, and nothing persists `Reply-To` (`email_message` has no headers column, `_row_to_message` never fills `NormalizedMessage.headers`, the parser keeps only `In-Reply-To`/`References`). A Reply-To branch here would work only in unit tests, so replies go to the sender, and persisting `Reply-To` is left as a future task (see "Reply text details" under open questions).
- `reply.body_text` = the draft body, a blank line, then `On YYYY-MM-DD HH:MM UTC, <sender> wrote:` and every line of `original.body_text` prefixed `"> "` (an empty line becomes `">"`).
- `reply.reply_to_provider_message_id` = `original.provider_message_id`; `reply.draft_id` = `str(draft.id)`; `mailbox_id`/`organization_id` from the original.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_outbound_reply.py`:

```python
"""Pure outbound reply construction (R17.2, design.md §5.8 "Outbound reply", task 6.3)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.dispatch import (
    MissingProviderThreadError,
    MissingRecipientError,
    build_outbound_reply,
    build_reply_message_id,
    reply_subject,
)
from packages.domain.entities import EmailAddress, GeneratedDraft, NormalizedMessage

DRAFT_ID = UUID("12345678-1234-5678-1234-567812345678")
DOMAIN = "acme.example"
OUR_MESSAGE_ID = "<dispatch-12345678123456781234567812345678@acme.example>"


def _original(**overrides: Any) -> NormalizedMessage:
    values: dict[str, Any] = {
        "message_id": uuid4(),
        "thread_id": uuid4(),
        "mailbox_id": uuid4(),
        "organization_id": uuid4(),
        "provider": "fake",
        "provider_message_id": "prov-msg-1",
        "sender": EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith"),
        "received_at": datetime(2026, 9, 28, 9, 30, tzinfo=UTC),
        "rfc822_message_id": "orig-1@clientcorp.com",
        "references_ids": ["root-0@clientcorp.com"],
        "subject": "Order status question",
        "body_text": "Where is my order ORD-82915?\n\nThanks,\nAlice",
    }
    values.update(overrides)
    return NormalizedMessage(**values)


def _draft(draft_id: UUID = DRAFT_ID) -> GeneratedDraft:
    return GeneratedDraft(
        id=draft_id,
        subject="Your order",
        body="Hello Alice,\nYour order ORD-82915 has been dispatched.",
    )


def _build(original: NormalizedMessage | None = None, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "draft": _draft(),
        "original": original or _original(),
        "provider_thread_id": "th-provider-77",
        "message_id_domain": DOMAIN,
    }
    params.update(kwargs)
    return build_outbound_reply(**params)


def test_reply_threads_on_the_original() -> None:
    """R17.2: In-Reply-To = original Message-ID; References = its References + its Message-ID."""
    original = _original()
    reply = _build(original)

    assert reply.in_reply_to == "<orig-1@clientcorp.com>"
    assert reply.references == ["<root-0@clientcorp.com>", "<orig-1@clientcorp.com>"]
    assert reply.thread_id == "th-provider-77"
    assert reply.message_id == OUR_MESSAGE_ID
    assert reply.reply_to_provider_message_id == "prov-msg-1"
    assert reply.draft_id == str(DRAFT_ID)
    assert reply.mailbox_id == original.mailbox_id
    assert reply.organization_id == original.organization_id
    assert reply.cc == []


def test_reply_uses_the_provider_thread_id_never_our_uuid() -> None:
    original = _original()
    reply = _build(original, provider_thread_id="  18c2f0a9d1e4b7aa ")
    assert reply.thread_id == "18c2f0a9d1e4b7aa"
    assert reply.thread_id != str(original.thread_id)


@pytest.mark.parametrize("provider_thread_id", [None, "", "   "])
def test_missing_provider_thread_id_is_permanent(provider_thread_id: str | None) -> None:
    """Migration 0003 allows a NULL provider thread id; dispatch must dead-letter (6.3)."""
    original = _original()
    with pytest.raises(MissingProviderThreadError) as exc_info:
        _build(original, provider_thread_id=provider_thread_id)
    assert isinstance(exc_info.value, ValueError)
    assert exc_info.value.draft_id == str(DRAFT_ID)
    assert exc_info.value.thread_id == str(original.thread_id)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Order status question", "Re: Order status question"),
        ("Re: Order status question", "Re: Order status question"),
        ("RE: Order status question", "Re: Order status question"),
        ("re:Order status question", "Re: Order status question"),
        ("Re: RE: re: Order status question", "Re: Order status question"),
        ("  Re :  Order status question  ", "Re: Order status question"),
        ("Regarding the invoice", "Re: Regarding the invoice"),
        ("Reorder request", "Re: Reorder request"),
        ("", "Re:"),
    ],
)
def test_subject_has_exactly_one_re_prefix(subject: str, expected: str) -> None:
    assert reply_subject(subject) == expected
    assert _build(_original(subject=subject)).subject == expected


def test_draft_subject_is_not_used() -> None:
    """The provider keeps the reply in the thread only when the subject matches the original."""
    assert _build().subject == "Re: Order status question"


def test_original_without_references() -> None:
    reply = _build(_original(references_ids=[]))
    assert reply.in_reply_to == "<orig-1@clientcorp.com>"
    assert reply.references == ["<orig-1@clientcorp.com>"]


def test_original_without_message_id() -> None:
    """Review focus 1: no Message-ID on the original still builds a threaded reply."""
    reply = _build(_original(rfc822_message_id=None))
    assert reply.in_reply_to is None
    assert reply.references == ["<root-0@clientcorp.com>"]
    assert reply.thread_id == "th-provider-77"
    assert reply.subject.startswith("Re: ")
    assert reply.message_id == OUR_MESSAGE_ID


def test_references_are_deduplicated_bracketed_and_end_with_the_parent() -> None:
    original = _original(
        references_ids=[
            "root-0@clientcorp.com",
            "<mid-1@clientcorp.com>",
            "root-0@clientcorp.com",
            "orig-1@clientcorp.com",
            "  ",
        ]
    )
    reply = _build(original)
    assert reply.references == [
        "<root-0@clientcorp.com>",
        "<mid-1@clientcorp.com>",
        "<orig-1@clientcorp.com>",
    ]


def test_quoted_original_sits_below_the_reply() -> None:
    reply = _build()
    assert reply.body_text == (
        "Hello Alice,\nYour order ORD-82915 has been dispatched.\n"
        "\n"
        "On 2026-09-28 09:30 UTC, Alice Smith <alice.smith@clientcorp.com> wrote:\n"
        "> Where is my order ORD-82915?\n"
        ">\n"
        "> Thanks,\n"
        "> Alice\n"
    )


def test_quote_header_is_in_utc_whatever_the_original_zone() -> None:
    plus_seven = timezone(timedelta(hours=7))
    reply = _build(_original(received_at=datetime(2026, 9, 28, 16, 30, tzinfo=plus_seven)))
    assert "On 2026-09-28 09:30 UTC, Alice Smith" in reply.body_text


def test_empty_original_body_still_quotes_a_marker() -> None:
    reply = _build(_original(body_text=""))
    assert reply.body_text.endswith("wrote:\n>\n")


def test_sender_is_the_default_recipient() -> None:
    reply = _build()
    assert reply.to == [EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith")]


def test_reply_goes_to_the_sender_even_with_a_reply_to_header() -> None:
    """Reply-To is not persisted on the dispatch path, so the builder does not read it."""
    reply = _build(_original(headers={"reply-to": "billing@clientcorp.com"}))
    assert reply.to == [EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith")]


def test_no_recipient_address_is_permanent() -> None:
    with pytest.raises(MissingRecipientError) as exc_info:
        _build(_original(sender=EmailAddress(email="  ")))
    assert isinstance(exc_info.value, ValueError)
    assert exc_info.value.draft_id == str(DRAFT_ID)


def test_message_id_is_deterministic_per_draft() -> None:
    """design.md §5.8 step 4: a retry rebuilds the same Message-ID so the sent copy is findable."""
    first = _build()
    second = _build()
    other = _build(draft=_draft(uuid4()))
    assert first.message_id == second.message_id == OUR_MESSAGE_ID
    assert other.message_id != first.message_id
    assert build_reply_message_id(str(DRAFT_ID), "@acme.example") == OUR_MESSAGE_ID


@pytest.mark.parametrize("domain", ["", "   ", "bad domain", "a@b.example", "<acme.example>"])
def test_invalid_message_id_domain_is_rejected(domain: str) -> None:
    with pytest.raises(ValueError, match="message_id_domain"):
        build_reply_message_id(DRAFT_ID, domain)


def test_building_a_reply_does_not_mutate_the_inputs() -> None:
    original = _original()
    draft = _draft()
    before = (copy.deepcopy(original), copy.deepcopy(draft))
    build_outbound_reply(
        draft=draft,
        original=original,
        provider_thread_id="th-provider-77",
        message_id_domain=DOMAIN,
    )
    assert (original, draft) == before
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_outbound_reply.py -v`

Expected: FAIL at collection with `ModuleNotFoundError: No module named 'packages.dispatch'`.

- [ ] **Step 3: Implement the builder**

Create `packages/dispatch/reply.py`:

```python
"""Pure construction of the outbound reply for a reviewed draft (R17.2, design.md §5.8).

Requirements:
- R17.2: correct threading headers (In-Reply-To, References) and the provider thread id.
- design.md §5.8 "Outbound reply": exactly one "Re: " before the original subject, a new
  Message-ID of our own (MIME-built replies only), the provider thread id (never our UUID),
  and the quoted original below the reply.
- ADR-0009: a null provider thread id dead-letters the dispatch.

Stored ids (email_message.rfc822_message_id, in_reply_to, references_ids) carry no angle
brackets (services/email_worker/parser.py strips them). Every id on the returned
OutboundReply is wrapped in <...>, ready for MIME headers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from packages.domain.entities import (
    EmailAddress,
    GeneratedDraft,
    NormalizedMessage,
    OutboundReply,
)

_REPLY_PREFIX = re.compile(r"^\s*(?:re\s*:\s*)+", re.IGNORECASE)
_DOMAIN_FORBIDDEN = re.compile(r"[<>@\s]")


class MissingProviderThreadError(ValueError):
    """The original's thread has no provider thread id, so the reply cannot join it.

    Permanent: the dispatch dead-letters (design.md §5.8 errors, ADR-0009).
    """

    def __init__(self, *, draft_id: str, thread_id: str) -> None:
        self.draft_id = draft_id
        self.thread_id = thread_id
        super().__init__(
            f"Draft {draft_id}: email_thread {thread_id} has no provider thread id; "
            "the reply cannot be threaded"
        )


class MissingRecipientError(ValueError):
    """The original's sender has no address to reply to. Permanent."""

    def __init__(self, *, draft_id: str) -> None:
        self.draft_id = draft_id
        super().__init__(f"Draft {draft_id}: the original email has no address to reply to")


def reply_subject(original_subject: str) -> str:
    """Return the original subject with exactly one leading ``"Re: "`` (R17.2)."""
    base = _REPLY_PREFIX.sub("", original_subject or "").strip()
    return f"Re: {base}" if base else "Re:"


def build_reply_message_id(draft_id: UUID | str, message_id_domain: str) -> str:
    """Return the deterministic Message-ID ``<dispatch-{draft id hex}@{domain}>``.

    Deterministic per draft, so a redelivered dispatch rebuilds the same header and the
    sent copy can be found by it after an ambiguous send (design.md §5.8 step 4).
    """
    domain = message_id_domain.strip().lstrip("@")
    if not domain or _DOMAIN_FORBIDDEN.search(domain):
        raise ValueError(f"Invalid message_id_domain {message_id_domain!r}")
    return f"<dispatch-{UUID(str(draft_id)).hex}@{domain}>"


def _clean_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("<>").strip()
    return cleaned or None


def _bracket(value: str) -> str:
    return f"<{value}>"


def _references(original: NormalizedMessage, parent_id: str | None) -> list[str]:
    ordered: list[str] = []
    for ref in original.references_ids:
        cleaned = _clean_id(ref)
        if cleaned and cleaned != parent_id and cleaned not in ordered:
            ordered.append(cleaned)
    if parent_id:
        ordered.append(parent_id)
    return [_bracket(ref) for ref in ordered]


def _recipients(original: NormalizedMessage, draft_id: str) -> list[EmailAddress]:
    # Reply-To is not persisted (email_message has no headers column), so the reply goes to
    # the sender; reading original.headers here would work only in unit tests.
    if original.sender.email.strip():
        return [original.sender]
    raise MissingRecipientError(draft_id=draft_id)


def _quote(original: NormalizedMessage) -> str:
    received: datetime = original.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    when = received.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    text = (original.body_text or "").replace("\r\n", "\n").rstrip("\n")
    lines = text.split("\n") if text else [""]
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return f"On {when}, {original.sender} wrote:\n{quoted}\n"


def build_outbound_reply(
    *,
    draft: GeneratedDraft,
    original: NormalizedMessage,
    provider_thread_id: str | None,
    message_id_domain: str,
) -> OutboundReply:
    """Build the reply to ``original`` carrying ``draft``'s body (R17.2, design.md §5.8).

    Raises:
        MissingProviderThreadError: ``provider_thread_id`` is None or blank.
        MissingRecipientError: the sender has no address.
        ValueError: ``message_id_domain`` is not a bare domain.
    """
    draft_id = str(draft.id)
    thread = (provider_thread_id or "").strip()
    if not thread:
        raise MissingProviderThreadError(draft_id=draft_id, thread_id=str(original.thread_id))

    parent_id = _clean_id(original.rfc822_message_id)
    return OutboundReply(
        thread_id=thread,
        mailbox_id=original.mailbox_id,
        organization_id=original.organization_id,
        to=_recipients(original, draft_id),
        body_text=f"{draft.body.rstrip()}\n\n{_quote(original)}",
        subject=reply_subject(original.subject),
        in_reply_to=_bracket(parent_id) if parent_id else None,
        references=_references(original, parent_id),
        draft_id=draft_id,
        message_id=build_reply_message_id(draft.id, message_id_domain),
        reply_to_provider_message_id=original.provider_message_id,
    )
```

Create `packages/dispatch/__init__.py`:

```python
"""Dispatch: outbound reply construction and the exactly-once dispatch flow (R17, ADR-0009)."""

from packages.dispatch.reply import (
    MissingProviderThreadError,
    MissingRecipientError,
    build_outbound_reply,
    build_reply_message_id,
    reply_subject,
)

__all__ = [
    "MissingProviderThreadError",
    "MissingRecipientError",
    "build_outbound_reply",
    "build_reply_message_id",
    "reply_subject",
]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_outbound_reply.py tests/unit/test_dependency_rules.py -v`

Expected: PASS (the dependency rules confirm `packages/dispatch` imports no service and no provider module).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format packages/dispatch tests/unit/test_outbound_reply.py && uv run ruff check . && uv run mypy packages services tests evaluation`

Expected: `All checks passed!` and `Success: no issues found in N source files`.

- [ ] **Step 6: Commit**

```bash
git add packages/dispatch/__init__.py packages/dispatch/reply.py tests/unit/test_outbound_reply.py
git commit -m "$(cat <<'EOF'
feat(dispatch): pure outbound reply construction [task 6.3] [R17.2]

build_outbound_reply sets In-Reply-To and References from the original,
exactly one "Re: " prefix, a deterministic Message-ID per draft, the provider
thread id (never our UUID) and the quoted original below the reply. A missing
provider thread id or recipient raises a permanent error for dead-lettering.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Dispatch mode per category [tasks.md 6.4]

**Files:**
- Modify: `packages/domain/dispatch.py` (append `DEFAULT_DISPATCH_MODE`, `parse_dispatch_mode`)
- Modify: `packages/domain/taxonomy.py` (imports lines 11–15; `CategoryDefinition` lines 31–58; `register_from_dict` lines 250–266; new `TaxonomyRegistry.dispatch_mode_for` after `all_categories`; new module function `get_dispatch_mode` after `get_category_definition`)
- Modify: `packages/domain/__init__.py` (dispatch and taxonomy import blocks, `__all__`)
- Modify: `config/categories.yaml` (header comment and all nine entries)
- Modify: `docs/configuration.md` (§2.16, after the `ROUTING__CONFIGURED_CONSUMERS` row, line 275)
- Test: `tests/unit/test_category_taxonomy.py` (imports lines 16–29; new class), `tests/unit/test_category_routing.py` (imports lines 10–28; new tests in `TestDeclarativeCategoryConfiguration`)

**Interfaces:**
- Consumes: `DispatchMode` (Task 1); `TaxonomyRegistry.get/register_from_dict`, `load_categories_from_yaml(path, registry=None)` (packages/broker/routing.py:108).
- Produces:
  - `packages.domain.dispatch.DEFAULT_DISPATCH_MODE: DispatchMode = DispatchMode.CREATE_DRAFT`
  - `packages.domain.dispatch.parse_dispatch_mode(value: object) -> DispatchMode` — `None`/blank → default; case-insensitive; unknown → `ValueError("Unknown dispatch_mode ...")`
  - `CategoryDefinition.dispatch_mode: DispatchMode = DispatchMode.CREATE_DRAFT`; `to_dict()["dispatch_mode"]` is the string value
  - `TaxonomyRegistry.dispatch_mode_for(category: str | Category) -> DispatchMode` — resolves aliases; an unregistered category gets `create_draft`
  - `packages.domain.taxonomy.get_dispatch_mode(category: str | Category) -> DispatchMode` (default registry; re-exported from `packages.domain`)
  - YAML: every category carries `dispatch_mode: create_draft` and `auto_send_eligible: false`.

Design decisions:
- An unknown `dispatch_mode` value fails at load (`register_from_dict` raises, `load_categories_from_yaml` propagates it, so the worker does not start). A silent fallback could turn a typo into the wrong posture.
- An unregistered category dispatches as `create_draft`: the safe, human-sends posture (R16.8).
- R17.6 needs no new gate here: no code path sends without an approval (dispatch starts only from `POST /v1/drafts/{id}/approve`, part B/C), and `auto_send_eligible` is `false` for every category. The YAML now states both keys explicitly so the posture is visible where it is configured.
- The dispatch-worker gets the YAML through `WorkerRuntime` → `setup_topology` → `load_categories_from_yaml` (packages/broker/topology.py:89–90). The API process does not load it through topology, so Task 6 Step 10 loads the same file into the API's own registry in `create_app` (`app.state.taxonomy`), and `GET /v1/drafts/{id}` reports that mode. Task 12's gate checks the API field too.

- [ ] **Step 1: Write the failing taxonomy tests**

In `tests/unit/test_category_taxonomy.py`, add `from packages.domain.dispatch import DispatchMode, parse_dispatch_mode` after `import pytest` (keeping isort order: this line goes before `from packages.domain.taxonomy import (`), and add `get_dispatch_mode,` to the `packages.domain.taxonomy` import list after `get_default_registry,`. Append:

```python
class TestDispatchMode:
    """6.4: dispatch_mode per category, default create_draft (R17.1, R17.6, R16.8)."""

    def test_every_canonical_category_defaults_to_create_draft(self) -> None:
        for defn in CANONICAL_DEFINITIONS.values():
            assert defn.dispatch_mode is DispatchMode.CREATE_DRAFT
            assert defn.auto_send_eligible is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("send_reply", DispatchMode.SEND_REPLY),
            (" SEND_REPLY ", DispatchMode.SEND_REPLY),
            ("create_draft", DispatchMode.CREATE_DRAFT),
            (None, DispatchMode.CREATE_DRAFT),
            ("", DispatchMode.CREATE_DRAFT),
            ("   ", DispatchMode.CREATE_DRAFT),
        ],
    )
    def test_parse_dispatch_mode(self, raw: str | None, expected: DispatchMode) -> None:
        assert parse_dispatch_mode(raw) is expected

    @pytest.mark.parametrize("raw", ["auto_send", "send", "draft", 1])
    def test_unknown_dispatch_mode_is_rejected(self, raw: object) -> None:
        with pytest.raises(ValueError, match="Unknown dispatch_mode"):
            parse_dispatch_mode(raw)

    def test_register_from_dict_reads_dispatch_mode(self) -> None:
        registry = TaxonomyRegistry()
        defn = registry.register_from_dict({"category": "refunds", "dispatch_mode": "send_reply"})
        assert defn.dispatch_mode is DispatchMode.SEND_REPLY
        assert registry.dispatch_mode_for("refunds") is DispatchMode.SEND_REPLY

    def test_register_from_dict_without_dispatch_mode_defaults(self) -> None:
        registry = TaxonomyRegistry()
        defn = registry.register_from_dict({"category": "refunds"})
        assert defn.dispatch_mode is DispatchMode.CREATE_DRAFT

    def test_register_from_dict_names_the_category_on_a_bad_mode(self) -> None:
        registry = TaxonomyRegistry()
        with pytest.raises(ValueError, match="Category 'refunds': Unknown dispatch_mode"):
            registry.register_from_dict({"category": "refunds", "dispatch_mode": "auto_send"})
        assert not registry.is_valid("refunds")

    def test_to_dict_includes_dispatch_mode(self) -> None:
        assert CANONICAL_DEFINITIONS["billing"].to_dict()["dispatch_mode"] == "create_draft"

    def test_dispatch_mode_for_resolves_aliases_and_defaults_unknown(self) -> None:
        registry = TaxonomyRegistry()
        registry.register_from_dict(
            {"category": "billing", "aliases": ["invoice"], "dispatch_mode": "send_reply"}
        )
        assert registry.dispatch_mode_for("billing") is DispatchMode.SEND_REPLY
        assert registry.dispatch_mode_for("Invoice") is DispatchMode.SEND_REPLY
        assert registry.dispatch_mode_for("partnerships") is DispatchMode.CREATE_DRAFT

    def test_module_level_lookup_uses_the_default_registry(self) -> None:
        assert get_dispatch_mode(Category.SUPPORT) is get_default_registry().dispatch_mode_for(
            "support"
        )
```

- [ ] **Step 2: Write the failing YAML tests**

In `tests/unit/test_category_routing.py`, add `import yaml` after `import pytest`, and `from packages.domain.dispatch import DispatchMode` before `from packages.domain.entities import Classification`. Append to `TestDeclarativeCategoryConfiguration`:

```python
    def test_default_yaml_states_create_draft_and_no_auto_send(self) -> None:
        """6.4: every shipped category says create_draft and auto_send_eligible: false."""
        config_path = Path("config/categories.yaml")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        entries = raw["categories"]
        assert len(entries) == 9
        for entry in entries:
            assert entry["dispatch_mode"] == "create_draft", entry["category"]
            assert entry["auto_send_eligible"] is False, entry["category"]

        registry = TaxonomyRegistry()
        for defn in load_categories_from_yaml(config_path, registry=registry):
            assert defn.dispatch_mode is DispatchMode.CREATE_DRAFT
            assert registry.dispatch_mode_for(defn.category) is DispatchMode.CREATE_DRAFT

    def test_yaml_can_switch_a_category_to_send_reply(self) -> None:
        with TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "categories.yaml"
            yaml_path.write_text(
                "categories:\n"
                "  - category: billing\n"
                "    dispatch_mode: send_reply\n"
                "    aliases: [invoice]\n",
                encoding="utf-8",
            )
            registry = TaxonomyRegistry()
            load_categories_from_yaml(yaml_path, registry=registry)
            assert registry.dispatch_mode_for("billing") is DispatchMode.SEND_REPLY
            assert registry.dispatch_mode_for("support") is DispatchMode.CREATE_DRAFT

    def test_yaml_with_an_unknown_dispatch_mode_fails_the_load(self) -> None:
        with TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "categories.yaml"
            yaml_path.write_text(
                "categories:\n  - category: billing\n    dispatch_mode: auto_send\n",
                encoding="utf-8",
            )
            with pytest.raises(ValueError, match="Unknown dispatch_mode"):
                load_categories_from_yaml(yaml_path, registry=TaxonomyRegistry())
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_category_taxonomy.py tests/unit/test_category_routing.py -v`

Expected: FAIL — `test_category_taxonomy.py` errors at collection with `ImportError: cannot import name 'parse_dispatch_mode' from 'packages.domain.dispatch'`; `test_default_yaml_states_create_draft_and_no_auto_send` fails with `KeyError: 'dispatch_mode'`; the switch test fails with `AttributeError: 'TaxonomyRegistry' object has no attribute 'dispatch_mode_for'`; the unknown-mode test fails with `Failed: DID NOT RAISE <class 'ValueError'>`.

- [ ] **Step 4: Add the parser**

Append to `packages/domain/dispatch.py`:

```python
DEFAULT_DISPATCH_MODE: DispatchMode = DispatchMode.CREATE_DRAFT


def parse_dispatch_mode(value: object) -> DispatchMode:
    """Parse a configured dispatch mode (R17.1, R16.8).

    None or blank means the default ``create_draft``; matching ignores case and surrounding
    space. Anything else raises ``ValueError`` so a typo never changes the posture silently.
    """
    if value is None:
        return DEFAULT_DISPATCH_MODE
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_DISPATCH_MODE
    try:
        return DispatchMode(text)
    except ValueError as err:
        allowed = ", ".join(mode.value for mode in DispatchMode)
        raise ValueError(f"Unknown dispatch_mode {value!r}; expected one of: {allowed}") from err
```

- [ ] **Step 5: Carry dispatch_mode through the taxonomy**

In `packages/domain/taxonomy.py`:

Add after `from typing import Any`:

```python

from packages.domain.dispatch import DEFAULT_DISPATCH_MODE, DispatchMode, parse_dispatch_mode
```

In `CategoryDefinition`, after `aliases: tuple[str, ...] = field(default_factory=tuple)` add:

```python
    dispatch_mode: DispatchMode = DispatchMode.CREATE_DRAFT  # R17.1, R16.8 (6.4)
```

and in `to_dict`, after `"aliases": list(self.aliases),` add:

```python
            "dispatch_mode": self.dispatch_mode.value,
```

Replace the body of `register_from_dict` from `category = str(data["category"]).strip().lower()` to `return defn` with:

```python
        category = str(data["category"]).strip().lower()
        try:
            dispatch_mode = parse_dispatch_mode(data.get("dispatch_mode"))
        except ValueError as err:
            raise ValueError(f"Category '{category}': {err}") from err
        defn = CategoryDefinition(
            category=category,
            description=str(data.get("description", "")),
            default_reply_required=bool(data.get("default_reply_required", True)),
            default_retrieval_required=bool(data.get("default_retrieval_required", True)),
            default_workflow_hint=str(data.get("default_workflow_hint", "ai")),
            default_priority=str(data.get("default_priority", "normal")),
            intents=tuple(data.get("intents", ())),
            auto_send_eligible=bool(data.get("auto_send_eligible", False)),
            aliases=tuple(data.get("aliases", ())),
            dispatch_mode=dispatch_mode,
        )
        self.register_category(defn)
        return defn
```

Add to `TaxonomyRegistry`, after `all_categories`:

```python
    def dispatch_mode_for(self, category: str | Category) -> DispatchMode:
        """Dispatch mode for a category or alias; unregistered ones get create_draft (R16.8)."""
        defn = self.get(category)
        return defn.dispatch_mode if defn is not None else DEFAULT_DISPATCH_MODE
```

Append after `get_category_definition`:

```python


def get_dispatch_mode(category: str | Category) -> DispatchMode:
    """Dispatch mode for a category from the default registry (R17.1, R16.8)."""
    return _DEFAULT_REGISTRY.dispatch_mode_for(category)
```

In `packages/domain/__init__.py`, change the dispatch import block from Task 1 to:

```python
from packages.domain.dispatch import (
    DEFAULT_DISPATCH_MODE,
    DispatchMode,
    ProviderDraftStatus,
    parse_dispatch_mode,
)
```

add `get_dispatch_mode,` to the `packages.domain.taxonomy` import list after `get_default_registry,`, and add to `__all__`: `"DEFAULT_DISPATCH_MODE",` after `"CANONICAL_DEFINITIONS",`; `"get_dispatch_mode",` after `"get_default_registry",`; `"parse_dispatch_mode",` after `"normalize_category",`.

- [ ] **Step 6: State the mode in config/categories.yaml**

Replace the header comment (lines 1–4) with:

```yaml
# Declarative Category Taxonomy Configuration (R6.4, R7.4, R17.1, R17.6, R16.8)
# Allows adding new categories or overriding defaults without code changes.
# Any category defined here will automatically have its corresponding queues
# (email.<category>.normal, email.<category>.priority) declared at startup.
#
# dispatch_mode: how an approved draft leaves the system (design.md §5.8, ADR-0009).
#   create_draft  the reply becomes a draft in the provider mailbox; a person sends it (default)
#   send_reply    the reply is sent after approval
# auto_send_eligible: sending without an approval (R17.6). false for every category.
# An unknown dispatch_mode value stops the service at startup.
```

Then, in each of the nine entries (`support`, `sales`, `billing`, `administration`, `scheduling`, `general_inquiry`, `automated_notification`, `acknowledgement`, `no_response`), insert these two lines directly after the entry's `default_priority:` line, at the same indentation:

```yaml
    auto_send_eligible: false
    dispatch_mode: create_draft
```

For example, the `support` entry becomes:

```yaml
  - category: support
    description: "Technical bugs, crashes, incidents, or hardware/software troubleshooting."
    default_reply_required: true
    default_retrieval_required: true
    default_workflow_hint: ai
    default_priority: normal
    auto_send_eligible: false
    dispatch_mode: create_draft
    intents:
      - bug_report
      - incident
      - technical_troubleshooting
      - feature_assistance
    aliases:
      - technical_support
      - tech_support
      - bug
      - issue
      - troubleshooting
```

Verify all nine got both keys: `grep -c 'dispatch_mode: create_draft' config/categories.yaml` → `9`, `grep -c 'auto_send_eligible: false' config/categories.yaml` → `9`.

- [ ] **Step 7: Document the keys**

In `docs/configuration.md`, insert after the `ROUTING__CONFIGURED_CONSUMERS` row of §2.16 (line 275), before `### 2.17`:

```markdown

#### Per-category dispatch mode (`config/categories.yaml`)
*How an approved draft leaves the system (R17.1, R17.6, R16.8, design.md §5.8, ADR-0009). These are YAML keys on each category entry, not environment variables.*

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `dispatch_mode` | `string` | `create_draft` | `create_draft` or `send_reply` (case-insensitive); any other value stops the service at startup | `create_draft`: the approved reply becomes a draft in the provider mailbox and a person sends it. `send_reply`: the approved reply is sent. Every shipped category uses `create_draft`. |
| `auto_send_eligible` | `boolean` | `false` | Boolean | Sending without an approval (R17.6). `false` for every category; dispatch starts only from an approval (`POST /v1/drafts/{id}/approve`). |

A category the classifier returns that is not registered dispatches as `create_draft`. Workers read the file at startup when they declare the broker topology (`ROUTING__CATEGORIES_CONFIG_PATH`), so a change needs a restart of the dispatch-worker.
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/unit/test_category_taxonomy.py tests/unit/test_category_routing.py tests/unit/test_dispatch_domain.py tests/unit/test_dependency_rules.py -v`

Expected: PASS.

- [ ] **Step 9: Format, lint, type-check, full unit suite**

Run: `uv run ruff format packages tests && uv run ruff check . && uv run mypy packages services tests evaluation && uv run pytest tests/unit -q`

Expected: `All checks passed!`, `Success: no issues found in N source files`, and the unit suite passes.

- [ ] **Step 10: Commit**

```bash
git add packages/domain/dispatch.py packages/domain/taxonomy.py packages/domain/__init__.py \
  config/categories.yaml docs/configuration.md \
  tests/unit/test_category_taxonomy.py tests/unit/test_category_routing.py
git commit -m "$(cat <<'EOF'
feat(dispatch): dispatch_mode per category, default create_draft [task 6.4] [R17.1, R17.6, R16.8]

Every category in config/categories.yaml states dispatch_mode: create_draft
and auto_send_eligible: false. The taxonomy parses the key (an unknown value
fails the load), exposes dispatch_mode_for / get_dispatch_mode, and treats an
unregistered category as create_draft. docs/configuration.md documents both keys.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

---

## Part B (Tasks 4–5): Adapter fixes and draft operations [tasks.md 6.3a]

Skills applied to this part: `fullstack-dev-skills:python-pro` (strict typing, dataclasses, pytest and `httpx.MockTransport` fixtures, mypy strict over `tests/`).

The shapes Part B produces:

```
                         MailProviderAdapter (packages/adapters/protocol.py)
   create_draft ─▶ DraftRef(provider_draft_id, provider_message_id, provider_thread_id)
   send_draft   ─▶ SentRef                        (new, 6.3a)
   get_draft_status ─▶ ProviderDraftStatus.DRAFT | SENT | MISSING   (new)
   find_sent_message(thread, key) ─▶ SentRef | None                  (new)
   find_draft(thread, our Message-ID) ─▶ DraftRef | None (unsent)    (new, orphan-draft lookup)

   Gmail                                   Graph (Prefer: IdType="ImmutableId")
   POST drafts            (raw MIME with    POST messages/{orig}/createReply
        Message-ID, threadId)               POST messages/{draft}/send   (202)
   POST drafts/send  ─▶ NEW message id      GET  messages/{draft}?$select=isDraft
   GET  drafts/{id}  200 DRAFT / 404 MISSING   isDraft true DRAFT / false SENT / 404 MISSING
   GET  threads/{t}?format=metadata         GET  messages?$filter=conversationId eq '…'
        match SENT label + Message-ID            match isDraft=false + id | internetMessageId
   GET  drafts?q=rfc822msgid:<id>           createReply sets internetMessageId = our Message-ID;
        (find_draft)                             find_draft matches isDraft=true + internetMessageId

   errors (packages/adapters/exceptions.py)
   ProviderError
     ├─ RetryableProviderError (retry_after_s)  ─┬─ RateLimited (429, Gmail 403 rate-limit reasons)
     │                                           └─ Transient   (5xx, network; Retry-After kept)
     └─ PermanentProviderError                  ─┬─ AuthExpired (401, other 403)
                                                 ├─ NotFound    (404)
                                                 └─ Permanent   (400, other 4xx, Gmail 403 dailyLimitExceeded)
```

**Names Part B binds (for Parts C/D):**
- Retryable type: `packages.adapters.exceptions.RetryableProviderError`, attribute `retry_after_s: float | None`. Its subclasses are `RateLimited`, which also keeps its existing `retry_after` attribute with the same value, and `Transient`.
- Permanent type: `packages.adapters.exceptions.PermanentProviderError`. Its subclasses are `AuthExpired`, `NotFound` and `Permanent`.
- `packages.adapters.exceptions.parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None`.
- The OutboundReply thread field is **`thread_id: str`**, the existing name retyped by Part A and documented as the provider thread id. Part B also needs `message_id: str | None` (Part A) and adds **`reply_to_provider_message_id: str | None = None`**, the original email's provider message id, which Graph `createReply` needs.
- The `find_sent_message` key is matched against **either** the provider message id **or** the RFC 5322 `Message-ID` (angle brackets optional). The dispatch rule (for Part C) is to call it first with `generated_draft.provider_draft_message_id`, then, if that returns `None` and the reply carries a `message_id`, call it again with that `Message-ID`. Graph finds the sent copy by its immutable draft id. Gmail finds it only by our `Message-ID`, because `drafts.send` gives the sent message a new id.
- **`find_draft(mailbox, provider_thread_id, message_id) -> DraftRef | None`** finds an **unsent** draft in the thread carrying our deterministic `Message-ID`. Part C calls it before `create_draft` whenever the job was not freshly `DRAFTED` and no handle is stored, so a crash between the provider accepting `create_draft` and the handle being recorded never leaves two provider drafts (tasks.md 6.5). Gmail searches `drafts.list?q=rfc822msgid:<id>`. Graph cannot search a draft by a `Message-ID` it chose itself, so `create_draft` sets `message.internetMessageId` to our `Message-ID` in the `createReply` body (Graph "Update message": `internetMessageId` is updatable while `isDraft = true`; `createReply`'s `message` takes writable properties). That differs from design §5.8's "Graph's `createReply` sets `internetMessageId` itself", which is open question **D3 (blocks Task 5 Step 5: the `internetMessageId` line of Graph `create_draft` and Graph `find_draft`)**.

---

### Task 4: Gmail adapter: Message-ID, draft send/status/lookup, retryable vs permanent errors [tasks.md 6.3a]

**Files:**
- Modify: `packages/adapters/exceptions.py`. Keep the whole file, add two marker bases and `parse_retry_after`, and re-parent the five existing classes. The existing constructors are unchanged.
- Modify: `packages/adapters/__init__.py`. Add `PermanentProviderError`, `RetryableProviderError` and `parse_retry_after` to the imports and to the sorted `__all__`.
- Modify: `packages/adapters/gmail.py`:
  - imports, lines 10–39;
  - `build_rfc822_mime`, lines 103–122: add `Message-ID`;
  - `_request`, lines 142–219: move error mapping into a new pure `classify_gmail_error`;
  - `create_draft`, lines 406–426: require both ids;
  - add `send_draft`, `get_draft_status`, `find_sent_message` and `find_draft` after `send_reply`, line 446.
- Test: `tests/unit/test_adapter_exceptions.py` (append), `tests/unit/test_gmail_adapter.py` (imports lines 9–33; append new tests).

**Interfaces:**
- Consumes (from Part A, tasks.md 6.3 domain changes):
  - `packages.domain.ProviderDraftStatus` (StrEnum `DRAFT`/`SENT`/`MISSING`);
  - `OutboundReply.thread_id: str` (provider thread id) and `OutboundReply.message_id: str | None`.
- Produces:
  - `class RetryableProviderError(ProviderError)`: `__init__(self, message: str = "", *, retry_after_s: float | None = None, provider: str | None = None, mailbox_id: str | None = None, raw_error: Any = None)`, attribute `retry_after_s`.
  - `class PermanentProviderError(ProviderError)`.
  - `RateLimited(RetryableProviderError)` with `retry_after` equal to `retry_after_s`; `Transient(RetryableProviderError)`; `AuthExpired`, `NotFound` and `Permanent` all subclass `PermanentProviderError`.
  - `DEFAULT_RETRY_AFTER_S: float = 60.0` and `def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None`.
  - `packages.adapters.gmail.classify_gmail_error(status: int, *, retry_after_header: str | None, raw_payload: Any, mailbox_id: str | None) -> ProviderError`.
  - On `GmailProviderAdapter`:
    - `async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef`
    - `async def get_draft_status(self, mailbox: Mailbox, provider_draft_id: str) -> ProviderDraftStatus`
    - `async def find_sent_message(self, mailbox: Mailbox, provider_thread_id: str, provider_message_id: str) -> SentRef | None`
    - `async def find_draft(self, mailbox: Mailbox, provider_thread_id: str, message_id: str) -> DraftRef | None` (`drafts.list?q=rfc822msgid:<id>`; the orphan-draft lookup of tasks.md 6.5)
  - `build_rfc822_mime(reply)` writes `Message-ID: <…>` whenever `reply.message_id` is set, adding the angle brackets if they are missing.

- [ ] **Step 0: Confirm the Part A domain prerequisites exist**

Run: `uv run python -c "from packages.domain import ProviderDraftStatus; from packages.domain.entities import OutboundReply; assert 'message_id' in OutboundReply.__dataclass_fields__; assert OutboundReply.__dataclass_fields__['thread_id'].type in ('str', str); print(sorted(ProviderDraftStatus))"`
Expected: `['DRAFT', 'MISSING', 'SENT']`. An ImportError or AssertionError means Part A's domain task has not landed yet. Stop and run that task first.

- [ ] **Step 1: Write the failing exception tests**

Append to `tests/unit/test_adapter_exceptions.py`. Also add `from datetime import UTC, datetime` at the top of the file, and extend the `packages.adapters.exceptions` import to `AuthExpired, DEFAULT_RETRY_AFTER_S, NotFound, Permanent, PermanentProviderError, ProviderError, RateLimited, RetryableProviderError, Transient, parse_retry_after`:

```python
def test_retryable_and_permanent_marker_bases() -> None:
    """6.3a / R17.5: dispatch classifies provider errors by two bases, not five classes."""
    for retryable in (RateLimited, Transient):
        assert issubclass(retryable, RetryableProviderError)
        assert not issubclass(retryable, PermanentProviderError)
    for permanent in (AuthExpired, NotFound, Permanent):
        assert issubclass(permanent, PermanentProviderError)
        assert not issubclass(permanent, RetryableProviderError)
    assert issubclass(RetryableProviderError, ProviderError)
    assert issubclass(PermanentProviderError, ProviderError)


def test_rate_limited_exposes_retry_after_s_alias() -> None:
    """R1.6 / 6.6: RateLimited keeps retry_after and exposes the same value as retry_after_s."""
    err = RateLimited("slow down", retry_after=42.0, provider="gmail")
    assert err.retry_after == 42.0
    assert err.retry_after_s == 42.0
    assert RateLimited().retry_after_s is None


def test_transient_carries_optional_retry_after_s() -> None:
    """6.6: a 503 with Retry-After keeps the hint; the default is None."""
    assert Transient("busy", retry_after_s=120.0).retry_after_s == 120.0
    assert Transient("busy").retry_after_s is None


def test_parse_retry_after_delta_seconds() -> None:
    """RFC 9110 delay-seconds form."""
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after("-5") == 0.0


def test_parse_retry_after_http_date() -> None:
    """RFC 9110 HTTP-date form is measured from `now`; a past date means retry now."""
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Mon, 28 Sep 2026 12:02:00 GMT", now=now) == 120.0
    assert parse_retry_after("Mon, 28 Sep 2026 11:00:00 GMT", now=now) == 0.0


def test_parse_retry_after_absent_or_garbage() -> None:
    """No header means no hint; an unparseable header keeps the historical 60 s fallback."""
    assert parse_retry_after(None) is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("soon") == DEFAULT_RETRY_AFTER_S
    assert parse_retry_after("nan") == DEFAULT_RETRY_AFTER_S
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_adapter_exceptions.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'DEFAULT_RETRY_AFTER_S' from 'packages.adapters.exceptions'`.

- [ ] **Step 3: Implement the exception changes**

Replace `packages/adapters/exceptions.py` with:

```python
"""Provider exception taxonomy.

Requirements:
- R1.5: Common error taxonomy: RateLimited, AuthExpired, NotFound, Transient, Permanent.
- R1.6: RateLimited carries optional provider-supplied retry_after (seconds).
- R17.5: Dispatch tells retryable from permanent provider failures (task 6.3a).
- design.md §5.1: Error taxonomy and failure handling modes.
- design.md §5.8: 429/5xx/rate-limit 403 retry with Retry-After; 400/404/auth dead-letter.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

# Kept from the pre-6.3a adapters: an unparseable Retry-After still waits a minute.
DEFAULT_RETRY_AFTER_S = 60.0


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP Retry-After header into seconds (R1.6, RFC 9110 §10.2.3).

    Accepts delay-seconds ("30") and HTTP-date ("Mon, 28 Sep 2026 12:02:00 GMT").
    Returns None when the header is absent or blank, 0.0 for a past date, and
    DEFAULT_RETRY_AFTER_S when the header is present but unparseable.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds):
            return DEFAULT_RETRY_AFTER_S
        return max(seconds, 0.0)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return DEFAULT_RETRY_AFTER_S
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max((when - current).total_seconds(), 0.0)


class ProviderError(Exception):
    """Base exception for all mail provider adapter operations (R1.5)."""

    def __init__(
        self,
        message: str = "",
        *,
        provider: str | None = None,
        mailbox_id: str | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.mailbox_id = mailbox_id
        self.raw_error = raw_error

    def __str__(self) -> str:
        parts = [self.message]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.mailbox_id:
            parts.append(f"mailbox_id={self.mailbox_id}")
        return " | ".join(filter(None, parts))


class RetryableProviderError(ProviderError):
    """A failure the broker retry ladder may retry (R17.5, design.md §5.8).

    `retry_after_s` is the provider's Retry-After hint in seconds, if it sent one.
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after_s: float | None = None,
        provider: str | None = None,
        mailbox_id: str | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            mailbox_id=mailbox_id,
            raw_error=raw_error,
        )
        self.retry_after_s = retry_after_s


class PermanentProviderError(ProviderError):
    """A failure no retry will fix; dispatch dead-letters it (R17.5, design.md §5.8)."""


class RateLimited(RetryableProviderError):  # noqa: N818
    """Provider API rate limit exceeded (R1.5, R1.6).

    Carries an optional retry_after duration in seconds as instructed by provider
    headers (e.g. Retry-After). `retry_after_s` holds the same value.
    """

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        *,
        retry_after: float | None = None,
        provider: str | None = None,
        mailbox_id: str | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            retry_after_s=retry_after,
            provider=provider,
            mailbox_id=mailbox_id,
            raw_error=raw_error,
        )
        self.retry_after = retry_after


class AuthExpired(PermanentProviderError):  # noqa: N818
    """Provider OAuth or access credentials expired or revoked (R1.5).

    Indicates the mailbox requires administrative re-authentication.
    """


class NotFound(PermanentProviderError):  # noqa: N818
    """Requested message, thread, draft, or mailbox resource does not exist (R1.5)."""


class Transient(RetryableProviderError):  # noqa: N818
    """Temporary failure (e.g. timeout, provider 5xx) eligible for retry (R1.5)."""


class Permanent(PermanentProviderError):  # noqa: N818
    """Fatal non-retryable failure (e.g. malformed request, invalid payload) (R1.5)."""
```

In `packages/adapters/__init__.py`, replace the exceptions import with:

```python
from packages.adapters.exceptions import (
    DEFAULT_RETRY_AFTER_S,
    AuthExpired,
    NotFound,
    Permanent,
    PermanentProviderError,
    ProviderError,
    RateLimited,
    RetryableProviderError,
    Transient,
    parse_retry_after,
)
```

Add `"DEFAULT_RETRY_AFTER_S"`, `"PermanentProviderError"`, `"RetryableProviderError"` and `"parse_retry_after"` to `__all__` in sorted position. Uppercase names sort before `"AuthExpired"` under ruff's RUF022 ordering. If ruff complains, run `uv run ruff check --fix packages/adapters/__init__.py`.

- [ ] **Step 4: Run the exception tests**

Run: `uv run pytest tests/unit/test_adapter_exceptions.py tests/unit/test_mail_adapter_contract.py::test_error_taxonomy_catchable_as_provider_error -v`
Expected: PASS (all).

- [ ] **Step 5: Write the failing Gmail tests**

In `tests/unit/test_gmail_adapter.py`, replace the import block (lines 9–33) with:

```python
import base64
import json
from email import message_from_bytes
from typing import Any

import httpx
import pytest

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    PermanentProviderError,
    RateLimited,
    RetryableProviderError,
    Transient,
)
from packages.adapters.gmail import (
    GmailProviderAdapter,
    GmailPushNotification,
    build_rfc822_mime,
    classify_gmail_error,
    decode_urlsafe_b64,
    encode_urlsafe_b64,
    parse_pubsub_notification,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter
from packages.adapters.testing import MailProviderAdapterContractSuite
from packages.domain import ProviderDraftStatus
from packages.domain.entities import Checkpoint, EmailAddress, Mailbox, OutboundReply
```

Append:

```python
# ---------------------------------------------------------------------------
# 6.3a: Message-ID, error classification, draft send / status / sent lookup.
# Every HTTP response below is a recorded Gmail REST shape; no live calls (R24.5).
# ---------------------------------------------------------------------------

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _gmail_mailbox() -> Mailbox:
    return Mailbox(
        id="mbx-gmail-63a",
        organization_id="org-63a",
        provider="gmail",
        address="support@example.com",
    )


def _gmail_reply(message_id: str | None = "<reply-63a@mail.example.com>") -> OutboundReply:
    return OutboundReply(
        thread_id="18f2c0ffee000001",
        mailbox_id="mbx-gmail-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="customer@example.com")],
        body_text="Thanks, your order has shipped.",
        subject="Re: Order #441",
        in_reply_to="<orig-441@example.com>",
        references=["<orig-441@example.com>"],
        message_id=message_id,
    )


def _gmail_error(status: int, reason: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "usageLimits", "reason": reason}],
        }
    }


def test_build_rfc822_mime_sets_message_id() -> None:
    """6.3a / R17.2: the Gmail reply carries our own Message-ID, bracketed."""
    parsed = message_from_bytes(build_rfc822_mime(_gmail_reply()))
    assert parsed["Message-ID"] == "<reply-63a@mail.example.com>"


def test_build_rfc822_mime_brackets_a_bare_message_id() -> None:
    """A Message-ID stored without angle brackets is written as a valid msg-id."""
    parsed = message_from_bytes(build_rfc822_mime(_gmail_reply("reply-63a@mail.example.com")))
    assert parsed["Message-ID"] == "<reply-63a@mail.example.com>"


def test_build_rfc822_mime_without_message_id_omits_header() -> None:
    """No message_id on the reply means no Message-ID header from us."""
    parsed = message_from_bytes(build_rfc822_mime(_gmail_reply(None)))
    assert parsed["Message-ID"] is None


@pytest.mark.parametrize(
    ("status", "headers", "payload", "expected", "retry_after_s"),
    [
        (429, {"Retry-After": "30"}, _gmail_error(429, "rateLimitExceeded", "Too many"), RateLimited, 30.0),
        (403, {"Retry-After": "12"}, _gmail_error(403, "rateLimitExceeded", "Rate Limit Exceeded"), RateLimited, 12.0),
        (403, {}, _gmail_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"), RateLimited, None),
        (503, {"Retry-After": "120"}, _gmail_error(503, "backendError", "Backend Error"), Transient, 120.0),
        (500, {}, _gmail_error(500, "backendError", "Backend Error"), Transient, None),
    ],
)
@pytest.mark.asyncio
async def test_gmail_retryable_errors(
    status: int,
    headers: dict[str, str],
    payload: dict[str, Any],
    expected: type[RetryableProviderError],
    retry_after_s: float | None,
) -> None:
    """6.3a / R17.5: 429, 5xx and rate-limit 403s are retryable and keep Retry-After."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json=payload, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(expected) as exc_info:
        await adapter._request("GET", f"{GMAIL_BASE}/drafts/d-1", mailbox_id="mbx-1")
    assert isinstance(exc_info.value, RetryableProviderError)
    assert exc_info.value.retry_after_s == retry_after_s
    assert exc_info.value.provider == "gmail"
    assert exc_info.value.mailbox_id == "mbx-1"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, _gmail_error(400, "invalidArgument", "Invalid To header"), Permanent),
        (401, _gmail_error(401, "authError", "Invalid Credentials"), AuthExpired),
        (403, _gmail_error(403, "insufficientPermissions", "Insufficient Permission"), AuthExpired),
        (403, _gmail_error(403, "dailyLimitExceeded", "Daily Limit Exceeded"), Permanent),
        (403, {"error": "forbidden"}, AuthExpired),
        (404, _gmail_error(404, "notFound", "Requested entity was not found."), NotFound),
    ],
)
@pytest.mark.asyncio
async def test_gmail_permanent_errors(
    status: int, payload: dict[str, Any], expected: type[PermanentProviderError]
) -> None:
    """6.3a / R17.5: 400, 404, auth and non-rate-limit 403s are permanent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(expected) as exc_info:
        await adapter._request("GET", f"{GMAIL_BASE}/drafts/d-1")
    assert isinstance(exc_info.value, PermanentProviderError)


def test_classify_gmail_error_is_pure() -> None:
    """The mapping is a pure function of status, header and body."""
    err = classify_gmail_error(
        403,
        retry_after_header=None,
        raw_payload=_gmail_error(403, "userRateLimitExceeded", "slow"),
        mailbox_id="m",
    )
    assert isinstance(err, RateLimited)
    assert err.retry_after is None


def _recording_transport(
    routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]
) -> httpx.MockTransport:
    """Recorded responses keyed by (method, path); every request is kept for assertions."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(404, json=_gmail_error(404, "notFound", "Not Found"), request=request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_gmail_create_draft_sends_message_id_and_returns_both_ids() -> None:
    """6.3a / R17.1: drafts.create carries threadId + our Message-ID; DraftRef has both ids."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/gmail/v1/users/me/drafts"): httpx.Response(
            200,
            json={
                "id": "r-4410001",
                "message": {"id": "18f2d0000000abcd", "threadId": "18f2c0ffee000001", "labelIds": ["DRAFT"]},
            },
        )
    }
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))

    draft = await adapter.create_draft(_gmail_mailbox(), _gmail_reply())

    assert draft.provider_draft_id == "r-4410001"
    assert draft.provider_message_id == "18f2d0000000abcd"
    assert draft.provider_thread_id == "18f2c0ffee000001"
    body = json.loads(seen[0].content)
    assert body["message"]["threadId"] == "18f2c0ffee000001"
    raw = base64.urlsafe_b64decode(body["message"]["raw"] + "==")
    assert message_from_bytes(raw)["Message-ID"] == "<reply-63a@mail.example.com>"


@pytest.mark.asyncio
async def test_gmail_create_draft_without_ids_is_permanent() -> None:
    """A draft we cannot track must not be reported as created (it could never be reused)."""
    seen: list[httpx.Request] = []
    routes = {("POST", "/gmail/v1/users/me/drafts"): httpx.Response(200, json={"message": {}})}
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))
    with pytest.raises(Permanent):
        await adapter.create_draft(_gmail_mailbox(), _gmail_reply())


@pytest.mark.asyncio
async def test_gmail_send_draft_posts_draft_id_and_returns_new_message_id() -> None:
    """6.3a / R17.3: drafts.send sends by draft id; the sent copy has a NEW message id."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/gmail/v1/users/me/drafts/send"): httpx.Response(
            200,
            json={"id": "18f2e11111110001", "threadId": "18f2c0ffee000001", "labelIds": ["SENT"]},
        )
    }
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))

    sent = await adapter.send_draft(_gmail_mailbox(), "r-4410001")

    assert json.loads(seen[0].content) == {"id": "r-4410001"}
    assert sent.provider_message_id == "18f2e11111110001"
    assert sent.provider_thread_id == "18f2c0ffee000001"


@pytest.mark.asyncio
async def test_gmail_send_draft_without_message_id_is_ambiguous_transient() -> None:
    """A 200 without an id may have sent; retry so dispatch reconciles instead of dead-lettering."""
    seen: list[httpx.Request] = []
    routes = {("POST", "/gmail/v1/users/me/drafts/send"): httpx.Response(200, json={})}
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))
    with pytest.raises(Transient):
        await adapter.send_draft(_gmail_mailbox(), "r-4410001")


@pytest.mark.asyncio
async def test_gmail_get_draft_status_draft_and_missing() -> None:
    """6.3a: drafts.get 200 is DRAFT; 404 (sent or deleted) is MISSING. Gmail never reports SENT."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/drafts/r-4410001"): httpx.Response(
            200, json={"id": "r-4410001", "message": {"id": "18f2d0000000abcd"}}
        )
    }
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))

    assert await adapter.get_draft_status(_gmail_mailbox(), "r-4410001") is ProviderDraftStatus.DRAFT
    assert await adapter.get_draft_status(_gmail_mailbox(), "r-gone") is ProviderDraftStatus.MISSING
    assert seen[0].url.params["format"] == "minimal"


def _thread_metadata(*messages: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"id": "18f2c0ffee000001", "messages": list(messages)})


def _meta_message(msg_id: str, labels: list[str], rfc_id: str) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": "18f2c0ffee000001",
        "labelIds": labels,
        "internalDate": "1790000000000",
        "payload": {"headers": [{"name": "Message-Id", "value": rfc_id}]},
    }


@pytest.mark.asyncio
async def test_gmail_find_sent_message_matches_our_message_id() -> None:
    """6.3a / design §5.8 step 4: the SENT copy is found by our Message-ID, not the draft copy."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/threads/18f2c0ffee000001"): _thread_metadata(
            _meta_message("18f2c0ffee000001", ["INBOX"], "<orig-441@example.com>"),
            _meta_message("18f2d0000000abcd", ["DRAFT"], "<reply-63a@mail.example.com>"),
            _meta_message("18f2e11111110001", ["SENT"], "<reply-63a@mail.example.com>"),
        )
    }
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))

    found = await adapter.find_sent_message(
        _gmail_mailbox(), "18f2c0ffee000001", "reply-63a@mail.example.com"
    )

    assert found is not None
    assert found.provider_message_id == "18f2e11111110001"
    assert found.provider_thread_id == "18f2c0ffee000001"
    assert seen[0].url.params["format"] == "metadata"
    assert seen[0].url.params.get_list("metadataHeaders") == ["Message-ID"]


@pytest.mark.asyncio
async def test_gmail_find_sent_message_matches_provider_id_and_ignores_drafts() -> None:
    """A provider id matches only a SENT message; an unsent draft or unknown id is None."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/threads/18f2c0ffee000001"): _thread_metadata(
            _meta_message("18f2d0000000abcd", ["DRAFT"], "<reply-63a@mail.example.com>"),
            _meta_message("18f2e11111110001", ["SENT"], "<reply-63a@mail.example.com>"),
        )
    }
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))
    mailbox = _gmail_mailbox()

    by_id = await adapter.find_sent_message(mailbox, "18f2c0ffee000001", "18f2e11111110001")
    assert by_id is not None and by_id.provider_message_id == "18f2e11111110001"
    assert await adapter.find_sent_message(mailbox, "18f2c0ffee000001", "18f2d0000000abcd") is None
    assert await adapter.find_sent_message(mailbox, "18f2c0ffee000001", "<other@x>") is None
    assert await adapter.find_sent_message(mailbox, "thread-gone", "<reply-63a@mail.example.com>") is None


@pytest.mark.asyncio
async def test_gmail_find_draft_searches_drafts_by_rfc822msgid() -> None:
    """6.5: a redelivery adopts the unrecorded draft carrying our Message-ID (no second draft)."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/gmail/v1/users/me/drafts"): httpx.Response(
            200,
            json={
                "drafts": [
                    {
                        "id": "r-4410001",
                        "message": {"id": "18f2d0000000abcd", "threadId": "18f2c0ffee000001"},
                    }
                ],
                "resultSizeEstimate": 1,
            },
        )
    }
    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=_recording_transport(routes, seen)))
    mailbox = _gmail_mailbox()

    found = await adapter.find_draft(mailbox, "18f2c0ffee000001", "<reply-63a@mail.example.com>")

    assert found is not None
    assert (found.provider_draft_id, found.provider_message_id, found.provider_thread_id) == (
        "r-4410001",
        "18f2d0000000abcd",
        "18f2c0ffee000001",
    )
    assert seen[0].url.params["q"] == "rfc822msgid:reply-63a@mail.example.com"
    # A draft in another thread is not ours to adopt.
    assert await adapter.find_draft(mailbox, "18f2c0ffee999999", "<reply-63a@mail.example.com>") is None
    assert await adapter.find_draft(mailbox, "18f2c0ffee000001", "  ") is None
```

- [ ] **Step 6: Run the Gmail tests to see them fail**

Run: `uv run pytest tests/unit/test_gmail_adapter.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'classify_gmail_error' from 'packages.adapters.gmail'`.

- [ ] **Step 7: Implement the Gmail changes**

In `packages/adapters/gmail.py`:

1. Extend the module docstring requirement list with `- R17.1, R17.2, R17.3, R17.5: reply Message-ID, draft send/status/sent lookup, retryable vs permanent errors (task 6.3a).`

2. Replace the imports (lines 10–39) with:

```python
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
    parse_retry_after,
)
from packages.adapters.registry import register_adapter
from packages.domain import ProviderDraftStatus
from packages.domain.entities import (
    Checkpoint,
    DraftRef,
    Mailbox,
    OutboundReply,
    RawMessage,
    RawThread,
    SentRef,
    Subscription,
    SyncResult,
)

# Gmail "Resolve errors" guide: these 403 reasons are rate limits, retried with backoff.
_GMAIL_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
# A spent daily quota does not recover within the retry ladder (30 s / 5 m / 30 m).
_GMAIL_DAILY_QUOTA_REASON = "dailyLimitExceeded"
```

3. Directly above `def build_rfc822_mime`, add:

```python
def _bare_message_id(value: str) -> str:
    """Strip whitespace and angle brackets from an RFC 5322 msg-id."""
    return value.strip().strip("<>").strip()


def _bracketed_message_id(value: str) -> str:
    """Return a msg-id in its RFC 5322 `<id-left@id-right>` form."""
    return f"<{_bare_message_id(value)}>"


def _gmail_error_reasons(raw_payload: Any) -> set[str]:
    """Collect `error.errors[].reason` from a Google API error body."""
    if not isinstance(raw_payload, dict):
        return set()
    error = raw_payload.get("error")
    if not isinstance(error, dict):
        return set()
    errors = error.get("errors")
    if not isinstance(errors, list):
        return set()
    return {
        str(item["reason"])
        for item in errors
        if isinstance(item, dict) and item.get("reason")
    }


def classify_gmail_error(
    status: int,
    *,
    retry_after_header: str | None,
    raw_payload: Any,
    mailbox_id: str | None,
) -> ProviderError:
    """Map a failed Gmail response to the common taxonomy (R1.5, R1.6, R17.5).

    429, 5xx and 403 rateLimitExceeded/userRateLimitExceeded are retryable and keep
    Retry-After; 400, 404, 401 and every other 403 are permanent.
    """
    retry_after_s = parse_retry_after(retry_after_header)
    reasons = _gmail_error_reasons(raw_payload)

    if status == 429 or (status == 403 and reasons & _GMAIL_RATE_LIMIT_REASONS):
        return RateLimited(
            f"Gmail rate limit exceeded (HTTP {status})",
            retry_after=retry_after_s,
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status == 403 and _GMAIL_DAILY_QUOTA_REASON in reasons:
        return Permanent(
            "Gmail daily quota exhausted (HTTP 403 dailyLimitExceeded)",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status in (401, 403):
        return AuthExpired(
            f"Gmail authentication failed or token expired (HTTP {status})",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status == 404:
        return NotFound(
            "Gmail resource not found",
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    if status >= 500:
        return Transient(
            f"Gmail temporary server error (HTTP {status})",
            retry_after_s=retry_after_s,
            provider="gmail",
            mailbox_id=mailbox_id,
            raw_error=raw_payload,
        )
    return Permanent(
        f"Gmail API permanent failure (HTTP {status}): {raw_payload}",
        provider="gmail",
        mailbox_id=mailbox_id,
        raw_error=raw_payload,
    )


def _gmail_internal_date(data: dict[str, Any]) -> datetime:
    """Convert Gmail `internalDate` (epoch ms) to an aware datetime, falling back to now."""
    try:
        return datetime.fromtimestamp(int(data["internalDate"]) / 1000.0, UTC)
    except (KeyError, TypeError, ValueError):
        return datetime.now(UTC)
```

4. In `build_rfc822_mime`, insert after the `References` block:

```python
    if reply.message_id:
        msg["Message-ID"] = _bracketed_message_id(reply.message_id)
```

5. In `_request`, replace everything from `status = resp.status_code` through the final `raise Permanent(...)` (lines 172–219) with:

```python
        raise classify_gmail_error(
            resp.status_code,
            retry_after_header=resp.headers.get("Retry-After"),
            raw_payload=raw_payload,
            mailbox_id=mailbox_id,
        )
```

6. Replace `create_draft` with:

```python
    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Create a reply draft in the Gmail thread (R1.1, R1.4, R17.1).

        Returns the stable draft id and the draft message's id; a response without
        both cannot be reused on redelivery, so it is permanent.
        """
        url = f"{self.base_url}/drafts"
        raw_b64 = encode_urlsafe_b64(build_rfc822_mime(reply))
        body = {"message": {"raw": raw_b64, "threadId": str(reply.thread_id)}}
        resp = await self._request("POST", url, mailbox_id=str(mailbox.id), json=body)
        data = resp.json()

        msg_obj = data.get("message") or {}
        draft_id = data.get("id")
        message_id = msg_obj.get("id")
        if not draft_id or not message_id:
            raise Permanent(
                "Gmail drafts.create returned no draft id or message id",
                provider="gmail",
                mailbox_id=str(mailbox.id),
                raw_error=data,
            )
        return DraftRef(
            provider_draft_id=str(draft_id),
            provider_message_id=str(message_id),
            provider_thread_id=msg_obj.get("threadId", str(reply.thread_id)),
        )
```

7. After `send_reply`, add:

```python
    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        """Send an existing draft with users.drafts.send (R17.3, design.md §5.8 step 3).

        Gmail deletes the draft and returns a NEW message carrying the SENT label; its
        id is the one mailbox sync will later see.
        """
        url = f"{self.base_url}/drafts/send"
        resp = await self._request(
            "POST", url, mailbox_id=str(mailbox.id), json={"id": provider_draft_id}
        )
        data = resp.json()
        sent_id = data.get("id")
        if not sent_id:
            # The send may have happened: retry, and let dispatch reconcile via get_draft_status.
            raise Transient(
                "Gmail drafts.send returned no message id (outcome unknown)",
                provider="gmail",
                mailbox_id=str(mailbox.id),
                raw_error=data,
            )
        return SentRef(
            provider_message_id=str(sent_id),
            provider_thread_id=data.get("threadId"),
            sent_at=datetime.now(UTC),
        )

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        """Report whether the draft still exists (R17.3, design.md §5.8 step 4).

        Gmail deletes a draft when it is sent, so a sent draft and a draft a person
        deleted both answer 404: MISSING. SENT is never returned by Gmail.
        """
        url = f"{self.base_url}/drafts/{quote(provider_draft_id, safe='')}"
        try:
            await self._request(
                "GET", url, mailbox_id=str(mailbox.id), params={"format": "minimal"}
            )
        except NotFound:
            return ProviderDraftStatus.MISSING
        return ProviderDraftStatus.DRAFT

    async def find_sent_message(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        provider_message_id: str,
    ) -> SentRef | None:
        """Find our sent reply in the thread (R17.3, design.md §5.8 step 4).

        Matches a SENT-labelled, non-draft message whose Gmail id equals the key or whose
        RFC 5322 Message-ID equals the key (angle brackets optional). A missing thread
        returns None.
        """
        url = f"{self.base_url}/threads/{quote(provider_thread_id, safe='')}"
        try:
            resp = await self._request(
                "GET",
                url,
                mailbox_id=str(mailbox.id),
                params=[("format", "metadata"), ("metadataHeaders", "Message-ID")],
            )
        except NotFound:
            return None

        wanted = _bare_message_id(provider_message_id)
        for msg in resp.json().get("messages", []):
            labels = msg.get("labelIds") or []
            if "SENT" not in labels or "DRAFT" in labels:
                continue
            headers = (msg.get("payload") or {}).get("headers") or []
            header_ids = {
                _bare_message_id(str(h.get("value", "")))
                for h in headers
                if str(h.get("name", "")).lower() == "message-id"
            }
            if msg.get("id") == provider_message_id or wanted in header_ids:
                return SentRef(
                    provider_message_id=str(msg["id"]),
                    provider_thread_id=provider_thread_id,
                    sent_at=_gmail_internal_date(msg),
                )
        return None

    async def find_draft(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        message_id: str,
    ) -> DraftRef | None:
        """Find an unsent draft carrying our RFC 5322 Message-ID (tasks.md 6.5, §5.8 step 2).

        A redelivery after a crash between drafts.create and recording the draft id adopts
        this draft instead of creating a second one. users.drafts.list takes Gmail search
        syntax, where ``rfc822msgid:`` matches the Message-ID header; each listed draft
        carries only its id and its message's id and threadId. A draft in another thread
        is ignored.
        """
        wanted = _bare_message_id(message_id)
        if not wanted:
            return None
        url = f"{self.base_url}/drafts"
        resp = await self._request(
            "GET",
            url,
            mailbox_id=str(mailbox.id),
            params={"q": f"rfc822msgid:{wanted}", "maxResults": "10"},
        )
        for entry in resp.json().get("drafts") or []:
            message = entry.get("message") or {}
            draft_id = entry.get("id")
            draft_message_id = message.get("id")
            thread = message.get("threadId")
            if not draft_id or not draft_message_id:
                continue
            if thread and provider_thread_id and thread != provider_thread_id:
                continue
            return DraftRef(
                provider_draft_id=str(draft_id),
                provider_message_id=str(draft_message_id),
                provider_thread_id=str(thread or provider_thread_id),
            )
        return None
```

Also replace the `internal_date` parsing in `get_message`, from `internal_date = None` through the `except` block, with `internal_date = _gmail_internal_date(data) if "internalDate" in data else None`. This keeps a single implementation.

- [ ] **Step 8: Run the Gmail and adapter unit tests**

Run: `uv run pytest tests/unit/test_gmail_adapter.py tests/unit/test_adapter_exceptions.py tests/unit/test_mail_sync_consumer.py tests/unit/test_subscription_renewal.py -v`
Expected: PASS. The existing `test_error_translation_rate_limited` still sees `retry_after == 60.0`, and the 8 existing contract tests still pass.

- [ ] **Step 9: Lint, type-check, format**

Run: `uv run ruff format packages/adapters tests/unit/test_gmail_adapter.py tests/unit/test_adapter_exceptions.py && uv run ruff check packages/adapters tests/unit && uv run mypy packages/adapters tests/unit/test_gmail_adapter.py tests/unit/test_adapter_exceptions.py`
Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 10: Commit**

```bash
git add packages/adapters/exceptions.py packages/adapters/__init__.py packages/adapters/gmail.py tests/unit/test_adapter_exceptions.py tests/unit/test_gmail_adapter.py
git commit -m "fix(adapters): Gmail reply Message-ID, draft send/status/lookup, retryable vs permanent errors [task 6.3a] [R1.1, R17.1, R17.2, R17.5]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Graph createReply + send with immutable ids, fake adapter, protocol and contract suite [tasks.md 6.3a]

**Files:**
- No entity change: Task 1 already added `OutboundReply.reply_to_provider_message_id: str | None = None`, and Task 2's `build_outbound_reply` already sets it from `original.provider_message_id`. Step 3 only pins that with a Graph-shaped test.
- Modify: `packages/adapters/protocol.py`. Add the four methods (`send_draft`, `get_draft_status`, `find_sent_message`, `find_draft`) and the `ProviderDraftStatus` import, and update the docstring.
- Modify: `packages/adapters/graph.py`:
  - imports, lines 11–40;
  - `_request` 429/5xx branches, lines 201–245;
  - replace `create_draft` and `send_reply` (lines 429–488); `create_draft` sets `internetMessageId` (open question D3);
  - add `send_draft`, `get_draft_status`, `find_sent_message` and `find_draft`;
  - add module helpers.
- Modify: `packages/adapters/fake.py`:
  - `__init__` (line 44);
  - `draft_count`, plus a new `pending_draft_count`;
  - all `inject_*` methods and `_maybe_raise_fault` (lines 93–171);
  - `create_draft` and `send_reply` (lines 438–460);
  - add `send_draft`, `get_draft_status`, `find_sent_message`, `find_draft` and `delete_draft`.
- Modify: `packages/adapters/testing.py`. Add `make_test_reply`, switch the two outbound tests to it, and add 8 new contract tests.
- Test:
  - `tests/unit/test_gmail_adapter.py`: `create_mock_gmail_transport`, lines 129–196, becomes stateful;
  - `tests/unit/test_graph_adapter.py`: `create_mock_graph_transport`, lines 206–299, becomes stateful, and the new Graph tests are appended;
  - `tests/unit/test_fake_adapter.py`: append;
  - `tests/unit/test_mail_adapter_contract.py`: `ConformingMockAdapter`, lines 33–95;
  - `tests/unit/test_adapter_registry.py`: `ConformingDummyAdapter`, lines 40–73;
  - `tests/unit/test_outbound_reply.py` (Part A's file): one appended test.

**Interfaces:**
- Consumes:
  - from Task 4: `RetryableProviderError`, `PermanentProviderError`, `parse_retry_after`, `Permanent`, `NotFound`, `Transient`, and Gmail's `send_draft`/`get_draft_status`/`find_sent_message`/`find_draft`;
  - from Part A: `ProviderDraftStatus`, `OutboundReply.message_id`, `OutboundReply.thread_id: str`, and `build_outbound_reply(*, draft, original, provider_thread_id, message_id_domain) -> OutboundReply` in `packages/dispatch/reply.py`.
- Produces:
  - `MailProviderAdapter.send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef`, `MailProviderAdapter.get_draft_status(self, mailbox: Mailbox, provider_draft_id: str) -> ProviderDraftStatus` and `MailProviderAdapter.find_sent_message(self, mailbox: Mailbox, provider_thread_id: str, provider_message_id: str) -> SentRef | None` and `MailProviderAdapter.find_draft(self, mailbox: Mailbox, provider_thread_id: str, message_id: str) -> DraftRef | None` (an **unsent** draft in the thread carrying that RFC 5322 `Message-ID`), all `async`.
  - (consumed from Task 1, not produced here) `OutboundReply.reply_to_provider_message_id: str | None = None`.
  - `packages.adapters.graph.IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'`.
  - `GraphProviderAdapter.create_draft` does `POST {base}/messages/{reply_to_provider_message_id}/createReply` and raises `Permanent` when that id is absent. `send_draft` does `POST {base}/messages/{id}/send`. `send_reply` is createReply followed by send, never `sendMail`.
  - `FakeProviderAdapter`:
    - `__init__(self, batch_size: int = 50, mailbox_id: str = "mbx-fake", *, deletes_sent_drafts: bool = False, **kwargs: Any)`. With `False` it behaves like Graph: after send, status is SENT and the sent id equals the draft message id. With `True` it behaves like Gmail: status is MISSING and the sent copy gets a new `sent-fake-N` id.
    - `create_draft` returns `DraftRef("draft-fake-N", "draft-msg-fake-N", thread)`.
    - New members: `send_draft`, `get_draft_status`, `find_sent_message`, `find_draft`, `delete_draft(provider_draft_id: str) -> None` (simulates a person deleting the draft), and `pending_draft_count: int`. `draft_count` still counts every draft ever created.
    - Every `inject_*` gains the keyword-only `method: str | None = None` (the fault fires only on that adapter method) and `after_success: bool = False`. `after_success=True` raises after the side effect, simulating "the provider accepted but we saw an error", for Part C's crash-after-step tests.
  - Contract suite (`MailProviderAdapterContractSuite`):
    - `make_test_reply(self, mailbox: Mailbox) -> OutboundReply`;
    - `test_create_draft_returns_draft_ref` now also asserts `provider_message_id`;
    - new tests: `test_send_draft_returns_sent_ref`, `test_get_draft_status_of_new_draft_is_draft`, `test_get_draft_status_after_send_is_sent_or_missing`, `test_get_draft_status_of_unknown_draft_is_missing`, `test_find_sent_message_locates_sent_draft`, `test_find_sent_message_is_none_before_send`, `test_find_draft_locates_the_unsent_draft_by_message_id`, `test_find_draft_is_none_for_an_unknown_message_id`.
  - `GraphProviderAdapter.create_draft` puts `internetMessageId` = our `Message-ID` (bracketed) into the `createReply` `message` when `reply.message_id` is set (open question D3).

- [ ] **Step 1: Write the failing contract-suite tests**

In `packages/adapters/testing.py`:
- Add `from packages.domain import ProviderDraftStatus` below the `packages.adapters.protocol` import.
- Add the requirement line `- R17.1, R17.3 (task 6.3a): draft ids, send_draft, get_draft_status, find_sent_message.` to the module docstring.
- Replace the two methods `test_create_draft_returns_draft_ref` and `test_send_reply_returns_sent_ref` with the block below. It also holds the new helper and the new tests.

```python
    UNKNOWN_DRAFT_ID = "draft-does-not-exist"

    def make_test_reply(self, mailbox: Mailbox) -> OutboundReply:
        """A reply to seeded message msg-001 in thread th-001 (all adapters accept it)."""
        return OutboundReply(
            thread_id="th-001",
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            to=[EmailAddress(email="recipient@example.com")],
            body_text="Contract reply body",
            subject="Re: Test subject",
            in_reply_to="<orig-001@example.com>",
            references=["<orig-001@example.com>"],
            message_id="<contract-reply-001@example.com>",
            reply_to_provider_message_id="msg-001",
        )

    async def _find_sent(
        self,
        adapter: MailProviderAdapter,
        mailbox: Mailbox,
        reply: OutboundReply,
        draft: DraftRef,
    ) -> SentRef | None:
        """The dispatch lookup rule: stored draft message id first, then our Message-ID."""
        found = await adapter.find_sent_message(
            mailbox, "th-001", draft.provider_message_id or ""
        )
        if found is None and reply.message_id:
            found = await adapter.find_sent_message(mailbox, "th-001", reply.message_id)
        return found

    @pytest.mark.asyncio
    async def test_create_draft_returns_draft_ref(self) -> None:
        """Assert create_draft() returns the draft id AND the draft's message id (R17.1)."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        assert isinstance(draft, DraftRef)
        assert bool(draft.provider_draft_id)
        assert bool(draft.provider_message_id)

    @pytest.mark.asyncio
    async def test_send_reply_returns_sent_ref(self) -> None:
        """Assert send_reply() returns SentRef with message ID and timestamp."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        sent = await adapter.send_reply(mailbox, self.make_test_reply(mailbox))
        assert isinstance(sent, SentRef)
        assert bool(sent.provider_message_id)
        assert isinstance(sent.sent_at, datetime)

    @pytest.mark.asyncio
    async def test_send_draft_returns_sent_ref(self) -> None:
        """6.3a: send_draft() sends an existing draft and returns the sent message ref."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        sent = await adapter.send_draft(mailbox, draft.provider_draft_id)
        assert isinstance(sent, SentRef)
        assert bool(sent.provider_message_id)
        assert isinstance(sent.sent_at, datetime)

    @pytest.mark.asyncio
    async def test_get_draft_status_of_new_draft_is_draft(self) -> None:
        """6.3a: an unsent draft reports DRAFT."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        status = await adapter.get_draft_status(mailbox, draft.provider_draft_id)
        assert status is ProviderDraftStatus.DRAFT

    @pytest.mark.asyncio
    async def test_get_draft_status_after_send_is_sent_or_missing(self) -> None:
        """6.3a: after send a draft is SENT (Graph-like) or MISSING (Gmail deletes it)."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        draft = await adapter.create_draft(mailbox, self.make_test_reply(mailbox))
        await adapter.send_draft(mailbox, draft.provider_draft_id)
        status = await adapter.get_draft_status(mailbox, draft.provider_draft_id)
        assert status in (ProviderDraftStatus.SENT, ProviderDraftStatus.MISSING)

    @pytest.mark.asyncio
    async def test_get_draft_status_of_unknown_draft_is_missing(self) -> None:
        """6.3a: a draft id the provider does not know reports MISSING, not an error."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        status = await adapter.get_draft_status(mailbox, self.UNKNOWN_DRAFT_ID)
        assert status is ProviderDraftStatus.MISSING

    @pytest.mark.asyncio
    async def test_find_sent_message_locates_sent_draft(self) -> None:
        """6.3a / design §5.8 step 4: after send, the sent copy is found in the thread."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = self.make_test_reply(mailbox)
        draft = await adapter.create_draft(mailbox, reply)
        sent = await adapter.send_draft(mailbox, draft.provider_draft_id)
        found = await self._find_sent(adapter, mailbox, reply, draft)
        assert found is not None
        assert found.provider_message_id == sent.provider_message_id

    @pytest.mark.asyncio
    async def test_find_sent_message_is_none_before_send(self) -> None:
        """6.3a: an unsent draft is never reported as sent (no false 'already sent')."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = self.make_test_reply(mailbox)
        draft = await adapter.create_draft(mailbox, reply)
        assert await self._find_sent(adapter, mailbox, reply, draft) is None

    @pytest.mark.asyncio
    async def test_find_draft_locates_the_unsent_draft_by_message_id(self) -> None:
        """6.5: a redelivery adopts the draft an earlier delivery created but never recorded."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        reply = self.make_test_reply(mailbox)
        draft = await adapter.create_draft(mailbox, reply)
        assert reply.message_id is not None
        found = await adapter.find_draft(mailbox, draft.provider_thread_id or "th-001", reply.message_id)
        assert found is not None
        assert found.provider_draft_id == draft.provider_draft_id

    @pytest.mark.asyncio
    async def test_find_draft_is_none_for_an_unknown_message_id(self) -> None:
        """6.5: no draft carries this Message-ID, so dispatch creates one."""
        adapter = self.create_adapter()
        mailbox = self.make_test_mailbox()
        found = await adapter.find_draft(mailbox, "th-001", "<never-created-001@example.com>")
        assert found is None
```

- [ ] **Step 2: Run the suites to see them fail**

Run: `uv run pytest tests/unit/test_fake_adapter.py tests/unit/test_graph_adapter.py tests/unit/test_gmail_adapter.py tests/unit/test_mail_adapter_contract.py -v`
Expected: FAIL. `AttributeError: ... has no attribute 'send_draft'` (and `get_draft_status` / `find_sent_message`) for the fake and Graph, and the Graph `createReply` tests fail against the old `/messages` + `/sendMail` code. `make_test_reply` itself builds, because Task 1 added `reply_to_provider_message_id`.

- [ ] **Step 3: Pin the original provider id on a Graph-shaped reply**

Task 1 added `OutboundReply.reply_to_provider_message_id` and Task 2 sets it. Confirm with `grep -n "reply_to_provider_message_id" packages/domain/entities.py packages/dispatch/reply.py` (expected: one hit in each file); if either is missing, go back and finish Task 1 / Task 2 rather than adding it here.

Append to `tests/unit/test_outbound_reply.py` (Part A's test module). Add the imports if they are missing: `from datetime import UTC, datetime`, `from packages.dispatch.reply import build_outbound_reply` and `from packages.domain.entities import EmailAddress, GeneratedDraft, NormalizedMessage`.

```python
def test_outbound_reply_carries_the_original_provider_message_id() -> None:
    """6.3a: Graph createReply needs the original email's provider id on the reply."""
    original = NormalizedMessage(
        message_id="00000000-0000-0000-0000-00000000a001",
        thread_id="00000000-0000-0000-0000-00000000b001",
        mailbox_id="00000000-0000-0000-0000-00000000c001",
        organization_id="00000000-0000-0000-0000-00000000d001",
        provider="graph",
        provider_message_id="AAMkAGI2-orig-001",
        sender=EmailAddress(email="customer@example.com"),
        received_at=datetime(2026, 9, 28, tzinfo=UTC),
        rfc822_message_id="orig-001@example.com",
        subject="Order question",
    )
    draft = GeneratedDraft(body="Your order shipped.", organization_id=original.organization_id)
    reply = build_outbound_reply(
        draft=draft,
        original=original,
        provider_thread_id="conv-001",
        message_id_domain="mail.example.com",
    )
    assert reply.reply_to_provider_message_id == "AAMkAGI2-orig-001"
```

Run: `uv run pytest tests/unit/test_outbound_reply.py::test_outbound_reply_carries_the_original_provider_message_id -v`
Expected: PASS (Task 2 already wires the field; this test guards it for Graph).

- [ ] **Step 4: Extend the protocol**

Replace the body of `packages/adapters/protocol.py` below the module docstring with the following. In the docstring, change "R1.1" to list the three new methods and add `- R17.1, R17.3 (task 6.3a): draft send, status and sent-message lookup for exactly-once dispatch.`

```python
from __future__ import annotations

from typing import Protocol, runtime_checkable

from packages.domain import ProviderDraftStatus
from packages.domain.entities import (
    Checkpoint,
    DraftRef,
    Mailbox,
    OutboundReply,
    RawMessage,
    RawThread,
    SentRef,
    Subscription,
    SyncResult,
)


@runtime_checkable
class MailProviderAdapter(Protocol):
    """Protocol defining the interface for all email provider adapters (R1.1, R1.4).

    Confined strictly within packages/adapters/. No implementation details or
    provider SDKs should leak beyond adapter boundaries.

    Errors: retryable failures raise RetryableProviderError subclasses (RateLimited,
    Transient) with `retry_after_s`; permanent ones raise PermanentProviderError
    subclasses (AuthExpired, NotFound, Permanent).
    """

    async def subscribe(self, mailbox: Mailbox) -> Subscription:
        """Create or initialize a webhook/push notification subscription."""
        ...

    async def renew_subscription(self, sub: Subscription) -> Subscription:
        """Renew an existing subscription before expiration."""
        ...

    async def synchronize(self, mailbox: Mailbox, cp: Checkpoint) -> SyncResult:
        """Fetch incremental change batch since the provided checkpoint."""
        ...

    async def get_message(self, mailbox: Mailbox, provider_message_id: str) -> RawMessage:
        """Retrieve full raw message payload by provider message ID."""
        ...

    async def get_thread(self, mailbox: Mailbox, provider_thread_id: str) -> RawThread:
        """Retrieve all raw messages in a provider thread."""
        ...

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Create a reply draft; the DraftRef carries the draft id and its message id."""
        ...

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Dispatch an outbound reply through the provider mailbox."""
        ...

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        """Send an existing provider draft (design.md §5.8 step 3)."""
        ...

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        """Report DRAFT, SENT or MISSING for a draft (design.md §5.8 step 4)."""
        ...

    async def find_sent_message(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        provider_message_id: str,
    ) -> SentRef | None:
        """Find a sent message in the thread by provider id or RFC 5322 Message-ID."""
        ...

    async def find_draft(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        message_id: str,
    ) -> DraftRef | None:
        """Find an unsent draft in the thread carrying this RFC 5322 Message-ID (tasks.md 6.5).

        Dispatch adopts it after a crash between create_draft and recording the handle, so
        a redelivery never leaves a second provider draft.
        """
        ...
```

- [ ] **Step 5: Implement the Graph adapter changes**

In `packages/adapters/graph.py`:

1. Add `- R17.1, R17.2, R17.3, R17.5: createReply + send with immutable ids; draft status and sent lookup (task 6.3a).` to the docstring. In the imports, add `from urllib.parse import quote`. Add `EmailAddress` to the entities import and `ProviderDraftStatus` via `from packages.domain import ProviderDraftStatus`. Add `parse_retry_after` to the exceptions import.

2. Below the imports, add the module helpers:

```python
# Graph "Obtain immutable identifiers": ids in responses survive folder moves, so the
# draft id we store is also the id of the copy in Sent Items after send.
IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'
_FIND_SENT_MAX_PAGES = 10


def _immutable_id_headers() -> dict[str, str]:
    return {"Prefer": IMMUTABLE_ID_PREFER}


def _graph_path_id(value: str) -> str:
    """Percent-encode a Graph id for a URL path segment ('=' padding kept)."""
    return quote(value, safe="=")


def _odata_string(value: str) -> str:
    """Escape a value for an OData single-quoted string literal."""
    return value.replace("'", "''")


def _bare_message_id(value: str) -> str:
    return value.strip().strip("<>").strip()


def _graph_recipients(addresses: list[EmailAddress]) -> list[dict[str, Any]]:
    return [
        {"emailAddress": {"address": str(a.email), "name": a.name or str(a.email)}}
        for a in addresses
    ]


def _graph_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.now(UTC)
```

3. In `_request`, replace the 429 branch (the `retry_after_hdr` parsing through `raise RateLimited(...)`) with:

```python
        retry_after_s = parse_retry_after(resp.headers.get("Retry-After"))
        if status == 429:
            raise RateLimited(
                "Microsoft Graph rate limit exceeded",
                retry_after=retry_after_s,
                provider="graph",
                mailbox_id=mailbox_id,
                raw_error=raw_payload,
            )
```

Then add `retry_after_s=retry_after_s,` to the `raise Transient(` call in the `status >= 500` branch. Graph 503 sends Retry-After, per the Graph errors page.

4. Replace `create_draft` and `send_reply` with the following, and add the four new methods:

```python
    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Create a reply draft with createReply (R1.1, R17.1, R17.2).

        The draft is addressed to the original message, so Exchange threads it; ids
        are immutable (Prefer IdType), so the draft id is also the sent copy's id.
        """
        original_id = reply.reply_to_provider_message_id
        if not original_id:
            raise Permanent(
                "Graph reply drafts need the original provider message id (createReply)",
                provider="graph",
                mailbox_id=str(mailbox.id),
            )
        url = f"{self.base_url}/messages/{_graph_path_id(original_id)}/createReply"
        message: dict[str, Any] = {
            "toRecipients": _graph_recipients(reply.to),
            "body": {
                "contentType": "HTML" if reply.body_html else "Text",
                "content": reply.body_html or reply.body_text or "",
            },
        }
        if reply.cc:
            message["ccRecipients"] = _graph_recipients(reply.cc)
        if reply.message_id:
            # Our deterministic Message-ID, so find_draft can adopt this draft after a crash
            # before its id was recorded (tasks.md 6.5). Graph "Update message":
            # internetMessageId is updatable while isDraft = true (open question D3).
            message["internetMessageId"] = f"<{_bare_message_id(reply.message_id)}>"
        # Graph rejects `comment` together with `message.body` (HTTP 400); send body only.
        resp = await self._request(
            "POST",
            url,
            mailbox_id=str(mailbox.id),
            json={"message": message},
            headers=_immutable_id_headers(),
        )
        data = resp.json()
        draft_id = data.get("id")
        if not draft_id:
            raise Permanent(
                "Graph createReply returned no draft id",
                provider="graph",
                mailbox_id=str(mailbox.id),
                raw_error=data,
            )
        return DraftRef(
            provider_draft_id=str(draft_id),
            provider_message_id=str(draft_id),
            provider_thread_id=data.get("conversationId") or str(reply.thread_id),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Reply in-thread: createReply then send, never sendMail (R1.1, R17.2, R17.4)."""
        draft = await self.create_draft(mailbox, reply)
        sent = await self.send_draft(mailbox, draft.provider_draft_id)
        return SentRef(
            provider_message_id=sent.provider_message_id,
            provider_thread_id=draft.provider_thread_id,
            sent_at=sent.sent_at,
        )

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        """Send an existing draft (POST /messages/{id}/send, 202) (R17.3, R17.4).

        With immutable ids the Sent Items copy keeps the draft's id, so that id is the
        real provider message id (not a request correlation id).
        """
        url = f"{self.base_url}/messages/{_graph_path_id(provider_draft_id)}/send"
        await self._request(
            "POST", url, mailbox_id=str(mailbox.id), headers=_immutable_id_headers()
        )
        return SentRef(
            provider_message_id=provider_draft_id,
            provider_thread_id=None,
            sent_at=datetime.now(UTC),
        )

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        """Read isDraft by immutable id: true DRAFT, false SENT, 404 MISSING (R17.3)."""
        url = f"{self.base_url}/messages/{_graph_path_id(provider_draft_id)}"
        try:
            resp = await self._request(
                "GET",
                url,
                mailbox_id=str(mailbox.id),
                params={"$select": "id,isDraft"},
                headers=_immutable_id_headers(),
            )
        except NotFound:
            return ProviderDraftStatus.MISSING
        is_draft = resp.json().get("isDraft")
        if is_draft is True:
            return ProviderDraftStatus.DRAFT
        if is_draft is False:
            return ProviderDraftStatus.SENT
        raise Transient(
            "Graph message response has no isDraft flag",
            provider="graph",
            mailbox_id=str(mailbox.id),
        )

    async def find_sent_message(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        provider_message_id: str,
    ) -> SentRef | None:
        """Find a sent (isDraft false) message in the conversation (R17.3, §5.8 step 4).

        Matches the immutable id or the RFC 5322 internetMessageId (brackets optional).
        """
        wanted = _bare_message_id(provider_message_id)
        next_url: str | None = f"{self.base_url}/messages"
        params: dict[str, str] | None = {
            "$filter": f"conversationId eq '{_odata_string(provider_thread_id)}'",
            "$select": "id,isDraft,internetMessageId,sentDateTime,conversationId",
            "$top": "50",
        }
        pages = 0
        while next_url and pages < _FIND_SENT_MAX_PAGES:
            pages += 1
            try:
                resp = await self._request(
                    "GET",
                    next_url,
                    mailbox_id=str(mailbox.id),
                    params=params,
                    headers=_immutable_id_headers(),
                )
            except NotFound:
                return None
            data = resp.json()
            for item in data.get("value", []):
                if item.get("isDraft") is not False:
                    continue
                rfc_id = _bare_message_id(str(item.get("internetMessageId") or ""))
                if item.get("id") == provider_message_id or (wanted and rfc_id == wanted):
                    return SentRef(
                        provider_message_id=str(item["id"]),
                        provider_thread_id=provider_thread_id,
                        sent_at=_graph_datetime(item.get("sentDateTime")),
                    )
            next_url = data.get("@odata.nextLink")
            params = None  # nextLink already carries the query
        return None

    async def find_draft(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        message_id: str,
    ) -> DraftRef | None:
        """Find an unsent (isDraft true) message in the conversation with our Message-ID.

        create_draft sets internetMessageId to our deterministic Message-ID, so a
        redelivery adopts a draft whose id was never recorded (tasks.md 6.5).
        """
        wanted = _bare_message_id(message_id)
        if not wanted:
            return None
        next_url: str | None = f"{self.base_url}/messages"
        params: dict[str, str] | None = {
            "$filter": f"conversationId eq '{_odata_string(provider_thread_id)}'",
            "$select": "id,isDraft,internetMessageId,conversationId",
            "$top": "50",
        }
        pages = 0
        while next_url and pages < _FIND_SENT_MAX_PAGES:
            pages += 1
            try:
                resp = await self._request(
                    "GET",
                    next_url,
                    mailbox_id=str(mailbox.id),
                    params=params,
                    headers=_immutable_id_headers(),
                )
            except NotFound:
                return None
            data = resp.json()
            for item in data.get("value", []):
                if item.get("isDraft") is not True or not item.get("id"):
                    continue
                if _bare_message_id(str(item.get("internetMessageId") or "")) == wanted:
                    return DraftRef(
                        provider_draft_id=str(item["id"]),
                        provider_message_id=str(item["id"]),
                        provider_thread_id=str(item.get("conversationId") or provider_thread_id),
                    )
            next_url = data.get("@odata.nextLink")
            params = None
        return None
```

- [ ] **Step 6: Write the Graph-specific recorded-response tests**

In `tests/unit/test_graph_adapter.py`:
- Add these imports: `from typing import Any`; `from packages.adapters.exceptions import PermanentProviderError, RetryableProviderError`, merged into the existing import; `from packages.adapters.graph import IMMUTABLE_ID_PREFER`; `from packages.domain import ProviderDraftStatus`; and `from packages.domain.entities import EmailAddress, OutboundReply`, merged into the existing entities import.
- Replace `create_mock_graph_transport` (lines 206–299) with the stateful version below.
- Append the new tests.

```python
def create_mock_graph_transport() -> httpx.MockTransport:
    """Recorded Microsoft Graph responses; drafts are stateful so send changes isDraft."""
    drafts: dict[str, bool] = {}  # draft id -> sent?
    rfc_ids: dict[str, str] = {}  # draft id -> internetMessageId set by createReply

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/v1.0/subscriptions" and method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "graph-sub-contract-001",
                    "resource": "/me/mailFolders('Inbox')/messages",
                    "expirationDateTime": "2026-09-20T18:00:00.000Z",
                    "clientState": "secret-state",
                },
                request=request,
            )
        if path.startswith("/v1.0/subscriptions/") and method == "PATCH":
            return httpx.Response(
                200,
                json={
                    "id": path.split("/")[-1],
                    "resource": "/me/mailFolders('Inbox')/messages",
                    "expirationDateTime": "2026-09-25T18:00:00.000Z",
                },
                request=request,
            )
        if path == "/v1.0/me/mailFolders/Inbox/messages/delta":
            return httpx.Response(
                200,
                json={
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages/delta?$deltatoken=initial-token",
                    "value": [{"id": "graph-msg-sync-1"}],
                },
                request=request,
            )
        if path == "/v1.0/me/messages/msg-001/createReply" and method == "POST":
            assert request.headers.get("Prefer") == IMMUTABLE_ID_PREFER
            draft_id = "draft-graph-contract-001"
            drafts[draft_id] = False
            sent_message = json.loads(request.content or b"{}").get("message") or {}
            rfc_ids[draft_id] = str(
                sent_message.get("internetMessageId") or "<AM0PR01MB0001@eurprd01.prod.outlook.com>"
            )
            return httpx.Response(
                201,
                json={"id": draft_id, "conversationId": "conv-contract-001", "isDraft": True},
                request=request,
            )
        if path.endswith("/send") and method == "POST":
            draft_id = path.split("/")[-2]
            if draft_id not in drafts or drafts[draft_id]:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}}, request=request)
            drafts[draft_id] = True
            return httpx.Response(202, request=request)
        if path.endswith("/$value"):
            return httpx.Response(
                200,
                content=(
                    b"From: sender@example.com\r\nTo: recipient@example.com\r\n"
                    b"Subject: Test Email\r\n\r\nHello from Graph!"
                ),
                headers={"Content-Type": "message/rfc822"},
                request=request,
            )
        if path.startswith("/v1.0/me/messages/draft-") and method == "GET":
            draft_id = path.split("/")[-1]
            if draft_id not in drafts:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}}, request=request)
            return httpx.Response(
                200, json={"id": draft_id, "isDraft": not drafts[draft_id]}, request=request
            )
        if "/v1.0/me/messages/" in path:
            msg_id = path.split("/")[-1]
            return httpx.Response(
                200,
                json={
                    "id": msg_id,
                    "conversationId": "conv-contract-001",
                    "receivedDateTime": "2026-09-16T12:00:00Z",
                },
                request=request,
            )
        if path == "/v1.0/me/messages" and method == "GET" and request.url.query:
            value: list[dict[str, Any]] = [
                {"id": "graph-msg-sync-1", "conversationId": "conv-contract-001", "isDraft": False,
                 "internetMessageId": "<orig-001@example.com>", "sentDateTime": "2026-09-16T12:00:00Z"}
            ]
            for draft_id, sent in drafts.items():
                value.append(
                    {"id": draft_id, "conversationId": "conv-contract-001", "isDraft": not sent,
                     "internetMessageId": rfc_ids[draft_id],
                     "sentDateTime": "2026-09-28T12:00:00Z" if sent else None}
                )
            return httpx.Response(200, json={"value": value}, request=request)
        return httpx.Response(404, json={"error": "Not Found"}, request=request)

    return httpx.MockTransport(handler)
```

The appended Graph tests:

```python
# ---------------------------------------------------------------------------
# 6.3a: createReply + send with immutable ids; status; sent lookup; errors.
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"


def _graph_mailbox() -> Mailbox:
    return Mailbox(
        id="mbx-graph-63a",
        organization_id="org-63a",
        address="support@example.com",
        provider="graph",
    )


def _graph_reply(original_id: str | None = "AAMkAGI2-orig=") -> OutboundReply:
    return OutboundReply(
        thread_id="AAQkAGI2-conv=",
        mailbox_id="mbx-graph-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="customer@example.com", name="Customer")],
        cc=[EmailAddress(email="lead@example.com")],
        body_text="Your order shipped.",
        subject="Re: Order",
        reply_to_provider_message_id=original_id,
    )


def _graph_client(
    routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        recorded = routes.get((request.method, request.url.path))
        if recorded is None:
            return httpx.Response(
                404,
                json={"error": {"code": "ErrorItemNotFound", "message": "Not found."}},
                request=request,
            )
        return httpx.Response(
            recorded.status_code,
            headers=recorded.headers,
            content=recorded.content,
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_graph_create_draft_uses_create_reply_with_immutable_ids() -> None:
    """6.3a / R17.2: the draft is a createReply on the original, not a new message."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/v1.0/me/messages/AAMkAGI2-orig=/createReply"): httpx.Response(
            201,
            json={"id": "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Ad-draft", "conversationId": "AAQkAGI2-conv=", "isDraft": True},
        )
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)

    draft = await adapter.create_draft(_graph_mailbox(), _graph_reply())

    assert draft.provider_draft_id == "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Ad-draft"
    assert draft.provider_message_id == draft.provider_draft_id
    assert draft.provider_thread_id == "AAQkAGI2-conv="
    request = seen[0]
    assert request.headers["Prefer"] == 'IdType="ImmutableId"'
    body = json.loads(request.content)
    assert "comment" not in body
    assert body["message"]["body"] == {"contentType": "Text", "content": "Your order shipped."}
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "customer@example.com"
    assert body["message"]["ccRecipients"][0]["emailAddress"]["address"] == "lead@example.com"


@pytest.mark.asyncio
async def test_graph_create_draft_without_original_id_is_permanent() -> None:
    """Without the original message id there is nothing to reply to: permanent."""
    seen: list[httpx.Request] = []
    adapter = GraphProviderAdapter(client=_graph_client({}, seen), base_url=GRAPH_BASE)
    with pytest.raises(Permanent):
        await adapter.create_draft(_graph_mailbox(), _graph_reply(None))
    assert seen == []


@pytest.mark.asyncio
async def test_graph_send_draft_posts_send_and_returns_the_immutable_id() -> None:
    """6.3a / R17.4: send returns 202 with no body; the stored id is the real message id."""
    seen: list[httpx.Request] = []
    routes = {("POST", "/v1.0/me/messages/draft-imm-1/send"): httpx.Response(202)}
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)

    sent = await adapter.send_draft(_graph_mailbox(), "draft-imm-1")

    assert sent.provider_message_id == "draft-imm-1"
    assert seen[0].headers["Prefer"] == 'IdType="ImmutableId"'


@pytest.mark.asyncio
async def test_graph_send_reply_never_calls_send_mail() -> None:
    """6.3a / design §5.8: send_reply is createReply + send; sendMail starts a new conversation."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/v1.0/me/messages/AAMkAGI2-orig=/createReply"): httpx.Response(
            201, json={"id": "draft-imm-2", "conversationId": "AAQkAGI2-conv=", "isDraft": True}
        ),
        ("POST", "/v1.0/me/messages/draft-imm-2/send"): httpx.Response(202),
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)

    sent = await adapter.send_reply(_graph_mailbox(), _graph_reply())

    assert [r.url.path for r in seen] == [
        "/v1.0/me/messages/AAMkAGI2-orig=/createReply",
        "/v1.0/me/messages/draft-imm-2/send",
    ]
    assert sent.provider_message_id == "draft-imm-2"
    assert sent.provider_thread_id == "AAQkAGI2-conv="


@pytest.mark.parametrize(
    ("is_draft", "expected"),
    [(True, ProviderDraftStatus.DRAFT), (False, ProviderDraftStatus.SENT)],
)
@pytest.mark.asyncio
async def test_graph_get_draft_status_reads_is_draft(
    is_draft: bool, expected: ProviderDraftStatus
) -> None:
    """6.3a: isDraft true is DRAFT, false is SENT (the Sent Items copy keeps the id)."""
    seen: list[httpx.Request] = []
    routes = {
        ("GET", "/v1.0/me/messages/draft-imm-3"): httpx.Response(
            200, json={"id": "draft-imm-3", "isDraft": is_draft}
        )
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    assert await adapter.get_draft_status(_graph_mailbox(), "draft-imm-3") is expected
    assert seen[0].url.params["$select"] == "id,isDraft"


@pytest.mark.asyncio
async def test_graph_get_draft_status_missing_on_404() -> None:
    """6.3a: a deleted draft is MISSING."""
    seen: list[httpx.Request] = []
    adapter = GraphProviderAdapter(client=_graph_client({}, seen), base_url=GRAPH_BASE)
    status = await adapter.get_draft_status(_graph_mailbox(), "draft-gone")
    assert status is ProviderDraftStatus.MISSING


@pytest.mark.asyncio
async def test_graph_find_sent_message_by_id_or_internet_message_id() -> None:
    """6.3a: only an isDraft=false item matches, by immutable id or internetMessageId."""
    seen: list[httpx.Request] = []
    listing: dict[str, Any] = {
        "value": [
            {"id": "draft-unsent", "isDraft": True, "internetMessageId": "<unsent@outlook.com>"},
            {"id": "draft-imm-4", "isDraft": False, "internetMessageId": "<AM0PR01@outlook.com>",
             "sentDateTime": "2026-09-28T12:00:00Z"},
        ]
    }
    routes = {("GET", "/v1.0/me/messages"): httpx.Response(200, json=listing)}
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    mailbox = _graph_mailbox()

    by_id = await adapter.find_sent_message(mailbox, "AAQk'conv", "draft-imm-4")
    assert by_id is not None and by_id.provider_message_id == "draft-imm-4"
    assert seen[0].url.params["$filter"] == "conversationId eq 'AAQk''conv'"
    by_rfc = await adapter.find_sent_message(mailbox, "AAQk'conv", "AM0PR01@outlook.com")
    assert by_rfc is not None and by_rfc.provider_message_id == "draft-imm-4"
    assert await adapter.find_sent_message(mailbox, "AAQk'conv", "draft-unsent") is None


@pytest.mark.asyncio
async def test_graph_create_draft_sets_our_message_id() -> None:
    """6.5 / D3: the draft carries our Message-ID as internetMessageId, so find_draft can adopt it."""
    seen: list[httpx.Request] = []
    routes = {
        ("POST", "/v1.0/me/messages/AAMkAGI2-orig=/createReply"): httpx.Response(
            201, json={"id": "draft-imm-5", "conversationId": "AAQkAGI2-conv=", "isDraft": True}
        )
    }
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    reply = OutboundReply(
        thread_id="AAQkAGI2-conv=",
        mailbox_id="mbx-graph-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="customer@example.com")],
        body_text="Your order shipped.",
        subject="Re: Order",
        message_id="dispatch-abc@example.com",
        reply_to_provider_message_id="AAMkAGI2-orig=",
    )

    await adapter.create_draft(_graph_mailbox(), reply)

    body = json.loads(seen[0].content)
    assert body["message"]["internetMessageId"] == "<dispatch-abc@example.com>"


@pytest.mark.asyncio
async def test_graph_find_draft_matches_only_an_unsent_draft_with_our_message_id() -> None:
    """6.5: the orphan-draft lookup ignores sent copies and other drafts."""
    seen: list[httpx.Request] = []
    listing: dict[str, Any] = {
        "value": [
            {"id": "draft-other", "isDraft": True, "internetMessageId": "<someone-else@outlook.com>",
             "conversationId": "AAQk'conv"},
            {"id": "sent-ours", "isDraft": False, "internetMessageId": "<dispatch-abc@example.com>",
             "conversationId": "AAQk'conv"},
            {"id": "draft-ours", "isDraft": True, "internetMessageId": "<dispatch-abc@example.com>",
             "conversationId": "AAQk'conv"},
        ]
    }
    routes = {("GET", "/v1.0/me/messages"): httpx.Response(200, json=listing)}
    adapter = GraphProviderAdapter(client=_graph_client(routes, seen), base_url=GRAPH_BASE)
    mailbox = _graph_mailbox()

    found = await adapter.find_draft(mailbox, "AAQk'conv", "dispatch-abc@example.com")
    assert found is not None
    assert (found.provider_draft_id, found.provider_message_id) == ("draft-ours", "draft-ours")
    assert seen[0].url.params["$filter"] == "conversationId eq 'AAQk''conv'"
    assert seen[0].headers["Prefer"] == 'IdType="ImmutableId"'
    assert await adapter.find_draft(mailbox, "AAQk'conv", "<missing@example.com>") is None


@pytest.mark.asyncio
async def test_graph_503_retry_after_is_retryable_and_403_is_permanent() -> None:
    """6.3a / R17.5: 503 keeps Retry-After as retry_after_s; Graph 403 stays AuthExpired."""

    def handler_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "10"}, json={"error": {"code": "serviceNotAvailable"}}, request=request)

    adapter = GraphProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler_503)))
    with pytest.raises(Transient) as exc_info:
        await adapter._request("GET", GRAPH_BASE)
    assert isinstance(exc_info.value, RetryableProviderError)
    assert exc_info.value.retry_after_s == 10.0

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}, request=request)

    adapter_403 = GraphProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler_403)))
    with pytest.raises(AuthExpired) as exc_403:
        await adapter_403._request("GET", GRAPH_BASE)
    assert isinstance(exc_403.value, PermanentProviderError)
```

- [ ] **Step 7: Make the Gmail contract transport stateful**

In `tests/unit/test_gmail_adapter.py`, replace `create_mock_gmail_transport` (lines 129–196) with:

```python
def create_mock_gmail_transport() -> httpx.MockTransport:
    """Recorded Gmail API responses; drafts.send deletes the draft and adds a SENT message."""
    raw_b64 = encode_urlsafe_b64(b"From: user@example.com\r\nSubject: Contract\r\n\r\nBody")
    state: dict[str, bool] = {"draft_exists": False, "sent": False}

    def meta(msg_id: str, labels: list[str], rfc_id: str) -> dict[str, Any]:
        return {
            "id": msg_id,
            "threadId": "th-001",
            "labelIds": labels,
            "internalDate": "1790000000000",
            "payload": {"headers": [{"name": "Message-ID", "value": rfc_id}]},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        method = request.method

        if "/watch" in url:
            return httpx.Response(200, json={"historyId": "100", "expiration": "1789000000000"}, request=request)
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "history": [{"messagesAdded": [{"message": {"id": "msg-001", "threadId": "th-001"}}]}],
                    "historyId": "105",
                },
                request=request,
            )
        if path.endswith("/drafts/send") and method == "POST":
            if not state["draft_exists"]:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}}, request=request)
            state["draft_exists"] = False
            state["sent"] = True
            return httpx.Response(
                200, json={"id": "sent-msg-102", "threadId": "th-001", "labelIds": ["SENT"]}, request=request
            )
        if path.endswith("/drafts") and method == "GET":
            # drafts.list?q=rfc822msgid:<id> (find_draft): only the unsent contract draft matches.
            query = request.url.params.get("q", "")
            listed = (
                [{"id": "draft-101", "message": {"id": "msg-draft-101", "threadId": "th-001"}}]
                if state["draft_exists"] and query == "rfc822msgid:contract-reply-001@example.com"
                else []
            )
            return httpx.Response(
                200, json={"drafts": listed, "resultSizeEstimate": len(listed)}, request=request
            )
        if path.endswith("/drafts") and method == "POST":
            state["draft_exists"] = True
            return httpx.Response(
                200,
                json={"id": "draft-101", "message": {"id": "msg-draft-101", "threadId": "th-001"}},
                request=request,
            )
        if path.endswith("/drafts/draft-101") and method == "GET" and state["draft_exists"]:
            return httpx.Response(200, json={"id": "draft-101", "message": {"id": "msg-draft-101"}}, request=request)
        if "/messages?" in url or path.rstrip("/").endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "msg-001", "threadId": "th-001"}]}, request=request)
        if "/messages/send" in url and method == "POST":
            return httpx.Response(200, json={"id": "sent-msg-101", "threadId": "th-001"}, request=request)
        if "/messages/msg-001" in url:
            return httpx.Response(
                200,
                json={"id": "msg-001", "threadId": "th-001", "raw": raw_b64, "internalDate": "1726500000000"},
                request=request,
            )
        if path.endswith("/threads/th-001") and request.url.params.get("format") == "metadata":
            messages = [meta("msg-001", ["INBOX"], "<orig-001@example.com>")]
            if state["draft_exists"]:
                messages.append(meta("msg-draft-101", ["DRAFT"], "<contract-reply-001@example.com>"))
            if state["sent"]:
                messages.append(meta("sent-msg-102", ["SENT"], "<contract-reply-001@example.com>"))
            return httpx.Response(200, json={"id": "th-001", "messages": messages}, request=request)
        if "/threads/th-001" in url:
            return httpx.Response(
                200,
                json={"id": "th-001", "messages": [{"id": "msg-001", "threadId": "th-001", "raw": raw_b64}]},
                request=request,
            )
        return httpx.Response(404, json={"error": "Not Found"}, request=request)

    return httpx.MockTransport(handler)
```

`drafts/send` is matched before `drafts` (GET is `find_draft`'s `drafts.list`, POST is `drafts.create`), and `send_reply` still posts `messages/send`. The `"/messages?"` check matches only list queries and does not catch `/messages/send`, as before.

- [ ] **Step 8: Implement the fake adapter changes**

In `packages/adapters/fake.py`:
- Add `- R17.1, R17.3 (task 6.3a): draft send/status/lookup, method-targeted and after-success fault injection.` to the docstring.
- Add `from packages.domain import ProviderDraftStatus` to the imports.

Then replace `__init__`, the two count properties, the five `inject_*` methods, `_maybe_raise_fault`, `create_draft` and `send_reply` with the code below, and add the new methods:

```python
    def __init__(
        self,
        batch_size: int = 50,
        mailbox_id: str = "mbx-fake",
        *,
        deletes_sent_drafts: bool = False,
        **kwargs: Any,
    ) -> None:
        self.batch_size = batch_size
        self.mailbox_id = mailbox_id
        # False: Graph-like (sent draft reads SENT, keeps its id).
        # True: Gmail-like (sent draft is deleted -> MISSING, sent copy gets a new id).
        self.deletes_sent_drafts = deletes_sent_drafts
        self.is_connected = False
        self.kwargs = kwargs
        self._messages: dict[str, RawMessage] = {}
        self._message_order: list[str] = []
        self._threads: dict[str, list[str]] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._drafts: dict[str, OutboundReply] = {}
        self._draft_message_ids: dict[str, str] = {}
        self._sent_drafts: dict[str, SentRef] = {}
        self._deleted_drafts: set[str] = set()
        self._sent_messages: list[tuple[SentRef, OutboundReply]] = []
        self._expired_checkpoints: set[str] = set()
        self._injected_faults: list[dict[str, Any]] = []
        self._draft_counter = 0
        self._sent_counter = 0
        self._sub_counter = 0
        self._history_counter = 0

    @property
    def sent_count(self) -> int:
        """Count of outbound messages dispatched (send_reply and send_draft)."""
        return len(self._sent_messages)

    @property
    def draft_count(self) -> int:
        """Count of drafts ever created (sending or deleting one does not lower it)."""
        return self._draft_counter

    @property
    def pending_draft_count(self) -> int:
        """Drafts that exist and are unsent."""
        return sum(
            1
            for draft_id in self._drafts
            if draft_id not in self._sent_drafts and draft_id not in self._deleted_drafts
        )

    def _add_fault(
        self,
        fault_type: str,
        calls: int,
        message: str,
        method: str | None,
        after_success: bool,
        **extra: Any,
    ) -> None:
        self._injected_faults.append(
            {
                "type": fault_type,
                "calls": calls,
                "message": message,
                "method": method,
                "phase": "after" if after_success else "before",
                **extra,
            }
        )

    def inject_rate_limit(
        self,
        retry_after: float = 30.0,
        calls: int = 1,
        message: str = "Provider rate limit exceeded",
        *,
        method: str | None = None,
        after_success: bool = False,
    ) -> None:
        """Inject a RateLimited fault carrying retry_after."""
        self._add_fault(
            "rate_limited", calls, message, method, after_success, retry_after=retry_after
        )

    def inject_auth_expired(
        self,
        calls: int = 1,
        message: str = "Credentials expired or revoked",
        *,
        method: str | None = None,
        after_success: bool = False,
    ) -> None:
        """Inject an AuthExpired fault."""
        self._add_fault("auth_expired", calls, message, method, after_success)

    def inject_transient_failure(
        self,
        message: str = "Temporary network error",
        calls: int = 1,
        *,
        method: str | None = None,
        after_success: bool = False,
    ) -> None:
        """Inject a Transient fault; with after_success it models an ambiguous send."""
        self._add_fault("transient", calls, message, method, after_success)

    def inject_permanent_failure(
        self,
        message: str = "Fatal request format error",
        calls: int = 1,
        *,
        method: str | None = None,
        after_success: bool = False,
    ) -> None:
        """Inject a Permanent fault."""
        self._add_fault("permanent", calls, message, method, after_success)

    def inject_not_found(
        self,
        message: str = "Resource not found",
        calls: int = 1,
        *,
        method: str | None = None,
        after_success: bool = False,
    ) -> None:
        """Inject a NotFound fault."""
        self._add_fault("not_found", calls, message, method, after_success)

    def _maybe_raise_fault(
        self,
        method_name: str,
        mailbox: Mailbox | None = None,
        phase: str = "before",
    ) -> None:
        """Fire the first injected fault targeting this method (or any) in this phase."""
        index = next(
            (
                i
                for i, fault in enumerate(self._injected_faults)
                if fault.get("method") in (None, method_name)
                and fault.get("phase", "before") == phase
            ),
            None,
        )
        if index is None:
            return

        fault = self._injected_faults[index]
        fault["calls"] -= 1
        if fault["calls"] <= 0:
            self._injected_faults.pop(index)

        mbx_id = str(mailbox.id) if mailbox else None
        ftype = fault["type"]
        msg = fault["message"]

        if ftype == "rate_limited":
            raise RateLimited(
                msg,
                retry_after=fault.get("retry_after"),
                provider="fake",
                mailbox_id=mbx_id,
            )
        if ftype == "auth_expired":
            raise AuthExpired(msg, provider="fake", mailbox_id=mbx_id)
        if ftype == "transient":
            raise Transient(msg, provider="fake", mailbox_id=mbx_id)
        if ftype == "permanent":
            raise Permanent(msg, provider="fake", mailbox_id=mbx_id)
        if ftype == "not_found":
            raise NotFound(msg, provider="fake", mailbox_id=mbx_id)
        raise ProviderError(msg, provider="fake", mailbox_id=mbx_id)
```

```python
    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        """Store a draft in memory; the DraftRef carries its draft and message ids."""
        self._maybe_raise_fault("create_draft", mailbox)
        self._draft_counter += 1
        draft_id = f"draft-fake-{self._draft_counter}"
        draft_message_id = f"draft-msg-fake-{self._draft_counter}"
        self._drafts[draft_id] = reply
        self._draft_message_ids[draft_id] = draft_message_id
        self._maybe_raise_fault("create_draft", mailbox, phase="after")
        return DraftRef(
            provider_draft_id=draft_id,
            provider_message_id=draft_message_id,
            provider_thread_id=str(reply.thread_id),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        """Record outbound message dispatch and return SentRef."""
        self._maybe_raise_fault("send_reply", mailbox)
        self._sent_counter += 1
        sent_id = f"sent-fake-{self._sent_counter}"
        ref = SentRef(
            provider_message_id=sent_id,
            provider_thread_id=str(reply.thread_id),
            sent_at=datetime.now(UTC),
        )
        self._sent_messages.append((ref, reply))
        self._maybe_raise_fault("send_reply", mailbox, phase="after")
        return ref

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        """Send an unsent draft once; a sent, deleted or unknown draft is NotFound."""
        self._maybe_raise_fault("send_draft", mailbox)
        if (
            provider_draft_id not in self._drafts
            or provider_draft_id in self._sent_drafts
            or provider_draft_id in self._deleted_drafts
        ):
            raise NotFound(
                f"Draft '{provider_draft_id}' not found.",
                provider="fake",
                mailbox_id=str(mailbox.id),
            )
        reply = self._drafts[provider_draft_id]
        if self.deletes_sent_drafts:
            self._sent_counter += 1
            sent_id = f"sent-fake-{self._sent_counter}"
        else:
            sent_id = self._draft_message_ids[provider_draft_id]
        ref = SentRef(
            provider_message_id=sent_id,
            provider_thread_id=str(reply.thread_id),
            sent_at=datetime.now(UTC),
        )
        self._sent_drafts[provider_draft_id] = ref
        self._sent_messages.append((ref, reply))
        self._maybe_raise_fault("send_draft", mailbox, phase="after")
        return ref

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        """DRAFT until sent; then SENT, or MISSING when the fake deletes sent drafts."""
        self._maybe_raise_fault("get_draft_status", mailbox)
        if provider_draft_id in self._deleted_drafts or provider_draft_id not in self._drafts:
            return ProviderDraftStatus.MISSING
        if provider_draft_id in self._sent_drafts:
            if self.deletes_sent_drafts:
                return ProviderDraftStatus.MISSING
            return ProviderDraftStatus.SENT
        return ProviderDraftStatus.DRAFT

    async def find_sent_message(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        provider_message_id: str,
    ) -> SentRef | None:
        """Find a sent draft in the thread by sent message id or by the reply's Message-ID."""
        self._maybe_raise_fault("find_sent_message", mailbox)
        wanted = provider_message_id.strip().strip("<>").strip()
        for draft_id, ref in self._sent_drafts.items():
            if ref.provider_thread_id != provider_thread_id:
                continue
            reply = self._drafts[draft_id]
            rfc_id = (reply.message_id or "").strip().strip("<>").strip()
            if ref.provider_message_id == provider_message_id or (rfc_id and rfc_id == wanted):
                return ref
        return None

    async def find_draft(
        self,
        mailbox: Mailbox,
        provider_thread_id: str,
        message_id: str,
    ) -> DraftRef | None:
        """Find an unsent, undeleted draft in the thread carrying this Message-ID (6.5)."""
        self._maybe_raise_fault("find_draft", mailbox)
        wanted = message_id.strip().strip("<>").strip()
        if not wanted:
            return None
        for draft_id, reply in self._drafts.items():
            if draft_id in self._sent_drafts or draft_id in self._deleted_drafts:
                continue
            if str(reply.thread_id) != provider_thread_id:
                continue
            if (reply.message_id or "").strip().strip("<>").strip() == wanted:
                return DraftRef(
                    provider_draft_id=draft_id,
                    provider_message_id=self._draft_message_ids[draft_id],
                    provider_thread_id=str(reply.thread_id),
                )
        return None

    def delete_draft(self, provider_draft_id: str) -> None:
        """Test helper: a person deletes the draft in the mailbox before it is sent."""
        self._deleted_drafts.add(provider_draft_id)
```

- [ ] **Step 9: Write the fake-specific tests**

Append to `tests/unit/test_fake_adapter.py`. Add `from packages.domain import ProviderDraftStatus` and `from packages.domain.entities import EmailAddress, OutboundReply` to the imports.

```python
def _fake_mailbox() -> Mailbox:
    return Mailbox(id="mbx-fake-63a", organization_id="org-63a", provider="gmail", address="s@x.com")


def _fake_reply() -> OutboundReply:
    return OutboundReply(
        thread_id="th-63a",
        mailbox_id="mbx-fake-63a",
        organization_id="org-63a",
        to=[EmailAddress(email="c@example.com")],
        body_text="Reply",
        message_id="<fake-reply-63a@mail.example.com>",
    )


@pytest.mark.asyncio
async def test_fake_gmail_like_mode_deletes_sent_drafts() -> None:
    """6.3a: deletes_sent_drafts models Gmail: MISSING after send, new id, found by Message-ID."""
    adapter = FakeProviderAdapter(deletes_sent_drafts=True)
    mailbox = _fake_mailbox()
    draft = await adapter.create_draft(mailbox, _fake_reply())
    sent = await adapter.send_draft(mailbox, draft.provider_draft_id)

    assert sent.provider_message_id != draft.provider_message_id
    assert await adapter.get_draft_status(mailbox, draft.provider_draft_id) is ProviderDraftStatus.MISSING
    assert await adapter.find_sent_message(mailbox, "th-63a", draft.provider_message_id or "") is None
    found = await adapter.find_sent_message(mailbox, "th-63a", "fake-reply-63a@mail.example.com")
    assert found == sent
    assert adapter.draft_count == 1
    assert adapter.pending_draft_count == 0
    assert adapter.sent_count == 1


@pytest.mark.asyncio
async def test_fake_graph_like_mode_keeps_the_draft_id() -> None:
    """6.3a: default mode models Graph immutable ids: SENT after send, same id."""
    adapter = FakeProviderAdapter()
    mailbox = _fake_mailbox()
    draft = await adapter.create_draft(mailbox, _fake_reply())
    sent = await adapter.send_draft(mailbox, draft.provider_draft_id)

    assert sent.provider_message_id == draft.provider_message_id
    assert await adapter.get_draft_status(mailbox, draft.provider_draft_id) is ProviderDraftStatus.SENT
    assert await adapter.find_sent_message(mailbox, "th-63a", sent.provider_message_id) == sent
    assert await adapter.find_sent_message(mailbox, "other-thread", sent.provider_message_id) is None


@pytest.mark.asyncio
async def test_fake_send_draft_twice_is_not_found_and_sends_once() -> None:
    """A second send of the same draft cannot produce a second email."""
    adapter = FakeProviderAdapter()
    mailbox = _fake_mailbox()
    draft = await adapter.create_draft(mailbox, _fake_reply())
    await adapter.send_draft(mailbox, draft.provider_draft_id)
    with pytest.raises(NotFound):
        await adapter.send_draft(mailbox, draft.provider_draft_id)
    assert adapter.sent_count == 1


@pytest.mark.asyncio
async def test_fake_deleted_draft_is_missing_and_never_found() -> None:
    """design §5.8 step 4: a person deleted the draft -> MISSING and no sent copy."""
    adapter = FakeProviderAdapter()
    mailbox = _fake_mailbox()
    draft = await adapter.create_draft(mailbox, _fake_reply())
    adapter.delete_draft(draft.provider_draft_id)
    assert await adapter.get_draft_status(mailbox, draft.provider_draft_id) is ProviderDraftStatus.MISSING
    assert await adapter.find_sent_message(mailbox, "th-63a", "fake-reply-63a@mail.example.com") is None
    with pytest.raises(NotFound):
        await adapter.send_draft(mailbox, draft.provider_draft_id)


@pytest.mark.asyncio
async def test_fake_after_success_fault_models_an_ambiguous_send() -> None:
    """6.3a: the send happens, then the caller sees Transient (the crash-after-send case)."""
    adapter = FakeProviderAdapter()
    mailbox = _fake_mailbox()
    draft = await adapter.create_draft(mailbox, _fake_reply())
    adapter.inject_transient_failure(method="send_draft", after_success=True)

    with pytest.raises(Transient):
        await adapter.send_draft(mailbox, draft.provider_draft_id)

    assert adapter.sent_count == 1
    assert await adapter.get_draft_status(mailbox, draft.provider_draft_id) is ProviderDraftStatus.SENT


@pytest.mark.asyncio
async def test_fake_method_targeted_fault_skips_other_methods() -> None:
    """A fault aimed at send_draft does not fire on create_draft."""
    adapter = FakeProviderAdapter()
    mailbox = _fake_mailbox()
    adapter.inject_rate_limit(retry_after=7.0, method="send_draft")
    draft = await adapter.create_draft(mailbox, _fake_reply())
    with pytest.raises(RateLimited) as exc_info:
        await adapter.send_draft(mailbox, draft.provider_draft_id)
    assert exc_info.value.retry_after_s == 7.0
    assert adapter.sent_count == 0


@pytest.mark.asyncio
async def test_fake_find_draft_skips_sent_deleted_and_other_threads() -> None:
    """6.5: only an unsent draft of this thread with our Message-ID is adopted."""
    adapter = FakeProviderAdapter()
    mailbox = _fake_mailbox()
    first = await adapter.create_draft(mailbox, _fake_reply())
    found = await adapter.find_draft(mailbox, "th-63a", "fake-reply-63a@mail.example.com")
    assert found is not None and found.provider_draft_id == first.provider_draft_id
    assert await adapter.find_draft(mailbox, "other-thread", "<fake-reply-63a@mail.example.com>") is None
    await adapter.send_draft(mailbox, first.provider_draft_id)
    assert await adapter.find_draft(mailbox, "th-63a", "<fake-reply-63a@mail.example.com>") is None
    second = await adapter.create_draft(mailbox, _fake_reply())
    adapter.delete_draft(second.provider_draft_id)
    assert await adapter.find_draft(mailbox, "th-63a", "<fake-reply-63a@mail.example.com>") is None
```

- [ ] **Step 10: Give the two structural test adapters the new methods**

In `tests/unit/test_mail_adapter_contract.py`, add `from packages.domain import ProviderDraftStatus` to the imports. Replace `ConformingMockAdapter.create_draft` and `send_reply` with the code below, and add the new members. The mock runs the full contract suite, so it keeps a little state.

```python
    def __init__(self) -> None:
        self._sent: set[str] = set()
        self._draft_message_ids: set[str] = set()

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        if reply.message_id:
            self._draft_message_ids.add(reply.message_id.strip("<>"))
        return DraftRef(
            provider_draft_id="draft-001",
            provider_message_id="draft-msg-001",
            provider_thread_id=str(reply.thread_id),
        )

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        return SentRef(
            provider_message_id="sent-001",
            provider_thread_id=str(reply.thread_id),
            sent_at=datetime.now(UTC),
        )

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        self._sent.add(provider_draft_id)
        return SentRef(provider_message_id="draft-msg-001", sent_at=datetime.now(UTC))

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        if provider_draft_id != "draft-001":
            return ProviderDraftStatus.MISSING
        if provider_draft_id in self._sent:
            return ProviderDraftStatus.SENT
        return ProviderDraftStatus.DRAFT

    async def find_sent_message(
        self, mailbox: Mailbox, provider_thread_id: str, provider_message_id: str
    ) -> SentRef | None:
        if "draft-001" in self._sent and provider_message_id == "draft-msg-001":
            return SentRef(provider_message_id="draft-msg-001", provider_thread_id=provider_thread_id)
        return None

    async def find_draft(
        self, mailbox: Mailbox, provider_thread_id: str, message_id: str
    ) -> DraftRef | None:
        if "draft-001" in self._sent or message_id.strip("<>") not in self._draft_message_ids:
            return None
        return DraftRef(
            provider_draft_id="draft-001",
            provider_message_id="draft-msg-001",
            provider_thread_id=provider_thread_id,
        )
```

In `tests/unit/test_adapter_registry.py`, add `from packages.domain import ProviderDraftStatus` to the imports and append to `ConformingDummyAdapter`:

```python
    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        return SentRef(provider_message_id="sent-1")

    async def get_draft_status(
        self, mailbox: Mailbox, provider_draft_id: str
    ) -> ProviderDraftStatus:
        return ProviderDraftStatus.MISSING

    async def find_sent_message(
        self, mailbox: Mailbox, provider_thread_id: str, provider_message_id: str
    ) -> SentRef | None:
        return None

    async def find_draft(
        self, mailbox: Mailbox, provider_thread_id: str, message_id: str
    ) -> DraftRef | None:
        return None
```

In the same file, update the module docstring's "exposing the 7 methods" to "exposing the 11 methods". `DraftRef` is already imported there.

- [ ] **Step 11: Run all adapter tests**

Run: `uv run pytest tests/unit/test_fake_adapter.py tests/unit/test_graph_adapter.py tests/unit/test_gmail_adapter.py tests/unit/test_mail_adapter_contract.py tests/unit/test_adapter_registry.py tests/unit/test_outbound_reply.py tests/unit/test_mail_sync_consumer.py tests/unit/test_subscription_renewal.py -v`
Expected: PASS. Each of the four contract subclasses (Conforming, Gmail, Graph, Fake) runs 16 contract tests.

- [ ] **Step 12: Full regression, lint, types**

Run: `uv run ruff format packages tests && uv run ruff check . && uv run mypy packages services tests evaluation && uv run pytest tests/unit -q`
Expected:
- `All checks passed!`
- `Success: no issues found in N source files`
- the unit suite green, including `tests/unit/test_dependency_rules.py`. Nothing under `services/` changed.

- [ ] **Step 13: Commit**

```bash
git add packages/domain/entities.py packages/dispatch/reply.py packages/adapters/protocol.py packages/adapters/gmail.py packages/adapters/graph.py packages/adapters/fake.py packages/adapters/testing.py tests/unit/test_gmail_adapter.py tests/unit/test_graph_adapter.py tests/unit/test_fake_adapter.py tests/unit/test_mail_adapter_contract.py tests/unit/test_adapter_registry.py tests/unit/test_outbound_reply.py
git commit -m "feat(adapters): Graph createReply+send with immutable ids, draft send/status/lookup on protocol, fake and contract suite [task 6.3a] [R1.1, R17.1, R17.2, R17.3, R17.5]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

---

## Part C (Tasks 6–8): Review API, dispatch core, dispatch-worker

Skills applied while writing this part: `fullstack-dev-skills:python-pro`, `fullstack-dev-skills:fastapi-expert`, `fullstack-dev-skills:postgres-pro`.

```
                      reviewer (UI / curl)                               provider (fake in CI, Gmail live)
                            │                                                        ▲
 GET/PATCH/approve/reject   ▼                                                        │ create_draft / send_draft /
 ┌──────────────────── services/api/routers/drafts.py ───────────────────┐           │ get_draft_status / find_sent_message
 │ ReviewStore (packages/db/review.py)                                    │           │
 │   approve txn: lock job → lock draft → status=approved + feedback row  │           │
 │   reject  txn: lock job → lock draft → status=rejected + feedback row  │           │
 │                + DRAFTED→COMPLETED (transition_job_state_on)          │           │
 │ after COMMIT: publish JobEnvelope(job_type="dispatch") ───────────────┼──▶ email.dispatch
 │ draft_decisions_total{decision,category} (first decision only)        │           │
 └────────────────────────────────────────────────────────────────────────┘           │
                                                                                      │
 services/dispatch_worker ── DispatchConsumer(BaseConsumer) ── DispatchService.dispatch(org, job)
   prepare_delivery(): no recovery, no lease                     │
   retry_after_s(exc) → Retry-After tier                         1 claim   (txn: key + queue_name + DRAFTED|RETRY_PENDING→DISPATCHED)
   failure_policy: RETRY | DEAD_LETTER                           2 draft   (create or reuse; record provider_draft_id/_message_id)
                                                                   └ create_draft mode → 5 finish (no outbound row)
                                                                 3 send    (send_draft)
                                                                 4 confirm (resumed: get_draft_status → DRAFT/SENT/MISSING→find_sent_message)
                                                                 5 finish  (txn: draft dispatched + outbound email_message + thread + DISPATCHED→COMPLETED)
```

### Contract names Part C consumes from Tasks 1–5 (binding, not re-implemented here)

- Migration 0005: `generated_draft.provider_draft_id`, `provider_draft_message_id`, `dispatch_idempotency_key UNIQUE`; `feedback.review_ms`, `UNIQUE (draft_id)` (so `ON CONFLICT (draft_id)` works).
- `GeneratedDraft` gains `provider_draft_id: str | None = None`, `provider_draft_message_id: str | None = None`, `dispatch_idempotency_key: str | None = None`, and `packages/db/draft.py::_row_to_draft` / `insert_draft` / `InMemoryDraftStore.create_draft` map them (Part A).
- `packages.domain` re-exports `DispatchMode` (`CREATE_DRAFT`, `SEND_REPLY`) and `ProviderDraftStatus` (`DRAFT`, `SENT`, `MISSING`).
- State machine: `RETRY_PENDING → {GENERATING, DISPATCHED, FAILED}`; the lease reaper skips `DISPATCHED` (Part A).
- `CategoryDefinition.dispatch_mode` (default `DispatchMode.CREATE_DRAFT`), `auto_send_eligible` stays `False` (Task 3, 6.4).
- `packages.dispatch.reply.build_outbound_reply(*, draft, original, provider_thread_id, message_id_domain) -> OutboundReply` and `packages.dispatch.reply.MissingProviderThreadError(*, draft_id: str, thread_id: str)` (keyword-only, Task 2). **Part C relies on `OutboundReply.message_id` being a deterministic function of `(draft.id, message_id_domain)`**: a redelivered dispatch rebuilds the reply and looks up the sent copy by that id (step 4).
- `MailProviderAdapter.send_draft(mailbox, provider_draft_id) -> SentRef`, `get_draft_status(mailbox, provider_draft_id) -> ProviderDraftStatus`, `find_sent_message(mailbox, provider_thread_id, provider_message_id) -> SentRef | None`, `find_draft(mailbox, provider_thread_id, message_id) -> DraftRef | None`; `FakeProviderAdapter.create_draft` returns a `DraftRef` whose `provider_message_id` is set (Part B). Part C's test double `tests/stubs/dispatch_fakes.py::RecordingFake` subclasses `FakeProviderAdapter` and overrides `send_draft`, `get_draft_status`, `find_sent_message` and `find_draft`, so Part C's tests depend only on the fake's `create_draft`.
- Adapter errors (Task 4): retryable base `packages.adapters.exceptions.RetryableProviderError` (attribute `retry_after_s: float | None`; subclasses `RateLimited`, `Transient`); permanent base `PermanentProviderError` (subclasses `AuthExpired`, `NotFound`, `Permanent`). The dispatch-worker branches on the two bases and reads `retry_after_s`.
- `find_sent_message` lookup rule (Task 5 contract suite): stored `provider_draft_message_id` first (Graph), then our `Message-ID` (Gmail).

### Task 6: Draft review API, feedback rows and `draft_decisions_total` [tasks.md 6.1, 6.2]

**Files:**
- Create: `packages/domain/review.py` (draft status, feedback decision, character edit distance)
- Modify: `packages/domain/__init__.py` (import block and `__all__`: add `DraftStatus`, `FeedbackDecision`, `ReviewVerdict`, `approval_verdict`, `edit_distance`, `rejection_verdict`)
- Modify: `packages/core/pagination.py` (append `InvalidCursorError`, `encode_cursor`, `decode_cursor` after line 53; add imports at lines 8–10)
- Modify: `packages/observability/metrics.py` (field after line 110 `business_lookups_total: Counter`; constructor entry after the `business_lookups_total=Counter(...)` block, lines 305–310)
- Modify: `packages/db/job.py` (add `JOB_SELECT_COLUMNS` after `_from_json_val`, line 52)
- Create: `packages/db/review.py` (`ReviewStore` Protocol, `PostgresReviewStore`, `InMemoryReviewStore`, view dataclasses, `DraftConflictError`)
- Create: `services/api/schemas/drafts.py`
- Create: `services/api/routers/drafts.py`
- Modify: `services/api/dependencies.py` (add `get_review_store` after `get_knowledge_store`, lines 168–180; add `ReviewStoreDep` to the alias block, lines 183–189)
- Modify: `services/api/routers/v1.py` (import at line 16–21 block; `v1_router.include_router(drafts_router)` after line 32)
- Modify: `services/api/main.py` (`create_app`, after `app.state.settings = active_settings`: load `config/categories.yaml` into `app.state.taxonomy`; imports lines 18–20)
- Modify: `docs/observability.md` (§2.2 table row after the `business_lookup_latency_ms` row; new `#### Review decisions` subsection before the `---` at line 156)
- Modify: `specs/design.md` line 686 (§5.8 Review API: the repeated-approve sentence; **needs open question D2 approved first**, GEMINI.md §7)
- Test: `tests/unit/test_review_domain.py` (new), `tests/unit/test_cursor_pagination.py` (new), `tests/unit/test_observability_metrics.py` (append one test after line 188), `tests/unit/test_drafts_api.py` (new), `tests/integration/test_drafts_api_integration.py` (new)

**Interfaces:**
- Consumes: `PostgresJobStore.transition_job_state_on(conn, organization_id, job_id, target_state, payload=..., result_ref=...)`, `PostgresJobStore._row_to_job(row)`, `InMemoryJobStore.transition_job_state(...)` (packages/db/job.py); `_row_to_draft(row)` (packages/db/draft.py, with Part A's new columns); `PostgresMessageStore.get_message(organization_id, message_id)`; `derive_idempotency_key(organization_id=, mailbox_id=, provider_message_id=, operation_type="dispatch")`; `MessagePublisher.publish(exchange_name, routing_key, envelope)` and `publisher.settings.queue_dispatch` / `exchange_for_queue(...)`; `get_organization_id` (header `X-Organization-ID`, case-insensitive, so the UI's `X-Organization-Id` works); migration 0005's `feedback.review_ms` and `UNIQUE (draft_id)`; the `CONTEXT_READY` event payload keys written by `packages/business/fetch.py::business_payload` (`customer_status`, `business_fact_statuses`, `business_data_degraded`).
- Produces:
  - `packages.domain.review`: `class DraftStatus(StrEnum)` (`DRAFT="draft"`, `APPROVED="approved"`, `REJECTED="rejected"`, `DISPATCHED="dispatched"`); `class FeedbackDecision(StrEnum)` (`ACCEPTED`, `EDITED`, `REJECTED`); `@dataclass(frozen=True) class ReviewVerdict(decision: FeedbackDecision, edited_body: str | None, edit_distance: int)`; `def edit_distance(a: str, b: str) -> int`; `def approval_verdict(original_body: str, final_body: str) -> ReviewVerdict`; `def rejection_verdict(original_body: str, final_body: str) -> ReviewVerdict`.
  - `packages.core.pagination`: `class InvalidCursorError(ValueError)`; `def encode_cursor(created_at: datetime, item_id: UUID) -> str`; `def decode_cursor(cursor: str) -> tuple[datetime, UUID]`.
  - `PipelineMetrics.draft_decisions_total: Counter` labels `["decision", "category"]`.
  - `packages.db.job.JOB_SELECT_COLUMNS: str`.
  - `packages.db.review`: `DraftView`, `CitedChunk`, `BusinessFactView`, `BusinessDataView` (with `from_context_payload`), `FeedbackRecord`, `DraftDetail`, `DraftPage`, `ReviewInput`, `DecisionOutcome`, `DraftConflictError(code: str, message: str, *, status: str)`, `DRAFT_EDITED_EVENT = "draft_edited"`, `ReviewStore` Protocol with `list_drafts(organization_id, *, status=None, category=None, mailbox_id=None, after=None, limit=50) -> DraftPage`, `get_draft_detail(organization_id, draft_id) -> DraftDetail | None`, `edit_draft(organization_id, draft_id, *, body, subject=None) -> DraftView | None`, `approve_draft(organization_id, draft_id, review) -> DecisionOutcome | None`, `reject_draft(organization_id, draft_id, review) -> DecisionOutcome | None`; `PostgresReviewStore(pool)`; `InMemoryReviewStore(job_store=None)` with seeding helpers `add_draft(draft, *, original, category=None, thread_summary=None, business_data=None)` and `add_chunk(chunk_id, *, content, heading_path=())` (the review-UI tests reuse these).
  - HTTP (all under `/v1`, org-scoped): `GET /drafts` → `DraftListResponse{items, next_cursor, limit}`; `GET /drafts/{draft_id}` → `DraftDetailResponse`; `PATCH /drafts/{draft_id}` body `DraftEditRequest{body, subject?}` → `DraftSummaryResponse`; `POST /drafts/{draft_id}/approve` and `/reject` body `DraftDecisionRequest{reviewer?, rating? 1–5, review_ms? ≥0, comment?}` → `DraftDecisionResponse{draft_id, job_id, draft_status, job_state, decision, edit_distance, feedback_id, created, published}`. Error codes: `DRAFT_NOT_FOUND` (404), `INVALID_CURSOR` (400), `DRAFT_NOT_EDITABLE`, `DRAFT_ALREADY_REJECTED`, `DRAFT_ALREADY_APPROVED`, `JOB_NOT_DRAFTED`, `DRAFT_HAS_NO_JOB`, `JOB_NOT_FOUND` (409), `PUBLISHER_UNAVAILABLE`, `DISPATCH_PUBLISH_FAILED` (503; the approval stays committed and a repeated approve re-publishes).
  - `app.state.taxonomy: TaxonomyRegistry` (built by `create_app` from `settings.routing.categories_config_path`); `GET /drafts/{id}`'s `dispatch_mode` reads it, so the API reports the mode the dispatch-worker (which loads the same YAML) will use.
  - Approve publishes `JobEnvelope(job_id=<the DRAFTED job>, idempotency_key=<dispatch key>, job_type="dispatch", organization_id, message_id, thread_id, mailbox_id, payload={"draft_id", "trigger": "approve"})` to exchange/queue `email.dispatch`, only while the job is `DRAFTED`, `DISPATCHED` or `RETRY_PENDING`.

Design notes that the steps below implement:
- **Lock order job → draft** in approve and reject, the same order as the dispatch claim and finish (Task 7), so a repeated approve racing a dispatch cannot deadlock.
- **The generated body survives edits:** the first `PATCH` of a draft writes a `processing_event` (`event_type='draft_edited'`, `state_from = state_to =` the job's state) whose payload holds `original_body`; later edits write `draft_edited` without it. The decision's `edit_distance` is measured from that original to the approved/rejected body, so edit-then-approve records `edited`, not `accepted`.
- **`review_ms`** is measured by the client (the UI times from opening the draft to the decision) and sent in the request body; the API stores it as given.

- [ ] **Step 1: Write the failing domain, cursor and metric tests**

Create `tests/unit/test_review_domain.py`:

```python
"""Draft review decisions and character edit distance (task 6.2; R16.7, R21.4)."""

from __future__ import annotations

import pytest

from packages.domain.review import (
    DraftStatus,
    FeedbackDecision,
    ReviewVerdict,
    approval_verdict,
    edit_distance,
    rejection_verdict,
)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("", "", 0),
        ("abc", "abc", 0),
        ("kitten", "sitting", 3),
        ("", "abc", 3),
        ("abc", "", 3),
        ("flaw", "lawn", 2),
        ("café", "cafe", 1),
        ("Order shipped.", "Order shipped today.", 6),
    ],
)
def test_edit_distance_is_character_levenshtein(a: str, b: str, expected: int) -> None:
    assert edit_distance(a, b) == expected
    assert edit_distance(b, a) == expected


def test_edit_distance_on_long_bodies_with_a_small_edit_is_exact() -> None:
    """Common prefix/suffix trimming keeps a one-character edit in a 10k body cheap."""
    original = "a" * 5000 + "x" + "b" * 5000
    edited = "a" * 5000 + "y" + "b" * 5000
    assert edit_distance(original, edited) == 1


def test_unchanged_approval_is_accepted() -> None:
    assert approval_verdict("Hello Alice", "Hello Alice") == ReviewVerdict(
        FeedbackDecision.ACCEPTED, None, 0
    )


def test_changed_approval_is_edited_with_body_and_distance() -> None:
    verdict = approval_verdict("Hello Alice", "Hello Alice!")
    assert verdict.decision is FeedbackDecision.EDITED
    assert verdict.edited_body == "Hello Alice!"
    assert verdict.edit_distance == 1


def test_rejection_keeps_edits_when_there_were_any() -> None:
    assert rejection_verdict("Hi", "Hi") == ReviewVerdict(FeedbackDecision.REJECTED, None, 0)
    edited = rejection_verdict("Hi", "Hey")
    assert edited.decision is FeedbackDecision.REJECTED
    assert edited.edited_body == "Hey"
    assert edited.edit_distance == 2


def test_status_and_decision_values_match_the_schema_checks() -> None:
    """Values are the migration 0001 CHECK constraint literals."""
    assert [s.value for s in DraftStatus] == ["draft", "approved", "rejected", "dispatched"]
    assert [d.value for d in FeedbackDecision] == ["accepted", "edited", "rejected"]
```

Create `tests/unit/test_cursor_pagination.py`:

```python
"""Opaque keyset cursors for GET /v1/drafts (task 6.1; R23.6)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.pagination import InvalidCursorError, decode_cursor, encode_cursor


def test_cursor_round_trips_timestamp_and_id() -> None:
    created = datetime(2026, 9, 28, 9, 30, 15, 123456, tzinfo=UTC)
    item = uuid4()
    cursor = encode_cursor(created, item)
    assert "=" not in cursor
    assert decode_cursor(cursor) == (created, item)


@pytest.mark.parametrize("cursor", ["", "not-a-cursor", "e30", "W10"])
def test_garbage_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_naive_timestamp_cursor_is_rejected() -> None:
    raw = json.dumps({"t": "2026-09-28T09:30:00", "id": str(uuid4())}).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_encoding_a_naive_timestamp_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        encode_cursor(datetime(2026, 9, 28), uuid4())
```

(`"e30"` is `{}` and `"W10"` is `[]` in base64url: valid JSON with no `t`/`id`.)

Append to `tests/unit/test_observability_metrics.py`:

```python
def test_draft_decisions_metric() -> None:
    """R16.7 / R21.4 (task 6.2): one series per review decision and category."""
    m = create_pipeline_metrics()

    m.draft_decisions_total.labels(decision="accepted", category="billing").inc()
    m.draft_decisions_total.labels(decision="edited", category="billing").inc()
    m.draft_decisions_total.labels(decision="rejected", category="support").inc()

    payload, _ = generate_metrics_payload(m.registry)
    text = payload.decode("utf-8")

    # The text exposition sorts label names, so category comes before decision.
    assert 'draft_decisions_total{category="billing",decision="accepted"} 1.0' in text
    assert 'draft_decisions_total{category="billing",decision="edited"} 1.0' in text
    assert 'draft_decisions_total{category="support",decision="rejected"} 1.0' in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_review_domain.py tests/unit/test_cursor_pagination.py tests/unit/test_observability_metrics.py::test_draft_decisions_metric -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'packages.domain.review'`, `ImportError: cannot import name 'InvalidCursorError' from 'packages.core.pagination'`, and `AttributeError: 'PipelineMetrics' object has no attribute 'draft_decisions_total'`.

- [ ] **Step 3: Implement the domain module, the cursor helpers and the metric**

Create `packages/domain/review.py`:

```python
"""Draft review decisions: draft status, feedback decision and character edit distance.

Requirements:
- R16.6: a draft moves draft -> approved | rejected -> dispatched (design.md §5.8).
- R16.7: every decision is one ``feedback`` row with decision, edited body and edit distance.
- R21.4 / SC3: ``draft_decisions_total{decision, category}`` counts these decisions.

Pure: standard library only (GEMINI.md; tests/unit/test_dependency_rules.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DraftStatus(StrEnum):
    """``generated_draft.status`` values (migration 0001 CHECK constraint)."""

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPATCHED = "dispatched"


class FeedbackDecision(StrEnum):
    """``feedback.decision`` values; ``accepted`` means approved unchanged (design.md §5.8)."""

    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ReviewVerdict:
    """What one reviewer decision records in its ``feedback`` row."""

    decision: FeedbackDecision
    edited_body: str | None
    edit_distance: int


def edit_distance(a: str, b: str) -> int:
    """Character-level Levenshtein distance (insert, delete, substitute each cost 1).

    The common prefix and suffix are trimmed first, so the usual review edit (a few
    characters in a long body) costs little; the remaining core is the classic two-row DP.
    """
    if a == b:
        return 0
    start = 0
    limit = min(len(a), len(b))
    while start < limit and a[start] == b[start]:
        start += 1
    end_a, end_b = len(a), len(b)
    while end_a > start and end_b > start and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
    core_a, core_b = a[start:end_a], b[start:end_b]
    if not core_a:
        return len(core_b)
    if not core_b:
        return len(core_a)
    if len(core_a) < len(core_b):
        core_a, core_b = core_b, core_a
    previous = list(range(len(core_b) + 1))
    for i, char_a in enumerate(core_a, start=1):
        current = [i]
        for j, char_b in enumerate(core_b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def approval_verdict(original_body: str, final_body: str) -> ReviewVerdict:
    """Approve: ``accepted`` when the body is the generated one, else ``edited``."""
    distance = edit_distance(original_body, final_body)
    if distance == 0:
        return ReviewVerdict(FeedbackDecision.ACCEPTED, None, 0)
    return ReviewVerdict(FeedbackDecision.EDITED, final_body, distance)


def rejection_verdict(original_body: str, final_body: str) -> ReviewVerdict:
    """Reject: always ``rejected``; edits made before rejecting are kept for analysis."""
    distance = edit_distance(original_body, final_body)
    return ReviewVerdict(
        FeedbackDecision.REJECTED, final_body if distance else None, distance
    )
```

In `packages/domain/__init__.py` add, after the `packages.domain.knowledge` import block:

```python
from packages.domain.review import (
    DraftStatus,
    FeedbackDecision,
    ReviewVerdict,
    approval_verdict,
    edit_distance,
    rejection_verdict,
)
```

and add `"DraftStatus"`, `"FeedbackDecision"`, `"ReviewVerdict"` to the capitalised names of `__all__` and `"approval_verdict"`, `"edit_distance"`, `"rejection_verdict"` to the lower-case names, keeping the existing sort order (ruff `I` does not sort `__all__`; keep it alphabetical by hand, next to whatever Part A added for `DispatchMode` / `ProviderDraftStatus`).

In `packages/core/pagination.py`, replace the import block (lines 8–10) with:

```python
import base64
import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field
```

and append at the end of the file:

```python
class InvalidCursorError(ValueError):
    """A pagination cursor that :func:`encode_cursor` did not produce (R23.6)."""


def encode_cursor(created_at: datetime, item_id: UUID) -> str:
    """Opaque keyset cursor for ``ORDER BY created_at DESC, id DESC`` pages (R23.6).

    The cursor names the last item of a page; the next page holds items strictly
    before it. base64url without padding, so it is safe in a query string.
    """
    if created_at.tzinfo is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    raw = json.dumps({"t": created_at.isoformat(), "id": str(item_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Inverse of :func:`encode_cursor`; anything else raises InvalidCursorError."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(data["t"])
        item_id = UUID(data["id"])
    except (ValueError, KeyError, TypeError) as err:
        raise InvalidCursorError(f"Invalid pagination cursor: {cursor!r}") from err
    if created_at.tzinfo is None:
        raise InvalidCursorError(f"Invalid pagination cursor: {cursor!r}")
    return created_at, item_id
```

(`binascii.Error`, `json.JSONDecodeError` and `UnicodeEncodeError` are all `ValueError` subclasses; an empty string decodes to `b""`, which `json.loads` rejects.)

In `packages/observability/metrics.py`, add after `business_lookups_total: Counter` (line 110):

```python
    draft_decisions_total: Counter
```

and after the `business_lookups_total=Counter(...)` entry (lines 305–310):

```python
        draft_decisions_total=Counter(
            "draft_decisions_total",
            "First reviewer decision per draft: accepted, edited or rejected (R16.7, R21.4)",
            ["decision", "category"],
            registry=reg,
        ),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_review_domain.py tests/unit/test_cursor_pagination.py tests/unit/test_observability_metrics.py tests/unit/test_dependency_rules.py -v`
Expected: PASS (all tests; `test_packages_domain_imports_stdlib_and_core_only` still green).

- [ ] **Step 5: Commit**

```bash
uv run ruff format packages/domain/review.py packages/domain/__init__.py packages/core/pagination.py packages/observability/metrics.py tests/unit/test_review_domain.py tests/unit/test_cursor_pagination.py tests/unit/test_observability_metrics.py
uv run ruff check packages/domain packages/core packages/observability tests/unit/test_review_domain.py tests/unit/test_cursor_pagination.py tests/unit/test_observability_metrics.py
uv run mypy packages/domain packages/core/pagination.py packages/observability/metrics.py tests/unit/test_review_domain.py tests/unit/test_cursor_pagination.py
git add packages/domain/review.py packages/domain/__init__.py packages/core/pagination.py packages/observability/metrics.py tests/unit/test_review_domain.py tests/unit/test_cursor_pagination.py tests/unit/test_observability_metrics.py
git commit -m "feat(review): draft decisions, edit distance, keyset cursor and draft_decisions_total [task 6.1, 6.2] [R16.7, R21.4, R23.6]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Write the failing API tests (in-memory unit and Postgres/RabbitMQ integration)**

Create `tests/unit/test_drafts_api.py`:

```python
"""Draft review API on in-memory stores (task 6.1, 6.2; R16.6, R16.7, R23.2, R23.6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import APISettings, BrokerSettings, CategoryRoutingSettings
from packages.db.review import BusinessDataView, BusinessFactView, InMemoryReviewStore
from packages.domain.dispatch import DispatchMode
from packages.domain.entities import EmailAddress, GeneratedDraft, Job, NormalizedMessage
from packages.domain.review import edit_distance
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import get_default_registry
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from services.api.main import create_app

CHUNK_ID = uuid4()
GENERATED_BODY = "Your order ORD-82915 was dispatched on 24 September."


@dataclass
class RecordingPublisher:
    """Stands in for MessagePublisher: records what approve publishes."""

    settings: BrokerSettings = field(default_factory=BrokerSettings)
    published: list[tuple[str, str, JobEnvelope]] = field(default_factory=list)
    fail: bool = False

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.published.append((exchange_name, routing_key, envelope))


@dataclass(frozen=True)
class Seeded:
    org_id: UUID
    mailbox_id: UUID
    draft: GeneratedDraft
    job: Job
    original: NormalizedMessage


async def _seed(
    store: InMemoryReviewStore,
    org_id: UUID,
    *,
    category: str = "billing",
    created_at: datetime | None = None,
    mailbox_id: UUID | None = None,
    job_state: JobState = JobState.DRAFTED,
) -> Seeded:
    mbx = mailbox_id or uuid4()
    thread_id = uuid4()
    original = NormalizedMessage(
        message_id=uuid4(),
        thread_id=thread_id,
        mailbox_id=mbx,
        organization_id=org_id,
        provider="fake",
        provider_message_id=f"prov-{uuid4().hex[:8]}",
        sender=EmailAddress("alice@customer.example", "Alice"),
        received_at=datetime.now(UTC),
        rfc822_message_id="orig-1@customer.example",
        subject="Where is order 82915?",
        body_text="What is the status of order 82915?",
    )
    job, _ = await store.jobs.create_job(
        Job(
            organization_id=org_id,
            message_id=original.message_id,
            thread_id=thread_id,
            state=job_state.value,
            idempotency_key=f"gen-{uuid4()}",
        )
    )
    draft = GeneratedDraft(
        organization_id=org_id,
        message_id=original.message_id,
        thread_id=thread_id,
        job_id=job.id,
        subject="Re: Where is order 82915?",
        body=GENERATED_BODY,
        citations=[
            {
                "citation_id": "DOC-7-01",
                "chunk_id": str(CHUNK_ID),
                "document_id": str(uuid4()),
                "external_id": "DOC-7-01",
            }
        ],
        created_at=created_at or datetime.now(UTC),
    )
    store.add_draft(
        draft,
        original=original,
        category=category,
        thread_summary="Alice asks where order 82915 is.",
        business_data=BusinessDataView(
            customer_status="FOUND",
            degraded=False,
            facts=(BusinessFactView(entity="order", reference="ORD-82915", status="FOUND"),),
        ),
    )
    return Seeded(org_id, mbx, draft, job, original)


@pytest.fixture
def store() -> InMemoryReviewStore:
    s = InMemoryReviewStore()
    s.add_chunk(CHUNK_ID, content="Orders ship within 2 business days.", heading_path=("Shipping",))
    return s


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def metrics() -> PipelineMetrics:
    return create_pipeline_metrics()


@pytest.fixture
def app(
    store: InMemoryReviewStore, publisher: RecordingPublisher, metrics: PipelineMetrics
) -> FastAPI:
    application = create_app(lifespan_enabled=False)
    application.state.review_store = store
    application.state.publisher = publisher
    application.state.metrics = metrics
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


def _h(org_id: UUID) -> dict[str, str]:
    return {"X-Organization-Id": str(org_id)}


def _decisions(metrics: PipelineMetrics, decision: str, category: str) -> float:
    value: float = metrics.draft_decisions_total.labels(
        decision=decision, category=category
    )._value.get()
    return value


async def test_list_filters_and_cursor_pagination(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    """R16.6 / R23.6: newest first, keyset cursor, filters, tenant scope."""
    org = uuid4()
    mbx = uuid4()
    base = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    a = await _seed(store, org, created_at=base, mailbox_id=mbx)
    b = await _seed(store, org, created_at=base + timedelta(minutes=1), mailbox_id=mbx)
    c = await _seed(store, org, category="support", created_at=base + timedelta(minutes=2))
    await _seed(store, uuid4())  # another tenant's draft never appears

    first = await client.get("/v1/drafts", params={"limit": 2}, headers=_h(org))
    assert first.status_code == 200
    page1 = first.json()
    assert [i["id"] for i in page1["items"]] == [str(c.draft.id), str(b.draft.id)]
    assert page1["limit"] == 2
    assert page1["next_cursor"]

    second = await client.get(
        "/v1/drafts", params={"limit": 2, "cursor": page1["next_cursor"]}, headers=_h(org)
    )
    page2 = second.json()
    assert [i["id"] for i in page2["items"]] == [str(a.draft.id)]
    assert page2["next_cursor"] is None

    billing = (await client.get("/v1/drafts", params={"category": "billing"}, headers=_h(org))).json()
    assert {i["id"] for i in billing["items"]} == {str(a.draft.id), str(b.draft.id)}
    assert billing["items"][0]["category"] == "billing"
    assert billing["items"][0]["job_state"] == "DRAFTED"

    by_mailbox = (await client.get("/v1/drafts", params={"mailbox": str(mbx)}, headers=_h(org))).json()
    assert {i["id"] for i in by_mailbox["items"]} == {str(a.draft.id), str(b.draft.id)}

    approved = (await client.get("/v1/drafts", params={"status": "approved"}, headers=_h(org))).json()
    assert approved["items"] == []


async def test_invalid_cursor_and_status_are_rejected(client: AsyncClient) -> None:
    org = uuid4()
    bad_cursor = await client.get("/v1/drafts", params={"cursor": "nope"}, headers=_h(org))
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"
    bad_status = await client.get("/v1/drafts", params={"status": "sent"}, headers=_h(org))
    assert bad_status.status_code == 422


async def test_detail_carries_original_summary_chunks_and_business_facts(
    client: AsyncClient, store: InMemoryReviewStore
) -> None:
    """6.1: the reviewer sees the email, the thread summary, cited chunks and business facts."""
    seeded = await _seed(store, uuid4())
    resp = await client.get(f"/v1/drafts/{seeded.draft.id}", headers=_h(seeded.org_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == GENERATED_BODY
    assert body["original"]["subject"] == "Where is order 82915?"
    assert body["original"]["sender_email"] == "alice@customer.example"
    assert body["thread_summary"] == "Alice asks where order 82915 is."
    assert body["cited_chunks"] == [
        {
            "citation_id": "DOC-7-01",
            "chunk_id": str(CHUNK_ID),
            "document_id": seeded.draft.citations[0]["document_id"],
            "external_id": "DOC-7-01",
            "content": "Orders ship within 2 business days.",
            "heading_path": ["Shipping"],
        }
    ]
    assert body["business_data"] == {
        "customer_status": "FOUND",
        "degraded": False,
        "facts": [{"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}],
    }
    assert body["feedback"] is None
    assert body["dispatch_mode"] == "create_draft"  # the UI tells the reviewer what approve does


async def test_detail_reports_the_dispatch_mode_from_the_categories_yaml(
    tmp_path: Path, store: InMemoryReviewStore
) -> None:
    """6.4 / 6.8: the API loads the same YAML as the dispatch-worker, so the reviewer sees the
    mode approve will really use; the process-default registry is not changed."""
    config = tmp_path / "categories.yaml"
    config.write_text(
        "categories:\n  - category: billing\n    dispatch_mode: send_reply\n",
        encoding="utf-8",
    )
    settings = APISettings(
        _env_file=None, routing=CategoryRoutingSettings(categories_config_path=str(config))
    )
    application = create_app(settings, lifespan_enabled=False)
    application.state.review_store = store
    seeded = await _seed(store, uuid4(), category="billing")

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as ac:
        resp = await ac.get(f"/v1/drafts/{seeded.draft.id}", headers=_h(seeded.org_id))

    assert resp.status_code == 200
    assert resp.json()["dispatch_mode"] == "send_reply"
    assert get_default_registry().dispatch_mode_for("billing") is DispatchMode.CREATE_DRAFT


async def test_other_tenant_gets_404_everywhere(
    client: AsyncClient, store: InMemoryReviewStore, publisher: RecordingPublisher
) -> None:
    """R23.6: a draft id from another organization is invisible and cannot be decided."""
    seeded = await _seed(store, uuid4())
    other = _h(uuid4())
    draft_url = f"/v1/drafts/{seeded.draft.id}"
    assert (await client.get(draft_url, headers=other)).status_code == 404
    assert (await client.patch(draft_url, json={"body": "x"}, headers=other)).status_code == 404
    assert (await client.post(f"{draft_url}/approve", headers=other)).status_code == 404
    assert (await client.post(f"{draft_url}/reject", headers=other)).status_code == 404
    assert publisher.published == []


async def test_patch_edits_only_while_draft(client: AsyncClient, store: InMemoryReviewStore) -> None:
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    edited = await client.patch(
        url, json={"body": "Order ORD-82915 shipped on 24 September."}, headers=_h(seeded.org_id)
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "draft"
    detail = (await client.get(url, headers=_h(seeded.org_id))).json()
    assert detail["body"] == "Order ORD-82915 shipped on 24 September."

    await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    late = await client.patch(url, json={"body": "too late"}, headers=_h(seeded.org_id))
    assert late.status_code == 409
    assert late.json()["code"] == "DRAFT_NOT_EDITABLE"


async def test_approve_unchanged_publishes_dispatch_and_counts_accepted(
    client: AsyncClient,
    store: InMemoryReviewStore,
    publisher: RecordingPublisher,
    metrics: PipelineMetrics,
) -> None:
    """6.1 / 6.2: approve commits, then publishes to email.dispatch; one accepted decision."""
    seeded = await _seed(store, uuid4())
    resp = await client.post(
        f"/v1/drafts/{seeded.draft.id}/approve",
        json={"reviewer": "demo", "rating": 5, "review_ms": 4200},
        headers=_h(seeded.org_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "accepted"
    assert body["edit_distance"] == 0
    assert body["draft_status"] == "approved"
    assert body["job_state"] == "DRAFTED"
    assert body["created"] is True
    assert body["published"] is True

    [(exchange, routing_key, envelope)] = publisher.published
    assert (exchange, routing_key) == ("email.dispatch", "email.dispatch")
    assert envelope.job_id == str(seeded.job.id)
    assert envelope.job_type == "dispatch"
    assert envelope.mailbox_id == str(seeded.mailbox_id)
    assert envelope.payload == {"draft_id": str(seeded.draft.id), "trigger": "approve"}
    assert envelope.idempotency_key == derive_idempotency_key(
        organization_id=seeded.org_id,
        mailbox_id=seeded.mailbox_id,
        provider_message_id=seeded.original.provider_message_id,
        operation_type="dispatch",
    )

    feedback = (
        await client.get(f"/v1/drafts/{seeded.draft.id}", headers=_h(seeded.org_id))
    ).json()["feedback"]
    assert feedback["decision"] == "accepted"
    assert feedback["review_ms"] == 4200
    assert feedback["rating"] == 5
    assert feedback["reviewer"] == "demo"
    assert _decisions(metrics, "accepted", "billing") == 1


async def test_edit_then_approve_records_edited_with_distance_from_generated_body(
    client: AsyncClient, store: InMemoryReviewStore, metrics: PipelineMetrics
) -> None:
    """6.2: two edits, then approve: distance is from the generated body, not the last edit."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    await client.patch(url, json={"body": "Draft one."}, headers=_h(seeded.org_id))
    final = "Order ORD-82915 shipped on 24 September. Tracking follows."
    await client.patch(url, json={"body": final}, headers=_h(seeded.org_id))

    body = (await client.post(f"{url}/approve", headers=_h(seeded.org_id))).json()
    assert body["decision"] == "edited"
    assert body["edit_distance"] == edit_distance(GENERATED_BODY, final)
    feedback = (await client.get(url, headers=_h(seeded.org_id))).json()["feedback"]
    assert feedback["edited_body"] == final
    assert _decisions(metrics, "edited", "billing") == 1
    assert _decisions(metrics, "accepted", "billing") == 0


async def test_repeated_approve_republishes_without_a_second_feedback_row(
    client: AsyncClient,
    store: InMemoryReviewStore,
    publisher: RecordingPublisher,
    metrics: PipelineMetrics,
) -> None:
    """6.1: a lost publish cannot strand an approved draft; feedback stays one row."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}/approve"
    first = (await client.post(url, headers=_h(seeded.org_id))).json()
    second = (await client.post(url, headers=_h(seeded.org_id))).json()

    assert second["created"] is False
    assert second["published"] is True
    assert second["feedback_id"] == first["feedback_id"]
    assert len(publisher.published) == 2
    assert len(store.feedback_rows(seeded.org_id)) == 1
    assert _decisions(metrics, "accepted", "billing") == 1


async def test_approve_after_completion_publishes_nothing(
    client: AsyncClient, store: InMemoryReviewStore, publisher: RecordingPublisher
) -> None:
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}/approve"
    await client.post(url, headers=_h(seeded.org_id))
    for state in (JobState.DISPATCHED, JobState.COMPLETED):
        await store.jobs.transition_job_state(seeded.org_id, seeded.job.id, state)

    again = (await client.post(url, headers=_h(seeded.org_id))).json()
    assert again["job_state"] == "COMPLETED"
    assert again["published"] is False
    assert len(publisher.published) == 1


async def test_publish_failure_keeps_the_approval_and_approve_again_republishes(
    client: AsyncClient, store: InMemoryReviewStore, publisher: RecordingPublisher
) -> None:
    """Approve commits before it publishes (design.md §5.8)."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    publisher.fail = True
    failed = await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    assert failed.status_code == 503
    assert failed.json()["code"] == "DISPATCH_PUBLISH_FAILED"
    assert (await client.get(url, headers=_h(seeded.org_id))).json()["status"] == "approved"

    publisher.fail = False
    retried = (await client.post(f"{url}/approve", headers=_h(seeded.org_id))).json()
    assert retried["published"] is True
    assert retried["created"] is False
    assert len(publisher.published) == 1
    assert len(store.feedback_rows(seeded.org_id)) == 1


async def test_missing_publisher_is_503_after_commit(
    app: FastAPI, client: AsyncClient, store: InMemoryReviewStore
) -> None:
    seeded = await _seed(store, uuid4())
    app.state.publisher = None
    resp = await client.post(f"/v1/drafts/{seeded.draft.id}/approve", headers=_h(seeded.org_id))
    assert resp.status_code == 503
    assert resp.json()["code"] == "PUBLISHER_UNAVAILABLE"
    assert len(store.feedback_rows(seeded.org_id)) == 1


async def test_reject_completes_the_job_without_dispatch(
    client: AsyncClient,
    store: InMemoryReviewStore,
    publisher: RecordingPublisher,
    metrics: PipelineMetrics,
) -> None:
    """6.1: reject is DRAFTED -> COMPLETED with no send; a repeated reject returns the first."""
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    first = (await client.post(f"{url}/reject", json={"review_ms": 900}, headers=_h(seeded.org_id))).json()
    assert first["decision"] == "rejected"
    assert first["job_state"] == "COMPLETED"
    assert first["draft_status"] == "rejected"
    assert first["published"] is False

    again = (await client.post(f"{url}/reject", headers=_h(seeded.org_id))).json()
    assert again["feedback_id"] == first["feedback_id"]
    assert again["created"] is False

    approve = await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    assert approve.status_code == 409
    assert approve.json()["code"] == "DRAFT_ALREADY_REJECTED"
    assert publisher.published == []
    job = await store.jobs.get_job(seeded.org_id, seeded.job.id)
    assert job is not None and job.state == JobState.COMPLETED.value
    assert _decisions(metrics, "rejected", "billing") == 1


async def test_reject_after_approve_is_a_conflict(client: AsyncClient, store: InMemoryReviewStore) -> None:
    seeded = await _seed(store, uuid4())
    url = f"/v1/drafts/{seeded.draft.id}"
    await client.post(f"{url}/approve", headers=_h(seeded.org_id))
    resp = await client.post(f"{url}/reject", headers=_h(seeded.org_id))
    assert resp.status_code == 409
    assert resp.json()["code"] == "DRAFT_ALREADY_APPROVED"


async def test_approve_requires_a_drafted_job(client: AsyncClient, store: InMemoryReviewStore) -> None:
    seeded = await _seed(store, uuid4(), job_state=JobState.GENERATING)
    resp = await client.post(f"/v1/drafts/{seeded.draft.id}/approve", headers=_h(seeded.org_id))
    assert resp.status_code == 409
    assert resp.json()["code"] == "JOB_NOT_DRAFTED"


async def test_decision_body_is_validated(client: AsyncClient, store: InMemoryReviewStore) -> None:
    seeded = await _seed(store, uuid4())
    resp = await client.post(
        f"/v1/drafts/{seeded.draft.id}/approve",
        json={"rating": 6, "review_ms": -1},
        headers=_h(seeded.org_id),
    )
    assert resp.status_code == 422
```

Create `tests/integration/test_drafts_api_integration.py`:

```python
"""Draft review API against PostgreSQL and RabbitMQ (task 6.1, 6.2; R16.6, R16.7, R18.5).

Proves on the real stores: keyset pagination and tenant scope, approve commits then publishes
to email.dispatch, a repeated approve re-publishes with one feedback row (UNIQUE (draft_id)),
the edit distance is measured from the generated body, and reject is DRAFTED -> COMPLETED.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft import insert_draft
from packages.db.job import PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.review import edit_distance
from packages.domain.state_machine import JobState
from packages.observability.metrics import create_pipeline_metrics
from services.api.main import create_app
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3)
GENERATED_BODY = "Your order ORD-82915 was dispatched on 24 September."


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "draftsapi") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def api_app(pool: asyncpg.Pool, broker: BrokerSettings, channel: AbstractChannel) -> FastAPI:
    app = create_app(lifespan_enabled=False)
    app.state.db_pool = pool
    app.state.publisher = MessagePublisher(broker_settings=broker, channel=channel)
    app.state.metrics = create_pipeline_metrics()
    return app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://testserver") as ac:
        yield ac


@dataclass(frozen=True)
class Seed:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    job_id: uuid.UUID
    draft_id: uuid.UUID
    chunk_id: uuid.UUID


async def _seed_org(pool: asyncpg.Pool) -> tuple[uuid.UUID, uuid.UUID]:
    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}")
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, status)"
            " VALUES ($1, $2, 'gmail', $3, 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@acme.example",
        )
    return org_id, mbx_id


async def _seed_draft(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    *,
    category: str = "billing",
    created_at: datetime | None = None,
) -> Seed:
    thread_id, msg_id, chunk_id, doc_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:8]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                rfc822_message_id, direction, sender_email, sender_name, recipients,
                subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'inbound', 'alice@customer.example', 'Alice',
                      '[]', 'Where is order 82915?', 'What is the status of order 82915?', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            f"orig-{msg_id.hex[:8]}@customer.example",
        )
        await conn.execute(
            """
            INSERT INTO classification_result (
                id, organization_id, message_id, category, priority, reply_required,
                retrieval_required, confidence, decided_by
            ) VALUES ($1, $2, $3, $4, 'normal', true, true, 0.9, 'rule')
            """,
            uuid.uuid4(),
            org_id,
            msg_id,
            category,
        )
        await conn.execute(
            "INSERT INTO thread_state (thread_id, organization_id, summary) VALUES ($1, $2, $3)",
            thread_id,
            org_id,
            "Alice asks where order 82915 is.",
        )
        await conn.execute(
            "INSERT INTO knowledge_document (id, organization_id, title)"
            " VALUES ($1, $2, 'Shipping')",
            doc_id,
            org_id,
        )
        await conn.execute(
            """
            INSERT INTO knowledge_chunk (
                id, document_id, organization_id, chunk_index, external_id, heading_path,
                content, version, content_tsv
            ) VALUES ($1, $2, $3, 0, 'DOC-7-01', ARRAY['Shipping'], $4, 1,
                      to_tsvector('english', $4))
            """,
            chunk_id,
            doc_id,
            org_id,
            "Orders ship within 2 business days.",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.DRAFTED.value,
            idempotency_key=f"draftsapi-{uuid.uuid4()}",
        )
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO processing_event (job_id, message_id, organization_id, event_type,
                                          state_from, state_to, payload)
            VALUES ($1, $2, $3, 'state_transition', 'QUEUED', 'CONTEXT_READY', $4::jsonb)
            """,
            job.id,
            msg_id,
            org_id,
            json.dumps(
                {
                    "customer_status": "FOUND",
                    "business_fact_statuses": [
                        {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
                    ],
                    "business_data_degraded": False,
                }
            ),
        )
        draft = await insert_draft(
            conn,
            GeneratedDraft(
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                job_id=job.id,
                subject="Re: Where is order 82915?",
                body=GENERATED_BODY,
                citations=[
                    {
                        "citation_id": "DOC-7-01",
                        "chunk_id": str(chunk_id),
                        "document_id": str(doc_id),
                        "external_id": "DOC-7-01",
                    }
                ],
                created_at=created_at or datetime.now(UTC),
            ),
        )
    return Seed(org_id, mbx_id, thread_id, msg_id, uuid.UUID(str(job.id)), draft.id, chunk_id)


def _h(org_id: uuid.UUID) -> dict[str, str]:
    return {"X-Organization-Id": str(org_id)}


async def _dispatch_envelopes(channel: AbstractChannel, broker: BrokerSettings) -> list[JobEnvelope]:
    queue = await channel.declare_queue(broker.queue_dispatch, passive=True)
    envelopes: list[JobEnvelope] = []
    while True:
        message = await queue.get(no_ack=True, fail=False, timeout=5)
        if message is None:
            return envelopes
        envelopes.append(JobEnvelope.model_validate_json(message.body))


async def test_list_detail_cursor_and_tenant_scope(client: AsyncClient, pool: asyncpg.Pool) -> None:
    org_id, mbx_id = await _seed_org(pool)
    base = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    older = await _seed_draft(pool, org_id, mbx_id, created_at=base)
    newer = await _seed_draft(pool, org_id, mbx_id, category="support", created_at=base + timedelta(minutes=1))
    other_org, other_mbx = await _seed_org(pool)
    foreign = await _seed_draft(pool, other_org, other_mbx)
    try:
        page1 = (await client.get("/v1/drafts", params={"limit": 1}, headers=_h(org_id))).json()
        assert [i["id"] for i in page1["items"]] == [str(newer.draft_id)]
        page2 = (
            await client.get(
                "/v1/drafts", params={"limit": 1, "cursor": page1["next_cursor"]}, headers=_h(org_id)
            )
        ).json()
        assert [i["id"] for i in page2["items"]] == [str(older.draft_id)]
        assert page2["next_cursor"] is None

        billing = (await client.get("/v1/drafts", params={"category": "billing"}, headers=_h(org_id))).json()
        assert [i["id"] for i in billing["items"]] == [str(older.draft_id)]

        detail = (await client.get(f"/v1/drafts/{older.draft_id}", headers=_h(org_id))).json()
        assert detail["thread_summary"] == "Alice asks where order 82915 is."
        assert detail["cited_chunks"][0]["content"] == "Orders ship within 2 business days."
        assert detail["cited_chunks"][0]["heading_path"] == ["Shipping"]
        assert detail["business_data"]["facts"][0]["reference"] == "ORD-82915"
        assert detail["original"]["body_text"] == "What is the status of order 82915?"

        assert (await client.get(f"/v1/drafts/{foreign.draft_id}", headers=_h(org_id))).status_code == 404
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = ANY($1::uuid[])", [org_id, other_org])


async def test_approve_commits_then_publishes_and_repeats_without_second_feedback(
    client: AsyncClient, pool: asyncpg.Pool, channel: AbstractChannel, broker: BrokerSettings
) -> None:
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        url = f"/v1/drafts/{seed.draft_id}/approve"
        first = await client.post(url, json={"reviewer": "demo", "review_ms": 4200}, headers=_h(org_id))
        assert first.status_code == 200
        second = await client.post(url, headers=_h(org_id))
        assert second.json()["created"] is False

        envelopes = await _dispatch_envelopes(channel, broker)
        assert [e.job_id for e in envelopes] == [str(seed.job_id), str(seed.job_id)]
        assert {e.job_type for e in envelopes} == {"dispatch"}

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT decision, edit_distance, review_ms, reviewer FROM feedback"
                " WHERE draft_id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
            status = await conn.fetchval(
                "SELECT status FROM generated_draft WHERE id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
            job_state = await conn.fetchval(
                "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                org_id,
            )
        assert [dict(r) for r in rows] == [
            {"decision": "accepted", "edit_distance": 0, "review_ms": 4200, "reviewer": "demo"}
        ]
        assert status == "approved"
        assert job_state == JobState.DRAFTED.value  # the dispatch-worker's claim moves it on
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_two_fast_approves_write_one_feedback_row(
    client: AsyncClient, pool: asyncpg.Pool, channel: AbstractChannel, broker: BrokerSettings
) -> None:
    """Review focus 2: approve clicked twice fast. Row locks serialise the two calls; both
    answer 200, exactly one is the decision, one feedback row exists, every envelope names
    the same job, and no UNIQUE (draft_id) violation surfaces as a 500."""
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        url = f"/v1/drafts/{seed.draft_id}/approve"
        first, second = await asyncio.gather(
            client.post(url, json={"reviewer": "demo", "review_ms": 900}, headers=_h(org_id)),
            client.post(url, json={"reviewer": "demo", "review_ms": 950}, headers=_h(org_id)),
        )
        assert (first.status_code, second.status_code) == (200, 200)
        assert sorted([first.json()["created"], second.json()["created"]]) == [False, True]
        assert first.json()["feedback_id"] == second.json()["feedback_id"]

        envelopes = await _dispatch_envelopes(channel, broker)
        assert envelopes
        assert {e.job_id for e in envelopes} == {str(seed.job_id)}

        async with pool.acquire() as conn:
            rows = await conn.fetchval(
                "SELECT count(*) FROM feedback WHERE draft_id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
        assert rows == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_edit_distance_is_measured_from_the_generated_body(
    client: AsyncClient, pool: asyncpg.Pool
) -> None:
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        url = f"/v1/drafts/{seed.draft_id}"
        await client.patch(url, json={"body": "Draft one."}, headers=_h(org_id))
        final = "Order ORD-82915 shipped on 24 September. Tracking follows."
        await client.patch(url, json={"body": final}, headers=_h(org_id))
        decided = (await client.post(f"{url}/approve", headers=_h(org_id))).json()
        assert decided["decision"] == "edited"
        assert decided["edit_distance"] == edit_distance(GENERATED_BODY, final)

        async with pool.acquire() as conn:
            payloads = [
                json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
                for r in await conn.fetch(
                    "SELECT payload FROM processing_event WHERE organization_id = $1"
                    " AND message_id = $2 AND event_type = 'draft_edited' ORDER BY id",
                    org_id,
                    seed.message_id,
                )
            ]
        assert [p.get("original_body") for p in payloads] == [GENERATED_BODY, None]
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_reject_moves_the_job_to_completed_in_one_transaction(
    client: AsyncClient, pool: asyncpg.Pool, channel: AbstractChannel, broker: BrokerSettings
) -> None:
    org_id, mbx_id = await _seed_org(pool)
    seed = await _seed_draft(pool, org_id, mbx_id)
    try:
        resp = await client.post(f"/v1/drafts/{seed.draft_id}/reject", headers=_h(org_id))
        assert resp.status_code == 200
        assert resp.json()["job_state"] == "COMPLETED"

        async with pool.acquire() as conn:
            last = await conn.fetchrow(
                "SELECT state_from, state_to, payload FROM processing_event"
                " WHERE job_id = $1 AND organization_id = $2 AND event_type = 'state_transition'"
                " ORDER BY id DESC LIMIT 1",
                seed.job_id,
                org_id,
            )
            decision = await conn.fetchval(
                "SELECT decision FROM feedback WHERE draft_id = $1 AND organization_id = $2",
                seed.draft_id,
                org_id,
            )
        assert last is not None
        assert (last["state_from"], last["state_to"]) == ("DRAFTED", "COMPLETED")
        payload = json.loads(last["payload"]) if isinstance(last["payload"], str) else last["payload"]
        assert payload["decision"] == "rejected"
        assert decision == "rejected"
        assert await _dispatch_envelopes(channel, broker) == []
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_drafts_api.py tests/integration/test_drafts_api_integration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'packages.db.review'` (collection error in both files).

- [ ] **Step 8: Add the shared job column list**

In `packages/db/job.py`, after `_from_json_val` (line 52), add:

```python
JOB_SELECT_COLUMNS = """
    id, organization_id, message_id, thread_id, job_type, state,
    attempt, max_attempts, idempotency_key, result_ref, queue_name,
    priority, lease_expires_at, last_error, next_retry_at, trace_id,
    created_at, updated_at
"""
"""Column list that ``PostgresJobStore._row_to_job`` reads; shared by the review and
dispatch units of work that lock ``processing_job`` rows themselves (tasks 6.1, 6.5)."""
```

- [ ] **Step 9: Implement the review store**

Create `packages/db/review.py`:

```python
"""Draft review persistence: list, read, edit, approve and reject with feedback rows.

Requirements:
- R16.6: list (keyset-paginated), read, edit, approve and reject drafts, org-scoped (R23.6).
- R16.7: one ``feedback`` row per draft (``UNIQUE (draft_id)``, migration 0005) with the
  decision, edited body, character edit distance, optional rating, a free-text reviewer
  label and ``review_ms``.
- R18.4 / R18.5: reject moves the job DRAFTED -> COMPLETED in the same transaction as the
  draft status and its feedback row.
- design.md §5.8 (review API, feedback), ADR-0009.

Locks are taken job first, then draft: the same order as the dispatch claim and finish
(packages/db/dispatch.py), so a repeated approve racing a dispatch cannot deadlock.

The generated body is kept for the edit distance: the first PATCH of a draft writes a
``draft_edited`` processing_event whose payload holds ``original_body``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import asyncpg

from packages.core.pagination import encode_cursor
from packages.db.draft import _row_to_draft
from packages.db.job import JOB_SELECT_COLUMNS, InMemoryJobStore, PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.domain.entities import GeneratedDraft, Job, NormalizedMessage
from packages.domain.review import (
    DraftStatus,
    ReviewVerdict,
    approval_verdict,
    rejection_verdict,
)
from packages.domain.state_machine import JobState

DRAFT_EDITED_EVENT = "draft_edited"
"""``processing_event.event_type`` of a reviewer edit; the first one carries original_body."""

_DECIDED_APPROVED = frozenset({DraftStatus.APPROVED.value, DraftStatus.DISPATCHED.value})


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class DraftView:
    """A draft with what the review queue shows next to it."""

    draft: GeneratedDraft
    mailbox_id: UUID
    original_subject: str
    sender_email: str
    original_provider_message_id: str
    category: str | None
    job_state: str | None


@dataclass(frozen=True)
class CitedChunk:
    """A knowledge chunk the draft cites, with its text (R16.5, R23.4)."""

    citation_id: str
    chunk_id: str
    document_id: str | None
    external_id: str | None
    content: str
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessFactView:
    """One ``[BUSINESS DATA]`` fact status as stored on the CONTEXT_READY event (R13.5)."""

    entity: str
    reference: str | None
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class BusinessDataView:
    """Customer status and fact statuses the draft was generated with (R13.5, R13.7)."""

    customer_status: str | None
    degraded: bool
    facts: tuple[BusinessFactView, ...] = ()

    @classmethod
    def from_context_payload(cls, payload: Mapping[str, Any] | None) -> BusinessDataView | None:
        """Read the payload written by ``packages.business.fetch.business_payload``."""
        if not payload or "business_fact_statuses" not in payload:
            return None
        facts = tuple(
            BusinessFactView(
                entity=str(fact.get("entity", "")),
                reference=fact.get("reference"),
                status=str(fact.get("status", "")),
                reason=fact.get("reason"),
            )
            for fact in payload.get("business_fact_statuses") or []
            if isinstance(fact, Mapping)
        )
        customer_status = payload.get("customer_status")
        return cls(
            customer_status=str(customer_status) if customer_status is not None else None,
            degraded=bool(payload.get("business_data_degraded", False)),
            facts=facts,
        )


@dataclass(frozen=True)
class FeedbackRecord:
    """One ``feedback`` row (R16.7)."""

    id: UUID
    organization_id: UUID
    draft_id: UUID
    decision: str
    edited_body: str | None
    edit_distance: int | None
    rating: int | None
    reviewer: str | None
    review_ms: int | None
    comment: str | None
    created_at: datetime


@dataclass(frozen=True)
class DraftDetail:
    """Everything the review screen needs for one draft (tasks 6.1, 6.8)."""

    view: DraftView
    original: NormalizedMessage
    thread_summary: str | None
    cited_chunks: tuple[CitedChunk, ...]
    business_data: BusinessDataView | None
    feedback: FeedbackRecord | None


@dataclass(frozen=True)
class DraftPage:
    """One keyset page, newest first; ``next_cursor`` is None on the last page."""

    items: tuple[DraftView, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ReviewInput:
    """Reviewer-supplied fields of a decision; ``review_ms`` is measured by the client."""

    reviewer: str | None = None
    rating: int | None = None
    review_ms: int | None = None
    comment: str | None = None


@dataclass(frozen=True)
class DecisionOutcome:
    """Result of approve/reject. ``created`` is False for a repeated call."""

    view: DraftView
    job: Job | None
    feedback: FeedbackRecord
    created: bool


class DraftConflictError(Exception):
    """The draft's or job's state forbids the requested review action (HTTP 409)."""

    def __init__(self, code: str, message: str, *, status: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@runtime_checkable
class ReviewStore(Protocol):
    """Tenant-scoped review operations behind /v1/drafts."""

    async def list_drafts(
        self,
        organization_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        mailbox_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> DraftPage:
        """Drafts newest first, strictly before ``after`` (created_at, id) when given."""
        ...

    async def get_draft_detail(self, organization_id: UUID, draft_id: UUID) -> DraftDetail | None:
        """The draft with its original email, thread summary, citations and facts."""
        ...

    async def edit_draft(
        self, organization_id: UUID, draft_id: UUID, *, body: str, subject: str | None = None
    ) -> DraftView | None:
        """Replace the body (and subject) while ``status=draft``; else DraftConflictError."""
        ...

    async def approve_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        """Mark approved and write the feedback row, once; repeats return the first result."""
        ...

    async def reject_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        """Mark rejected, write feedback and move the job DRAFTED -> COMPLETED, once."""
        ...


_VIEW_SELECT = """
    SELECT d.*,
           m.mailbox_id AS mailbox_id,
           m.subject AS original_subject,
           m.sender_email AS sender_email,
           m.provider_message_id AS original_provider_message_id,
           j.state AS job_state,
           c.category AS category
    FROM generated_draft d
    JOIN email_message m
      ON m.id = d.message_id AND m.organization_id = d.organization_id
    LEFT JOIN processing_job j
      ON j.id = d.job_id AND j.organization_id = d.organization_id
    LEFT JOIN LATERAL (
        SELECT cr.category
        FROM classification_result cr
        WHERE cr.message_id = d.message_id AND cr.organization_id = d.organization_id
        ORDER BY cr.created_at DESC
        LIMIT 1
    ) c ON TRUE
"""


def _row_to_view(row: asyncpg.Record) -> DraftView:
    return DraftView(
        draft=_row_to_draft(row),
        mailbox_id=row["mailbox_id"],
        original_subject=row["original_subject"] or "",
        sender_email=row["sender_email"] or "",
        original_provider_message_id=row["original_provider_message_id"],
        category=row["category"],
        job_state=row["job_state"],
    )


def _row_to_feedback(row: asyncpg.Record) -> FeedbackRecord:
    return FeedbackRecord(
        id=row["id"],
        organization_id=row["organization_id"],
        draft_id=row["draft_id"],
        decision=row["decision"],
        edited_body=row["edited_body"],
        edit_distance=row["edit_distance"],
        rating=row["rating"],
        reviewer=row["reviewer"],
        review_ms=row["review_ms"],
        comment=row["comment"],
        created_at=row["created_at"],
    )


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


class PostgresReviewStore:
    """PostgreSQL review unit of work (asyncpg)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._jobs = PostgresJobStore(pool)
        self._messages = PostgresMessageStore(pool)

    async def list_drafts(
        self,
        organization_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        mailbox_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> DraftPage:
        after_ts, after_id = after if after is not None else (None, None)
        query = (
            _VIEW_SELECT
            + """
            WHERE d.organization_id = $1
              AND ($2::text IS NULL OR d.status = $2)
              AND ($3::text IS NULL OR c.category = $3)
              AND ($4::uuid IS NULL OR m.mailbox_id = $4)
              AND ($5::timestamptz IS NULL OR (d.created_at, d.id) < ($5::timestamptz, $6::uuid))
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT $7
            """
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                query, organization_id, status, category, mailbox_id, after_ts, after_id, limit + 1
            )
        views = tuple(_row_to_view(r) for r in rows[:limit])
        next_cursor = (
            encode_cursor(views[-1].draft.created_at, views[-1].draft.id)
            if len(rows) > limit and views
            else None
        )
        return DraftPage(items=views, next_cursor=next_cursor)

    async def get_draft_detail(self, organization_id: UUID, draft_id: UUID) -> DraftDetail | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                _VIEW_SELECT + " WHERE d.id = $1 AND d.organization_id = $2",
                draft_id,
                organization_id,
            )
            if row is None:
                return None
            view = _row_to_view(row)
            summary = await conn.fetchval(
                "SELECT summary FROM thread_state WHERE thread_id = $1 AND organization_id = $2",
                _to_uuid(view.draft.thread_id),
                organization_id,
            )
            chunks = await self._cited_chunks(conn, organization_id, view.draft.citations)
            business = await self._business_data(conn, organization_id, view.draft.job_id)
            feedback = await self._feedback(conn, organization_id, draft_id)
        original = await self._messages.get_message(organization_id, view.draft.message_id)
        if original is None:
            return None
        return DraftDetail(
            view=view,
            original=original,
            thread_summary=summary,
            cited_chunks=chunks,
            business_data=business,
            feedback=feedback,
        )

    async def edit_draft(
        self, organization_id: UUID, draft_id: UUID, *, body: str, subject: str | None = None
    ) -> DraftView | None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                _VIEW_SELECT + " WHERE d.id = $1 AND d.organization_id = $2 FOR UPDATE OF d",
                draft_id,
                organization_id,
            )
            if row is None:
                return None
            view = _row_to_view(row)
            draft = view.draft
            if draft.status != DraftStatus.DRAFT.value:
                raise DraftConflictError(
                    "DRAFT_NOT_EDITABLE",
                    f"Draft '{draft_id}' is '{draft.status}'; only a draft can be edited.",
                    status=draft.status,
                )
            payload: dict[str, Any] = {"draft_id": str(draft.id), "body_chars": len(body)}
            if await self._generated_body(conn, organization_id, draft) is None:
                payload["original_body"] = draft.body
            await conn.execute(
                """
                INSERT INTO processing_event (job_id, message_id, organization_id, event_type,
                                              state_from, state_to, payload)
                VALUES ($1, $2, $3, $4, $5, $5, $6::jsonb)
                """,
                _to_uuid(draft.job_id) if draft.job_id else None,
                _to_uuid(draft.message_id),
                organization_id,
                DRAFT_EDITED_EVENT,
                view.job_state or JobState.DRAFTED.value,
                json.dumps(payload),
            )
            updated = await conn.fetchrow(
                """
                UPDATE generated_draft SET body = $3, subject = COALESCE($4, subject)
                WHERE id = $1 AND organization_id = $2
                RETURNING *
                """,
                draft_id,
                organization_id,
                body,
                subject,
            )
            assert updated is not None
        return replace(view, draft=_row_to_draft(updated))

    async def approve_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await self._lock(conn, organization_id, draft_id)
            if locked is None:
                return None
            view, job = locked
            draft = view.draft
            if job is None:
                raise DraftConflictError(
                    "DRAFT_HAS_NO_JOB",
                    f"Draft '{draft_id}' has no processing job to dispatch.",
                    status=draft.status,
                )
            if draft.status == DraftStatus.REJECTED.value:
                raise DraftConflictError(
                    "DRAFT_ALREADY_REJECTED", f"Draft '{draft_id}' was rejected.", status=draft.status
                )
            if draft.status in _DECIDED_APPROVED:
                return DecisionOutcome(
                    view=view,
                    job=job,
                    feedback=await self._require_feedback(conn, organization_id, draft_id),
                    created=False,
                )
            if job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be approved.",
                    status=draft.status,
                )
            generated = await self._generated_body(conn, organization_id, draft) or draft.body
            verdict = approval_verdict(generated, draft.body)
            updated = await self._set_status(conn, organization_id, draft_id, DraftStatus.APPROVED)
            feedback = await self._insert_feedback(conn, organization_id, draft_id, verdict, review)
            return DecisionOutcome(
                view=replace(view, draft=updated), job=job, feedback=feedback, created=True
            )

    async def reject_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await self._lock(conn, organization_id, draft_id)
            if locked is None:
                return None
            view, job = locked
            draft = view.draft
            if draft.status in _DECIDED_APPROVED:
                raise DraftConflictError(
                    "DRAFT_ALREADY_APPROVED", f"Draft '{draft_id}' was approved.", status=draft.status
                )
            if draft.status == DraftStatus.REJECTED.value:
                return DecisionOutcome(
                    view=view,
                    job=job,
                    feedback=await self._require_feedback(conn, organization_id, draft_id),
                    created=False,
                )
            if job is not None and job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be rejected.",
                    status=draft.status,
                )
            generated = await self._generated_body(conn, organization_id, draft) or draft.body
            verdict = rejection_verdict(generated, draft.body)
            updated = await self._set_status(conn, organization_id, draft_id, DraftStatus.REJECTED)
            feedback = await self._insert_feedback(conn, organization_id, draft_id, verdict, review)
            if job is not None:
                job, _ = await self._jobs.transition_job_state_on(
                    conn,
                    organization_id=organization_id,
                    job_id=job.id,
                    target_state=JobState.COMPLETED,
                    payload={
                        "decision": verdict.decision.value,
                        "draft_id": str(draft_id),
                        "feedback_id": str(feedback.id),
                    },
                )
                await conn.execute(
                    "UPDATE processing_job SET lease_expires_at = NULL"
                    " WHERE id = $1 AND organization_id = $2",
                    _to_uuid(job.id),
                    organization_id,
                )
                job.lease_expires_at = None
            return DecisionOutcome(
                view=replace(view, draft=updated, job_state=job.state if job else None),
                job=job,
                feedback=feedback,
                created=True,
            )

    async def _lock(
        self, conn: Any, organization_id: UUID, draft_id: UUID
    ) -> tuple[DraftView, Job | None] | None:
        """Lock the draft's job (first) and then the draft; None when the draft is unknown."""
        job_id = await conn.fetchval(
            "SELECT job_id FROM generated_draft WHERE id = $1 AND organization_id = $2",
            draft_id,
            organization_id,
        )
        job: Job | None = None
        if job_id is not None:
            job_row = await conn.fetchrow(
                f"SELECT {JOB_SELECT_COLUMNS} FROM processing_job"
                " WHERE id = $1 AND organization_id = $2 FOR UPDATE",
                job_id,
                organization_id,
            )
            if job_row is None:
                raise DraftConflictError(
                    "JOB_NOT_FOUND", f"Job '{job_id}' of draft '{draft_id}' is gone.", status="draft"
                )
            job = PostgresJobStore._row_to_job(job_row)  # noqa: SLF001
        row = await conn.fetchrow(
            _VIEW_SELECT + " WHERE d.id = $1 AND d.organization_id = $2 FOR UPDATE OF d",
            draft_id,
            organization_id,
        )
        if row is None:
            return None
        return _row_to_view(row), job

    async def _set_status(
        self, conn: Any, organization_id: UUID, draft_id: UUID, status: DraftStatus
    ) -> GeneratedDraft:
        row = await conn.fetchrow(
            "UPDATE generated_draft SET status = $3 WHERE id = $1 AND organization_id = $2"
            " RETURNING *",
            draft_id,
            organization_id,
            status.value,
        )
        assert row is not None
        return _row_to_draft(row)

    async def _insert_feedback(
        self,
        conn: Any,
        organization_id: UUID,
        draft_id: UUID,
        verdict: ReviewVerdict,
        review: ReviewInput,
    ) -> FeedbackRecord:
        row = await conn.fetchrow(
            """
            INSERT INTO feedback (id, organization_id, draft_id, reviewer, decision,
                                  edited_body, edit_distance, rating, comment, review_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (draft_id) DO NOTHING
            RETURNING *
            """,
            uuid4(),
            organization_id,
            draft_id,
            review.reviewer,
            verdict.decision.value,
            verdict.edited_body,
            verdict.edit_distance,
            review.rating,
            review.comment,
            review.review_ms,
        )
        if row is None:
            return await self._require_feedback(conn, organization_id, draft_id)
        return _row_to_feedback(row)

    async def _feedback(
        self, conn: Any, organization_id: UUID, draft_id: UUID
    ) -> FeedbackRecord | None:
        row = await conn.fetchrow(
            "SELECT * FROM feedback WHERE draft_id = $1 AND organization_id = $2",
            draft_id,
            organization_id,
        )
        return _row_to_feedback(row) if row is not None else None

    async def _require_feedback(
        self, conn: Any, organization_id: UUID, draft_id: UUID
    ) -> FeedbackRecord:
        feedback = await self._feedback(conn, organization_id, draft_id)
        if feedback is None:
            raise DraftConflictError(
                "FEEDBACK_MISSING",
                f"Draft '{draft_id}' was decided without a feedback row.",
                status="unknown",
            )
        return feedback

    async def _generated_body(
        self, conn: Any, organization_id: UUID, draft: GeneratedDraft
    ) -> str | None:
        """The body as generated, from the first ``draft_edited`` event; None if never edited."""
        value = await conn.fetchval(
            """
            SELECT payload->>'original_body' FROM processing_event
            WHERE organization_id = $1 AND message_id = $2 AND event_type = $3
              AND payload->>'draft_id' = $4 AND payload ? 'original_body'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            organization_id,
            _to_uuid(draft.message_id),
            DRAFT_EDITED_EVENT,
            str(draft.id),
        )
        return str(value) if value is not None else None

    async def _cited_chunks(
        self, conn: Any, organization_id: UUID, citations: Sequence[Mapping[str, Any]]
    ) -> tuple[CitedChunk, ...]:
        ids: list[UUID] = []
        for citation in citations:
            try:
                ids.append(UUID(str(citation.get("chunk_id"))))
            except ValueError:
                continue
        if not ids:
            return ()
        rows = await conn.fetch(
            "SELECT id, document_id, external_id, content, heading_path FROM knowledge_chunk"
            " WHERE organization_id = $1 AND id = ANY($2::uuid[])",
            organization_id,
            ids,
        )
        by_id = {str(r["id"]): r for r in rows}
        chunks: list[CitedChunk] = []
        for citation in citations:
            found = by_id.get(str(citation.get("chunk_id")))
            if found is None:
                continue
            chunks.append(
                CitedChunk(
                    citation_id=str(citation.get("citation_id") or found["external_id"] or found["id"]),
                    chunk_id=str(found["id"]),
                    document_id=str(found["document_id"]),
                    external_id=found["external_id"],
                    content=found["content"],
                    heading_path=tuple(found["heading_path"] or ()),
                )
            )
        return tuple(chunks)

    async def _business_data(
        self, conn: Any, organization_id: UUID, job_id: UUID | str | None
    ) -> BusinessDataView | None:
        if job_id is None or str(job_id) == "":
            return None
        raw = await conn.fetchval(
            """
            SELECT payload FROM processing_event
            WHERE organization_id = $1 AND job_id = $2 AND state_to = $3
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            organization_id,
            _to_uuid(job_id),
            JobState.CONTEXT_READY.value,
        )
        payload = _json(raw)
        return BusinessDataView.from_context_payload(payload if isinstance(payload, Mapping) else None)


@dataclass
class _Entry:
    draft: GeneratedDraft
    original: NormalizedMessage
    category: str | None
    thread_summary: str | None
    business_data: BusinessDataView | None
    generated_body: str | None = None


class InMemoryReviewStore:
    """In-memory ReviewStore for unit and UI tests; applies the same rules as Postgres."""

    def __init__(self, job_store: InMemoryJobStore | None = None) -> None:
        self.jobs = job_store or InMemoryJobStore()
        self._entries: dict[UUID, _Entry] = {}
        self._feedback: dict[UUID, FeedbackRecord] = {}
        self._chunks: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._lock = asyncio.Lock()

    def add_draft(
        self,
        draft: GeneratedDraft,
        *,
        original: NormalizedMessage,
        category: str | None = None,
        thread_summary: str | None = None,
        business_data: BusinessDataView | None = None,
    ) -> None:
        """Seed a draft and the context the review screen shows with it."""
        self._entries[_to_uuid(draft.id)] = _Entry(
            draft=draft,
            original=original,
            category=category,
            thread_summary=thread_summary,
            business_data=business_data,
        )

    def add_chunk(
        self, chunk_id: UUID | str, *, content: str, heading_path: Sequence[str] = ()
    ) -> None:
        """Seed a knowledge chunk that drafts may cite by ``chunk_id``."""
        self._chunks[str(chunk_id)] = (content, tuple(heading_path))

    def feedback_rows(self, organization_id: UUID) -> list[FeedbackRecord]:
        """Every feedback row of the tenant (test inspection)."""
        return [f for f in self._feedback.values() if f.organization_id == organization_id]

    def _entry(self, organization_id: UUID, draft_id: UUID) -> _Entry | None:
        entry = self._entries.get(draft_id)
        if entry is None or _to_uuid(entry.draft.organization_id) != organization_id:
            return None
        return entry

    async def _job(self, entry: _Entry) -> Job | None:
        if entry.draft.job_id is None or str(entry.draft.job_id) == "":
            return None
        return await self.jobs.get_job(entry.draft.organization_id, entry.draft.job_id)

    async def _view(self, entry: _Entry) -> DraftView:
        job = await self._job(entry)
        return DraftView(
            draft=entry.draft,
            mailbox_id=_to_uuid(entry.original.mailbox_id),
            original_subject=entry.original.subject,
            sender_email=entry.original.sender.email,
            original_provider_message_id=entry.original.provider_message_id,
            category=entry.category,
            job_state=job.state if job is not None else None,
        )

    async def list_drafts(
        self,
        organization_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        mailbox_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> DraftPage:
        async with self._lock:
            entries = [
                e for e in self._entries.values() if _to_uuid(e.draft.organization_id) == organization_id
            ]
            views = [await self._view(e) for e in entries]
        matching = [
            v
            for v in views
            if (status is None or v.draft.status == status)
            and (category is None or v.category == category)
            and (mailbox_id is None or v.mailbox_id == mailbox_id)
            and (after is None or (v.draft.created_at, v.draft.id) < after)
        ]
        matching.sort(key=lambda v: (v.draft.created_at, v.draft.id), reverse=True)
        page = tuple(matching[:limit])
        next_cursor = (
            encode_cursor(page[-1].draft.created_at, page[-1].draft.id)
            if len(matching) > limit and page
            else None
        )
        return DraftPage(items=page, next_cursor=next_cursor)

    async def get_draft_detail(self, organization_id: UUID, draft_id: UUID) -> DraftDetail | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            chunks: list[CitedChunk] = []
            for citation in entry.draft.citations:
                stored = self._chunks.get(str(citation.get("chunk_id")))
                if stored is None:
                    continue
                content, heading_path = stored
                chunks.append(
                    CitedChunk(
                        citation_id=str(citation.get("citation_id") or citation.get("chunk_id")),
                        chunk_id=str(citation.get("chunk_id")),
                        document_id=(
                            str(citation["document_id"]) if citation.get("document_id") else None
                        ),
                        external_id=citation.get("external_id"),
                        content=content,
                        heading_path=heading_path,
                    )
                )
            return DraftDetail(
                view=await self._view(entry),
                original=entry.original,
                thread_summary=entry.thread_summary,
                cited_chunks=tuple(chunks),
                business_data=entry.business_data,
                feedback=self._feedback.get(draft_id),
            )

    async def edit_draft(
        self, organization_id: UUID, draft_id: UUID, *, body: str, subject: str | None = None
    ) -> DraftView | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            if entry.draft.status != DraftStatus.DRAFT.value:
                raise DraftConflictError(
                    "DRAFT_NOT_EDITABLE",
                    f"Draft '{draft_id}' is '{entry.draft.status}'; only a draft can be edited.",
                    status=entry.draft.status,
                )
            if entry.generated_body is None:
                entry.generated_body = entry.draft.body
            entry.draft = replace(
                entry.draft, body=body, subject=subject if subject is not None else entry.draft.subject
            )
            return await self._view(entry)

    async def approve_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            draft = entry.draft
            job = await self._job(entry)
            if job is None:
                raise DraftConflictError(
                    "DRAFT_HAS_NO_JOB",
                    f"Draft '{draft_id}' has no processing job to dispatch.",
                    status=draft.status,
                )
            if draft.status == DraftStatus.REJECTED.value:
                raise DraftConflictError(
                    "DRAFT_ALREADY_REJECTED", f"Draft '{draft_id}' was rejected.", status=draft.status
                )
            if draft.status in _DECIDED_APPROVED:
                return DecisionOutcome(
                    view=await self._view(entry),
                    job=job,
                    feedback=self._feedback[draft_id],
                    created=False,
                )
            if job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be approved.",
                    status=draft.status,
                )
            verdict = approval_verdict(entry.generated_body or draft.body, draft.body)
            entry.draft = replace(draft, status=DraftStatus.APPROVED.value)
            feedback = self._record(organization_id, draft_id, verdict, review)
            return DecisionOutcome(
                view=await self._view(entry), job=job, feedback=feedback, created=True
            )

    async def reject_draft(
        self, organization_id: UUID, draft_id: UUID, review: ReviewInput
    ) -> DecisionOutcome | None:
        async with self._lock:
            entry = self._entry(organization_id, draft_id)
            if entry is None:
                return None
            draft = entry.draft
            job = await self._job(entry)
            if draft.status in _DECIDED_APPROVED:
                raise DraftConflictError(
                    "DRAFT_ALREADY_APPROVED", f"Draft '{draft_id}' was approved.", status=draft.status
                )
            if draft.status == DraftStatus.REJECTED.value:
                return DecisionOutcome(
                    view=await self._view(entry),
                    job=job,
                    feedback=self._feedback[draft_id],
                    created=False,
                )
            if job is not None and job.state != JobState.DRAFTED.value:
                raise DraftConflictError(
                    "JOB_NOT_DRAFTED",
                    f"Job '{job.id}' is '{job.state}'; only a DRAFTED job can be rejected.",
                    status=draft.status,
                )
            verdict = rejection_verdict(entry.generated_body or draft.body, draft.body)
            feedback_id = uuid4()
            if job is not None:
                # Transition first: it raises on an illegal state before anything is stored.
                job, _ = await self.jobs.transition_job_state(
                    organization_id=organization_id,
                    job_id=job.id,
                    target_state=JobState.COMPLETED,
                    payload={
                        "decision": verdict.decision.value,
                        "draft_id": str(draft_id),
                        "feedback_id": str(feedback_id),
                    },
                )
                job.lease_expires_at = None
            entry.draft = replace(draft, status=DraftStatus.REJECTED.value)
            feedback = self._record(organization_id, draft_id, verdict, review, feedback_id)
            return DecisionOutcome(
                view=await self._view(entry), job=job, feedback=feedback, created=True
            )

    def _record(
        self,
        organization_id: UUID,
        draft_id: UUID,
        verdict: ReviewVerdict,
        review: ReviewInput,
        feedback_id: UUID | None = None,
    ) -> FeedbackRecord:
        existing = self._feedback.get(draft_id)
        if existing is not None:  # UNIQUE (draft_id)
            return existing
        record = FeedbackRecord(
            id=feedback_id or uuid4(),
            organization_id=organization_id,
            draft_id=draft_id,
            decision=verdict.decision.value,
            edited_body=verdict.edited_body,
            edit_distance=verdict.edit_distance,
            rating=review.rating,
            reviewer=review.reviewer,
            review_ms=review.review_ms,
            comment=review.comment,
            created_at=datetime.now(UTC),
        )
        self._feedback[draft_id] = record
        return record
```

- [ ] **Step 10: Implement the API schemas, router and wiring**

Create `services/api/schemas/drafts.py`:

```python
"""Pydantic schemas for the draft review API (R16.6, R16.7, R23.2, R23.6; design.md §5.8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DraftSummaryResponse(BaseModel):
    """One row of the review queue."""

    id: UUID = Field(description="Draft identifier.")
    organization_id: UUID = Field(description="Tenant organization UUID.")
    job_id: UUID | None = Field(description="Processing job carrying generation and dispatch.")
    message_id: UUID = Field(description="The inbound email the draft answers.")
    thread_id: UUID = Field(description="Conversation thread UUID.")
    mailbox_id: UUID = Field(description="Mailbox that received the email.")
    status: str = Field(description="draft | approved | rejected | dispatched.")
    category: str | None = Field(description="Latest triage category of the email.")
    job_state: str | None = Field(description="Current processing_job state.")
    subject: str | None = Field(description="Draft subject.")
    original_subject: str = Field(description="Subject of the inbound email.")
    sender_email: str = Field(description="Sender of the inbound email.")
    confidence: float | None = Field(description="Model confidence, if reported.")
    citation_mismatch: bool = Field(description="True when a citation was not in the context.")
    model_tier: str | None = Field(description="fast | strong model tier.")
    created_at: datetime = Field(description="Draft creation time.")


class DraftListResponse(BaseModel):
    """A keyset page of drafts, newest first (R23.6)."""

    items: list[DraftSummaryResponse] = Field(description="Drafts on this page.")
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` for the next page; null on the last page."
    )
    limit: int = Field(ge=1, description="Requested page size.")


class OriginalEmailResponse(BaseModel):
    """The inbound email under review."""

    message_id: UUID = Field(description="Email message UUID.")
    sender_email: str = Field(description="Sender address.")
    sender_name: str | None = Field(description="Sender display name.")
    subject: str = Field(description="Subject line.")
    body_text: str = Field(description="Plain-text body.")
    received_at: datetime = Field(description="Receipt time.")
    rfc822_message_id: str | None = Field(description="RFC 5322 Message-ID, without <>.")


class CitedChunkResponse(BaseModel):
    """A knowledge chunk the draft cites, with its text."""

    citation_id: str = Field(description="Citation id as written in the draft.")
    chunk_id: str = Field(description="knowledge_chunk UUID.")
    document_id: str | None = Field(description="knowledge_document UUID.")
    external_id: str | None = Field(description="Stable chunk id such as DOC-125-08.")
    content: str = Field(description="Chunk text.")
    heading_path: list[str] = Field(description="Section headings above the chunk.")


class BusinessFactResponse(BaseModel):
    """One [BUSINESS DATA] fact status used for the draft."""

    entity: str = Field(description="order | ticket | invoice.")
    reference: str | None = Field(description="Reference such as ORD-82915.")
    status: str = Field(description="FOUND | NOT_FOUND | NOT_LOOKED_UP | UNAVAILABLE.")
    reason: str | None = Field(description="Why a fact was not looked up.")


class BusinessDataResponse(BaseModel):
    """Customer status and fact statuses from the job's CONTEXT_READY event."""

    customer_status: str | None = Field(description="Customer resolution status.")
    degraded: bool = Field(description="True when the business lookup timed out or failed.")
    facts: list[BusinessFactResponse] = Field(description="Planned facts and their status.")


class FeedbackResponse(BaseModel):
    """The draft's feedback row (R16.7)."""

    id: UUID = Field(description="Feedback UUID.")
    decision: str = Field(description="accepted | edited | rejected.")
    edited_body: str | None = Field(description="Final body when it differs from the draft.")
    edit_distance: int | None = Field(description="Character edit distance from the draft.")
    rating: int | None = Field(description="Optional 1-5 rating.")
    reviewer: str | None = Field(description="Free-text reviewer label (no login, ADR-0009).")
    review_ms: int | None = Field(description="Time from opening the draft to deciding.")
    comment: str | None = Field(description="Optional reviewer comment.")
    created_at: datetime = Field(description="Decision time.")


class DraftDetailResponse(DraftSummaryResponse):
    """A draft with everything a reviewer needs (task 6.1)."""

    body: str = Field(description="Draft body.")
    action: str = Field(description="reply | forward | escalate.")
    citations: list[dict[str, Any]] = Field(description="Raw citation records of the draft.")
    provider_ref: str | None = Field(description="Provider id recorded at dispatch.")
    original: OriginalEmailResponse = Field(description="The inbound email.")
    thread_summary: str | None = Field(description="Rolling thread summary, if any.")
    cited_chunks: list[CitedChunkResponse] = Field(description="Cited chunks with text.")
    business_data: BusinessDataResponse | None = Field(description="Business fact statuses.")
    feedback: FeedbackResponse | None = Field(description="The decision, once made.")
    dispatch_mode: str | None = Field(
        default=None,
        description="create_draft | send_reply for the draft's category, read at request time.",
    )


class DraftEditRequest(BaseModel):
    """PATCH body: the reviewer's edited text."""

    body: str = Field(min_length=1, max_length=100_000, description="New draft body.")
    subject: str | None = Field(default=None, max_length=998, description="New subject line.")


class DraftDecisionRequest(BaseModel):
    """Optional fields of an approve or reject call."""

    reviewer: str | None = Field(default=None, max_length=200, description="Reviewer label.")
    rating: int | None = Field(default=None, ge=1, le=5, description="Optional 1-5 rating.")
    review_ms: int | None = Field(
        default=None, ge=0, le=86_400_000, description="Client-measured review time in ms."
    )
    comment: str | None = Field(default=None, max_length=2000, description="Optional comment.")


class DraftDecisionResponse(BaseModel):
    """Result of approve or reject."""

    draft_id: UUID = Field(description="Draft identifier.")
    job_id: UUID | None = Field(description="Processing job identifier.")
    draft_status: str = Field(description="Draft status after the call.")
    job_state: str | None = Field(description="Job state after the call.")
    decision: str = Field(description="accepted | edited | rejected (the first decision).")
    edit_distance: int | None = Field(description="Character edit distance from the draft.")
    feedback_id: UUID = Field(description="The draft's one feedback row.")
    created: bool = Field(description="False when this repeated an earlier decision.")
    published: bool = Field(description="True when a dispatch job was published by this call.")
```

Create `services/api/routers/drafts.py`:

```python
"""Draft review endpoints: list, read, edit, approve and reject (R16.6, R16.7, R23.2, R23.6).

Approve commits the decision first, then publishes the dispatch job to ``email.dispatch``. A
repeated approve re-publishes while the job is DRAFTED, DISPATCHED or RETRY_PENDING (dispatch
is idempotent) and writes no second feedback row, so a lost publish cannot strand an
approved draft. Reject moves the job DRAFTED -> COMPLETED with no send (design.md §5.8).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from packages.broker.envelope import JobEnvelope
from packages.core.idempotency import derive_idempotency_key
from packages.core.pagination import InvalidCursorError, decode_cursor
from packages.db.review import DecisionOutcome, DraftConflictError, DraftDetail, DraftView, ReviewInput
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry, get_default_registry
from packages.observability.context import bind_log_context
from packages.observability.metrics import PipelineMetrics, get_metrics
from services.api.dependencies import PublisherDep, ReviewStoreDep, get_organization_id
from services.api.schemas.drafts import (
    BusinessDataResponse,
    BusinessFactResponse,
    CitedChunkResponse,
    DraftDecisionRequest,
    DraftDecisionResponse,
    DraftDetailResponse,
    DraftEditRequest,
    DraftListResponse,
    DraftSummaryResponse,
    FeedbackResponse,
    OriginalEmailResponse,
)

logger = logging.getLogger("api.drafts")

drafts_router = APIRouter(prefix="/drafts", tags=["drafts"])

REPUBLISH_JOB_STATES = frozenset(
    {JobState.DRAFTED.value, JobState.DISPATCHED.value, JobState.RETRY_PENDING.value}
)
"""Job states in which an approve (first or repeated) publishes the dispatch job."""

DraftIdPath = Annotated[UUID, Path(description="Draft unique identifier.")]
OrgId = Annotated[UUID, Depends(get_organization_id)]


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _opt_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or str(value) == "":
        return None
    return _uuid(value)


def _not_found(draft_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": f"Draft '{draft_id}' not found", "code": "DRAFT_NOT_FOUND"},
    )


def _conflict(err: DraftConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": err.message, "code": err.code, "current_status": err.status},
    )


def _metrics(request: Request) -> PipelineMetrics:
    metrics: PipelineMetrics | None = getattr(request.app.state, "metrics", None)
    return metrics or get_metrics()


def _taxonomy(request: Request) -> TaxonomyRegistry:
    """The registry create_app loaded from config/categories.yaml (tasks 6.4, 6.8)."""
    registry: TaxonomyRegistry | None = getattr(request.app.state, "taxonomy", None)
    return registry or get_default_registry()


def _summary_fields(view: DraftView) -> dict[str, Any]:
    draft = view.draft
    return {
        "id": draft.id,
        "organization_id": _uuid(draft.organization_id),
        "job_id": _opt_uuid(draft.job_id),
        "message_id": _uuid(draft.message_id),
        "thread_id": _uuid(draft.thread_id),
        "mailbox_id": view.mailbox_id,
        "status": draft.status,
        "category": view.category,
        "job_state": view.job_state,
        "subject": draft.subject,
        "original_subject": view.original_subject,
        "sender_email": view.sender_email,
        "confidence": draft.confidence,
        "citation_mismatch": draft.citation_mismatch,
        "model_tier": draft.model_tier,
        "created_at": draft.created_at,
    }


def _detail_response(detail: DraftDetail, taxonomy: TaxonomyRegistry) -> DraftDetailResponse:
    draft = detail.view.draft
    original = detail.original
    feedback = detail.feedback
    business = detail.business_data
    return DraftDetailResponse(
        **_summary_fields(detail.view),
        body=draft.body,
        action=draft.action,
        citations=list(draft.citations),
        provider_ref=draft.provider_ref,
        original=OriginalEmailResponse(
            message_id=_uuid(original.message_id),
            sender_email=original.sender.email,
            sender_name=original.sender.name,
            subject=original.subject,
            body_text=original.body_text,
            received_at=original.received_at,
            rfc822_message_id=original.rfc822_message_id,
        ),
        thread_summary=detail.thread_summary,
        cited_chunks=[
            CitedChunkResponse(
                citation_id=c.citation_id,
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                external_id=c.external_id,
                content=c.content,
                heading_path=list(c.heading_path),
            )
            for c in detail.cited_chunks
        ],
        business_data=(
            BusinessDataResponse(
                customer_status=business.customer_status,
                degraded=business.degraded,
                facts=[
                    BusinessFactResponse(
                        entity=f.entity, reference=f.reference, status=f.status, reason=f.reason
                    )
                    for f in business.facts
                ],
            )
            if business is not None
            else None
        ),
        feedback=(
            FeedbackResponse(
                id=feedback.id,
                decision=feedback.decision,
                edited_body=feedback.edited_body,
                edit_distance=feedback.edit_distance,
                rating=feedback.rating,
                reviewer=feedback.reviewer,
                review_ms=feedback.review_ms,
                comment=feedback.comment,
                created_at=feedback.created_at,
            )
            if feedback is not None
            else None
        ),
        dispatch_mode=(
            taxonomy.dispatch_mode_for(detail.view.category).value
            if detail.view.category
            else None
        ),
    )


def _review_input(body: DraftDecisionRequest | None) -> ReviewInput:
    if body is None:
        return ReviewInput()
    return ReviewInput(
        reviewer=body.reviewer, rating=body.rating, review_ms=body.review_ms, comment=body.comment
    )


def _decision_response(outcome: DecisionOutcome, *, published: bool) -> DraftDecisionResponse:
    draft = outcome.view.draft
    return DraftDecisionResponse(
        draft_id=draft.id,
        job_id=_opt_uuid(draft.job_id),
        draft_status=draft.status,
        job_state=outcome.job.state if outcome.job is not None else None,
        decision=outcome.feedback.decision,
        edit_distance=outcome.feedback.edit_distance,
        feedback_id=outcome.feedback.id,
        created=outcome.created,
        published=published,
    )


def _count_decision(request: Request, outcome: DecisionOutcome) -> None:
    """draft_decisions_total counts first decisions only (R16.7, R21.4, SC3)."""
    if outcome.created:
        _metrics(request).draft_decisions_total.labels(
            decision=outcome.feedback.decision, category=outcome.view.category or "unknown"
        ).inc()


async def _publish_dispatch(publisher: Any, org_id: UUID, outcome: DecisionOutcome) -> None:
    """Publish the dispatch job after the approval committed (design.md §5.8)."""
    view = outcome.view
    draft = view.draft
    job = outcome.job
    assert job is not None
    unavailable = {
        "error": (
            "The approval is recorded but the dispatch job was not queued; "
            "approve again once the broker is available."
        ),
        "draft_id": str(draft.id),
    }
    if publisher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**unavailable, "code": "PUBLISHER_UNAVAILABLE"},
        )
    routing_key = publisher.settings.queue_dispatch
    exchange_name = publisher.settings.exchange_for_queue(routing_key)
    if exchange_name is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**unavailable, "code": "PUBLISHER_UNAVAILABLE"},
        )
    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=derive_idempotency_key(
            organization_id=org_id,
            mailbox_id=view.mailbox_id,
            provider_message_id=view.original_provider_message_id,
            operation_type="dispatch",
        ),
        job_type="dispatch",
        organization_id=str(org_id),
        message_id=str(draft.message_id),
        thread_id=str(draft.thread_id),
        mailbox_id=str(view.mailbox_id),
        payload={"draft_id": str(draft.id), "trigger": "approve"},
    )
    try:
        await publisher.publish(exchange_name=exchange_name, routing_key=routing_key, envelope=envelope)
    except Exception as err:
        logger.error("Dispatch publish for draft %s failed: %s", draft.id, err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**unavailable, "code": "DISPATCH_PUBLISH_FAILED"},
        ) from err


@drafts_router.get(
    "",
    summary="List Drafts",
    description="Drafts newest first, filtered by status, category and mailbox (R16.6, R23.6).",
    response_model=DraftListResponse,
)
async def list_drafts(
    org_id: OrgId,
    store: ReviewStoreDep,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")] = 50,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page.")] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(draft|approved|rejected|dispatched)$"),
    ] = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    mailbox: Annotated[UUID | None, Query(description="Mailbox UUID.")] = None,
) -> DraftListResponse:
    bind_log_context(organization_id=str(org_id))
    after = None
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursorError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": str(err), "code": "INVALID_CURSOR"},
            ) from err
    page = await store.list_drafts(
        org_id,
        status=status_filter,
        category=category,
        mailbox_id=mailbox,
        after=after,
        limit=limit,
    )
    return DraftListResponse(
        items=[DraftSummaryResponse(**_summary_fields(v)) for v in page.items],
        next_cursor=page.next_cursor,
        limit=limit,
    )


@drafts_router.get(
    "/{draft_id}",
    summary="Get Draft",
    description="Draft with its email, thread summary, cited chunks and business facts (R16.6).",
    response_model=DraftDetailResponse,
)
async def get_draft(
    request: Request, draft_id: DraftIdPath, org_id: OrgId, store: ReviewStoreDep
) -> DraftDetailResponse:
    bind_log_context(organization_id=str(org_id))
    detail = await store.get_draft_detail(org_id, draft_id)
    if detail is None:
        raise _not_found(draft_id)
    return _detail_response(detail, _taxonomy(request))


@drafts_router.patch(
    "/{draft_id}",
    summary="Edit Draft",
    description="Replace the draft body while its status is draft (R16.6).",
    response_model=DraftSummaryResponse,
)
async def edit_draft(
    draft_id: DraftIdPath, org_id: OrgId, store: ReviewStoreDep, body: DraftEditRequest
) -> DraftSummaryResponse:
    bind_log_context(organization_id=str(org_id))
    try:
        view = await store.edit_draft(org_id, draft_id, body=body.body, subject=body.subject)
    except DraftConflictError as err:
        raise _conflict(err) from err
    if view is None:
        raise _not_found(draft_id)
    return DraftSummaryResponse(**_summary_fields(view))


@drafts_router.post(
    "/{draft_id}/approve",
    summary="Approve Draft",
    description=(
        "Record the approval and one feedback row, then publish the dispatch job to "
        "email.dispatch. Repeating it re-publishes until the job completes (R16.6, R16.7)."
    ),
    response_model=DraftDecisionResponse,
)
async def approve_draft(
    request: Request,
    draft_id: DraftIdPath,
    org_id: OrgId,
    store: ReviewStoreDep,
    publisher: PublisherDep,
    body: DraftDecisionRequest | None = None,
) -> DraftDecisionResponse:
    bind_log_context(organization_id=str(org_id))
    try:
        outcome = await store.approve_draft(org_id, draft_id, _review_input(body))
    except DraftConflictError as err:
        raise _conflict(err) from err
    if outcome is None:
        raise _not_found(draft_id)
    _count_decision(request, outcome)
    published = False
    if outcome.job is not None and outcome.job.state in REPUBLISH_JOB_STATES:
        await _publish_dispatch(publisher, org_id, outcome)
        published = True
    return _decision_response(outcome, published=published)


@drafts_router.post(
    "/{draft_id}/reject",
    summary="Reject Draft",
    description="Record the rejection and move the job DRAFTED -> COMPLETED; nothing is sent.",
    response_model=DraftDecisionResponse,
)
async def reject_draft(
    request: Request,
    draft_id: DraftIdPath,
    org_id: OrgId,
    store: ReviewStoreDep,
    body: DraftDecisionRequest | None = None,
) -> DraftDecisionResponse:
    bind_log_context(organization_id=str(org_id))
    try:
        outcome = await store.reject_draft(org_id, draft_id, _review_input(body))
    except DraftConflictError as err:
        raise _conflict(err) from err
    if outcome is None:
        raise _not_found(draft_id)
    _count_decision(request, outcome)
    return _decision_response(outcome, published=False)
```

In `services/api/dependencies.py`, add after `get_knowledge_store` (after line 180):

```python
def get_review_store(request: Request) -> Any:
    """Retrieve the draft ReviewStore from app.state or create it from db_pool (task 6.1)."""
    store = getattr(request.app.state, "review_store", None)
    if store is not None:
        return store
    db_pool = getattr(request.app.state, "db_pool", None)
    if db_pool is not None:
        from packages.db.review import PostgresReviewStore

        return PostgresReviewStore(db_pool)
    from packages.db.review import InMemoryReviewStore

    return InMemoryReviewStore()
```

and add to the alias block (after `PublisherDep`, line 189):

```python
ReviewStoreDep = Annotated[Any, Depends(get_review_store)]
```

In `services/api/routers/v1.py`, add `from services.api.routers.drafts import drafts_router` to the router imports (alphabetical, before `jobs_router`) and `v1_router.include_router(drafts_router)` after `v1_router.include_router(search_router)`.

In `services/api/main.py`, add to the imports (after `from packages.adapters.webhooks import webhook_router`):

```python
from packages.broker.routing import load_categories_from_yaml
```

and (after `from packages.db.connection import create_pool_from_settings`):

```python
from packages.domain.taxonomy import TaxonomyRegistry
```

then in `create_app`, directly after `app.state.settings = active_settings`, add:

```python
    # dispatch_mode per category (tasks 6.4, 6.8): the same config/categories.yaml the
    # dispatch-worker loads, in the API's own registry, so GET /v1/drafts/{id} tells the
    # reviewer what approve will really do. A bad dispatch_mode fails startup, as it does
    # in the worker; the process-default registry is left untouched.
    taxonomy = TaxonomyRegistry()
    load_categories_from_yaml(active_settings.routing.categories_config_path, registry=taxonomy)
    app.state.taxonomy = taxonomy
```

It runs in `create_app`, not in the lifespan, so tests built with `lifespan_enabled=False` see the same mode as the running API.

- [ ] **Step 11: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_drafts_api.py tests/integration/test_drafts_api_integration.py tests/unit/test_api_skeleton.py tests/unit/test_dependency_rules.py -v`
Expected: PASS. (The integration file needs the local Postgres/RabbitMQ from `make up`; the package conftest isolates it to `rag_email_test`, and migration 0005 from Part A is applied there automatically.)

Run: `uv run python -m services.api.openapi --check`
Expected: exit 0 (the new `/v1/drafts` paths validate).

- [ ] **Step 12: Document the review metric and its PromQL**

In `docs/observability.md` §2.2, add this row after the `business_lookup_latency_ms` row:

```markdown
| `draft_decisions_total` | Counter | `decision`, `category` | One increment per draft's first reviewer decision: `accepted` (approved unchanged), `edited` (approved after edits) or `rejected`. A repeated approve or reject does not count again; `category` is the email's latest triage category, `unknown` if none (R16.7, R21.4, SC3). |
```

and insert this subsection before the `---` that closes §2 (line 156):

````markdown
#### Review decisions (R16.7, R21.4, SC3)

`POST /v1/drafts/{id}/approve` and `/reject` write the draft's single `feedback` row
(`UNIQUE (draft_id)`) with `decision`, `edited_body`, the character-level `edit_distance`
from the generated body (kept on the first `draft_edited` event), `rating`, the free-text
`reviewer` label and the client-measured `review_ms`, then increment
`draft_decisions_total{decision, category}` once. The acceptance rate and the
approved-without-edits rate are reported separately, because an unchanged approval can also
mean an unread draft (design.md §5.8).

```promql
# Acceptance rate (SC3 target >= 80%): approved drafts, edited or not, over all decisions
sum by (category) (increase(draft_decisions_total{decision=~"accepted|edited"}[7d]))
/
sum by (category) (increase(draft_decisions_total[7d]))

# Approved-without-edits rate: unchanged approvals over all decisions
sum by (category) (increase(draft_decisions_total{decision="accepted"}[7d]))
/
sum by (category) (increase(draft_decisions_total[7d]))
```

Edit size and review time come from the table, not from Prometheus:

```sql
SELECT decision,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY edit_distance) AS median_edit_distance,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY review_ms) AS median_review_ms
FROM feedback
WHERE organization_id = $1 AND created_at > now() - interval '7 days'
GROUP BY decision;
```
````

- [ ] **Step 12a (needs Open Question D2): Make design §5.8 agree with itself on a repeated approve**

`specs/design.md:686` (Review API) says "Approve and reject are idempotent: a repeated call returns the first result and publishes nothing new." `specs/design.md:721` and tasks.md 6.1 say a repeated approve re-publishes the dispatch job while the job is not `COMPLETED`, which is what this task implements and what `test_two_fast_approves_write_one_feedback_row` and the repeated-approve tests assert. After the owner approves D2 (GEMINI.md §7), replace that sentence on line 686 with:

```
Approve and reject are idempotent: a repeated approve returns the first result and re-publishes the dispatch job while the job is not `COMPLETED` (dispatch is idempotent, so a lost publish cannot strand an approved draft); a repeated reject returns the first result.
```

Run: `grep -n "publishes nothing new" specs/design.md`
Expected: no output. If D2 is not approved yet, skip this step, leave `specs/design.md` out of the commit below, and Task 13's audit keeps 6.1 at `[~]`.

- [ ] **Step 13: Lint, type-check and commit**

```bash
uv run ruff format packages/db/job.py packages/db/review.py services/api/schemas/drafts.py services/api/routers/drafts.py services/api/dependencies.py services/api/routers/v1.py services/api/main.py tests/unit/test_drafts_api.py tests/integration/test_drafts_api_integration.py
uv run ruff check packages services tests
uv run mypy packages services tests
uv run pytest tests/unit -q
git add packages/db/job.py packages/db/review.py services/api/schemas/drafts.py services/api/routers/drafts.py services/api/dependencies.py services/api/routers/v1.py services/api/main.py tests/unit/test_drafts_api.py tests/integration/test_drafts_api_integration.py docs/observability.md specs/design.md
git commit -m "feat(api): draft review API with feedback rows and approve-then-publish [task 6.1, 6.2] [R16.6, R16.7, R21.4, R23.2, R23.6]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
Expected: ruff and mypy report no issues; the unit suite passes.

### Task 7: DispatchService — claim, provider draft, send, confirm, finish [tasks.md 6.5, 6.6, 6.7]

**Files:**
- Create: `tests/stubs/dispatch_fakes.py` (`RecordingFake`, `registry_with`, `DispatchWorld`, `build_dispatch_world`; shared with Task 8)
- Modify: `packages/db/message.py` (module-level `_INSERT_MESSAGE_SQL`, `_recipients_json`, `_message_insert_args`, `insert_message_on`; `PostgresMessageStore._serialize_recipients` (line 290) delegates; `PostgresMessageStore.insert_message` lines 369–438 use the shared SQL)
- Modify: `packages/db/thread.py` (module-level `_TOUCH_THREAD_SQL` and `touch_thread_on`; `PostgresThreadStore.update_thread_on_message` lines 474–489 use `_TOUCH_THREAD_SQL`; imports lines 10–13)
- Create: `packages/db/dispatch.py` (`DispatchContext`, `ClaimStatus`, `ClaimOutcome`, `DispatchKeyConflictError`, `DispatchStore` Protocol, `PostgresDispatchStore`, `InMemoryDispatchStore`)
- Create: `packages/dispatch/service.py` (`DispatchService`, `DispatchOutcome`, `DispatchPermanentError`, `outbound_message`, `bare_message_id`, `message_id_domain`)
- Test: `tests/unit/test_dispatch_service.py` (new), `tests/integration/test_dispatch_store_postgres.py` (new); regression `tests/unit/test_message_persistence.py`, `tests/integration/test_database_schema.py`, `tests/unit/test_thread*.py`

**Interfaces:**
- Consumes: `derive_idempotency_key(organization_id=, mailbox_id=, provider_message_id=, operation_type="dispatch")`; `JOB_SELECT_COLUMNS` (Task 6); `PostgresJobStore.transition_job_state_on(...)`, `PostgresJobStore._row_to_job(row)`, `InMemoryJobStore.transition_job_state(...)`; `fetch_draft_for_job(conn, job_id, organization_id)`, `_row_to_draft(row)`; `PostgresMessageStore.get_message(org, id)`; `PostgresThreadStore.get_thread(org, id)`; `build_outbound_reply(*, draft, original, provider_thread_id, message_id_domain)` and `MissingProviderThreadError` (packages/dispatch/reply.py, Task 2); `MailProviderAdapter.create_draft / send_draft / get_draft_status / find_sent_message / find_draft` (Task 5); `get_adapter_for_mailbox(mailbox)`; `TaxonomyRegistry.get(category).dispatch_mode / .auto_send_eligible`; `DispatchMode`, `ProviderDraftStatus` (packages.domain); `DraftStatus` (Task 6).
- Produces:
  - `packages.db.message.insert_message_on(conn: Any, message: NormalizedMessage) -> bool` (False on the `(organization_id, mailbox_id, provider_message_id)` conflict).
  - `packages.db.thread.touch_thread_on(conn: Any, *, thread_id: UUID, organization_id: UUID, message_time: datetime, participants: Sequence[str]) -> bool`.
  - `packages.db.dispatch`: `@dataclass(frozen=True) DispatchContext(job: Job, draft: GeneratedDraft, original: NormalizedMessage, thread: EmailThread, mailbox: Mailbox, category: str | None)`; `class ClaimStatus(StrEnum)` (`CLAIMED`, `RESUMED`, `ALREADY_COMPLETED`, `NOT_DISPATCHABLE`); `ClaimOutcome(status, job, draft)`; `class DispatchKeyConflictError(Exception)`; `DispatchStore` Protocol: `job_lock(organization_id: UUID, job_id: UUID) -> AbstractAsyncContextManager[bool]` (Postgres: session-level `pg_try_advisory_lock(hashtextextended('dispatch:<org>:<job>', 0))` on its own pool connection, never waits; in-memory: a per-job `asyncio.Lock`), `set_dispatch_queue(*, organization_id, job_id, queue_name) -> None` (org-scoped; only while the job is `DRAFTED`/`DISPATCHED`/`RETRY_PENDING`/`FAILED`), `load(organization_id: UUID, job_id: UUID) -> DispatchContext | None`, `claim(*, organization_id, job_id, draft_id, idempotency_key, queue_name) -> ClaimOutcome`, `record_provider_draft(*, organization_id, draft_id, provider_draft_id, provider_draft_message_id) -> GeneratedDraft`, `finish(*, organization_id, job_id, draft_id, mode: DispatchMode, provider_ref: str, outbound: NormalizedMessage | None) -> Job`; `PostgresDispatchStore(pool)`; `InMemoryDispatchStore(job_store=None)` with `add_mailbox`, `add_thread`, `add_message`, `add_draft`, `set_category`, and inspectable `drafts`, `threads`, `outbound`.
  - `packages.dispatch.service`: `class DispatchOutcome(StrEnum)` (`COMPLETED_DRAFT="completed_draft"`, `COMPLETED_SENT="completed_sent"`, `ALREADY_DONE="already_done"`, `NOT_DISPATCHABLE="not_dispatchable"`); `class DispatchPermanentError(Exception)`; `class DispatchJobBusyError(Exception)`; `AdapterResolver = Callable[[Mailbox], MailProviderAdapter]`; `class DispatchService(*, store: DispatchStore, adapter_for: AdapterResolver = get_adapter_for_mailbox, registry: TaxonomyRegistry | None = None, dispatch_queue: str = "email.dispatch", confirm_recheck_delay_s: float = 2.0)` with `async def dispatch(self, *, organization_id: UUID, job_id: UUID) -> DispatchOutcome` (holds `job_lock` for all five steps) and `async def mark_dispatch_route(self, *, organization_id: UUID, job_id: UUID) -> None`; pure helpers `outbound_message(ctx, draft, reply, sent) -> NormalizedMessage`, `bare_message_id(value: str | None) -> str | None`, `message_id_domain(address: str) -> str`.

How `dispatch()` maps onto design §5.8 (each line is a code path below):

```
job_lock(org, job) busy (another delivery of this job runs) ──▶ DispatchJobBusyError (deferred, no attempt used)
load ctx (org-scoped) ── none ─────────────────────────────────▶ DispatchPermanentError (dead-letter)
job COMPLETED ─────────────────────────────────────────────────▶ ALREADY_DONE (ack, send nothing)
draft not approved and category not auto_send_eligible ─────────▶ NOT_DISPATCHABLE (ack, R16.8/R17.6)
1 claim  key → draft, queue_name='email.dispatch', DRAFTED|RETRY_PENDING→DISPATCHED (one txn)
         COMPLETED ⇒ ALREADY_DONE · DISPATCHED ⇒ resume · other states ⇒ NOT_DISPATCHABLE
  null/blank provider thread id ───────────────────────────────▶ MissingProviderThreadError (dead-letter)
2 draft  provider_draft_id stored? reuse
         : job was not DRAFTED (redelivery / replay)? find_draft(our Message-ID) ⇒ adopt + record
         : create_draft + record (created_here)
  create_draft mode ──▶ 5 finish(outbound=None) ─────────────────▶ COMPLETED_DRAFT
3 send   created_here ──▶ send_draft
4 confirm any other handle ──▶ get_draft_status: DRAFT (re-check once after delay) ⇒ send
                                             SENT ⇒ 5 · MISSING ⇒ find_sent_message(thread, our Message-ID)
                                             not found ──────────▶ DispatchPermanentError (dead-letter)
5 finish one txn: draft dispatched + provider_ref, outbound email_message, thread touched,
         DISPATCHED→COMPLETED ──────────────────────────────────▶ COMPLETED_SENT
adapter RateLimited/Transient propagate (job stays DISPATCHED); Permanent/NotFound/AuthExpired propagate
(all of the above runs inside job_lock; the lock is released when dispatch() returns or raises)
```

- [ ] **Step 1: Write the shared dispatch test doubles**

Create `tests/stubs/dispatch_fakes.py`:

```python
"""Dispatch test doubles (tasks 6.5–6.7): a call-counting fake provider with Gmail send
semantics and crash points, and an in-memory world for DispatchService tests.

``RecordingFake`` subclasses the contract-tested ``FakeProviderAdapter`` and keeps its own
record of sends, so these tests depend only on the fake's ``create_draft``:
- ``send_draft`` removes the draft (Gmail deletes a sent draft), so ``get_draft_status``
  then reports MISSING and ``find_sent_message`` finds the sent copy by our Message-ID;
- ``fail_send`` = "before" | "after" raises one Transient before or after the provider
  accepted the send (the ambiguous failure of design §5.8 step 4);
- ``crash_at`` in ``CRASH_POINTS`` parks the calling worker forever at that point, which is
  how the forced-redelivery tests kill a worker mid-step;
- ``pause_at`` in ``CRASH_POINTS`` holds the first caller there until ``resume`` is set, so a
  test can run a second delivery of the same job while the first is mid-dispatch;
- ``find_draft`` finds an unsent draft by our Message-ID (the orphan-draft lookup of 6.5).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.adapters.exceptions import NotFound, Transient
from packages.adapters.fake import FakeProviderAdapter
from packages.db.dispatch import InMemoryDispatchStore
from packages.dispatch.service import DispatchService
from packages.domain import DispatchMode, ProviderDraftStatus
from packages.domain.entities import (
    DraftRef,
    EmailAddress,
    EmailThread,
    GeneratedDraft,
    Job,
    Mailbox,
    NormalizedMessage,
    OutboundReply,
    SentRef,
)
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry

CRASH_POINTS = (
    "before_create_draft",
    "after_create_draft",  # the provider holds the draft; its id is not recorded yet
    "before_send_draft",
    "after_send_draft",
)


class RecordingFake(FakeProviderAdapter):
    """FakeProviderAdapter with call counts, Gmail send semantics and crash points."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter[str] = Counter()
        self.replies: dict[str, OutboundReply] = {}
        self.sent: dict[str, SentRef] = {}
        self.find_queries: list[str] = []
        self.fail_send: str | None = None
        self.status_override: ProviderDraftStatus | None = None
        self.hide_sent = False
        self.refs: dict[str, DraftRef] = {}
        self.crash_at: str | None = None
        self.crash_reached = asyncio.Event()
        self._hang = asyncio.Event()
        self.pause_at: str | None = None
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()

    async def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None  # only the first worker to get here is killed
            self.crash_reached.set()
            await self._hang.wait()  # never set: the worker is gone
        if self.pause_at == point:
            self.pause_at = None  # only the first caller is held
            self.paused.set()
            await self.resume.wait()

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        await self._maybe_crash("before_create_draft")
        ref = await super().create_draft(mailbox, reply)
        self.calls["create_draft"] += 1
        self.replies[ref.provider_draft_id] = reply
        self.refs[ref.provider_draft_id] = ref
        await self._maybe_crash("after_create_draft")
        return ref

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        await self._maybe_crash("before_send_draft")
        self._maybe_raise_fault("send_draft", mailbox)
        if self.fail_send == "before":
            self.fail_send = None
            raise Transient(
                "connection reset before the send", provider="fake", mailbox_id=str(mailbox.id)
            )
        if provider_draft_id not in self.replies or provider_draft_id in self.sent:
            raise NotFound(
                f"Draft '{provider_draft_id}' not found", provider="fake", mailbox_id=str(mailbox.id)
            )
        self.calls["send_draft"] += 1
        ref = SentRef(
            provider_message_id=f"sent-rec-{self.calls['send_draft']}",
            sent_at=datetime.now(UTC),
        )
        self.sent[provider_draft_id] = ref
        if self.fail_send == "after":
            self.fail_send = None
            raise Transient(
                "timeout after the provider accepted the send",
                provider="fake",
                mailbox_id=str(mailbox.id),
            )
        await self._maybe_crash("after_send_draft")
        return ref

    async def get_draft_status(self, mailbox: Mailbox, provider_draft_id: str) -> ProviderDraftStatus:
        self._maybe_raise_fault("get_draft_status", mailbox)
        self.calls["get_draft_status"] += 1
        if self.status_override is not None:
            return self.status_override
        if provider_draft_id in self.sent or provider_draft_id not in self.replies:
            return ProviderDraftStatus.MISSING
        return ProviderDraftStatus.DRAFT

    async def find_sent_message(
        self, mailbox: Mailbox, provider_thread_id: str, provider_message_id: str
    ) -> SentRef | None:
        self._maybe_raise_fault("find_sent_message", mailbox)
        self.calls["find_sent_message"] += 1
        self.find_queries.append(provider_message_id)
        if self.hide_sent:
            return None
        for draft_id, ref in self.sent.items():
            if self.replies[draft_id].message_id == provider_message_id:
                return ref
        return None

    async def find_draft(
        self, mailbox: Mailbox, provider_thread_id: str, message_id: str
    ) -> DraftRef | None:
        self._maybe_raise_fault("find_draft", mailbox)
        self.calls["find_draft"] += 1
        for draft_id, reply in self.replies.items():
            if draft_id in self.sent or str(reply.thread_id) != provider_thread_id:
                continue
            if reply.message_id == message_id:
                return self.refs[draft_id]
        return None

    def delete_draft(self, provider_draft_id: str) -> None:
        """A person deletes the draft in the mailbox before it is sent."""
        super().delete_draft(provider_draft_id)
        self.replies.pop(provider_draft_id, None)


def registry_with(
    mode: DispatchMode, *, auto_send: bool = False, category: str = "billing"
) -> TaxonomyRegistry:
    """A taxonomy whose ``category`` uses ``mode`` (every other category keeps its default)."""
    registry = TaxonomyRegistry()
    definition = registry.get(category)
    assert definition is not None
    registry.register_category(
        replace(definition, dispatch_mode=mode, auto_send_eligible=auto_send)
    )
    return registry


@dataclass
class DispatchWorld:
    """One approved billing draft ready for dispatch, on in-memory stores."""

    store: InMemoryDispatchStore
    fake: RecordingFake
    service: DispatchService
    org_id: UUID
    job_id: UUID
    draft_id: UUID
    mailbox: Mailbox
    thread: EmailThread
    original: NormalizedMessage
    registry: TaxonomyRegistry


async def build_dispatch_world(
    *,
    mode: DispatchMode = DispatchMode.CREATE_DRAFT,
    draft_status: str = "approved",
    job_state: JobState = JobState.DRAFTED,
    provider_thread_id: str | None = "th-001",
    auto_send: bool = False,
    recheck_delay_s: float = 0.0,
    provider_draft_id: str | None = None,
) -> DispatchWorld:
    """Seed mailbox, thread, inbound email (category billing), job and draft."""
    org_id, mailbox_id, thread_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    mailbox = Mailbox(
        id=mailbox_id,
        organization_id=org_id,
        provider="fake",
        address="support@acme.example",
        display_name="Acme Support",
    )
    thread = EmailThread(
        id=thread_id,
        organization_id=org_id,
        mailbox_id=mailbox_id,
        subject_normalized="where is order 82915",
        provider_thread_id=provider_thread_id,
        participants=["alice@customer.example", "support@acme.example"],
    )
    original = NormalizedMessage(
        message_id=uuid4(),
        thread_id=thread_id,
        mailbox_id=mailbox_id,
        organization_id=org_id,
        provider="fake",
        provider_message_id="prov-msg-001",
        sender=EmailAddress("alice@customer.example", "Alice"),
        received_at=datetime(2026, 9, 28, 8, 0, tzinfo=UTC),
        rfc822_message_id="orig-1@customer.example",
        references_ids=["root-0@customer.example"],
        recipients=[EmailAddress("support@acme.example")],
        subject="Where is order 82915?",
        body_text="What is the status of order 82915?",
    )
    store = InMemoryDispatchStore()
    store.add_mailbox(mailbox)
    store.add_thread(thread)
    store.add_message(original)
    store.set_category(original.message_id, "billing")
    await store.jobs.create_job(
        Job(
            id=job_id,
            organization_id=org_id,
            message_id=original.message_id,
            thread_id=thread_id,
            state=job_state.value,
            idempotency_key=f"gen-{uuid4()}",
        )
    )
    draft = GeneratedDraft(
        organization_id=org_id,
        message_id=original.message_id,
        thread_id=thread_id,
        job_id=job_id,
        subject="Re: Where is order 82915?",
        body="Order ORD-82915 was dispatched on 24 September.",
        status=draft_status,
        provider_draft_id=provider_draft_id,
        provider_draft_message_id=f"{provider_draft_id}-msg" if provider_draft_id else None,
    )
    store.add_draft(draft)
    fake = RecordingFake()
    registry = registry_with(mode, auto_send=auto_send)
    service = DispatchService(
        store=store,
        adapter_for=lambda _mailbox: fake,
        registry=registry,
        confirm_recheck_delay_s=recheck_delay_s,
    )
    return DispatchWorld(
        store=store,
        fake=fake,
        service=service,
        org_id=org_id,
        job_id=job_id,
        draft_id=draft.id,
        mailbox=mailbox,
        thread=thread,
        original=original,
        registry=registry,
    )
```

- [ ] **Step 2: Write the failing DispatchService unit tests**

Create `tests/unit/test_dispatch_service.py`:

```python
"""DispatchService: design §5.8's five steps on the fake adapter (tasks 6.5, 6.6, 6.7).

Requirements: R17.1 (modes), R17.3 (idempotency key), R17.4 (provider ref, COMPLETED),
R17.5 (transient vs permanent), R17.6 (approval before send), R17.7 (outbound write-back),
R19.2 (key derivation), R19.3 (exactly one provider send under redelivery).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from uuid import uuid4

import pytest

from packages.adapters.exceptions import AuthExpired, RateLimited, Transient
from packages.core.idempotency import derive_idempotency_key
from packages.db.dispatch import DispatchKeyConflictError
from packages.dispatch.reply import MissingProviderThreadError
from packages.dispatch.service import (
    DispatchJobBusyError,
    DispatchOutcome,
    DispatchPermanentError,
    bare_message_id,
    message_id_domain,
)
from packages.domain import DispatchMode, ProviderDraftStatus
from packages.domain.entities import GeneratedDraft
from packages.domain.state_machine import JobState
from tests.stubs.dispatch_fakes import DispatchWorld, build_dispatch_world

SEND = DispatchMode.SEND_REPLY


async def _dispatch(w: DispatchWorld) -> DispatchOutcome:
    return await w.service.dispatch(organization_id=w.org_id, job_id=w.job_id)


async def _state(w: DispatchWorld) -> str:
    job = await w.store.jobs.get_job(w.org_id, w.job_id)
    assert job is not None
    return job.state


async def _states(w: DispatchWorld) -> list[str]:
    return [e.state_to for e in await w.store.jobs.list_events_for_job(w.org_id, w.job_id)]


def _key(w: DispatchWorld) -> str:
    return derive_idempotency_key(
        organization_id=w.org_id,
        mailbox_id=w.mailbox.id,
        provider_message_id=w.original.provider_message_id,
        operation_type="dispatch",
    )


async def test_create_draft_mode_stops_after_the_provider_draft() -> None:
    """R17.1 / 6.5: default mode creates one provider draft and records no outbound message."""
    w = await build_dispatch_world()
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT

    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 0
    assert w.fake.calls["find_draft"] == 0  # a fresh DRAFTED claim has no orphan to look for
    assert await _state(w) == JobState.COMPLETED.value
    assert (await _states(w))[-2:] == [JobState.DISPATCHED.value, JobState.COMPLETED.value]
    job = await w.store.jobs.get_job(w.org_id, w.job_id)
    assert job is not None and job.queue_name == "email.dispatch"
    draft = w.store.drafts[w.draft_id]
    assert draft.status == "dispatched"
    assert draft.provider_draft_id
    assert draft.provider_ref == draft.provider_draft_id
    assert draft.dispatch_idempotency_key == _key(w)
    assert w.store.outbound == []


async def test_send_reply_mode_sends_once_and_writes_back_the_outbound_message() -> None:
    """R17.4 / R17.7: one send, provider id persisted, outbound row and thread updated."""
    w = await build_dispatch_world(mode=SEND)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT

    assert w.fake.calls["send_draft"] == 1
    [sent] = list(w.fake.sent.values())
    [reply] = list(w.fake.replies.values())
    [outbound] = w.store.outbound
    assert outbound.direction == "outbound"
    assert outbound.provider_message_id == sent.provider_message_id
    assert outbound.rfc822_message_id == bare_message_id(reply.message_id)
    assert outbound.rfc822_message_id and "<" not in outbound.rfc822_message_id
    assert outbound.in_reply_to == "orig-1@customer.example"
    assert outbound.thread_id == w.thread.id
    assert outbound.sender.email == "support@acme.example"
    assert w.store.threads[w.thread.id].message_count == w.thread.message_count + 1
    draft = w.store.drafts[w.draft_id]
    assert draft.status == "dispatched"
    assert draft.provider_ref == sent.provider_message_id
    assert await _state(w) == JobState.COMPLETED.value


async def test_redelivery_after_completion_sends_nothing() -> None:
    """R19.3: a replayed dispatch job after COMPLETED is acknowledged with no provider call."""
    w = await build_dispatch_world(mode=SEND)
    await _dispatch(w)
    calls_before = dict(w.fake.calls)
    assert await _dispatch(w) is DispatchOutcome.ALREADY_DONE
    assert dict(w.fake.calls) == calls_before
    assert len(w.store.outbound) == 1


async def test_unapproved_draft_is_never_dispatched() -> None:
    """R16.8 / R17.6: without approval (and no auto-send) nothing is claimed or sent."""
    w = await build_dispatch_world(mode=SEND, draft_status="draft")
    assert await _dispatch(w) is DispatchOutcome.NOT_DISPATCHABLE
    assert await _state(w) == JobState.DRAFTED.value
    assert sum(w.fake.calls.values()) == 0


async def test_auto_send_category_dispatches_without_approval() -> None:
    """R17.6: auto_send_eligible opts a category out of the approval requirement."""
    w = await build_dispatch_world(mode=SEND, draft_status="draft", auto_send=True)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["send_draft"] == 1


async def test_rate_limit_before_the_draft_leaves_the_job_dispatched() -> None:
    """R17.5: a 429 propagates with its Retry-After; the retry then completes."""
    w = await build_dispatch_world()
    w.fake.inject_rate_limit(retry_after=120.0)
    with pytest.raises(RateLimited) as exc_info:
        await _dispatch(w)
    assert exc_info.value.retry_after == 120.0
    assert await _state(w) == JobState.DISPATCHED.value
    assert w.store.drafts[w.draft_id].provider_draft_id is None

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    assert w.fake.calls["create_draft"] == 1


async def test_ambiguous_send_failure_is_confirmed_not_resent() -> None:
    """Step 4: the provider accepted the send, the call failed; the retry confirms instead."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.fail_send = "after"
    with pytest.raises(Transient):
        await _dispatch(w)
    assert w.fake.calls["send_draft"] == 1
    assert await _state(w) == JobState.DISPATCHED.value

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["send_draft"] == 1
    assert w.fake.calls["get_draft_status"] == 1
    [reply] = list(w.fake.replies.values())
    draft = w.store.drafts[w.draft_id]
    # Draft message id first (Graph keeps it), then our Message-ID (Gmail's new id).
    assert w.fake.find_queries == [draft.provider_draft_message_id, reply.message_id]
    [sent] = list(w.fake.sent.values())
    assert [m.provider_message_id for m in w.store.outbound] == [sent.provider_message_id]


async def test_send_that_never_happened_is_sent_on_retry_after_one_recheck() -> None:
    """Step 4: DRAFT on resume is re-checked once after the delay, then sent exactly once."""
    w = await build_dispatch_world(mode=SEND, recheck_delay_s=0.01)
    w.fake.fail_send = "before"
    with pytest.raises(Transient):
        await _dispatch(w)
    assert w.fake.calls["send_draft"] == 0

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["get_draft_status"] == 2
    assert w.fake.calls["send_draft"] == 1
    assert w.fake.calls["create_draft"] == 1


async def test_missing_draft_without_a_sent_copy_is_permanent() -> None:
    """Step 4: MISSING and no sent message in the thread means a person deleted the draft."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.fail_send = "after"
    with pytest.raises(Transient):
        await _dispatch(w)
    w.fake.hide_sent = True
    with pytest.raises(DispatchPermanentError):
        await _dispatch(w)
    assert w.fake.calls["send_draft"] == 1
    assert await _state(w) == JobState.DISPATCHED.value  # the consumer dead-letters it
    assert w.store.outbound == []


async def test_provider_reported_sent_finishes_without_sending() -> None:
    """Step 4 (Graph semantics): SENT goes straight to finish with the draft's message id."""
    w = await build_dispatch_world(mode=SEND, job_state=JobState.DISPATCHED, provider_draft_id="d-1")
    w.fake.status_override = ProviderDraftStatus.SENT
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["send_draft"] == 0
    assert w.fake.calls["find_sent_message"] == 0
    assert [m.provider_message_id for m in w.store.outbound] == ["d-1-msg"]


async def test_existing_provider_draft_is_reused_not_recreated() -> None:
    """Step 2: a crash after the draft was recorded resumes without a second provider draft."""
    w = await build_dispatch_world(job_state=JobState.DISPATCHED, provider_draft_id="d-1")
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    assert w.fake.calls["create_draft"] == 0
    assert w.store.drafts[w.draft_id].provider_ref == "d-1"


async def test_null_provider_thread_id_fails_after_the_claim() -> None:
    """6.3 / 6.6: an IMAP-style thread without a provider id is permanent; nothing is sent."""
    w = await build_dispatch_world(mode=SEND, provider_thread_id=None)
    with pytest.raises(MissingProviderThreadError):
        await _dispatch(w)
    assert await _state(w) == JobState.DISPATCHED.value
    assert sum(w.fake.calls.values()) == 0


async def test_operator_replay_resumes_from_retry_pending() -> None:
    """6.5: RETRY_PENDING -> DISPATCHED (operator replay) without regenerating."""
    w = await build_dispatch_world(job_state=JobState.RETRY_PENDING)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    states = await _states(w)
    assert states[-2:] == [JobState.DISPATCHED.value, JobState.COMPLETED.value]
    assert JobState.GENERATING.value not in states


async def test_key_held_by_another_draft_is_a_conflict() -> None:
    """R19.2: the UNIQUE dispatch key cannot be claimed twice; the job is not moved."""
    w = await build_dispatch_world()
    w.store.add_draft(
        GeneratedDraft(
            organization_id=w.org_id,
            message_id=w.original.message_id,
            thread_id=w.thread.id,
            job_id=uuid4(),
            body="another draft for the same email",
            status="approved",
            dispatch_idempotency_key=_key(w),
        )
    )
    with pytest.raises(DispatchKeyConflictError):
        await _dispatch(w)
    assert await _state(w) == JobState.DRAFTED.value


async def test_token_expiry_mid_dispatch_is_permanent_and_replay_reuses_the_draft() -> None:
    """Review focus 3: a 401 between the provider draft and the send dead-letters; after the
    owner refreshes the token, the operator replay sends once with the recorded draft."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.inject_auth_expired(method="send_draft")
    with pytest.raises(AuthExpired):
        await _dispatch(w)
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 0
    assert await _state(w) == JobState.DISPATCHED.value  # the consumer dead-letters it
    assert w.store.drafts[w.draft_id].provider_draft_id is not None
    assert w.store.outbound == []

    # What the consumer and the operator do: DISPATCHED -> FAILED -> DEAD_LETTER -> RETRY_PENDING.
    for target in (JobState.FAILED, JobState.DEAD_LETTER, JobState.RETRY_PENDING):
        await w.store.jobs.transition_job_state(w.org_id, w.job_id, target)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 1
    assert len(w.store.outbound) == 1


async def test_provider_draft_deleted_before_the_send_is_not_recreated() -> None:
    """Review focus 4: a person deletes the provider draft after it was recorded and before
    the send; the resumed dispatch finds neither the draft nor a sent copy and dead-letters."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.inject_transient_failure(method="send_draft")
    with pytest.raises(Transient):
        await _dispatch(w)
    [provider_draft_id] = list(w.fake.replies)
    w.fake.delete_draft(provider_draft_id)

    with pytest.raises(DispatchPermanentError):
        await _dispatch(w)
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 0
    assert await _state(w) == JobState.DISPATCHED.value  # the consumer dead-letters it
    assert w.store.outbound == []


async def test_dispatch_mode_is_read_when_the_dispatch_runs() -> None:
    """Review focus 5: the category switched to send_reply after approve; the dispatch uses
    the mode in force when it runs and sends exactly once."""
    w = await build_dispatch_world(mode=DispatchMode.CREATE_DRAFT)
    definition = w.registry.get("billing")
    assert definition is not None
    w.registry.register_category(replace(definition, dispatch_mode=SEND))

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 1


async def test_mode_switch_after_a_completed_draft_does_not_send() -> None:
    """Review focus 5: a job completed in create_draft mode is not sent when the category
    later switches to send_reply and the dispatch job is redelivered."""
    w = await build_dispatch_world()
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    definition = w.registry.get("billing")
    assert definition is not None
    w.registry.register_category(replace(definition, dispatch_mode=SEND))

    assert await _dispatch(w) is DispatchOutcome.ALREADY_DONE
    assert w.fake.calls["send_draft"] == 0
    assert w.store.outbound == []


async def test_second_delivery_of_the_same_job_is_refused_while_the_first_runs() -> None:
    """R19.3: two deliveries of one job never run the steps together (a repeated approve
    re-publishes while DISPATCHED; the consumer runs several deliveries at once)."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.pause_at = "before_send_draft"
    first = asyncio.create_task(_dispatch(w))
    await asyncio.wait_for(w.fake.paused.wait(), timeout=5)

    with pytest.raises(DispatchJobBusyError):
        await _dispatch(w)
    assert w.fake.calls["create_draft"] == 1

    w.fake.resume.set()
    assert await first is DispatchOutcome.COMPLETED_SENT
    assert await _dispatch(w) is DispatchOutcome.ALREADY_DONE
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 1
    assert len(w.store.outbound) == 1


@pytest.mark.parametrize("mode", [DispatchMode.CREATE_DRAFT, SEND])
async def test_crash_after_create_draft_adopts_the_unrecorded_provider_draft(
    mode: DispatchMode,
) -> None:
    """tasks.md 6.5: the provider accepted create_draft, the worker died before recording the
    id; the redelivery finds that draft by our Message-ID and never creates a second one."""
    w = await build_dispatch_world(mode=mode)
    w.fake.crash_at = "after_create_draft"
    crashed = asyncio.create_task(_dispatch(w))
    await asyncio.wait_for(w.fake.crash_reached.wait(), timeout=5)
    crashed.cancel()  # the worker dies; its per-job lock goes with it
    with contextlib.suppress(asyncio.CancelledError):
        await crashed
    assert w.store.drafts[w.draft_id].provider_draft_id is None
    assert await _state(w) == JobState.DISPATCHED.value

    expected = DispatchOutcome.COMPLETED_SENT if mode is SEND else DispatchOutcome.COMPLETED_DRAFT
    assert await _dispatch(w) is expected
    [orphan] = list(w.fake.refs)
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["find_draft"] == 1
    assert w.store.drafts[w.draft_id].provider_draft_id == orphan
    if mode is SEND:
        # Not created by this delivery, so step 4 confirms before the one send.
        assert w.fake.calls["get_draft_status"] == 1
        assert w.fake.calls["send_draft"] == 1
        assert len(w.store.outbound) == 1


def test_message_id_helpers() -> None:
    assert message_id_domain("support@Acme.Example") == "acme.example"
    assert bare_message_id("<abc@acme.example>") == "abc@acme.example"
    assert bare_message_id("  ") is None
    assert bare_message_id(None) is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_dispatch_service.py -v`
Expected: FAIL — collection error `ModuleNotFoundError: No module named 'packages.db.dispatch'` (raised while importing `tests.stubs.dispatch_fakes`).

- [ ] **Step 4: Add connection-scoped message insert and thread update helpers**

Step 5 must insert the outbound row and update the thread inside the finish transaction, but `PostgresMessageStore.insert_message` and `PostgresThreadStore.update_thread_on_message` each open their own connection. Extract their SQL so both paths share it.

In `packages/db/message.py`, add after `_clean_pg_str` (before `class MessageInsertResult`):

```python
_INSERT_MESSAGE_SQL = """
    INSERT INTO email_message (
        id,
        organization_id,
        mailbox_id,
        thread_id,
        provider_message_id,
        rfc822_message_id,
        in_reply_to,
        references_ids,
        direction,
        sender_email,
        sender_name,
        recipients,
        cc,
        subject,
        subject_normalized,
        body_text,
        body_text_clean,
        snippet,
        raw_object_key,
        html_object_key,
        received_at,
        has_attachments,
        normalization_failed,
        search_tsv
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11, $12::jsonb, $13::jsonb, $14, $15, $16, $17, $18, $19, $20,
        $21, $22, $23,
        setweight(to_tsvector('english', coalesce($14, '')), 'A')
        || setweight(to_tsvector('english', coalesce($17, '')), 'B')
    )
    ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING
    RETURNING id, thread_id;
"""


def _recipients_json(recipients: Sequence[EmailAddress]) -> str:
    return json.dumps([{"name": r.name or "", "email": r.email} for r in recipients])


def _message_insert_args(message: NormalizedMessage, *, has_attachments: bool) -> tuple[Any, ...]:
    """Positional parameters of ``_INSERT_MESSAGE_SQL`` for ``message``."""
    return (
        _to_uuid(message.message_id),
        _to_uuid(message.organization_id),
        _to_uuid(message.mailbox_id),
        _to_uuid(message.thread_id),
        message.provider_message_id,
        message.rfc822_message_id,
        message.in_reply_to,
        list(message.references_ids) if message.references_ids else [],
        message.direction,
        message.sender.email if message.sender else None,
        message.sender.name if message.sender else None,
        _recipients_json(message.recipients),
        _recipients_json(message.cc),
        _clean_pg_str(message.subject),
        _clean_pg_str(message.subject_normalized),
        _clean_pg_str(message.body_text),
        _clean_pg_str(message.body_text_clean),
        _clean_pg_str(message.snippet),
        message.raw_object_key,
        message.html_object_key,
        message.received_at,
        has_attachments,
        message.normalization_failed,
    )


async def insert_message_on(conn: Any, message: NormalizedMessage) -> bool:
    """Insert ``message`` (no attachments) on a caller-owned connection and transaction.

    Returns False when ``(organization_id, mailbox_id, provider_message_id)`` already exists
    (R4.8). Dispatch step 5 uses it so the outbound row commits together with
    ``DISPATCHED -> COMPLETED`` (R17.7, R18.5).
    """
    row = await conn.fetchrow(_INSERT_MESSAGE_SQL, *_message_insert_args(message, has_attachments=False))
    return row is not None
```

Replace the body of `PostgresMessageStore._serialize_recipients` (line 290) with `return _recipients_json(recipients)`.

In `PostgresMessageStore.insert_message`, delete the three now-unused locals (lines 370–372: `recipients_json`, `cc_json`, `references_list`), delete the inline `query = """ ... """` string (lines 375–410), and replace the `row = await conn.fetchrow(query, msg_u, ..., message.normalization_failed,)` call inside the transaction (lines 413–438) with:

```python
            row = await conn.fetchrow(
                _INSERT_MESSAGE_SQL, *_message_insert_args(message, has_attachments=has_atts)
            )
```

Everything after it (duplicate result, attachment insert, `MessageInsertResult`) is unchanged; `org_u`, `mbx_u`, `thd_u`, `msg_u` are still used there.

In `packages/db/thread.py`, change the imports (lines 10–13) to:

```python
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID
```

add after `_to_uuid` (before `class ThreadStore`):

```python
_TOUCH_THREAD_SQL = """
    UPDATE email_thread
    SET
        message_count = message_count + 1,
        first_message_at = LEAST(first_message_at, $3),
        last_message_at = GREATEST(last_message_at, $3),
        participants = (
            SELECT array_agg(DISTINCT p)
            FROM unnest(participants || $4::text[]) AS p
        ),
        provider_thread_id = COALESCE(email_thread.provider_thread_id, $5)
    WHERE id = $1 AND organization_id = $2
    RETURNING id, organization_id, mailbox_id, provider_thread_id,
              subject_normalized, participants, first_message_at, last_message_at,
              message_count, status;
"""


async def touch_thread_on(
    conn: Any,
    *,
    thread_id: UUID,
    organization_id: UUID,
    message_time: datetime,
    participants: Sequence[str],
) -> bool:
    """Count one more message on the thread, on a caller-owned connection (R17.7).

    Returns False when the thread does not exist in the organization.
    """
    clean = [p.strip().lower() for p in participants if p.strip()]
    row = await conn.fetchrow(
        _TOUCH_THREAD_SQL, thread_id, organization_id, message_time, clean, None
    )
    return row is not None
```

and in `PostgresThreadStore.update_thread_on_message` replace the inline `query = """ UPDATE email_thread ... """` (lines 474–489) by passing `_TOUCH_THREAD_SQL` to `conn.fetchrow` in place of `query` (the parameters are unchanged: `thread_u, org_u, message_time, clean_parts, provider_thread_id`).

Run: `uv run pytest tests/unit/test_message_persistence.py tests/integration/test_database_schema.py tests/integration/test_job_timeline_and_replay_integration.py -q`
Expected: PASS (pure refactor; message and thread writes behave as before).

- [ ] **Step 5: Implement the dispatch store**

Create `packages/db/dispatch.py`:

```python
"""Dispatch unit of work: claim, provider-draft handle and finish (design.md §5.8, ADR-0009).

Requirements:
- R17.3 / R19.2: the dispatch key ``key(org, mailbox, original provider_message_id,
  "dispatch")`` is claimed into ``generated_draft.dispatch_idempotency_key`` (UNIQUE) in the
  same transaction as ``DRAFTED | RETRY_PENDING -> DISPATCHED``.
- R17.4 / R17.7 / R18.5: finish writes the provider ref, draft ``dispatched``, the outbound
  ``email_message`` (send_reply mode only), the thread update and ``DISPATCHED -> COMPLETED``
  in one transaction.
- R18.7: the claim records ``queue_name = email.dispatch`` so an operator replay of a
  dead-lettered dispatch is routed back to the dispatch-worker, not to generation;
  ``set_dispatch_queue`` does the same for a failure before or during the claim.
- R19.3: ``job_lock`` serializes the deliveries of one job. Postgres holds a session-level
  advisory lock on a dedicated pool connection, so a crashed worker's session end
  releases it (and asyncpg's pool reset runs ``pg_advisory_unlock_all()`` on release).

Every statement is scoped by ``organization_id``. Rows are locked job first, then draft
(the same order as the review store), so approve and dispatch never deadlock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.db.draft import _row_to_draft, fetch_draft_for_job
from packages.db.job import JOB_SELECT_COLUMNS, InMemoryJobStore, PostgresJobStore
from packages.db.message import PostgresMessageStore, insert_message_on
from packages.db.thread import PostgresThreadStore, touch_thread_on
from packages.domain import DispatchMode
from packages.domain.entities import (
    EmailThread,
    GeneratedDraft,
    Job,
    Mailbox,
    NormalizedMessage,
)
from packages.domain.review import DraftStatus
from packages.domain.state_machine import JobState

_CLAIMABLE = frozenset({JobState.DRAFTED.value, JobState.RETRY_PENDING.value})
# Generation is done in these states, so a failed dispatch may take over the replay route.
_ROUTABLE_TO_DISPATCH = (
    JobState.DRAFTED.value,
    JobState.DISPATCHED.value,
    JobState.RETRY_PENDING.value,
    JobState.FAILED.value,
)


def _lock_key(organization_id: UUID, job_id: UUID) -> str:
    return f"dispatch:{organization_id}:{job_id}"


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class DispatchContext:
    """Everything one dispatch needs, loaded tenant-scoped."""

    job: Job
    draft: GeneratedDraft
    original: NormalizedMessage
    thread: EmailThread
    mailbox: Mailbox
    category: str | None


class ClaimStatus(StrEnum):
    """What step 1 found."""

    CLAIMED = "claimed"
    RESUMED = "resumed"
    ALREADY_COMPLETED = "already_completed"
    NOT_DISPATCHABLE = "not_dispatchable"


@dataclass(frozen=True)
class ClaimOutcome:
    """Step 1 result: the job and draft as they are after the claim transaction."""

    status: ClaimStatus
    job: Job
    draft: GeneratedDraft


class DispatchKeyConflictError(Exception):
    """The dispatch key is held by another draft, or this draft holds a different key."""


def _finish_payloads(
    draft_id: UUID, mode: DispatchMode, provider_ref: str, outbound_id: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    event = {
        "step": "finish",
        "dispatch_mode": mode.value,
        "provider_ref": provider_ref,
        "outbound_message_id": outbound_id,
    }
    result = {"draft_id": str(draft_id), "dispatch_mode": mode.value, "provider_ref": provider_ref}
    return event, result


def _claim_payload(draft_id: UUID, key: str, from_state: str) -> dict[str, Any]:
    return {
        "step": "claim",
        "draft_id": str(draft_id),
        "dispatch_idempotency_key": key,
        "operator_replay": from_state == JobState.RETRY_PENDING.value,
    }


@runtime_checkable
class DispatchStore(Protocol):
    """Persistence side of DispatchService; every method is tenant-scoped."""

    def job_lock(self, organization_id: UUID, job_id: UUID) -> AbstractAsyncContextManager[bool]:
        """Hold the per-job dispatch lock; yields False (without waiting) when it is taken."""
        ...

    async def set_dispatch_queue(
        self, *, organization_id: UUID, job_id: UUID, queue_name: str
    ) -> None:
        """Route an operator replay of this job to the dispatch-worker (R18.7)."""
        ...

    async def load(self, organization_id: UUID, job_id: UUID) -> DispatchContext | None:
        """Job, its draft, the original email, thread, mailbox and latest category."""
        ...

    async def claim(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        idempotency_key: str,
        queue_name: str,
    ) -> ClaimOutcome:
        """Step 1 in one transaction; raises DispatchKeyConflictError on a key clash."""
        ...

    async def record_provider_draft(
        self,
        *,
        organization_id: UUID,
        draft_id: UUID,
        provider_draft_id: str,
        provider_draft_message_id: str | None,
    ) -> GeneratedDraft:
        """Step 2: store the provider draft handle; the first stored handle wins."""
        ...

    async def finish(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        """Step 5 in one transaction; a COMPLETED job is returned unchanged."""
        ...


class PostgresDispatchStore:
    """PostgreSQL dispatch unit of work (asyncpg)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._jobs = PostgresJobStore(pool)
        self._messages = PostgresMessageStore(pool)
        self._threads = PostgresThreadStore(pool)

    @asynccontextmanager
    async def job_lock(self, organization_id: UUID, job_id: UUID) -> AsyncIterator[bool]:
        """Session-level ``pg_try_advisory_lock`` held on its own connection for the whole
        dispatch. Never waits: a busy lock yields False. A worker that dies ends its
        session, and PostgreSQL releases the lock with it."""
        key = _lock_key(organization_id, job_id)
        async with self._pool.acquire() as conn:
            held = bool(
                await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key)
            )
            try:
                yield held
            finally:
                if held:
                    await conn.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)

    async def set_dispatch_queue(
        self, *, organization_id: UUID, job_id: UUID, queue_name: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE processing_job SET queue_name = $3"
                " WHERE id = $1 AND organization_id = $2 AND state = ANY($4::text[])",
                job_id,
                organization_id,
                queue_name,
                list(_ROUTABLE_TO_DISPATCH),
            )

    async def load(self, organization_id: UUID, job_id: UUID) -> DispatchContext | None:
        async with self._pool.acquire() as conn:
            job_row = await conn.fetchrow(
                f"SELECT {JOB_SELECT_COLUMNS} FROM processing_job"
                " WHERE id = $1 AND organization_id = $2",
                job_id,
                organization_id,
            )
            if job_row is None:
                return None
            draft = await fetch_draft_for_job(conn, job_id, organization_id)
            if draft is None:
                return None
            mailbox_row = await conn.fetchrow(
                """
                SELECT b.id, b.organization_id, b.provider, b.address, b.display_name,
                       b.status, b.credentials_ref
                FROM mailbox b
                JOIN email_message m
                  ON m.mailbox_id = b.id AND m.organization_id = b.organization_id
                WHERE m.id = $1 AND m.organization_id = $2
                """,
                _to_uuid(draft.message_id),
                organization_id,
            )
            category = await conn.fetchval(
                """
                SELECT category FROM classification_result
                WHERE message_id = $1 AND organization_id = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                _to_uuid(draft.message_id),
                organization_id,
            )
        original = await self._messages.get_message(organization_id, draft.message_id)
        thread = await self._threads.get_thread(organization_id, draft.thread_id)
        if mailbox_row is None or original is None or thread is None:
            return None
        return DispatchContext(
            job=PostgresJobStore._row_to_job(job_row),  # noqa: SLF001
            draft=draft,
            original=original,
            thread=thread,
            mailbox=Mailbox(
                id=mailbox_row["id"],
                organization_id=mailbox_row["organization_id"],
                provider=mailbox_row["provider"],
                address=mailbox_row["address"],
                display_name=mailbox_row["display_name"],
                status=mailbox_row["status"],
                credentials_ref=mailbox_row["credentials_ref"],
            ),
            category=category,
        )

    async def claim(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        idempotency_key: str,
        queue_name: str,
    ) -> ClaimOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            job = await self._lock_job(conn, organization_id, job_id)
            draft = await fetch_draft_for_job(conn, job_id, organization_id)
            if draft is None or draft.id != draft_id:
                raise KeyError(f"Draft {draft_id} is not the draft of job {job_id}")
            if job.state == JobState.COMPLETED.value:
                return ClaimOutcome(ClaimStatus.ALREADY_COMPLETED, job, draft)
            if job.state == JobState.DISPATCHED.value:
                return ClaimOutcome(ClaimStatus.RESUMED, job, draft)
            if job.state not in _CLAIMABLE:
                return ClaimOutcome(ClaimStatus.NOT_DISPATCHABLE, job, draft)
            try:
                row = await conn.fetchrow(
                    """
                    UPDATE generated_draft SET dispatch_idempotency_key = $3
                    WHERE id = $1 AND organization_id = $2
                      AND (dispatch_idempotency_key IS NULL OR dispatch_idempotency_key = $3)
                    RETURNING *
                    """,
                    draft_id,
                    organization_id,
                    idempotency_key,
                )
            except asyncpg.UniqueViolationError as err:
                raise DispatchKeyConflictError(
                    f"Dispatch key of draft {draft_id} is already held by another draft"
                ) from err
            if row is None:
                raise DispatchKeyConflictError(f"Draft {draft_id} holds a different dispatch key")
            await conn.execute(
                "UPDATE processing_job SET queue_name = $3, lease_expires_at = NULL"
                " WHERE id = $1 AND organization_id = $2",
                job_id,
                organization_id,
                queue_name,
            )
            claimed, _ = await self._jobs.transition_job_state_on(
                conn,
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.DISPATCHED,
                payload=_claim_payload(draft_id, idempotency_key, job.state),
            )
            return ClaimOutcome(ClaimStatus.CLAIMED, claimed, _row_to_draft(row))

    async def record_provider_draft(
        self,
        *,
        organization_id: UUID,
        draft_id: UUID,
        provider_draft_id: str,
        provider_draft_message_id: str | None,
    ) -> GeneratedDraft:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE generated_draft
                SET provider_draft_id = $3, provider_draft_message_id = $4
                WHERE id = $1 AND organization_id = $2 AND provider_draft_id IS NULL
                RETURNING *
                """,
                draft_id,
                organization_id,
                provider_draft_id,
                provider_draft_message_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM generated_draft WHERE id = $1 AND organization_id = $2",
                    draft_id,
                    organization_id,
                )
            if row is None:
                raise KeyError(f"Draft {draft_id} not found for organization {organization_id}")
            return _row_to_draft(row)

    async def finish(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        async with self._pool.acquire() as conn, conn.transaction():
            job = await self._lock_job(conn, organization_id, job_id)
            if job.state == JobState.COMPLETED.value:
                return job
            await conn.execute(
                "UPDATE generated_draft SET status = $3, provider_ref = $4"
                " WHERE id = $1 AND organization_id = $2",
                draft_id,
                organization_id,
                DraftStatus.DISPATCHED.value,
                provider_ref,
            )
            outbound_id: str | None = None
            if outbound is not None and await insert_message_on(conn, outbound):
                outbound_id = str(outbound.message_id)
                await touch_thread_on(
                    conn,
                    thread_id=_to_uuid(outbound.thread_id),
                    organization_id=organization_id,
                    message_time=outbound.received_at,
                    participants=[outbound.sender.email, *(r.email for r in outbound.recipients)],
                )
            event, result = _finish_payloads(draft_id, mode, provider_ref, outbound_id)
            done, _ = await self._jobs.transition_job_state_on(
                conn,
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.COMPLETED,
                payload=event,
                result_ref=result,
            )
            return done

    async def _lock_job(self, conn: Any, organization_id: UUID, job_id: UUID) -> Job:
        row = await conn.fetchrow(
            f"SELECT {JOB_SELECT_COLUMNS} FROM processing_job"
            " WHERE id = $1 AND organization_id = $2 FOR UPDATE",
            job_id,
            organization_id,
        )
        if row is None:
            raise KeyError(f"Job {job_id} not found for organization {organization_id}")
        return PostgresJobStore._row_to_job(row)  # noqa: SLF001


class InMemoryDispatchStore:
    """In-memory DispatchStore for unit tests; validates each transition before storing."""

    def __init__(self, job_store: InMemoryJobStore | None = None) -> None:
        self.jobs = job_store or InMemoryJobStore()
        self.drafts: dict[UUID, GeneratedDraft] = {}
        self.messages: dict[UUID, NormalizedMessage] = {}
        self.threads: dict[UUID, EmailThread] = {}
        self.mailboxes: dict[UUID, Mailbox] = {}
        self.categories: dict[UUID, str] = {}
        self.outbound: list[NormalizedMessage] = []
        self._lock = asyncio.Lock()
        self._job_locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}

    @asynccontextmanager
    async def job_lock(self, organization_id: UUID, job_id: UUID) -> AsyncIterator[bool]:
        lock = self._job_locks.setdefault((organization_id, job_id), asyncio.Lock())
        if lock.locked():
            yield False
            return
        async with lock:
            yield True

    async def set_dispatch_queue(
        self, *, organization_id: UUID, job_id: UUID, queue_name: str
    ) -> None:
        job = await self.jobs.get_job(organization_id, job_id)
        if job is not None and job.state in _ROUTABLE_TO_DISPATCH:
            job.queue_name = queue_name

    def add_mailbox(self, mailbox: Mailbox) -> None:
        self.mailboxes[_to_uuid(mailbox.id)] = mailbox

    def add_thread(self, thread: EmailThread) -> None:
        self.threads[thread.id] = thread

    def add_message(self, message: NormalizedMessage) -> None:
        self.messages[_to_uuid(message.message_id)] = message

    def add_draft(self, draft: GeneratedDraft) -> None:
        self.drafts[draft.id] = draft

    def set_category(self, message_id: UUID | str, category: str) -> None:
        self.categories[_to_uuid(message_id)] = category

    def _draft_for_job(self, organization_id: UUID, job_id: UUID) -> GeneratedDraft | None:
        mine = [
            d
            for d in self.drafts.values()
            if d.job_id is not None
            and str(d.job_id) == str(job_id)
            and _to_uuid(d.organization_id) == organization_id
        ]
        return max(mine, key=lambda d: d.created_at) if mine else None

    async def load(self, organization_id: UUID, job_id: UUID) -> DispatchContext | None:
        job = await self.jobs.get_job(organization_id, job_id)
        draft = self._draft_for_job(organization_id, job_id)
        if job is None or draft is None:
            return None
        original = self.messages.get(_to_uuid(draft.message_id))
        thread = self.threads.get(_to_uuid(draft.thread_id))
        if original is None or thread is None or thread.organization_id != organization_id:
            return None
        mailbox = self.mailboxes.get(_to_uuid(original.mailbox_id))
        if mailbox is None or _to_uuid(mailbox.organization_id) != organization_id:
            return None
        return DispatchContext(
            job=job,
            draft=draft,
            original=original,
            thread=thread,
            mailbox=mailbox,
            category=self.categories.get(_to_uuid(original.message_id)),
        )

    async def claim(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        idempotency_key: str,
        queue_name: str,
    ) -> ClaimOutcome:
        async with self._lock:
            job = await self.jobs.get_job(organization_id, job_id)
            draft = self._draft_for_job(organization_id, job_id)
            if job is None or draft is None or draft.id != draft_id:
                raise KeyError(f"Job {job_id} with draft {draft_id} not found")
            if job.state == JobState.COMPLETED.value:
                return ClaimOutcome(ClaimStatus.ALREADY_COMPLETED, job, draft)
            if job.state == JobState.DISPATCHED.value:
                return ClaimOutcome(ClaimStatus.RESUMED, job, draft)
            if job.state not in _CLAIMABLE:
                return ClaimOutcome(ClaimStatus.NOT_DISPATCHABLE, job, draft)
            if draft.dispatch_idempotency_key not in (None, idempotency_key) or any(
                other.id != draft.id and other.dispatch_idempotency_key == idempotency_key
                for other in self.drafts.values()
            ):
                raise DispatchKeyConflictError(f"Dispatch key of draft {draft_id} is taken")
            from_state = job.state
            claimed, _ = await self.jobs.transition_job_state(
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.DISPATCHED,
                payload=_claim_payload(draft_id, idempotency_key, from_state),
            )
            claimed.queue_name = queue_name
            claimed.lease_expires_at = None
            stored = replace(draft, dispatch_idempotency_key=idempotency_key)
            self.drafts[stored.id] = stored
            return ClaimOutcome(ClaimStatus.CLAIMED, claimed, stored)

    async def record_provider_draft(
        self,
        *,
        organization_id: UUID,
        draft_id: UUID,
        provider_draft_id: str,
        provider_draft_message_id: str | None,
    ) -> GeneratedDraft:
        async with self._lock:
            draft = self.drafts.get(draft_id)
            if draft is None or _to_uuid(draft.organization_id) != organization_id:
                raise KeyError(f"Draft {draft_id} not found for organization {organization_id}")
            if draft.provider_draft_id is None:
                draft = replace(
                    draft,
                    provider_draft_id=provider_draft_id,
                    provider_draft_message_id=provider_draft_message_id,
                )
                self.drafts[draft_id] = draft
            return draft

    async def finish(
        self,
        *,
        organization_id: UUID,
        job_id: UUID,
        draft_id: UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        async with self._lock:
            job = await self.jobs.get_job(organization_id, job_id)
            if job is None:
                raise KeyError(f"Job {job_id} not found for organization {organization_id}")
            if job.state == JobState.COMPLETED.value:
                return job
            duplicate = outbound is not None and any(
                m.provider_message_id == outbound.provider_message_id
                and str(m.mailbox_id) == str(outbound.mailbox_id)
                for m in self.messages.values()
            )
            outbound_id = (
                str(outbound.message_id) if outbound is not None and not duplicate else None
            )
            event, result = _finish_payloads(draft_id, mode, provider_ref, outbound_id)
            # Transition first: it raises on an illegal state before anything is stored.
            done, _ = await self.jobs.transition_job_state(
                organization_id=organization_id,
                job_id=job_id,
                target_state=JobState.COMPLETED,
                payload=event,
                result_ref=result,
            )
            self.drafts[draft_id] = replace(
                self.drafts[draft_id],
                status=DraftStatus.DISPATCHED.value,
                provider_ref=provider_ref,
            )
            if outbound is not None and not duplicate:
                self.messages[_to_uuid(outbound.message_id)] = outbound
                self.outbound.append(outbound)
                thread = self.threads[_to_uuid(outbound.thread_id)]
                self.threads[thread.id] = replace(
                    thread,
                    message_count=thread.message_count + 1,
                    last_message_at=max(thread.last_message_at, outbound.received_at),
                    participants=sorted(
                        set(thread.participants)
                        | {outbound.sender.email.lower()}
                        | {r.email.lower() for r in outbound.recipients}
                    ),
                )
            return done
```

- [ ] **Step 6: Implement DispatchService**

Create `packages/dispatch/service.py` (`packages/dispatch/__init__.py` exists from Task 2 (6.3); if it does not yet, create it with the docstring `"""Outbound reply construction and exactly-once dispatch (design.md §5.8)."""`):

```python
"""Exactly-once dispatch of an approved draft in five steps (design.md §5.8, ADR-0009).

Requirements:
- R17.1 / R17.6 / R16.8: the category's ``dispatch_mode`` (default create_draft) decides
  whether the provider draft is sent; nothing is dispatched without an approval unless the
  category is ``auto_send_eligible``.
- R17.2: the reply comes from ``build_outbound_reply`` with the provider thread id; a null
  provider thread id fails permanently.
- R17.3 / R19.2 / R19.3: the claim stores the dispatch key; the provider draft is the durable
  handle that lets a redelivery confirm instead of sending twice.
- R17.4 / R17.7: finish persists the provider ref and, in send_reply mode, the outbound
  ``email_message``, in one transaction with DISPATCHED -> COMPLETED.
- R17.5: adapter RateLimited / Transient propagate (the job stays DISPATCHED and the broker
  retry ladder redelivers); Permanent / NotFound / AuthExpired, MissingProviderThreadError
  and DispatchPermanentError are dead-lettered by the dispatch-worker.
- R19.3 / tasks.md 6.5: the five steps run under the store's per-job lock, so two deliveries
  of one job never overlap (DispatchJobBusyError defers the second); a redelivery with no
  recorded handle adopts the provider draft an earlier delivery created (find_draft) instead
  of creating a second one; and a send on a handle this delivery did not create is always
  preceded by step 4.

The provider send never runs inside a database transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter_for_mailbox
from packages.core.idempotency import derive_idempotency_key
from packages.db.dispatch import ClaimStatus, DispatchContext, DispatchStore
from packages.dispatch.reply import MissingProviderThreadError, build_outbound_reply
from packages.domain import DispatchMode, ProviderDraftStatus
from packages.domain.entities import (
    EmailAddress,
    GeneratedDraft,
    Mailbox,
    NormalizedMessage,
    OutboundReply,
    SentRef,
)
from packages.domain.review import DraftStatus
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry, get_default_registry

logger = logging.getLogger(__name__)

AdapterResolver = Callable[[Mailbox], MailProviderAdapter]
DEFAULT_DISPATCH_QUEUE = "email.dispatch"
_APPROVED = frozenset({DraftStatus.APPROVED.value, DraftStatus.DISPATCHED.value})
_OUTBOUND_NAMESPACE = uuid5(NAMESPACE_URL, "rag-email/outbound-message")


class DispatchOutcome(StrEnum):
    """How one dispatch delivery ended."""

    COMPLETED_DRAFT = "completed_draft"
    COMPLETED_SENT = "completed_sent"
    ALREADY_DONE = "already_done"
    NOT_DISPATCHABLE = "not_dispatchable"


class DispatchPermanentError(Exception):
    """Retrying cannot help; the dispatch-worker dead-letters the job with this reason."""


class DispatchJobBusyError(Exception):
    """Another delivery of the same job holds its dispatch lock; this one is deferred.

    Not a failure: the dispatch-worker re-queues the delivery without using a retry attempt
    and without touching the job.
    """


def bare_message_id(value: str | None) -> str | None:
    """A Message-ID as stored in email_message: no angle brackets (parser convention)."""
    if value is None:
        return None
    cleaned = value.strip().strip("<>").strip()
    return cleaned or None


def message_id_domain(address: str) -> str:
    """Right-hand side of our Message-ID: the mailbox's own domain."""
    return address.rsplit("@", 1)[-1].strip().lower() or "localhost"


def outbound_message(
    ctx: DispatchContext, draft: GeneratedDraft, reply: OutboundReply, sent: SentRef
) -> NormalizedMessage:
    """The sent reply as an ``email_message`` row, direction='outbound' (R17.7).

    ``provider_message_id`` is the provider's id of the SENT copy, the id mailbox sync later
    sees, so the (org, mailbox, provider_message_id) dedup recognises our own reply.
    """
    references = [ref for ref in (bare_message_id(r) for r in reply.references) if ref]
    return NormalizedMessage(
        message_id=uuid5(_OUTBOUND_NAMESPACE, str(draft.id)),
        thread_id=ctx.thread.id,
        mailbox_id=ctx.mailbox.id,
        organization_id=ctx.job.organization_id,
        provider=ctx.mailbox.provider,
        provider_message_id=sent.provider_message_id,
        sender=EmailAddress(email=ctx.mailbox.address, name=ctx.mailbox.display_name),
        received_at=sent.sent_at,
        rfc822_message_id=bare_message_id(reply.message_id),
        in_reply_to=bare_message_id(reply.in_reply_to),
        references_ids=references,
        recipients=list(reply.to),
        cc=list(reply.cc),
        subject=reply.subject,
        subject_normalized=ctx.thread.subject_normalized,
        body_text=reply.body_text,
        body_text_clean=draft.body,
        snippet=draft.body[:200],
        direction="outbound",
    )


class DispatchService:
    """Runs design §5.8's five steps for one job; safe to call again for the same job."""

    def __init__(
        self,
        *,
        store: DispatchStore,
        adapter_for: AdapterResolver = get_adapter_for_mailbox,
        registry: TaxonomyRegistry | None = None,
        dispatch_queue: str = DEFAULT_DISPATCH_QUEUE,
        confirm_recheck_delay_s: float = 2.0,
    ) -> None:
        self._store = store
        self._adapter_for = adapter_for
        self._registry = registry
        self._dispatch_queue = dispatch_queue
        self._recheck_delay_s = confirm_recheck_delay_s

    async def dispatch(self, *, organization_id: UUID, job_id: UUID) -> DispatchOutcome:
        """Dispatch the job's approved draft exactly once (design.md §5.8).

        Serialized per job: a repeated approve re-publishes while the job is DRAFTED,
        DISPATCHED or RETRY_PENDING, and the consumer runs several deliveries at once, so
        two deliveries of one job can arrive together. The loser raises
        DispatchJobBusyError instead of running any step.
        """
        async with self._store.job_lock(organization_id, job_id) as held:
            if not held:
                raise DispatchJobBusyError(
                    f"Job {job_id} is being dispatched by another delivery; deferring this one"
                )
            return await self._dispatch_locked(organization_id, job_id)

    async def mark_dispatch_route(self, *, organization_id: UUID, job_id: UUID) -> None:
        """Route an operator replay of this job to the dispatch-worker (R18.7).

        The claim writes queue_name too, but a failure before or during the claim (load
        finds nothing, a key conflict rolls the claim back) would leave the generation
        queue there, and the replay would regenerate the draft.
        """
        await self._store.set_dispatch_queue(
            organization_id=organization_id, job_id=job_id, queue_name=self._dispatch_queue
        )

    async def _dispatch_locked(self, organization_id: UUID, job_id: UUID) -> DispatchOutcome:
        ctx = await self._store.load(organization_id, job_id)
        if ctx is None:
            raise DispatchPermanentError(
                f"Job {job_id} has no dispatchable draft in organization {organization_id}"
            )
        if ctx.job.state == JobState.COMPLETED.value:
            return DispatchOutcome.ALREADY_DONE
        mode, auto_send = self._policy(ctx.category)
        if ctx.draft.status not in _APPROVED and not auto_send:
            logger.warning(
                "Dispatch of job %s skipped: draft %s is '%s' and not approved",
                job_id,
                ctx.draft.id,
                ctx.draft.status,
            )
            return DispatchOutcome.NOT_DISPATCHABLE

        # 1 claim
        key = derive_idempotency_key(
            organization_id=organization_id,
            mailbox_id=ctx.mailbox.id,
            provider_message_id=ctx.original.provider_message_id,
            operation_type="dispatch",
        )
        claim = await self._store.claim(
            organization_id=organization_id,
            job_id=job_id,
            draft_id=ctx.draft.id,
            idempotency_key=key,
            queue_name=self._dispatch_queue,
        )
        if claim.status is ClaimStatus.ALREADY_COMPLETED:
            return DispatchOutcome.ALREADY_DONE
        if claim.status is ClaimStatus.NOT_DISPATCHABLE:
            logger.warning("Dispatch of job %s skipped: job is %s", job_id, claim.job.state)
            return DispatchOutcome.NOT_DISPATCHABLE
        draft = claim.draft

        provider_thread_id = ctx.thread.provider_thread_id
        if provider_thread_id is None or not provider_thread_id.strip():
            raise MissingProviderThreadError(draft_id=str(draft.id), thread_id=str(ctx.thread.id))
        reply = build_outbound_reply(
            draft=draft,
            original=ctx.original,
            provider_thread_id=provider_thread_id,
            message_id_domain=message_id_domain(ctx.mailbox.address),
        )
        adapter = self._adapter_for(ctx.mailbox)

        # 2 draft: reuse the stored handle, adopt an unrecorded one, or create it once
        created_here = False
        if not draft.provider_draft_id and ctx.job.state != JobState.DRAFTED.value:
            # A redelivery or an operator replay: an earlier delivery may have died after the
            # provider accepted create_draft and before the id was recorded (tasks.md 6.5).
            orphan = (
                await adapter.find_draft(ctx.mailbox, provider_thread_id, reply.message_id)
                if reply.message_id
                else None
            )
            if orphan is not None and orphan.provider_draft_id:
                logger.info(
                    "Job %s: adopting unrecorded provider draft %s",
                    job_id,
                    orphan.provider_draft_id,
                )
                draft = await self._store.record_provider_draft(
                    organization_id=organization_id,
                    draft_id=draft.id,
                    provider_draft_id=orphan.provider_draft_id,
                    provider_draft_message_id=orphan.provider_message_id,
                )
        if not draft.provider_draft_id:
            ref = await adapter.create_draft(ctx.mailbox, reply)
            if not ref.provider_draft_id:
                raise DispatchPermanentError("The provider returned an empty draft id")
            stored = await self._store.record_provider_draft(
                organization_id=organization_id,
                draft_id=draft.id,
                provider_draft_id=ref.provider_draft_id,
                provider_draft_message_id=ref.provider_message_id,
            )
            created_here = stored.provider_draft_id == ref.provider_draft_id
            if not created_here:
                # Cannot happen under the job lock; if it does, the stored handle may already
                # be sent, so step 4 runs before any send.
                logger.warning(
                    "Provider draft %s left unused: draft %s already holds %s",
                    ref.provider_draft_id,
                    draft.id,
                    stored.provider_draft_id,
                )
            draft = stored
        provider_draft_id = draft.provider_draft_id
        if not provider_draft_id:
            raise DispatchPermanentError(f"Draft {draft.id} has no provider draft handle")

        if mode is DispatchMode.CREATE_DRAFT:
            # The customer has received nothing: no outbound email_message (R17.7, 6.7).
            await self._store.finish(
                organization_id=organization_id,
                job_id=job_id,
                draft_id=draft.id,
                mode=mode,
                provider_ref=provider_draft_id,
                outbound=None,
            )
            logger.info("Job %s: provider draft %s created", job_id, provider_draft_id)
            return DispatchOutcome.COMPLETED_DRAFT

        # 3 send / 4 confirm (every handle this delivery did not create is confirmed first)
        sent = await self._send_exactly_once(
            adapter, ctx, draft, provider_draft_id, reply, confirm_first=not created_here
        )
        # 5 finish
        await self._store.finish(
            organization_id=organization_id,
            job_id=job_id,
            draft_id=draft.id,
            mode=mode,
            provider_ref=sent.provider_message_id,
            outbound=outbound_message(ctx, draft, reply, sent),
        )
        logger.info("Job %s: reply sent as %s", job_id, sent.provider_message_id)
        return DispatchOutcome.COMPLETED_SENT

    def _policy(self, category: str | None) -> tuple[DispatchMode, bool]:
        registry = self._registry or get_default_registry()
        definition = registry.get(category) if category else None
        if definition is None:
            return DispatchMode.CREATE_DRAFT, False
        return DispatchMode(str(definition.dispatch_mode)), bool(definition.auto_send_eligible)

    async def _send_exactly_once(
        self,
        adapter: MailProviderAdapter,
        ctx: DispatchContext,
        draft: GeneratedDraft,
        provider_draft_id: str,
        reply: OutboundReply,
        *,
        confirm_first: bool,
    ) -> SentRef:
        """Step 3, preceded by step 4 whenever an earlier delivery may have sent already."""
        if confirm_first:
            status = await adapter.get_draft_status(ctx.mailbox, provider_draft_id)
            if status == ProviderDraftStatus.DRAFT and self._recheck_delay_s > 0:
                # A just-accepted send can take a moment to show (Graph 202, design §5.8).
                await asyncio.sleep(self._recheck_delay_s)
                status = await adapter.get_draft_status(ctx.mailbox, provider_draft_id)
            if status == ProviderDraftStatus.SENT:
                return SentRef(
                    provider_message_id=draft.provider_draft_message_id or provider_draft_id
                )
            if status == ProviderDraftStatus.MISSING:
                return await self._find_sent(adapter, ctx, draft, reply, provider_draft_id)
        return await adapter.send_draft(ctx.mailbox, provider_draft_id)

    async def _find_sent(
        self,
        adapter: MailProviderAdapter,
        ctx: DispatchContext,
        draft: GeneratedDraft,
        reply: OutboundReply,
        provider_draft_id: str,
    ) -> SentRef:
        provider_thread_id = ctx.thread.provider_thread_id or ""
        # Graph keeps the draft's immutable id on the sent copy; Gmail's drafts.send gives the
        # sent copy a new id, so only our Message-ID finds it there (Task 5 contract rule).
        lookups = [
            key
            for key in dict.fromkeys((draft.provider_draft_message_id, reply.message_id))
            if key
        ]
        if not lookups:
            raise DispatchPermanentError(
                f"Provider draft {provider_draft_id} is gone and there is no message id to find"
            )
        for lookup in lookups:
            found = await adapter.find_sent_message(ctx.mailbox, provider_thread_id, lookup)
            if found is not None:
                return found
        raise DispatchPermanentError(
            f"Provider draft {provider_draft_id} is gone and thread {provider_thread_id} "
            f"holds no sent message {' or '.join(lookups)}; a person may have deleted the "
            "draft. Check the mailbox, then replay or close the job."
        )
```

- [ ] **Step 7: Run the unit tests to verify they pass**

Run: `uv run pytest tests/unit/test_dispatch_service.py tests/unit/test_dependency_rules.py -v`
Expected: PASS (22 dispatch tests; `packages/dispatch` imports no provider module and no `services.*`).

- [ ] **Step 8: Write the Postgres dispatch-store tests and run them**

Create `tests/integration/test_dispatch_store_postgres.py`:

```python
"""PostgresDispatchStore transactions (tasks 6.5, 6.6, 6.7; R17.3, R17.4, R17.7, R18.5, R19.2).

The claim and the finish are single transactions: key + queue_name + DISPATCHED together,
and provider ref + outbound email_message + thread + COMPLETED together; every query is
scoped by organization_id.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.dispatch import ClaimStatus, DispatchKeyConflictError, PostgresDispatchStore
from packages.db.draft import insert_draft
from packages.db.job import PostgresJobStore
from packages.domain import DispatchMode
from packages.domain.entities import EmailAddress, GeneratedDraft, Job, NormalizedMessage
from packages.domain.state_machine import JobState

QUEUE = "email.dispatch"


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


@dataclass(frozen=True)
class Seed:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    job_id: uuid.UUID
    draft_id: uuid.UUID


async def _seed(
    pool: asyncpg.Pool,
    *,
    state: JobState = JobState.DRAFTED,
    org_id: uuid.UUID | None = None,
    mailbox_id: uuid.UUID | None = None,
) -> Seed:
    org = org_id or uuid.uuid4()
    mbx = mailbox_id or uuid.uuid4()
    thread_id, msg_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        if org_id is None:
            await conn.execute("INSERT INTO organization (id, name) VALUES ($1, $2)", org, f"org-{org.hex[:6]}")
            await conn.execute(
                "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
                " VALUES ($1, $2, 'gmail', $3, 'Acme Support', 'active')",
                mbx,
                org,
                f"support-{mbx.hex[:6]}@acme.example",
            )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id,"
            " message_count, last_message_at) VALUES ($1, $2, $3, $4, 1, now())",
            thread_id,
            org,
            mbx,
            f"th-{thread_id.hex[:8]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                rfc822_message_id, direction, sender_email, sender_name, recipients,
                subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'inbound', 'alice@customer.example', 'Alice',
                      '[]', 'Where is order 82915?', 'Status of 82915?', now())
            """,
            msg_id,
            org,
            mbx,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            f"orig-{msg_id.hex[:8]}@customer.example",
        )
        for category, age in (("support", "1 hour"), ("billing", "0 seconds")):
            await conn.execute(
                f"""
                INSERT INTO classification_result (
                    id, organization_id, message_id, category, priority, reply_required,
                    retrieval_required, confidence, decided_by, created_at
                ) VALUES ($1, $2, $3, $4, 'normal', true, true, 0.9, 'rule',
                          now() - interval '{age}')
                """,
                uuid.uuid4(),
                org,
                msg_id,
                category,
            )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org,
            message_id=msg_id,
            thread_id=thread_id,
            state=state.value,
            idempotency_key=f"dstore-{uuid.uuid4()}",
        )
    )
    async with pool.acquire() as conn:
        draft = await insert_draft(
            conn,
            GeneratedDraft(
                organization_id=org,
                message_id=msg_id,
                thread_id=thread_id,
                job_id=job.id,
                body="Order ORD-82915 was dispatched on 24 September.",
                status="approved",
            ),
        )
    return Seed(org, mbx, thread_id, msg_id, uuid.UUID(str(job.id)), draft.id)


def _outbound(seed: Seed, provider_message_id: str) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid.uuid4(),
        thread_id=seed.thread_id,
        mailbox_id=seed.mailbox_id,
        organization_id=seed.org_id,
        provider="gmail",
        provider_message_id=provider_message_id,
        sender=EmailAddress("support@acme.example", "Acme Support"),
        received_at=datetime.now(UTC),
        rfc822_message_id="reply-1@acme.example",
        in_reply_to="orig-1@customer.example",
        recipients=[EmailAddress("alice@customer.example")],
        subject="Re: Where is order 82915?",
        body_text="Order ORD-82915 was dispatched on 24 September.",
        direction="outbound",
    )


async def _cleanup(pool: asyncpg.Pool, *org_ids: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organization WHERE id = ANY($1::uuid[])", list(org_ids))


async def test_load_is_tenant_scoped_and_reads_the_latest_category(pool: asyncpg.Pool) -> None:
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        ctx = await store.load(seed.org_id, seed.job_id)
        assert ctx is not None
        assert ctx.category == "billing"
        assert ctx.draft.id == seed.draft_id
        assert ctx.thread.provider_thread_id == f"th-{seed.thread_id.hex[:8]}"
        assert ctx.mailbox.address.startswith("support-")
        assert ctx.original.rfc822_message_id == f"orig-{seed.message_id.hex[:8]}@customer.example"
        assert await store.load(uuid.uuid4(), seed.job_id) is None
    finally:
        await _cleanup(pool, seed.org_id)


async def test_claim_sets_key_queue_and_dispatched_in_one_transaction(pool: asyncpg.Pool) -> None:
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        first = await store.claim(
            organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
            idempotency_key="key-1", queue_name=QUEUE,
        )
        again = await store.claim(
            organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
            idempotency_key="key-1", queue_name=QUEUE,
        )
        assert first.status is ClaimStatus.CLAIMED
        assert first.draft.dispatch_idempotency_key == "key-1"
        assert again.status is ClaimStatus.RESUMED
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                "SELECT state, queue_name, lease_expires_at FROM processing_job"
                " WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                seed.org_id,
            )
            dispatched_events = await conn.fetchval(
                "SELECT count(*) FROM processing_event WHERE job_id = $1 AND organization_id = $2"
                " AND state_to = 'DISPATCHED'",
                seed.job_id,
                seed.org_id,
            )
        assert job is not None
        assert (job["state"], job["queue_name"], job["lease_expires_at"]) == ("DISPATCHED", QUEUE, None)
        assert dispatched_events == 1
    finally:
        await _cleanup(pool, seed.org_id)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (JobState.COMPLETED, ClaimStatus.ALREADY_COMPLETED),
        (JobState.GENERATING, ClaimStatus.NOT_DISPATCHABLE),
        (JobState.RETRY_PENDING, ClaimStatus.CLAIMED),
    ],
)
async def test_claim_by_job_state(pool: asyncpg.Pool, state: JobState, expected: ClaimStatus) -> None:
    """COMPLETED acks, a job still generating is left alone, RETRY_PENDING is the replay edge."""
    seed = await _seed(pool, state=state)
    try:
        outcome = await PostgresDispatchStore(pool).claim(
            organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
            idempotency_key=f"key-{state.value}", queue_name=QUEUE,
        )
        assert outcome.status is expected
        want = JobState.DISPATCHED.value if expected is ClaimStatus.CLAIMED else state.value
        assert outcome.job.state == want
    finally:
        await _cleanup(pool, seed.org_id)


async def test_one_key_cannot_be_claimed_by_two_drafts(pool: asyncpg.Pool) -> None:
    """R19.2: UNIQUE (dispatch_idempotency_key); the losing claim rolls back entirely."""
    first = await _seed(pool)
    second = await _seed(pool, org_id=first.org_id, mailbox_id=first.mailbox_id)
    store = PostgresDispatchStore(pool)
    try:
        await store.claim(
            organization_id=first.org_id, job_id=first.job_id, draft_id=first.draft_id,
            idempotency_key="shared-key", queue_name=QUEUE,
        )
        with pytest.raises(DispatchKeyConflictError):
            await store.claim(
                organization_id=second.org_id, job_id=second.job_id, draft_id=second.draft_id,
                idempotency_key="shared-key", queue_name=QUEUE,
            )
        async with pool.acquire() as conn:
            state = await conn.fetchval(
                "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
                second.job_id,
                second.org_id,
            )
        assert state == JobState.DRAFTED.value
    finally:
        await _cleanup(pool, first.org_id)


async def test_record_provider_draft_keeps_the_first_handle(pool: asyncpg.Pool) -> None:
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        first = await store.record_provider_draft(
            organization_id=seed.org_id, draft_id=seed.draft_id,
            provider_draft_id="d-1", provider_draft_message_id="m-1",
        )
        second = await store.record_provider_draft(
            organization_id=seed.org_id, draft_id=seed.draft_id,
            provider_draft_id="d-2", provider_draft_message_id="m-2",
        )
        assert (first.provider_draft_id, first.provider_draft_message_id) == ("d-1", "m-1")
        assert second.provider_draft_id == "d-1"
    finally:
        await _cleanup(pool, seed.org_id)


async def test_finish_send_reply_writes_one_outbound_row_and_updates_the_thread(
    pool: asyncpg.Pool,
) -> None:
    """R17.7: the outbound row and COMPLETED commit together; a second finish is a no-op."""
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        await store.claim(
            organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
            idempotency_key="key-send", queue_name=QUEUE,
        )
        for _ in range(2):
            job = await store.finish(
                organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
                mode=DispatchMode.SEND_REPLY, provider_ref="sent-1",
                outbound=_outbound(seed, "sent-1"),
            )
            assert job.state == JobState.COMPLETED.value
        async with pool.acquire() as conn:
            outbound = await conn.fetch(
                "SELECT provider_message_id, rfc822_message_id, direction FROM email_message"
                " WHERE organization_id = $1 AND thread_id = $2 AND direction = 'outbound'",
                seed.org_id,
                seed.thread_id,
            )
            thread = await conn.fetchrow(
                "SELECT message_count, participants FROM email_thread"
                " WHERE id = $1 AND organization_id = $2",
                seed.thread_id,
                seed.org_id,
            )
            draft = await conn.fetchrow(
                "SELECT status, provider_ref FROM generated_draft"
                " WHERE id = $1 AND organization_id = $2",
                seed.draft_id,
                seed.org_id,
            )
        assert [dict(r) for r in outbound] == [
            {"provider_message_id": "sent-1", "rfc822_message_id": "reply-1@acme.example", "direction": "outbound"}
        ]
        assert thread is not None and thread["message_count"] == 2
        assert "alice@customer.example" in thread["participants"]
        assert draft is not None and (draft["status"], draft["provider_ref"]) == ("dispatched", "sent-1")
    finally:
        await _cleanup(pool, seed.org_id)


async def test_finish_create_draft_writes_no_outbound_row(pool: asyncpg.Pool) -> None:
    """6.7: in create_draft mode the customer has received nothing, so nothing is recorded."""
    seed = await _seed(pool)
    store = PostgresDispatchStore(pool)
    try:
        await store.claim(
            organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
            idempotency_key="key-draft", queue_name=QUEUE,
        )
        job = await store.finish(
            organization_id=seed.org_id, job_id=seed.job_id, draft_id=seed.draft_id,
            mode=DispatchMode.CREATE_DRAFT, provider_ref="d-1", outbound=None,
        )
        assert job.state == JobState.COMPLETED.value
        assert job.result_ref == {"draft_id": str(seed.draft_id), "dispatch_mode": "create_draft", "provider_ref": "d-1"}
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM email_message"
                " WHERE organization_id = $1 AND direction = 'outbound'",
                seed.org_id,
            )
        assert count == 0
    finally:
        await _cleanup(pool, seed.org_id)


async def test_job_lock_admits_one_delivery_per_job_and_dies_with_its_session(
    pool: asyncpg.Pool,
) -> None:
    """R19.3: a second holder of the same job is refused without waiting; another job is not
    blocked; a terminated session (a killed worker) releases the lock."""
    org, job, other_job = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store = PostgresDispatchStore(pool)
    async with store.job_lock(org, job) as first:
        assert first is True
        async with store.job_lock(org, job) as second:
            assert second is False
        async with store.job_lock(org, other_job) as unrelated:
            assert unrelated is True
    async with store.job_lock(org, job) as again:
        assert again is True

    killed_pool = await create_pool_from_settings(AppSettings().database)
    killed = PostgresDispatchStore(killed_pool)
    holder = killed.job_lock(org, job)
    assert await holder.__aenter__() is True
    async with store.job_lock(org, job) as while_held:
        assert while_held is False
    killed_pool.terminate()  # the worker process dies: its session ends
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        async with store.job_lock(org, job) as after_kill:
            if after_kill:
                break
        assert asyncio.get_running_loop().time() < deadline, "lock survived its session"
        await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):  # the dead worker's context never exits cleanly
        await holder.__aexit__(None, None, None)


async def test_set_dispatch_queue_routes_a_failed_dispatch_but_not_a_generating_job(
    pool: asyncpg.Pool,
) -> None:
    """R18.7: a dispatch that fails before its claim still replays to the dispatch-worker."""
    drafted = await _seed(pool)
    generating = await _seed(pool, state=JobState.GENERATING)
    store = PostgresDispatchStore(pool)
    try:
        for seed in (drafted, generating):
            await pool.execute(
                "UPDATE processing_job SET queue_name = 'email.triage'"
                " WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                seed.org_id,
            )
            await store.set_dispatch_queue(
                organization_id=seed.org_id, job_id=seed.job_id, queue_name=QUEUE
            )
        queues = [
            await pool.fetchval(
                "SELECT queue_name FROM processing_job WHERE id = $1 AND organization_id = $2",
                seed.job_id,
                seed.org_id,
            )
            for seed in (drafted, generating)
        ]
        assert queues == [QUEUE, "email.triage"]
        # Another tenant's id changes nothing.
        await store.set_dispatch_queue(
            organization_id=uuid.uuid4(), job_id=generating.job_id, queue_name=QUEUE
        )
    finally:
        await _cleanup(pool, drafted.org_id, generating.org_id)
```

Run: `uv run pytest tests/integration/test_dispatch_store_postgres.py -v`
Expected: PASS (11 tests, including the 3 parametrized claim cases). A failure on `test_claim_by_job_state[RETRY_PENDING]` with `IllegalStateTransitionError` means Part A's `RETRY_PENDING → DISPATCHED` edge is not in yet.

- [ ] **Step 9: Lint, type-check and commit**

```bash
uv run ruff format packages/db/message.py packages/db/thread.py packages/db/dispatch.py packages/dispatch/service.py tests/stubs/dispatch_fakes.py tests/unit/test_dispatch_service.py tests/integration/test_dispatch_store_postgres.py
uv run ruff check packages tests
uv run mypy packages services tests
uv run pytest tests/unit -q
git add packages/db/message.py packages/db/thread.py packages/db/dispatch.py packages/dispatch/service.py tests/stubs/dispatch_fakes.py tests/unit/test_dispatch_service.py tests/integration/test_dispatch_store_postgres.py
git commit -m "feat(dispatch): five-step exactly-once DispatchService with outbound write-back [task 6.5, 6.6, 6.7] [R17.1, R17.3, R17.4, R17.5, R17.6, R17.7, R18.5, R19.2, R19.3]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
Expected: ruff and mypy clean; unit suite passes.

### Task 8: dispatch-worker service, Retry-After tiers and forced-redelivery crash tests [tasks.md 6.5, 6.6]

**Files:**
- Modify: `packages/broker/backoff.py` (add `import math` at line 10; append `resolve_retry_after_tier_delay` after `resolve_retry_tier_delay`, end of file line 95)
- Modify: `packages/broker/retry.py` (`handle_job_transient_failure` signature lines 86–95 gains `retry_after_s: float | None = None`; delay selection line 125; payload line 152; import line 17)
- Modify: `packages/broker/consumer.py` (lines 310–326 move into a new overridable `prepare_delivery`; new `retry_after_s` hook next to `is_transient_error` (line 116); transient call lines 339–347 pass `retry_after_s=self.retry_after_s(exc)`)
- Create: `services/dispatch_worker/failure_policy.py`, `services/dispatch_worker/consumer.py`, `services/dispatch_worker/main.py`
- Modify: `docker-compose.yml` (replace the `dispatch-worker` block, lines 289–303)
- Modify: `scripts/image_smoke.py` (entrypoint tuple, line 13–23: add `"services.dispatch_worker.main"`)
- Modify: `tests/unit/test_production_imports.py` (`ENTRYPOINTS`, line 25: add `"services.dispatch_worker.main"`)
- Test: `tests/unit/test_retry_after_tier.py` (new), `tests/unit/test_dispatch_worker.py` (new), `tests/unit/test_runtime_image_contract.py` (append one test after line 181), `tests/integration/test_dispatch_worker_integration.py` (new); regression `tests/unit/test_retry_and_dead_letter.py`

**Interfaces:**
- Consumes: `BaseConsumer` (packages/broker/consumer.py), `FatalError`; `handle_job_transient_failure` / `handle_job_terminal_failure` (terminal already moves any non-FAILED job to FAILED then DEAD_LETTER with the reason as `last_error`); `MessagePublisher.retry_tier_suffix` (maps a delay to the first tier ≥ it); `WorkerRuntime`, `WorkerResources`, `StartFn` (packages/broker/worker_runtime.py); `DispatchWorkerSettings` (`service_name="dispatch_worker"`), `settings.broker.queue_dispatch`, `settings.concurrency.dispatch_worker_concurrency`, `settings.retry`, `settings.routing.categories_config_path`; `load_categories_from_yaml`; `DispatchService`, `DispatchOutcome`, `DispatchPermanentError`, `PostgresDispatchStore`, `DispatchKeyConflictError` (Task 7); `MissingProviderThreadError`, `MissingRecipientError` (Task 2); `RateLimited`, `Transient`, `Permanent`, `NotFound`, `AuthExpired`; `PostgresJobStore.replay_job(organization_id, job_id, payload)`; `tests.stubs.dispatch_fakes` (Task 7).
- Produces:
  - `packages.broker.backoff.resolve_retry_after_tier_delay(retry_after_s: float, settings: RetryLadderSettings | None = None) -> int`.
  - `handle_job_transient_failure(..., job_store=None, retry_after_s: float | None = None)`: with `retry_after_s` the delay is `resolve_retry_after_tier_delay(retry_after_s)`; without it, unchanged.
  - `BaseConsumer.prepare_delivery(self, envelope: JobEnvelope) -> None` (default: RETRY_PENDING→GENERATING recovery plus lease, exactly today's behaviour) and `BaseConsumer.retry_after_s(self, exc: Exception) -> float | None` (default `None`).
  - `services.dispatch_worker.failure_policy`: `Disposition` (`RETRY`, `DEAD_LETTER`), `FailureDecision(disposition, reason)`, `classify_dispatch_failure(exc: BaseException) -> FailureDecision`, `provider_retry_after(exc: BaseException) -> float | None`.
  - `services.dispatch_worker.consumer.DispatchConsumer(queue_name, *, service: DispatchService, job_store: JobStore, broker_settings=None, retry_settings=None, prefetch_count=None, connection=None, shutdown_coordinator=None, metrics=None)`: no recovery, no lease; permanent failures raise `FatalError(reason)`. On **every** dispatch failure it first calls `service.mark_dispatch_route(...)` (R18.7), so a dead-lettered dispatch replays to the dispatch-worker. A `DispatchJobBusyError` (another delivery of the same job holds `job_lock`) is not a failure: the consumer re-publishes the same envelope (same `attempt`) to the first retry tier through `publish_to_retry` and returns, so `BaseConsumer` acks it and the job row is not touched.
  - `services.dispatch_worker.main`: `SERVICE_NAME = "dispatch_worker"`, `DEFAULT_PORT = 8006`, `build_consumer(res: WorkerResources, *, service: DispatchService | None = None) -> DispatchConsumer`, `async build_components(res) -> list[StartFn]`, `main()`.
  - Compose `dispatch-worker`: `command: ["python", "-m", "services.dispatch_worker.main"]`, `<<: *app-env`, `/readyz` healthcheck, `GMAIL_ACCESS_TOKEN: ${GMAIL_ACCESS_TOKEN:-}` (the dispatch-worker calls the provider; see Contract Conflicts for the 6.10 overlap).

Why the base consumer needs two hooks (verified in code): `BaseConsumer._handle_message` always runs `handle_job_recovery`, which moves **any** `RETRY_PENDING` job to `GENERATING` (retry.py:59), and then `acquire_lease` on any non-terminal state (job.py:523). For the dispatch-worker that would turn an operator replay (`DEAD_LETTER → RETRY_PENDING`) into a regeneration and would lease `DISPATCHED` jobs, while design §5.8 says the dispatch-worker takes no leases and the claim itself moves `RETRY_PENDING → DISPATCHED`. `DispatchConsumer.prepare_delivery` therefore does nothing. The consumer still passes `job_store`, so a permanent failure goes `DISPATCHED → FAILED → DEAD_LETTER` with the provider error as `last_error`, and a transient failure leaves `DISPATCHED` untouched (`handle_job_transient_failure` only moves `GENERATING`/`FAILED` jobs).

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_retry_after_tier.py`:

```python
"""Retry-After picks the first ladder tier >= its value, capped at the last (task 6.6; R17.5)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from packages.broker.backoff import resolve_retry_after_tier_delay
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.retry import handle_job_transient_failure
from packages.core.settings import BrokerSettings, RetryLadderSettings

LADDER = RetryLadderSettings(tier_1_delay_s=30, tier_2_delay_s=300, tier_3_delay_s=1800)


@pytest.mark.parametrize(
    ("retry_after", "tier"),
    [(-5.0, 30), (0.0, 30), (1.0, 30), (30.0, 30), (30.2, 300), (299.0, 300), (300.0, 300),
     (301.0, 1800), (1800.0, 1800), (86_400.0, 1800)],
)
def test_retry_after_maps_to_first_tier_at_least_as_long(retry_after: float, tier: int) -> None:
    assert resolve_retry_after_tier_delay(retry_after, LADDER) == tier


def _publisher() -> MessagePublisher:
    publisher = MagicMock(spec=MessagePublisher)
    publisher.settings = BrokerSettings()
    publisher.publish_to_retry = AsyncMock()
    return cast(MessagePublisher, publisher)


def _envelope(attempt: int) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(uuid4()),
        idempotency_key="dispatch-key",
        job_type="dispatch",
        organization_id=str(uuid4()),
        attempt=attempt,
    )


async def _published_delay(retry_after_s: float | None, attempt: int = 0) -> int:
    publisher = _publisher()
    await handle_job_transient_failure(
        envelope=_envelope(attempt),
        exception=RuntimeError("429 Too Many Requests"),
        publisher=publisher,
        retry_settings=LADDER,
        origin_exchange="email.dispatch",
        origin_routing_key="email.dispatch",
        queue_name="email.dispatch",
        retry_after_s=retry_after_s,
    )
    call = cast(AsyncMock, publisher.publish_to_retry).await_args
    assert call is not None
    delay: int = call.kwargs["tier_delay_s"]
    return delay


async def test_transient_failure_uses_the_retry_after_tier() -> None:
    assert await _published_delay(45.0) == 300
    assert await _published_delay(45.0, attempt=2) == 300  # Retry-After wins over the attempt


async def test_transient_failure_without_retry_after_keeps_the_attempt_ladder() -> None:
    assert await _published_delay(None) == 30
    assert await _published_delay(None, attempt=1) == 300
```

Create `tests/unit/test_dispatch_worker.py`:

```python
"""dispatch-worker composition, failure policy and consumer hooks (tasks 6.5, 6.6).

Requirements: R17.5 (retry with Retry-After, dead-letter with the provider error),
R18.3 (legal transitions only), R18.7 (operator replay RETRY_PENDING -> DISPATCHED),
design.md §5.8 (the dispatch-worker takes no leases).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.adapters.exceptions import AuthExpired, NotFound, Permanent, RateLimited, Transient
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings, DispatchWorkerSettings, RetryLadderSettings
from packages.db.dispatch import DispatchKeyConflictError
from packages.dispatch.reply import MissingProviderThreadError, MissingRecipientError
from packages.dispatch.service import DispatchPermanentError, DispatchService
from packages.domain import DispatchMode
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from services.dispatch_worker.consumer import DispatchConsumer
from services.dispatch_worker.failure_policy import (
    Disposition,
    classify_dispatch_failure,
    provider_retry_after,
)
from services.dispatch_worker.main import build_consumer
from tests.stubs.dispatch_fakes import DispatchWorld, build_dispatch_world
from tests.stubs.worker_resources import fake_worker_resources

LADDER = RetryLadderSettings(tier_1_delay_s=30, tier_2_delay_s=300, tier_3_delay_s=1800, max_retries=3)


def test_build_consumer_uses_shared_resources() -> None:
    settings = DispatchWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    consumer = build_consumer(res)

    assert consumer.queue_name == settings.broker.queue_dispatch == "email.dispatch"
    assert consumer.prefetch_count == settings.concurrency.dispatch_worker_concurrency
    assert consumer._connection is res.connection
    assert consumer.shutdown_coordinator is res.shutdown
    assert consumer.job_store is not None
    assert isinstance(consumer.service, DispatchService)


def test_build_consumer_passes_the_adapter_resolver_to_the_service() -> None:
    """Task 11 injects the fake adapter here instead of patching the adapter registry."""
    res = fake_worker_resources(DispatchWorkerSettings(_env_file=None))
    resolver = MagicMock(name="adapter_resolver")
    consumer = build_consumer(res, adapter_resolver=resolver)
    assert consumer.service._adapter_for is resolver


@pytest.mark.parametrize(
    ("exc", "disposition"),
    [
        (RateLimited("429", retry_after=12.0), Disposition.RETRY),
        (Transient("503"), Disposition.RETRY),
        (Transient("503", retry_after_s=30.0), Disposition.RETRY),
        (MissingRecipientError(draft_id="d-1"), Disposition.DEAD_LETTER),
        (IllegalStateTransitionError("DRAFTED", "DISPATCHED"), Disposition.RETRY),
        (RuntimeError("unknown"), Disposition.RETRY),
        (Permanent("400 Bad Request"), Disposition.DEAD_LETTER),
        (NotFound("404 on send"), Disposition.DEAD_LETTER),
        (AuthExpired("401"), Disposition.DEAD_LETTER),
        (MissingProviderThreadError(draft_id="d-1", thread_id="t-1"), Disposition.DEAD_LETTER),
        (DispatchPermanentError("draft deleted"), Disposition.DEAD_LETTER),
        (DispatchKeyConflictError("key taken"), Disposition.DEAD_LETTER),
    ],
)
def test_failure_policy(exc: Exception, disposition: Disposition) -> None:
    decision = classify_dispatch_failure(exc)
    assert decision.disposition is disposition
    assert type(exc).__name__ in decision.reason


def test_provider_retry_after_reads_only_retryable_provider_errors() -> None:
    assert provider_retry_after(RateLimited("429", retry_after=12.0)) == 12.0
    assert provider_retry_after(RateLimited("429")) is None
    assert provider_retry_after(Transient("503")) is None
    assert provider_retry_after(Transient("503", retry_after_s=30.0)) == 30.0
    assert provider_retry_after(ValueError("x")) is None


def _consumer(world: DispatchWorld) -> tuple[DispatchConsumer, MagicMock]:
    consumer = DispatchConsumer(
        "email.dispatch",
        service=world.service,
        job_store=world.store.jobs,
        retry_settings=LADDER,
    )
    publisher = MagicMock(spec=MessagePublisher)
    publisher.settings = BrokerSettings()
    publisher.publish_to_retry = AsyncMock()
    publisher.publish_to_dead_letter = AsyncMock()
    consumer._publisher = cast(MessagePublisher, publisher)
    return consumer, publisher


def _delivery(world: DispatchWorld, attempt: int = 0) -> AbstractIncomingMessage:
    envelope = JobEnvelope(
        job_id=str(world.job_id),
        idempotency_key="dispatch-key",
        job_type="dispatch",
        organization_id=str(world.org_id),
        attempt=attempt,
    )
    message = MagicMock(spec=AbstractIncomingMessage)
    message.body = envelope.model_dump_json().encode("utf-8")
    message.routing_key = "email.dispatch"
    message.exchange = "email.dispatch"
    message.headers = {}
    message.ack = AsyncMock()
    message.nack = AsyncMock()
    return cast(AbstractIncomingMessage, message)


async def _job_state(world: DispatchWorld) -> str:
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    return job.state


async def test_rate_limit_retries_on_the_retry_after_tier_and_stays_dispatched() -> None:
    world = await build_dispatch_world()
    world.fake.inject_rate_limit(retry_after=200.0)
    consumer, publisher = _consumer(world)

    await consumer._handle_message(_delivery(world))

    call = cast(AsyncMock, publisher.publish_to_retry).await_args
    assert call is not None and call.kwargs["tier_delay_s"] == 300
    assert await _job_state(world) == JobState.DISPATCHED.value
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None and job.lease_expires_at is None  # no lease taken


async def test_permanent_failure_dead_letters_with_the_provider_error() -> None:
    world = await build_dispatch_world()
    world.fake.inject_permanent_failure("400 Bad Request: invalid To header")
    consumer, publisher = _consumer(world)

    await consumer._handle_message(_delivery(world))

    cast(AsyncMock, publisher.publish_to_dead_letter).assert_awaited_once()
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    assert job.state == JobState.DEAD_LETTER.value
    assert job.last_error is not None and "invalid To header" in job.last_error
    states = [e.state_to for e in await world.store.jobs.list_events_for_job(world.org_id, world.job_id)]
    assert states[-3:] == ["DISPATCHED", "FAILED", "DEAD_LETTER"]


async def test_replayed_dispatch_goes_to_dispatched_not_generating() -> None:
    world = await build_dispatch_world(job_state=JobState.RETRY_PENDING)
    consumer, _ = _consumer(world)

    await consumer._handle_message(_delivery(world))

    states = [e.state_to for e in await world.store.jobs.list_events_for_job(world.org_id, world.job_id)]
    assert JobState.GENERATING.value not in states
    assert states[-2:] == ["DISPATCHED", "COMPLETED"]


async def test_send_reply_delivery_acks_once_after_completion() -> None:
    world = await build_dispatch_world(mode=DispatchMode.SEND_REPLY)
    consumer, publisher = _consumer(world)
    message = _delivery(world)

    await consumer._handle_message(message)

    cast(AsyncMock, message.ack).assert_awaited_once()
    cast(AsyncMock, publisher.publish_to_retry).assert_not_awaited()
    assert world.fake.calls["send_draft"] == 1
    assert await _job_state(world) == JobState.COMPLETED.value


async def test_busy_job_defers_the_delivery_without_an_attempt_or_a_transition() -> None:
    """R19.3: a second delivery of a job being dispatched is re-queued, not processed."""
    world = await build_dispatch_world(mode=DispatchMode.SEND_REPLY)
    consumer, publisher = _consumer(world)
    message = _delivery(world, attempt=1)

    async with world.store.job_lock(world.org_id, world.job_id) as held:
        assert held
        await consumer._handle_message(message)

    call = cast(AsyncMock, publisher.publish_to_retry).await_args
    assert call is not None
    assert call.kwargs["tier_delay_s"] == LADDER.tier_1_delay_s
    assert call.args[0].attempt == 1  # no attempt used
    assert call.kwargs["origin_routing_key"] == "email.dispatch"
    cast(AsyncMock, message.ack).assert_awaited_once()
    cast(AsyncMock, publisher.publish_to_dead_letter).assert_not_awaited()
    assert world.fake.calls["create_draft"] == 0
    assert await _job_state(world) == JobState.DRAFTED.value


async def test_failure_before_the_claim_routes_the_job_to_email_dispatch() -> None:
    """R18.7: a dispatch that dead-letters before the claim still replays to this worker."""
    world = await build_dispatch_world()
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    job.queue_name = "email.triage"  # what the generation pipeline left there
    world.service.dispatch = AsyncMock(  # type: ignore[method-assign]
        side_effect=DispatchKeyConflictError("key taken by another draft")
    )
    consumer, publisher = _consumer(world)

    await consumer._handle_message(_delivery(world))

    cast(AsyncMock, publisher.publish_to_dead_letter).assert_awaited_once()
    job = await world.store.jobs.get_job(world.org_id, world.job_id)
    assert job is not None
    assert job.state == JobState.DEAD_LETTER.value
    assert job.queue_name == "email.dispatch"
```

Append to `tests/unit/test_runtime_image_contract.py`:

```python
def test_dispatch_worker_runs_its_entrypoint_with_readiness() -> None:
    """6.5: the dispatch-worker is a real service, not the Phase-0 stub."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["dispatch-worker"]
    assert service["command"] == ["python", "-m", "services.dispatch_worker.main"]
    assert "http://localhost:8006/readyz" in service["healthcheck"]["test"]
    assert service["environment"]["SERVICE_NAME"] == "dispatch_worker"
    assert service["environment"]["DATABASE__HOST"] == "postgres"  # merged *app-env
    assert service["environment"]["GMAIL_ACCESS_TOKEN"] == "${GMAIL_ACCESS_TOKEN:-}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_retry_after_tier.py tests/unit/test_dispatch_worker.py tests/unit/test_runtime_image_contract.py::test_dispatch_worker_runs_its_entrypoint_with_readiness -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_retry_after_tier_delay'`, `ModuleNotFoundError: No module named 'services.dispatch_worker.consumer'`, and `KeyError: 'command'` for the compose test.

- [ ] **Step 3: Add the Retry-After tier and the two BaseConsumer hooks**

In `packages/broker/backoff.py`, add `import math` above `import random` and append:

```python
def resolve_retry_after_tier_delay(
    retry_after_s: float,
    settings: RetryLadderSettings | None = None,
) -> int:
    """First declared retry tier whose delay is >= Retry-After, capped at the last tier.

    Requirements: R17.5, design.md §5.8 ("a Retry-After picks the first ladder tier >=
    Retry-After, capped at the last tier"). Negative values count as 0.
    """
    cfg = settings or RetryLadderSettings()
    wait = math.ceil(max(0.0, retry_after_s))
    for tier in (cfg.tier_1_delay_s, cfg.tier_2_delay_s, cfg.tier_3_delay_s):
        if wait <= tier:
            return tier
    return cfg.tier_3_delay_s
```

In `packages/broker/retry.py`, change the import (line 17) to

```python
from packages.broker.backoff import (
    calculate_exponential_backoff,
    resolve_retry_after_tier_delay,
    resolve_retry_tier_delay,
)
```

add the last parameter of `handle_job_transient_failure` (after `job_store: JobStoreProtocol | None = None,` at line 94):

```python
    retry_after_s: float | None = None,
```

document it in the docstring's Parameters list:

```
    retry_after_s : float | None
        Provider-requested wait (Retry-After). When given, it picks the first ladder tier
        >= its value, capped at the last tier, instead of the attempt-based tier (R17.5).
```

replace `delay_s = resolve_retry_tier_delay(next_attempt, retry_settings)` (line 125) with

```python
    delay_s = (
        resolve_retry_tier_delay(next_attempt, retry_settings)
        if retry_after_s is None
        else resolve_retry_after_tier_delay(retry_after_s, retry_settings)
    )
```

and add `"retry_after_s": retry_after_s,` to the RETRY_PENDING payload dict next to `"jittered_delay_s"` (line 152).

In `packages/broker/consumer.py`, add after `is_transient_error` (line 116–118):

```python
    def retry_after_s(self, exc: Exception) -> float | None:
        """Provider-requested wait carried by ``exc`` (Retry-After), or None (R17.5).

        A non-None value picks the first retry tier >= it instead of the attempt tier.
        """
        return None

    async def prepare_delivery(self, envelope: JobEnvelope) -> None:
        """Run before ``process_job``: recover a RETRY_PENDING job and take a claim lease.

        Requirements: R19.5 (RETRY_PENDING -> GENERATING on redelivery), R19.8 (lease on
        claim). Consumers whose job claims itself (the dispatch-worker, design.md §5.8)
        override this.
        """
        await handle_job_recovery(envelope=envelope, job_store=self.job_store)
        if self.job_store is not None and is_valid_uuid(envelope.job_id):
            try:
                await self.job_store.acquire_lease(
                    organization_id=envelope.organization_id,
                    job_id=UUID(envelope.job_id),
                    lease_timeout_s=self.lease_timeout_s,
                )
            except Exception as lease_err:
                logger.warning("Failed to acquire lease for job %s: %s", envelope.job_id, lease_err)
```

In `_handle_message`, replace the block from `# 3.5 Re-deliver recovery` through the lease `except` (lines 311–326) with:

```python
                # 3.5 / 3.6 Redelivery recovery and claim lease (R19.5, R19.8), overridable
                await self.prepare_delivery(envelope)
```

and add `retry_after_s=self.retry_after_s(exc),` after `job_store=self.job_store,` in the `handle_job_transient_failure(...)` call (line 346).

Run: `uv run pytest tests/unit/test_retry_after_tier.py tests/unit/test_retry_and_dead_letter.py -v`
Expected: PASS (the existing retry/DLQ tests are unchanged in behaviour).

- [ ] **Step 4: Implement the dispatch-worker service**

Create `services/dispatch_worker/failure_policy.py`:

```python
"""Dispatch failure routing (tasks 6.5, 6.6; design.md §5.8 "errors").

Retryable (the job stays DISPATCHED; the broker retry ladder redelivers, a Retry-After
picks its tier): 429 / rate-limit 403 (RateLimited), 5xx and network faults (Transient) and
anything unknown. Permanent (DISPATCHED -> FAILED -> DEAD_LETTER with the provider error
kept on the job): 400 (Permanent), 404 on send (NotFound), auth failures (AuthExpired), a
null provider thread id, a provider draft that vanished without a sent copy, and a dispatch
key held by another draft. Requirements: R17.5, R18.2, R19.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from packages.adapters.exceptions import (
    AuthExpired,
    PermanentProviderError,
    RetryableProviderError,
)
from packages.broker.consumer import FatalError
from packages.db.dispatch import DispatchKeyConflictError
from packages.dispatch.reply import MissingProviderThreadError, MissingRecipientError
from packages.dispatch.service import DispatchPermanentError


class Disposition(StrEnum):
    """What the consumer does with a delivery whose dispatch failed."""

    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class FailureDecision:
    """A disposition and the reason recorded with it (the job's last_error on dead-letter)."""

    disposition: Disposition
    reason: str


_PERMANENT: tuple[type[BaseException], ...] = (
    DispatchPermanentError,
    MissingProviderThreadError,
    MissingRecipientError,
    DispatchKeyConflictError,
    PermanentProviderError,  # AuthExpired (handled first), NotFound, Permanent
    FatalError,
)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def classify_dispatch_failure(exc: BaseException) -> FailureDecision:
    """Map a dispatch failure to retry or dead-letter; unknown exceptions are retried."""
    if isinstance(exc, AuthExpired):
        return FailureDecision(
            Disposition.DEAD_LETTER,
            f"{_describe(exc)} (the mailbox credentials expired or were revoked; "
            "refresh the token, then replay the job)",
        )
    if isinstance(exc, _PERMANENT):
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    return FailureDecision(Disposition.RETRY, _describe(exc))


def provider_retry_after(exc: BaseException) -> float | None:
    """Seconds the provider asked us to wait (Retry-After), for retryable provider errors."""
    if not isinstance(exc, RetryableProviderError):
        return None
    return exc.retry_after_s
```

Create `services/dispatch_worker/consumer.py`:

```python
"""dispatch-worker consumer (tasks 6.5, 6.6): email.dispatch -> DispatchService.

The job carries its own claim (DRAFTED | RETRY_PENDING -> DISPATCHED inside the claim
transaction), so this consumer skips BaseConsumer's RETRY_PENDING -> GENERATING recovery and
takes no lease (design.md §5.8: the lease reaper skips DISPATCHED; redelivery is the
broker's). BaseConsumer keeps ack, the retry ladder (with the provider's Retry-After) and the
dead-letter path, which moves the job DISPATCHED -> FAILED -> DEAD_LETTER with the reason.

Two deliveries of one job never run together (R19.3): DispatchService holds a per-job lock,
and a delivery that finds it busy is re-published to the first retry tier with the same
attempt, then acked, without touching the job. Every dispatch failure first routes the job
to email.dispatch (R18.7), so an operator replay never regenerates the draft.
"""

from __future__ import annotations

import logging
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import resolve_origin_exchange
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import JobStore
from packages.dispatch.service import DispatchJobBusyError, DispatchService
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.dispatch_worker.failure_policy import (
    Disposition,
    classify_dispatch_failure,
    provider_retry_after,
)

logger = logging.getLogger(__name__)


class DispatchConsumer(BaseConsumer):
    """Consumes ``email.dispatch`` and dispatches each job's approved draft exactly once."""

    def __init__(
        self,
        queue_name: str,
        *,
        service: DispatchService,
        job_store: JobStore,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        super().__init__(
            queue_name=queue_name,
            broker_settings=broker_settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=job_store,
            metrics=metrics,
        )
        self.service = service

    async def prepare_delivery(self, envelope: JobEnvelope) -> None:
        """No recovery and no lease: the dispatch claim owns the job's state (design §5.8)."""
        return None

    def retry_after_s(self, exc: Exception) -> float | None:
        """The provider's Retry-After picks the retry tier (R17.5)."""
        return provider_retry_after(exc)

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        """Dispatch one job; route a failure to the retry ladder or the dead-letter queue."""
        try:
            organization_id = UUID(str(envelope.organization_id))
            job_id = UUID(str(envelope.job_id))
        except ValueError as err:
            raise FatalError(
                f"Dispatch envelope ids are not UUIDs: organization_id="
                f"{envelope.organization_id!r} job_id={envelope.job_id!r}"
            ) from err
        try:
            outcome = await self.service.dispatch(organization_id=organization_id, job_id=job_id)
        except DispatchJobBusyError as busy:
            await self._defer_busy(envelope, raw_message, str(busy))
            return
        except Exception as exc:
            await self._mark_dispatch_route(organization_id, job_id)
            decision = classify_dispatch_failure(exc)
            if decision.disposition is Disposition.DEAD_LETTER:
                logger.error("Dispatch of job %s failed permanently: %s", job_id, decision.reason)
                if isinstance(exc, FatalError):
                    raise
                raise FatalError(decision.reason) from exc
            logger.warning("Dispatch of job %s will be retried: %s", job_id, decision.reason)
            raise
        logger.info("Dispatch of job %s finished: %s", job_id, outcome.value)

    async def _defer_busy(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage, reason: str
    ) -> None:
        """Re-publish a delivery whose job another delivery is dispatching (R19.3).

        Same attempt number, first retry tier, no job transition: this delivery did no work.
        Returning normally lets BaseConsumer ack the original after the publish succeeded; if
        the publish raises, the delivery goes through the normal retry path instead.
        """
        if self._publisher is None:
            raise RuntimeError("consumer publisher is not initialised")
        await self._publisher.publish_to_retry(
            envelope,
            tier_delay_s=self.retry_settings.tier_1_delay_s,
            origin_exchange=resolve_origin_exchange(raw_message, self.broker_settings),
            origin_routing_key=raw_message.routing_key or self.queue_name,
            failure_reason=reason,
        )
        logger.info("Dispatch of job %s deferred: %s", envelope.job_id, reason)

    async def _mark_dispatch_route(self, organization_id: UUID, job_id: UUID) -> None:
        """Point the job's queue_name at email.dispatch before any retry or dead-letter."""
        try:
            await self.service.mark_dispatch_route(
                organization_id=organization_id, job_id=job_id
            )
        except Exception as route_err:  # never hide the dispatch failure behind this one
            logger.warning("Could not route job %s to email.dispatch: %s", job_id, route_err)
```

Create `services/dispatch_worker/main.py`:

```python
"""dispatch-worker entrypoint (tasks 6.5, 6.6; design.md §3.2, §5.8, ADR-0009).

One DispatchConsumer on ``email.dispatch`` runs DispatchService's five steps on the
Postgres dispatch store. The provider adapter is resolved per mailbox through the adapter
registry (R1.3), so this service names no provider. Process lifecycle (topology, /healthz,
/readyz, /metrics, graceful drain) comes from WorkerRuntime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.routing import load_categories_from_yaml
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import DispatchWorkerSettings
from packages.db.dispatch import PostgresDispatchStore
from packages.db.job import PostgresJobStore
from packages.dispatch.service import AdapterResolver, DispatchService
from services.dispatch_worker.consumer import DispatchConsumer

SERVICE_NAME = "dispatch_worker"
DEFAULT_PORT = 8006


def build_consumer(
    res: WorkerResources,
    *,
    service: DispatchService | None = None,
    adapter_resolver: AdapterResolver | None = None,
) -> DispatchConsumer:
    """Compose the dispatch consumer from shared resources.

    Tests inject ``service`` (unit) or ``adapter_resolver`` (the 6.9 end-to-end test, which
    hands every mailbox the fake adapter without patching the registry).
    """
    settings = res.settings
    queue = settings.broker.queue_dispatch
    dispatch = service
    if dispatch is None:
        store = PostgresDispatchStore(res.db_pool)
        dispatch = (
            DispatchService(store=store, adapter_for=adapter_resolver, dispatch_queue=queue)
            if adapter_resolver is not None
            else DispatchService(store=store, dispatch_queue=queue)
        )
    return DispatchConsumer(
        queue,
        service=dispatch,
        job_store=PostgresJobStore(res.db_pool),
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.dispatch_worker_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    """Load the category dispatch modes, then start the one dispatch consumer."""
    routing = res.settings.routing
    if routing.categories_config_path:
        load_categories_from_yaml(routing.categories_config_path)
    return [build_consumer(res).start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=DispatchWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
```

In `docker-compose.yml`, replace the whole `dispatch-worker:` block (lines 289–303) with:

```yaml
  dispatch-worker:
    build: *app-build
    restart: unless-stopped
    command: ["python", "-m", "services.dispatch_worker.main"]
    stop_grace_period: 30s
    expose:
      - "8006"
    environment:
      <<: *app-env
      SERVICE_NAME: dispatch_worker
      PORT: "8006"
      # The dispatch-worker calls the provider; the token is short-lived (ADR-0009).
      GMAIL_ACCESS_TOKEN: ${GMAIL_ACCESS_TOKEN:-}
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8006/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 20s
```

Add `"services.dispatch_worker.main",` after `"services.ai_worker.main",` in `scripts/image_smoke.py`, and after `"services.api.main",` in `ENTRYPOINTS` of `tests/unit/test_production_imports.py`.

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `uv run pytest tests/unit/test_retry_after_tier.py tests/unit/test_dispatch_worker.py tests/unit/test_runtime_image_contract.py tests/unit/test_production_imports.py tests/unit/test_dependency_rules.py tests/unit/test_retry_and_dead_letter.py -v`
Expected: PASS (no provider literal in `services/dispatch_worker`; the dispatch entrypoint imports without dev-only dependencies).

- [ ] **Step 6: Write the forced-redelivery integration tests**

Create `tests/integration/test_dispatch_worker_integration.py`:

```python
"""The dispatch-worker on a real broker and Postgres (tasks 6.5, 6.6, 6.7).

Review Focus (design §9, ADR-0009, R19.3): kill the worker after each dispatch step; the
redelivered job still ends with exactly one provider draft and at most one provider send.
Also: Retry-After keeps the job DISPATCHED and picks its ladder tier; a permanent provider
error dead-letters with the error kept; an operator replay goes RETRY_PENDING -> DISPATCHED
(never GENERATING); a republished dispatch after COMPLETED sends nothing; two deliveries of
one job at once draft and send once (the job lock defers one); a dispatch that dead-letters
before its claim still replays to the dispatch-worker. A killed worker runs on its own pool,
which the test terminates, so PostgreSQL frees its job lock as it would for a dead process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import (
    AppSettings,
    BrokerSettings,
    DispatchWorkerSettings,
    RetryLadderSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.dispatch import PostgresDispatchStore
from packages.db.draft import insert_draft
from packages.db.job import PostgresJobStore
from packages.dispatch.service import DispatchOutcome, DispatchService
from packages.domain import DispatchMode
from packages.domain.entities import GeneratedDraft, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics, get_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.dispatch_worker.consumer import DispatchConsumer
from services.dispatch_worker.main import build_consumer
from tests.integration.isolation import scratch_vhost
from tests.stubs.dispatch_fakes import RecordingFake, registry_with

FAST_RETRY = RetryLadderSettings(tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3)
SEND = DispatchMode.SEND_REPLY
DRAFT = DispatchMode.CREATE_DRAFT


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "dispatchsvc") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


@dataclass(frozen=True)
class Seed:
    org_id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    job_id: uuid.UUID
    draft_id: uuid.UUID


async def _seed_approved(pool: asyncpg.Pool, *, provider_thread_id: str | None = "th-live") -> Seed:
    """An approved billing draft on a DRAFTED job (what POST approve leaves behind)."""
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}")
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Acme Support', 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@acme.example",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id,"
            " message_count, last_message_at) VALUES ($1, $2, $3, $4, 1, now())",
            thread_id,
            org_id,
            mbx_id,
            f"{provider_thread_id}-{thread_id.hex[:6]}" if provider_thread_id else None,
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                rfc822_message_id, direction, sender_email, sender_name, recipients,
                subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'inbound', 'alice@customer.example', 'Alice',
                      '[]', 'Where is order 82915?', 'What is the status of order 82915?', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            f"orig-{msg_id.hex[:8]}@customer.example",
        )
        await conn.execute(
            """
            INSERT INTO classification_result (
                id, organization_id, message_id, category, priority, reply_required,
                retrieval_required, confidence, decided_by
            ) VALUES ($1, $2, $3, 'billing', 'normal', true, true, 0.9, 'rule')
            """,
            uuid.uuid4(),
            org_id,
            msg_id,
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.DRAFTED.value,
            idempotency_key=f"dispatchsvc-{uuid.uuid4()}",
        )
    )
    async with pool.acquire() as conn:
        draft = await insert_draft(
            conn,
            GeneratedDraft(
                organization_id=org_id,
                message_id=msg_id,
                thread_id=thread_id,
                job_id=job.id,
                subject="Re: Where is order 82915?",
                body="Order ORD-82915 was dispatched on 24 September.",
                status="approved",
            ),
        )
    return Seed(org_id, thread_id, msg_id, uuid.UUID(str(job.id)), draft.id)


async def _cleanup(pool: asyncpg.Pool, seed: Seed) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organization WHERE id = $1", seed.org_id)


async def _resources(broker: BrokerSettings, pool: asyncpg.Pool) -> WorkerResources:
    connection = await aio_pika.connect_robust(broker.url)
    return WorkerResources(
        settings=DispatchWorkerSettings(broker=broker, retry=FAST_RETRY),
        db_pool=pool,
        connection=connection,
        publisher=MessagePublisher(broker_settings=broker, connection=connection, retry_settings=FAST_RETRY),
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=create_pipeline_metrics(),
    )


def _service(
    pool: asyncpg.Pool,
    fake: RecordingFake,
    mode: DispatchMode,
    *,
    store: PostgresDispatchStore | None = None,
) -> DispatchService:
    return DispatchService(
        store=store or PostgresDispatchStore(pool),
        adapter_for=lambda _mailbox: fake,
        registry=registry_with(mode),
        confirm_recheck_delay_s=0,
    )


def _envelope(seed: Seed) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(seed.job_id),
        idempotency_key=f"dispatch-{seed.job_id}",
        job_type="dispatch",
        organization_id=str(seed.org_id),
        message_id=str(seed.message_id),
        thread_id=str(seed.thread_id),
    )


async def _publish(broker: BrokerSettings, seed: Seed, queue: str | None = None) -> None:
    routing_key = queue or broker.queue_dispatch
    exchange = broker.exchange_for_queue(routing_key)
    assert exchange is not None
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(exchange, routing_key, _envelope(seed))
    finally:
        await publisher.close()


async def _wait_for_state(pool: asyncpg.Pool, seed: Seed, state: JobState, timeout_s: float) -> Job:
    deadline = asyncio.get_running_loop().time() + timeout_s
    current: Job | None = None
    while asyncio.get_running_loop().time() < deadline:
        current = await PostgresJobStore(pool).get_job(seed.org_id, seed.job_id)
        if current is not None and current.state == state.value:
            return current
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {seed.job_id} ended {current.state if current else None}, expected {state.value}")


async def _event_states(
    pool: asyncpg.Pool,
    seed: Seed,
    *,
    event_types: tuple[str, ...] = ("state_transition",),
) -> list[str]:
    """``state_to`` of the job's events in order. ``PostgresJobStore.replay_job`` writes its
    RETRY_PENDING event as ``operator_replay``, so a replay test asks for both types."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT state_to FROM processing_event WHERE job_id = $1 AND organization_id = $2"
            " AND event_type = ANY($3::text[]) ORDER BY id",
            seed.job_id,
            seed.org_id,
            list(event_types),
        )
    return [r["state_to"] for r in rows]


async def _outbound_count(pool: asyncpg.Pool, seed: Seed) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM email_message WHERE organization_id = $1 AND thread_id = $2"
                " AND direction = 'outbound'",
                seed.org_id,
                seed.thread_id,
            )
        )


async def _doomed_pool() -> asyncpg.Pool:
    """A pool for the worker a test kills. ``terminate()`` drops its sessions the way a dead
    process does, so PostgreSQL releases that worker's per-job advisory lock (R19.3)."""
    return await create_pool_from_settings(AppSettings().database)


async def _kill(res: WorkerResources, pool: asyncpg.Pool) -> None:
    """The kill: the broker sees the connection drop with the delivery unacked and requeues
    it; the database sees the sessions end and frees the job lock. The worker's task stays
    parked at its crash point; stop() on its closed connection would block, so it is left as
    is (same pattern as the 4.13b ai-worker kill test)."""
    await res.connection.close()
    pool.terminate()


async def _run_until(consumer: DispatchConsumer, res: WorkerResources, pool: asyncpg.Pool, seed: Seed, state: JobState) -> Job:
    await consumer.start()
    try:
        return await _wait_for_state(pool, seed, state, timeout_s=20)
    finally:
        await consumer.stop()
        await res.connection.close()


class _FinishCrashStore(PostgresDispatchStore):
    """Kills the worker after the provider draft is recorded, before step 5 commits."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(pool)
        self.armed = True
        self.reached = asyncio.Event()
        self._hang = asyncio.Event()

    async def finish(
        self,
        *,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        draft_id: uuid.UUID,
        mode: DispatchMode,
        provider_ref: str,
        outbound: NormalizedMessage | None,
    ) -> Job:
        if self.armed:
            self.armed = False
            self.reached.set()
            await self._hang.wait()
        return await super().finish(
            organization_id=organization_id,
            job_id=job_id,
            draft_id=draft_id,
            mode=mode,
            provider_ref=provider_ref,
            outbound=outbound,
        )


class _RecordingService(DispatchService):
    """Records every dispatch outcome so a test can wait for a specific delivery."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.outcomes: list[DispatchOutcome] = []

    async def dispatch(self, *, organization_id: uuid.UUID, job_id: uuid.UUID) -> DispatchOutcome:
        outcome = await super().dispatch(organization_id=organization_id, job_id=job_id)
        self.outcomes.append(outcome)
        return outcome


async def test_create_draft_mode_completes_with_one_provider_draft(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    res = await _resources(broker, pool)
    try:
        consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
        await _publish(broker, seed)
        job = await _run_until(consumer, res, pool, seed, JobState.COMPLETED)
        assert job.queue_name == broker.queue_dispatch
        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == 0
        assert await _outbound_count(pool, seed) == 0
        assert (await _event_states(pool, seed))[-2:] == ["DISPATCHED", "COMPLETED"]
    finally:
        await _cleanup(pool, seed)


@pytest.mark.parametrize(
    ("crash_at", "mode"),
    [
        ("before_create_draft", DRAFT),
        ("before_create_draft", SEND),
        ("after_create_draft", DRAFT),  # provider holds the draft, its id is not recorded
        ("after_create_draft", SEND),
        ("before_send_draft", SEND),
        ("after_send_draft", SEND),
    ],
)
async def test_worker_killed_after_each_step_sends_exactly_once(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    crash_at: str,
    mode: DispatchMode,
) -> None:
    """R19.3 / ADR-0009 / tasks.md 6.5: kill -> broker redelivery -> exactly one provider
    draft (after_create_draft: the redelivery adopts it through find_draft) and at most one
    send."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.crash_at = crash_at
    pool_a = await _doomed_pool()
    try:
        res_a = await _resources(broker, pool_a)
        worker_a = build_consumer(res_a, service=_service(pool_a, fake, mode))
        await worker_a.start()
        await _publish(broker, seed)
        await asyncio.wait_for(fake.crash_reached.wait(), timeout=15)
        await _kill(res_a, pool_a)

        res_b = await _resources(broker, pool)
        worker_b = build_consumer(res_b, service=_service(pool, fake, mode))
        await _run_until(worker_b, res_b, pool, seed, JobState.COMPLETED)

        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == (1 if mode is SEND else 0)
        assert await _outbound_count(pool, seed) == (1 if mode is SEND else 0)
        if crash_at == "after_create_draft":
            assert fake.calls["find_draft"] == 1
    finally:
        await _cleanup(pool, seed)


async def test_worker_killed_before_finish_resumes_without_a_second_draft(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Crash after step 2 committed (draft handle stored), before step 5: reuse, never recreate."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    pool_a = await _doomed_pool()
    crashing = _FinishCrashStore(pool_a)
    try:
        res_a = await _resources(broker, pool_a)
        worker_a = build_consumer(res_a, service=_service(pool_a, fake, DRAFT, store=crashing))
        await worker_a.start()
        await _publish(broker, seed)
        await asyncio.wait_for(crashing.reached.wait(), timeout=15)
        await _kill(res_a, pool_a)

        res_b = await _resources(broker, pool)
        worker_b = build_consumer(res_b, service=_service(pool, fake, DRAFT))
        await _run_until(worker_b, res_b, pool, seed, JobState.COMPLETED)

        assert fake.calls["create_draft"] == 1
    finally:
        await _cleanup(pool, seed)


async def test_retry_after_keeps_the_job_dispatched_and_picks_its_tier(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """R17.5: a 429 with Retry-After 2.5 s goes to the 3 s tier; the job never leaves DISPATCHED."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.inject_rate_limit(retry_after=2.5)
    retries = get_metrics().retry_jobs_total.labels(queue=broker.queue_dispatch, tier="3s")
    before = retries._value.get()
    res = await _resources(broker, pool)
    try:
        consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
        await _publish(broker, seed)
        await _run_until(consumer, res, pool, seed, JobState.COMPLETED)
        assert retries._value.get() == before + 1
        states = await _event_states(pool, seed)
        assert JobState.RETRY_PENDING.value not in states
        assert states[-2:] == ["DISPATCHED", "COMPLETED"]
        assert fake.calls["create_draft"] == 1
    finally:
        await _cleanup(pool, seed)


async def test_permanent_error_dead_letters_then_operator_replay_completes(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """R17.5 / R18.7: DISPATCHED -> FAILED -> DEAD_LETTER with the error; replay -> DISPATCHED."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.inject_permanent_failure("400 Bad Request: invalid To header")
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
    await consumer.start()
    try:
        await _publish(broker, seed)
        dead = await _wait_for_state(pool, seed, JobState.DEAD_LETTER, timeout_s=20)
        assert dead.last_error is not None and "invalid To header" in dead.last_error
        dlq = await channel.declare_queue(broker.queue_dead_letter, passive=True)
        message = await dlq.get(no_ack=True, fail=True, timeout=5)
        assert "invalid To header" in str((message.headers or {}).get("x-failure-reason"))

        replayed, _ = await PostgresJobStore(pool).replay_job(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            payload={"operator_replay": True, "reason": "fixed recipient"},
        )
        assert replayed.queue_name == broker.queue_dispatch  # written by the claim
        await _publish(broker, seed, queue=replayed.queue_name)
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)

        states = await _event_states(
            pool, seed, event_types=("state_transition", "operator_replay")
        )
        assert JobState.GENERATING.value not in states
        replay_at = states.index(JobState.RETRY_PENDING.value)
        assert states[replay_at + 1 :] == ["DISPATCHED", "COMPLETED"]
        assert fake.calls["create_draft"] == 1
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, seed)


async def test_null_provider_thread_is_dead_lettered_without_provider_calls(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """6.3 / 6.6: a thread without a provider id cannot be replied to in-thread."""
    seed = await _seed_approved(pool, provider_thread_id=None)
    fake = RecordingFake()
    res = await _resources(broker, pool)
    try:
        consumer = build_consumer(res, service=_service(pool, fake, SEND))
        await _publish(broker, seed)
        dead = await _run_until(consumer, res, pool, seed, JobState.DEAD_LETTER)
        assert dead.last_error is not None and "MissingProviderThreadError" in dead.last_error
        assert sum(fake.calls.values()) == 0
    finally:
        await _cleanup(pool, seed)


async def test_republished_dispatch_after_completion_sends_nothing(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Phase 6 gate item 4 in CI form: replaying the dispatch job sends nothing further."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    service = _RecordingService(
        store=PostgresDispatchStore(pool),
        adapter_for=lambda _mailbox: fake,
        registry=registry_with(SEND),
        confirm_recheck_delay_s=0,
    )
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=service)
    await consumer.start()
    try:
        await _publish(broker, seed)
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)
        await _publish(broker, seed)
        deadline = asyncio.get_running_loop().time() + 20
        while len(service.outcomes) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
        assert service.outcomes == [DispatchOutcome.COMPLETED_SENT, DispatchOutcome.ALREADY_DONE]
        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == 1
        assert await _outbound_count(pool, seed) == 1
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, seed)


@pytest.mark.parametrize("mode", [DRAFT, SEND])
async def test_two_deliveries_of_one_job_at_once_draft_and_send_once(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool, mode: DispatchMode
) -> None:
    """R19.3: a double approve publishes two envelopes for one job; with prefetch > 1 both
    are delivered together. The job lock defers the second, which then finds COMPLETED."""
    seed = await _seed_approved(pool)
    fake = RecordingFake()
    fake.pause_at = "before_create_draft"  # hold delivery 1 inside the lock
    service = _RecordingService(
        store=PostgresDispatchStore(pool),
        adapter_for=lambda _mailbox: fake,
        registry=registry_with(mode),
        confirm_recheck_delay_s=0,
    )
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=service)
    assert consumer.prefetch_count > 1
    await consumer.start()
    try:
        await _publish(broker, seed)
        await _publish(broker, seed)
        await asyncio.wait_for(fake.paused.wait(), timeout=15)
        await asyncio.sleep(1.0)  # delivery 2 arrives, finds the lock busy, is deferred
        fake.resume.set()
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)
        deadline = asyncio.get_running_loop().time() + 20
        while len(service.outcomes) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
        assert sorted(service.outcomes) == sorted(
            [
                DispatchOutcome.COMPLETED_SENT if mode is SEND else DispatchOutcome.COMPLETED_DRAFT,
                DispatchOutcome.ALREADY_DONE,
            ]
        )
        assert fake.calls["create_draft"] == 1
        assert fake.calls["send_draft"] == (1 if mode is SEND else 0)
        assert await _outbound_count(pool, seed) == (1 if mode is SEND else 0)
        job = await PostgresJobStore(pool).get_job(seed.org_id, seed.job_id)
        assert job is not None and job.state == JobState.COMPLETED.value
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, seed)


async def test_key_conflict_dead_letters_and_the_replay_reaches_the_dispatch_worker(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """R18.7 / tasks.md 6.5: a dispatch that fails before its claim commits (the key rolls the
    claim back) still leaves queue_name = email.dispatch, so the replay never regenerates."""
    seed = await _seed_approved(pool)
    other = await _seed_approved(pool)
    fake = RecordingFake()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE processing_job SET queue_name = 'email.triage'"
            " WHERE id = $1 AND organization_id = $2",
            seed.job_id,
            seed.org_id,
        )
        # Another draft already holds this job's dispatch key, so the claim raises
        # DispatchKeyConflictError and rolls back (queue_name included).
        key = derive_idempotency_key(
            organization_id=seed.org_id,
            mailbox_id=await conn.fetchval(
                "SELECT mailbox_id FROM email_message WHERE id = $1", seed.message_id
            ),
            provider_message_id=f"prov-{seed.message_id.hex[:8]}",
            operation_type="dispatch",
        )
        await conn.execute(
            "UPDATE generated_draft SET dispatch_idempotency_key = $1 WHERE id = $2",
            key,
            other.draft_id,
        )
    res = await _resources(broker, pool)
    consumer = build_consumer(res, service=_service(pool, fake, DRAFT))
    await consumer.start()
    try:
        await _publish(broker, seed)
        dead = await _wait_for_state(pool, seed, JobState.DEAD_LETTER, timeout_s=20)
        assert dead.last_error is not None and "DispatchKeyConflictError" in dead.last_error
        assert dead.queue_name == broker.queue_dispatch
        assert sum(fake.calls.values()) == 0

        async with pool.acquire() as conn:  # the operator frees the key, then replays
            await conn.execute(
                "UPDATE generated_draft SET dispatch_idempotency_key = NULL WHERE id = $1",
                other.draft_id,
            )
        replayed, _ = await PostgresJobStore(pool).replay_job(
            organization_id=seed.org_id,
            job_id=seed.job_id,
            payload={"operator_replay": True, "reason": "freed the dispatch key"},
        )
        assert replayed.queue_name == broker.queue_dispatch
        await _publish(broker, seed, queue=replayed.queue_name)
        await _wait_for_state(pool, seed, JobState.COMPLETED, timeout_s=20)
        states = await _event_states(
            pool, seed, event_types=("state_transition", "operator_replay")
        )
        assert JobState.GENERATING.value not in states
        assert fake.calls["create_draft"] == 1
    finally:
        await consumer.stop()
        await res.connection.close()
        await _cleanup(pool, other)
        await _cleanup(pool, seed)
```

- [ ] **Step 7: Run the integration tests and the whole gate**

Run: `uv run pytest tests/integration/test_dispatch_worker_integration.py -v`
Expected: PASS (15 tests: 1 + 6 parametrized crash cases + 5 + 2 concurrent-delivery cases + 1 key-conflict replay). Each crash case prints nothing extra; a failure in `after_send_draft` with `DispatchPermanentError: ... holds no sent message` means `build_outbound_reply` does not derive `message_id` deterministically from the draft (see "Contract deviations" at the top of this plan).

Run: `make ci`
Expected: `fmt-check`, `lint` (ruff + mypy on packages services tests evaluation), `test-unit` and `test-integration` all pass.

- [ ] **Step 8: Commit**

```bash
git add packages/broker/backoff.py packages/broker/retry.py packages/broker/consumer.py services/dispatch_worker/failure_policy.py services/dispatch_worker/consumer.py services/dispatch_worker/main.py docker-compose.yml scripts/image_smoke.py tests/unit/test_production_imports.py tests/unit/test_retry_after_tier.py tests/unit/test_dispatch_worker.py tests/unit/test_runtime_image_contract.py tests/integration/test_dispatch_worker_integration.py
git commit -m "feat(dispatch-worker): email.dispatch consumer with Retry-After tiers and crash-safe redelivery [task 6.5, 6.6] [R17.3, R17.4, R17.5, R18.3, R18.7, R19.2, R19.3]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

The owner restarts the stack (`make up`) to run the new `dispatch-worker` container; that live step is not part of this task.

---

## Part D (Tasks 9–10): Review UI (tasks.md 6.8)

Skills applied while writing this part: `fullstack-dev-skills:python-pro` (strict typing, dataclasses, pytest), `fullstack-dev-skills:fastapi-expert` (Annotated dependencies, Form/UploadFile, exception handlers), `fullstack-dev-skills:playwright-expert` (role/label locators, auto-waiting `expect`, no fixed sleeps in the browser, independent tests), `design:accessibility-review` (WCAG 2.2 AA basics: 2.4.7/2.4.11 visible focus, 2.5.8 target size ≥ 24 px, 4.1.3 status messages, 1.3.1 labels and landmarks, 1.4.3 contrast).

Verified against current upstream while writing: htmx **2.0.11** is the npm `latest` (`hx-vals='js:{…}'`, `hx-swap-oob`, `hx-on::after-request`, `HX-Reswap: none` still processes out-of-band swaps); Playwright for Python **1.63.0** on PyPI; Starlette 1.6.0 in `.venv` takes the request first in `TemplateResponse(request, name, context)`; uvicorn 0.53.0 skips signal handlers off the main thread (`Server.capture_signals`) and exposes `Server.started`.

**What part D builds:**

```
 browser ── http://127.0.0.1:3001 (compose publishes on loopback only; no login, ADR-0009)
   │  GET  /drafts                      pending-draft queue            (Task 9)
   │  GET  /drafts/{id}                 original email · thread summary · [BUSINESS DATA] facts
   │                                    · draft with citations beside sentences
   │  POST /drafts/{id}/edit|approve|reject   htmx; review_ms = performance.now() delta (review.js)
   │  GET  /messages/{id}/timeline      processing_event timeline, polls every 5 s   (Task 10)
   │  GET/POST /knowledge               upload + per-document status, rows poll every 3 s (Task 10)
   ▼
 frontend service  services/frontend/  FastAPI + Jinja2 + htmx (vendored static/htmx.min.js)
   │  no database, no broker. ReviewApiClient ─ httpx ─ header X-Organization-Id: FRONTEND__ORGANIZATION_ID
   │  base URL FRONTEND__API_BASE_URL (compose: http://api:8000)
   ▼
 api  http://127.0.0.1:8000   /v1/drafts … (Task 6, 6.1) · /v1/messages/{id}[/timeline] · /v1/knowledge/documents
   │                                   │
   ▼                                   ▼
 Postgres: generated_draft, feedback, processing_event        email.dispatch (approve publishes, Task 6)

 Edit flow:    textarea changed ─▶ frontend PATCH /v1/drafts/{id} {"body"} ─▶ POST …/approve {"review_ms","reviewer"}
 Approve flow: POST …/approve {"review_ms","reviewer"}           ─▶ re-render panel + live-region "Draft approved."
```

**Global constraints for part D (in addition to the plan's):**
- The UI calls only `/v1` over HTTP. `services/frontend` never imports `services.api`, `packages.db` or `packages.broker`; Task 9 adds a dependency-rule test for it.
- No provider literals (`gmail`, `graph`, `imap`) anywhere under `services/frontend` (`tests/unit/test_dependency_rules.py`).
- No CDN: htmx is vendored at a pinned version and hash; the UI must work on a machine with no internet.
- Every page works without JavaScript (plain form posts, 303 back to the page); htmx only enhances.
- Tests need no credentials and no live stack: unit tests use recorded `/v1` responses (`httpx.MockTransport`) or the real API app with in-memory stores (`httpx.ASGITransport`); the Playwright tests run the real frontend and the real API against the isolated `rag_email_test` database (R24.5).
- The UI has no login and must stay on loopback (ADR-0009, GEMINI.md §6): no authentication, CSRF or session code is added.

**File structure (part D):**

```
services/frontend/
  __init__.py              package docstring
  main.py                  create_app(settings, *, transport, configure_logging) + module-level `app`
  api_client.py            ReviewApiClient, response models, ApiError, build_http_client
  annotate.py              pure: sentences, citation placement, [BUSINESS DATA] highlight
  web.py                   shared route helpers + ApiError handler + require_organization
  drafts.py                queue, detail, edit/approve/reject, resolve_review_ms        (Task 9)
  timeline.py              /messages/{id}/timeline                                       (Task 10)
  knowledge.py             /knowledge upload + status rows                               (Task 10)
  templates/base.html, _status.html, error.html
  templates/drafts/queue.html, detail.html, _panel.html                                  (Task 9)
  templates/timeline/message.html, _events.html                                          (Task 10)
  templates/knowledge/index.html, _documents.html, _row.html                             (Task 10)
  static/htmx.min.js (2.0.11, vendored), review.css, review.js
tests/unit/test_frontend_annotate.py, test_frontend_review_ui.py                          (Task 9)
tests/unit/test_frontend_timeline_knowledge.py                                            (Task 10)
tests/e2e/review_stack.py, conftest.py, test_review_ui_flows.py, test_review_ui_accessibility.py (Task 10)
```

---

### Task 9: Review UI part 1 — frontend service, settings, draft queue, draft review and decisions [tasks.md 6.8]

**Files:**
- Create: `services/frontend/__init__.py`, `services/frontend/main.py`, `services/frontend/api_client.py`, `services/frontend/annotate.py`, `services/frontend/web.py`, `services/frontend/drafts.py`
- Create: `services/frontend/templates/base.html`, `services/frontend/templates/_status.html`, `services/frontend/templates/error.html`, `services/frontend/templates/drafts/queue.html`, `services/frontend/templates/drafts/detail.html`, `services/frontend/templates/drafts/_panel.html`
- Create: `services/frontend/static/htmx.min.js` (downloaded, pinned), `services/frontend/static/review.css`, `services/frontend/static/review.js`
- Modify: `packages/core/settings.py` (import `UUID` at the top; add `FrontendSettings` after `BusinessDataSettings`, ~line 373; mount `frontend` on `AppSettings` after `business_data`, ~line 767; add `FrontendServiceSettings` after `DispatchWorkerSettings`, end of file)
- Modify: `docker-compose.yml` (api `ports` ~line 178-179; `frontend` service ~lines 305-322)
- Modify: `Dockerfile` (comment above `CMD`, ~lines 52-54)
- Modify: `.dockerignore` (delete the bare `frontend` line, line 28); Delete: `frontend/.gitkeep` (empty root directory the design tree predates)
- Modify: `scripts/image_smoke.py` (entrypoint tuple, ~line 13)
- Modify: `.env.example` (append section 21), `docs/configuration.md` (append §2.21), `README.md` (§7.5 consoles list)
- Modify: `specs/design.md` §4 tree (~lines 124-143): move `frontend/` under `services/` and add `packages/dispatch/` (see Step 17; divergence recorded as open question D1 at the end of this plan)
- Test (create): `tests/unit/test_frontend_annotate.py`, `tests/unit/test_frontend_review_ui.py`
- Test (modify): `tests/unit/test_settings.py` (imports + 3 tests), `tests/unit/test_runtime_image_contract.py` (`_runtime_assets` + 2 compose tests), `tests/unit/test_dependency_rules.py` (1 test), `tests/unit/test_production_imports.py` (`ENTRYPOINTS`, line 25)

**Interfaces:**
- Consumes (Task 6, tasks.md 6.1 — JSON over HTTP; the names below are exactly what `services/api/schemas/drafts.py` emits; unknown fields are ignored):
  - `GET /v1/drafts?status=draft&limit=25[&category=…][&cursor=…]` → `{"items": [DraftSummary], "next_cursor": str | null, "limit": int}`; `DraftSummary` = `id, job_id, message_id, thread_id, mailbox_id, category, status, subject, confidence, created_at` (plus fields the UI ignores)
  - `GET /v1/drafts/{id}` → `DraftSummary` + `body: str, citation_mismatch: bool, job_state: str | null, dispatch_mode: str | null, original: {message_id, sender_email, sender_name, subject, body_text, received_at, rfc822_message_id}, thread_summary: str | null, cited_chunks: [{citation_id, chunk_id, document_id, external_id, content, heading_path}], business_data: {customer_status, degraded, facts: [{entity, reference, status, reason}]} | null`. `api_client.py` maps these onto the UI models (`original_message`, `citations` with `title`/`text`, `business_facts`) in one place.
  - `PATCH /v1/drafts/{id}` body `{"body": str}` (2xx)
  - `POST /v1/drafts/{id}/approve` body `{"review_ms": int, "reviewer": str | null}` (2xx)
  - `POST /v1/drafts/{id}/reject` body `{"review_ms": int, "reviewer": str | null, "comment": str | null}` (2xx)
  - Errors: the existing `APIErrorResponse` `{"error", "code", …}` (`services/api/errors.py:23`)
  - `packages.observability.health.HealthRegistry`, `create_health_router`; `packages.observability.logging.setup_logging`
- Produces:
  - `class FrontendSettings(BaseModel)`: `api_base_url: str = "http://localhost:8000"` (http/https, trailing `/` dropped), `organization_id: UUID | None = None` (blank string ⇒ `None`); `AppSettings.frontend`; `class FrontendServiceSettings(AppSettings)` with `service_name = "frontend"`
  - `services.frontend.api_client`: `ORG_HEADER = "X-Organization-Id"`, `class ApiError(Exception)(status_code: int, code: str, message: str)`, models `DraftSummary`, `DraftPage`, `OriginalMessage`, `CitedChunk`, `BusinessFactView`, `DraftDetail`; `def build_http_client(settings: FrontendSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient`; `class ReviewApiClient(http)`: `list_drafts(*, status="draft", category=None, cursor=None, limit=25) -> DraftPage`, `get_draft(draft_id) -> DraftDetail`, `update_draft_body(draft_id, body) -> None`, `approve_draft(draft_id, *, review_ms, reviewer) -> None`, `reject_draft(draft_id, *, review_ms, reviewer, comment) -> None`, `aclose()`
  - `services.frontend.annotate`: `MIN_SHARED_TERMS = 2`, `Segment`, `AnnotatedSentence`, `AnnotatedDraft(paragraphs, unplaced, numbers)`, `split_paragraphs(body) -> list[list[str]]`, `highlight_facts(text, facts) -> list[Segment]`, `annotate_draft(body, citations, facts) -> AnnotatedDraft`
  - `services.frontend.web`: `require_organization`, `api_client`, `templates`, `is_htmx`, `optional_text`, `api_error_handler`
  - `services.frontend.drafts`: `drafts_router`, `NOTICES`, `normalize_body`, `resolve_review_ms(review_ms, rendered_at_ms, *, now) -> int`, `now_ms()`
  - `services.frontend.main`: `TEMPLATES_DIR`, `STATIC_DIR`, `create_app(settings: FrontendServiceSettings | None = None, *, transport: httpx.AsyncBaseTransport | None = None, configure_logging: bool = True) -> FastAPI`, module-level `app`
  - Routes: `GET /` (303 → `/drafts`), `GET /drafts`, `GET /drafts/{id}[?notice=saved|approved|rejected]`, `POST /drafts/{id}/edit`, `POST /drafts/{id}/approve`, `POST /drafts/{id}/reject`, `GET /healthz`, `GET /readyz` (readiness check `api` = `GET {api}/healthz`), `GET /metrics`, `/static/*`

- [ ] **Step 1: Write the failing settings tests**

In `tests/unit/test_settings.py`, add `from uuid import UUID` after `import json`, and add `FrontendServiceSettings,` and `FrontendSettings,` to the `from packages.core.settings import (...)` block (alphabetically, after `EmailWorkerSettings,`). Append:

```python
def test_frontend_settings_defaults_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R23.6 / R20.6: the frontend group reads FRONTEND__*; a blank organization is unset."""
    defaults = AppSettings(_env_file=None).frontend
    assert defaults.api_base_url == "http://localhost:8000"
    assert defaults.organization_id is None

    monkeypatch.setenv("FRONTEND__API_BASE_URL", "http://api:8000/")
    monkeypatch.setenv("FRONTEND__ORGANIZATION_ID", "00000000-0000-0000-0000-000000000001")
    overridden = AppSettings(_env_file=None).frontend
    assert overridden.api_base_url == "http://api:8000"
    assert overridden.organization_id == UUID("00000000-0000-0000-0000-000000000001")

    monkeypatch.setenv("FRONTEND__ORGANIZATION_ID", "")
    assert AppSettings(_env_file=None).frontend.organization_id is None


def test_frontend_settings_reject_bad_values() -> None:
    """R20.6: fail fast on a base URL without a scheme or an organization that is not a UUID."""
    with pytest.raises(ValidationError, match="api_base_url"):
        FrontendSettings(api_base_url="localhost:8000")
    with pytest.raises(ValidationError, match="organization_id"):
        FrontendSettings(organization_id="not-a-uuid")


def test_frontend_service_settings_name() -> None:
    assert FrontendServiceSettings(_env_file=None).service_name == "frontend"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_settings.py -v`
Expected: FAIL — `ImportError: cannot import name 'FrontendServiceSettings' from 'packages.core.settings'`.

- [ ] **Step 3: Implement the settings group**

In `packages/core/settings.py`, add `from uuid import UUID` after `from typing import Any`. Insert after the `BusinessDataSettings` class (before `class TriageSettings`):

```python
class FrontendSettings(BaseModel):
    """Review UI connection to the /v1 API (R23.4-R23.7, design.md §5.8, ADR-0009)."""

    api_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the API the review UI calls; only /v1 paths are used (R23.6)",
    )
    organization_id: UUID | None = Field(
        default=None,
        description=(
            "Tenant whose drafts the review UI shows, sent as the X-Organization-Id header "
            "(R23.6); unset renders a setup error instead of calling the API"
        ),
    )

    @field_validator("api_base_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("api_base_url must start with http:// or https://")
        return cleaned

    @field_validator("organization_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value
```

In `AppSettings`, after `business_data: BusinessDataSettings = Field(default_factory=BusinessDataSettings)`:

```python
    frontend: FrontendSettings = Field(default_factory=FrontendSettings)
```

At the end of the file, after `DispatchWorkerSettings`:

```python
class FrontendServiceSettings(AppSettings):
    """Settings specialized for the review UI service (design.md §5.8, ADR-0009)."""

    service_name: str = "frontend"
```

- [ ] **Step 4: Run the settings tests**

Run: `uv run pytest tests/unit/test_settings.py -v`
Expected: PASS (all tests, including the three new ones).

- [ ] **Step 5: Write the failing annotation tests**

Create `tests/unit/test_frontend_annotate.py`:

```python
"""Pure draft annotation for the review screen (task 6.8; R23.4, R13.5, R16.5)."""

from __future__ import annotations

from services.frontend.annotate import (
    AnnotatedSentence,
    Segment,
    annotate_draft,
    highlight_facts,
    split_paragraphs,
)
from services.frontend.api_client import BusinessFactView, CitedChunk


def _chunk(citation_id: str, text: str | None = None, chunk_id: str | None = None) -> CitedChunk:
    return CitedChunk(
        citation_id=citation_id, chunk_id=chunk_id, title=f"Doc {citation_id}", text=text
    )


def _plain(sentence: AnnotatedSentence) -> str:
    return "".join(segment.text for segment in sentence.segments)


def _order_fact(reference: str = "ORD-82915") -> BusinessFactView:
    return BusinessFactView(entity="order", reference=reference, status="FOUND")


def test_paragraphs_and_sentences_are_preserved() -> None:
    assert split_paragraphs("Hello Alice.\n\nYour order shipped. It arrives soon.\n") == [
        ["Hello Alice."],
        ["Your order shipped.", "It arrives soon."],
    ]
    assert split_paragraphs("   ") == []


def test_inline_marker_places_the_citation_and_is_removed_from_the_text() -> None:
    draft = annotate_draft(
        "We refund within 14 days [CITATION: KB-7]. Thanks for waiting.", [_chunk("kb-7")], []
    )
    first, second = draft.paragraphs[0]
    assert _plain(first) == "We refund within 14 days."
    assert [c.citation_id for c in first.citations] == ["kb-7"]
    assert second.citations == []
    assert draft.unplaced == []


def test_marker_after_the_full_stop_stays_with_its_sentence() -> None:
    draft = annotate_draft(
        "Returns are free. [CITATION: kb-1] Contact us anytime.", [_chunk("kb-1")], []
    )
    first, second = draft.paragraphs[0]
    assert _plain(first) == "Returns are free."
    assert [c.citation_id for c in first.citations] == ["kb-1"]
    assert _plain(second) == "Contact us anytime."


def test_marker_may_name_the_chunk_id_alias() -> None:
    chunk = _chunk("kb-9", chunk_id="c-9")
    draft = annotate_draft("Refunds take a week [CITATION: c-9].", [chunk], [])
    assert [c.citation_id for c in draft.paragraphs[0][0].citations] == ["kb-9"]


def test_without_a_marker_the_citation_goes_to_the_sentence_sharing_most_terms() -> None:
    body = (
        "Your parcel left on Monday. "
        "Refunds are issued within fourteen days after the return reaches our warehouse."
    )
    chunk = _chunk(
        "kb-2",
        text=(
            "Refunds are issued within fourteen days after the returned item "
            "reaches the warehouse."
        ),
    )
    draft = annotate_draft(body, [chunk], [])
    first, second = draft.paragraphs[0]
    assert first.citations == []
    assert [c.citation_id for c in second.citations] == ["kb-2"]


def test_a_citation_with_no_support_is_listed_as_unplaced() -> None:
    weak = _chunk("kb-3", text="Patience is appreciated in queues.")  # shares one term only
    no_text = _chunk("kb-4")
    draft = annotate_draft("Thanks for your patience.", [weak, no_text], [])
    assert draft.paragraphs[0][0].citations == []
    assert [c.citation_id for c in draft.unplaced] == ["kb-3", "kb-4"]


def test_citation_numbers_follow_api_order_and_ignore_duplicates() -> None:
    draft = annotate_draft("Text.", [_chunk("b"), _chunk("a"), _chunk("b")], [])
    assert draft.numbers == {"b": 1, "a": 2}
    assert [c.citation_id for c in draft.unplaced] == ["b", "a"]


def test_business_references_are_highlighted_case_insensitively_on_whole_tokens() -> None:
    segments = highlight_facts("Order ord-82915 shipped; ORD-829150 did not.", [_order_fact()])
    assert segments == [
        Segment("Order "),
        Segment("ord-82915", fact_reference="ORD-82915"),
        Segment(" shipped; ORD-829150 did not."),
    ]


def test_no_facts_leaves_the_text_whole() -> None:
    assert highlight_facts("Plain sentence.", []) == [Segment("Plain sentence.")]
    fact_without_reference = BusinessFactView(entity="order", reference=None, status="NOT_FOUND")
    assert highlight_facts("Plain.", [fact_without_reference]) == [Segment("Plain.")]


def test_annotate_highlights_facts_inside_sentences() -> None:
    draft = annotate_draft("Your order ORD-82915 was dispatched.", [], [_order_fact()])
    assert draft.paragraphs[0][0].segments == [
        Segment("Your order "),
        Segment("ORD-82915", fact_reference="ORD-82915"),
        Segment(" was dispatched."),
    ]
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_frontend_annotate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.frontend'`.

- [ ] **Step 7: Implement the API client and the annotation module**

Create `services/frontend/__init__.py`:

```python
"""Review UI service: server-rendered pages over the /v1 API (R23.4, R23.5, R23.7, ADR-0009)."""
```

Create `services/frontend/api_client.py`:

```python
"""HTTP client for the review UI: the only way the frontend reaches the system.

Requirements: R23.4, R23.6; design.md §5.8 "Review UI"; ADR-0009.

The frontend never imports services.api or the database layer: it calls /v1 over HTTP and
sends the configured tenant as the X-Organization-Id header (R23.6). Response models ignore
unknown fields, so an additive API change never breaks a page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from packages.core.settings import FrontendSettings

ORG_HEADER = "X-Organization-Id"
API_TIMEOUT_S = 10.0
DRAFT_PAGE_SIZE = 25


class ApiError(Exception):
    """A /v1 call that did not succeed: HTTP status, API error code and message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DraftSummary(_ApiModel):
    """One pending-queue row (GET /v1/drafts item)."""

    id: UUID
    job_id: UUID | None = None
    message_id: UUID | None = None
    thread_id: UUID | None = None
    mailbox_id: UUID | None = None
    category: str | None = None
    status: str
    subject: str | None = None
    confidence: float | None = None
    created_at: datetime | None = None


class DraftPage(_ApiModel):
    """A cursor page of drafts (R23.6)."""

    items: list[DraftSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class OriginalMessage(_ApiModel):
    """The inbound email the draft answers (the API's `original` object)."""

    id: UUID | None = Field(default=None, validation_alias=AliasChoices("message_id", "id"))
    sender_email: str | None = None
    sender_name: str | None = None
    subject: str = ""
    body_text: str = ""
    received_at: datetime | None = None


class CitedChunk(_ApiModel):
    """A knowledge chunk the draft cites (verified by packages/llm/citations.py, R16.5)."""

    citation_id: str
    chunk_id: str | None = None
    document_id: str | None = None
    title: str | None = None
    text: str | None = Field(default=None, validation_alias=AliasChoices("content", "text"))

    @model_validator(mode="before")
    @classmethod
    def title_from_headings(cls, data: Any) -> Any:
        """The API sends `heading_path` and `external_id`; the UI shows one title."""
        if isinstance(data, dict) and not data.get("title"):
            headings = [str(h) for h in data.get("heading_path") or [] if h]
            title = " › ".join(headings) or data.get("external_id")
            if title:
                data = {**data, "title": title}
        return data


class BusinessFactView(_ApiModel):
    """One [BUSINESS DATA] fact the draft was generated with (R13.5)."""

    entity: str
    reference: str | None = None
    status: str
    reason: str | None = None


class DraftDetail(DraftSummary):
    """GET /v1/drafts/{id}: everything a reviewer needs on one screen (R23.4)."""

    body: str = ""
    citation_mismatch: bool = False
    job_state: str | None = None
    dispatch_mode: str | None = None
    original_message: OriginalMessage | None = Field(
        default=None, validation_alias=AliasChoices("original", "original_message")
    )
    thread_summary: str | None = None
    # The API's `citations` are raw records; the chunks with text arrive as `cited_chunks`.
    citations: list[CitedChunk] = Field(
        default_factory=list, validation_alias=AliasChoices("cited_chunks")
    )
    business_facts: list[BusinessFactView] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def flatten_business_data(cls, data: Any) -> Any:
        """The API nests the facts under `business_data.facts`."""
        if isinstance(data, dict) and "business_facts" not in data:
            business = data.get("business_data") or {}
            data = {**data, "business_facts": business.get("facts") or []}
        return data


def build_http_client(
    settings: FrontendSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """httpx client for /v1 with the tenant header set once (R23.6)."""
    headers: dict[str, str] = {}
    if settings.organization_id is not None:
        headers[ORG_HEADER] = str(settings.organization_id)
    return httpx.AsyncClient(
        base_url=settings.api_base_url,
        headers=headers,
        timeout=API_TIMEOUT_S,
        transport=transport,
    )


def _error_from(response: httpx.Response) -> ApiError:
    code = "HTTP_ERROR"
    message = response.reason_phrase or "Request failed"
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("code") or code)
        message = str(payload.get("error") or payload.get("detail") or message)
    return ApiError(response.status_code, code, message)


class ReviewApiClient:
    """Typed /v1 calls used by the review pages (R23.4, R23.6)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(
                502, "API_UNREACHABLE", f"The API could not be reached: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise _error_from(response)
        if not response.content:
            return None
        return response.json()

    async def list_drafts(
        self,
        *,
        status: str = "draft",
        category: str | None = None,
        cursor: str | None = None,
        limit: int = DRAFT_PAGE_SIZE,
    ) -> DraftPage:
        params: dict[str, str | int] = {"status": status, "limit": limit}
        if category:
            params["category"] = category
        if cursor:
            params["cursor"] = cursor
        return DraftPage.model_validate(await self._request("GET", "/v1/drafts", params=params))

    async def get_draft(self, draft_id: UUID) -> DraftDetail:
        return DraftDetail.model_validate(await self._request("GET", f"/v1/drafts/{draft_id}"))

    async def update_draft_body(self, draft_id: UUID, body: str) -> None:
        await self._request("PATCH", f"/v1/drafts/{draft_id}", json={"body": body})

    async def approve_draft(self, draft_id: UUID, *, review_ms: int, reviewer: str | None) -> None:
        await self._request(
            "POST",
            f"/v1/drafts/{draft_id}/approve",
            json={"review_ms": review_ms, "reviewer": reviewer},
        )

    async def reject_draft(
        self, draft_id: UUID, *, review_ms: int, reviewer: str | None, comment: str | None
    ) -> None:
        await self._request(
            "POST",
            f"/v1/drafts/{draft_id}/reject",
            json={"review_ms": review_ms, "reviewer": reviewer, "comment": comment},
        )
```

Create `services/frontend/annotate.py`:

```python
"""Pure draft annotation for the review screen (R23.4, R13.5, R16.5; design.md §5.8).

Splits a draft into paragraphs and sentences, places each cited chunk next to the sentence it
supports, and marks the [BUSINESS DATA] references the draft states. No I/O.

A citation is placed, in order of preference:
1. on the sentence carrying an inline ``[CITATION: <id>]`` marker, the marker the prompts show
   the model (prompts/*.j2, packages/llm/citations.py); the marker is removed from the text;
2. otherwise on the sentence sharing the most content terms (4+ letters or digits) with the
   chunk text, if it shares at least MIN_SHARED_TERMS;
3. otherwise it is listed as "not matched to a sentence" under the draft.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from services.frontend.api_client import BusinessFactView, CitedChunk

MIN_SHARED_TERMS = 2

_MARKER = re.compile(r"\s*\[CITATION:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_TRAILING_MARKERS = re.compile(r"([.!?])((?:\s*\[CITATION:[^\]]*\])+)", re.IGNORECASE)
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_TERM = re.compile(r"[^\W_]{4,}")


@dataclass(frozen=True)
class Segment:
    """A run of sentence text; ``fact_reference`` is set when it names a business record."""

    text: str
    fact_reference: str | None = None


@dataclass
class AnnotatedSentence:
    segments: list[Segment]
    citations: list[CitedChunk] = field(default_factory=list)


@dataclass
class AnnotatedDraft:
    paragraphs: list[list[AnnotatedSentence]]
    unplaced: list[CitedChunk]
    numbers: dict[str, int]  # citation_id -> 1-based display number, in API order
    sources: list[CitedChunk] = field(default_factory=list)  # each citation once, API order


def _key(value: str) -> str:
    return value.strip().casefold()


def _terms(text: str) -> set[str]:
    return {term.casefold() for term in _TERM.findall(text)}


def split_paragraphs(body: str) -> list[list[str]]:
    """Paragraphs (blank-line separated) of sentences (split after . ! ?)."""
    paragraphs: list[list[str]] = []
    for block in _PARAGRAPH_BREAK.split(body.strip()):
        sentences = [s for s in _SENTENCE_BREAK.split(block.strip()) if s.strip()]
        if sentences:
            paragraphs.append(sentences)
    return paragraphs


def highlight_facts(text: str, facts: Sequence[BusinessFactView]) -> list[Segment]:
    """Split text so every whole-token mention of a fact reference is its own segment."""
    references = sorted(
        {f.reference.strip() for f in facts if f.reference and f.reference.strip()},
        key=len,
        reverse=True,
    )
    if not text:
        return []
    if not references:
        return [Segment(text)]
    alternatives = "|".join(re.escape(r) for r in references)
    pattern = re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)
    canonical = {_key(r): r for r in references}
    segments: list[Segment] = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            segments.append(Segment(text[cursor : match.start()]))
        segments.append(Segment(match.group(0), fact_reference=canonical[_key(match.group(0))]))
        cursor = match.end()
    if cursor < len(text):
        segments.append(Segment(text[cursor:]))
    return segments


def annotate_draft(
    body: str, citations: Sequence[CitedChunk], facts: Sequence[BusinessFactView]
) -> AnnotatedDraft:
    """Place citations beside sentences and highlight business references (R23.4)."""
    unique: list[CitedChunk] = []
    numbers: dict[str, int] = {}
    for cited in citations:
        if cited.citation_id not in numbers:
            numbers[cited.citation_id] = len(numbers) + 1
            unique.append(cited)

    by_alias: dict[str, CitedChunk] = {}
    for cited in unique:
        for alias in (cited.citation_id, cited.chunk_id):
            if alias:
                by_alias.setdefault(_key(alias), cited)

    prepared = _TRAILING_MARKERS.sub(lambda m: m.group(2) + m.group(1), body)
    cleaned: list[list[str]] = []
    placed: list[list[list[CitedChunk]]] = []
    placed_ids: set[str] = set()
    for paragraph in split_paragraphs(prepared):
        cleaned_paragraph: list[str] = []
        placed_paragraph: list[list[CitedChunk]] = []
        for sentence in paragraph:
            found: list[CitedChunk] = []
            for match in _MARKER.finditer(sentence):
                marked = by_alias.get(_key(match.group(1)))
                if marked is not None and marked.citation_id not in placed_ids:
                    found.append(marked)
                    placed_ids.add(marked.citation_id)
            cleaned_paragraph.append(_MARKER.sub("", sentence).strip())
            placed_paragraph.append(found)
        cleaned.append(cleaned_paragraph)
        placed.append(placed_paragraph)

    unplaced: list[CitedChunk] = []
    for citation in unique:
        if citation.citation_id in placed_ids:
            continue
        chunk_terms = _terms(citation.text or "")
        best: tuple[int, int] | None = None
        best_score = 0
        for p_index, paragraph_text in enumerate(cleaned):
            for s_index, sentence_text in enumerate(paragraph_text):
                score = len(chunk_terms & _terms(sentence_text))
                if score > best_score:
                    best, best_score = (p_index, s_index), score
        if best is not None and best_score >= MIN_SHARED_TERMS:
            placed[best[0]][best[1]].append(citation)
            placed_ids.add(citation.citation_id)
        else:
            unplaced.append(citation)

    paragraphs = [
        [
            AnnotatedSentence(segments=highlight_facts(text, facts), citations=placed[p][s])
            for s, text in enumerate(paragraph_text)
        ]
        for p, paragraph_text in enumerate(cleaned)
    ]
    return AnnotatedDraft(
        paragraphs=paragraphs, unplaced=unplaced, numbers=numbers, sources=unique
    )
```

- [ ] **Step 8: Run the annotation tests**

Run: `uv run pytest tests/unit/test_frontend_annotate.py -v`
Expected: PASS (10 passed).

- [ ] **Step 9: Write the failing review-page tests**

Create `tests/unit/test_frontend_review_ui.py`:

```python
"""Review UI draft queue, detail and decisions over recorded /v1 responses (task 6.8).

Requirements: R23.4 (queue, original email, thread summary, citations, approve/edit/reject),
R23.6 (X-Organization-Id on every call), R16.6/R16.7 (decisions carry review_ms).
The /v1 API is replaced at the HTTP boundary only (httpx.MockTransport); the real API is
exercised by tests/e2e (task 6.8 Playwright) against the isolated database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from packages.core.settings import FrontendServiceSettings, FrontendSettings
from services.frontend.drafts import resolve_review_ms
from services.frontend.main import STATIC_DIR, create_app

ORG = UUID("11111111-1111-4111-8111-111111111111")
DRAFT_ID = UUID("22222222-2222-4222-8222-222222222222")
SUBJECT = "Re: Where is order ORD-82915?"
BODY = (
    "Your order ORD-82915 was dispatched on 26 September. "
    "Refunds are issued within 14 days of the return arriving at our warehouse."
)
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
HX = {"HX-Request": "true"}


def _detail() -> dict[str, Any]:
    return {
        "id": str(DRAFT_ID),
        "job_id": str(uuid4()),
        "message_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "mailbox_id": str(uuid4()),
        "category": "billing",
        "status": "draft",
        "subject": SUBJECT,
        "confidence": 0.82,
        "created_at": "2026-09-28T10:00:00+00:00",
        "body": BODY,
        "citation_mismatch": False,
        "job_state": "DRAFTED",
        "dispatch_mode": "create_draft",
        # Recorded from Task 6's DraftDetailResponse (services/api/schemas/drafts.py).
        "original": {
            "message_id": str(uuid4()),
            "sender_email": "alice.smith@clientcorp.com",
            "sender_name": "Alice Smith",
            "subject": "Where is order ORD-82915?",
            "body_text": "Hello, where is my order ORD-82915? Thanks, Alice",
            "received_at": "2026-09-28T09:00:00+00:00",
            "rfc822_message_id": "orig-1@clientcorp.com",
        },
        "thread_summary": "Alice asks for the delivery status of ORD-82915.",
        "citations": [{"citation_id": "kb-returns-2", "chunk_id": "c-1"}],
        "cited_chunks": [
            {
                "citation_id": "kb-returns-2",
                "chunk_id": "c-1",
                "document_id": "d-1",
                "external_id": "kb-returns-2",
                "content": (
                    "Refunds are issued within 14 days after the return arrives at the warehouse."
                ),
                "heading_path": ["Returns policy"],
            }
        ],
        "business_data": {
            "customer_status": "FOUND",
            "degraded": False,
            "facts": [
                {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
            ],
        },
        "feedback": None,
    }


class FakeV1:
    """Recorded /v1 drafts responses; records every request the UI makes."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        self.requests: list[httpx.Request] = []
        self.approve_error: tuple[int, dict[str, Any]] | None = None

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        base = f"/v1/drafts/{self.detail['id']}"
        if method == "GET" and path == "/v1/drafts":
            keys = (
                "id",
                "job_id",
                "message_id",
                "thread_id",
                "mailbox_id",
                "category",
                "status",
                "subject",
                "confidence",
                "created_at",
            )
            item = {k: self.detail[k] for k in keys}
            return httpx.Response(200, json={"items": [item], "next_cursor": "c2"})
        if method == "GET" and path == base:
            return httpx.Response(200, json=self.detail)
        if method == "PATCH" and path == base:
            self.detail["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json=self.detail)
        if method == "POST" and path == f"{base}/approve":
            if self.approve_error is not None:
                return httpx.Response(self.approve_error[0], json=self.approve_error[1])
            self.detail["status"] = "approved"
            return httpx.Response(200, json={"draft_id": self.detail["id"], "status": "approved"})
        if method == "POST" and path == f"{base}/reject":
            self.detail["status"] = "rejected"
            return httpx.Response(200, json={"draft_id": self.detail["id"], "status": "rejected"})
        return httpx.Response(404, json={"error": "Draft not found", "code": "DRAFT_NOT_FOUND"})


def _settings(org: UUID | None = ORG) -> FrontendServiceSettings:
    return FrontendServiceSettings(
        _env_file=None,
        frontend=FrontendSettings(api_base_url="http://api.test", organization_id=org),
    )


@pytest.fixture
def fake() -> FakeV1:
    return FakeV1(_detail())


@pytest.fixture
async def ui(fake: FakeV1) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        _settings(), transport=httpx.MockTransport(fake.handler), configure_logging=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui"
    ) as client:
        yield client


async def test_queue_lists_pending_drafts_through_v1_with_the_org_header(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    response = await ui.get("/drafts")

    assert response.status_code == 200
    sent = fake.requests[0]
    assert sent.url.path == "/v1/drafts"
    assert sent.url.params["status"] == "draft"
    assert sent.headers["X-Organization-Id"] == str(ORG)
    html = response.text
    assert f'href="/drafts/{DRAFT_ID}"' in html
    assert SUBJECT in html
    assert 'href="/drafts?cursor=c2"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html


async def test_root_redirects_to_the_queue(ui: httpx.AsyncClient) -> None:
    response = await ui.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/drafts"


async def test_detail_shows_email_summary_facts_and_citation_beside_its_sentence(
    ui: httpx.AsyncClient,
) -> None:
    response = await ui.get(f"/drafts/{DRAFT_ID}")

    assert response.status_code == 200
    html = response.text
    assert "Hello, where is my order ORD-82915? Thanks, Alice" in html
    assert "Alice asks for the delivery status of ORD-82915." in html
    marked = '<mark class="fact" title="Business data: ORD-82915">ORD-82915</mark>'
    assert f"{marked} was dispatched" in html
    assert html.index("our warehouse.") < html.index('href="#cite-1"')
    assert "Returns policy" in html
    assert "hx-vals='js:{review_ms: reviewElapsedMs()}'" in html
    assert 'name="rendered_at_ms"' in html


async def test_approve_sends_review_ms_and_announces_the_result(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    form = {
        "body": BODY,
        "original_body": BODY,
        "review_ms": "4200",
        "rendered_at_ms": "1",
        "reviewer": " Quan ",
    }
    response = await ui.post(f"/drafts/{DRAFT_ID}/approve", data=form, headers=HX)

    assert response.status_code == 200
    assert fake.calls("PATCH") == []
    approve = fake.calls("POST")[0]
    assert approve.url.path == f"/v1/drafts/{DRAFT_ID}/approve"
    assert json.loads(approve.content) == {"review_ms": 4200, "reviewer": "Quan"}
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "Draft approved." in response.text
    assert "This draft was approved." in response.text


async def test_an_edited_body_is_patched_before_the_approval(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    edited = "Your order ORD-82915 left today.\r\nThanks."
    form = {"body": edited, "original_body": BODY, "review_ms": "900", "rendered_at_ms": "1"}
    response = await ui.post(f"/drafts/{DRAFT_ID}/approve", data=form, headers=HX)

    assert response.status_code == 200
    methods = [r.method for r in fake.requests if r.url.path.startswith("/v1/drafts/")]
    assert methods[:2] == ["PATCH", "POST"]
    assert json.loads(fake.calls("PATCH")[0].content) == {
        "body": "Your order ORD-82915 left today.\nThanks."
    }
    assert json.loads(fake.calls("POST")[0].content) == {"review_ms": 900, "reviewer": None}


async def test_save_changes_patches_without_a_decision(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    response = await ui.post(
        f"/drafts/{DRAFT_ID}/edit", data={"body": "New text.", "review_ms": "10"}, headers=HX
    )

    assert response.status_code == 200
    assert json.loads(fake.calls("PATCH")[0].content) == {"body": "New text."}
    assert fake.calls("POST") == []
    assert "Changes saved." in response.text


async def test_reject_sends_the_reason(ui: httpx.AsyncClient, fake: FakeV1) -> None:
    form = {"comment": " Wrong order. ", "review_ms": "900", "rendered_at_ms": "1"}
    response = await ui.post(f"/drafts/{DRAFT_ID}/reject", data=form, headers=HX)

    assert response.status_code == 200
    reject = fake.calls("POST")[0]
    assert reject.url.path == f"/v1/drafts/{DRAFT_ID}/reject"
    assert json.loads(reject.content) == {
        "review_ms": 900,
        "reviewer": None,
        "comment": "Wrong order.",
    }
    assert "Draft rejected." in response.text


async def test_without_javascript_a_decision_redirects_back_with_a_notice(
    ui: httpx.AsyncClient,
) -> None:
    response = await ui.post(
        f"/drafts/{DRAFT_ID}/approve", data={"body": BODY, "original_body": BODY}
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/drafts/{DRAFT_ID}?notice=approved"

    page = await ui.get(response.headers["location"])
    assert "Draft approved." in page.text


def test_review_ms_prefers_the_client_measurement_then_the_render_time() -> None:
    assert resolve_review_ms("4200", "1000", now=5000) == 4200
    assert resolve_review_ms(None, "1000", now=5000) == 4000
    assert resolve_review_ms("", "1000", now=5000) == 4000
    assert resolve_review_ms("-3", "1000", now=5000) == 4000
    assert resolve_review_ms("abc", None, now=5000) == 0
    assert resolve_review_ms(None, "9000", now=5000) == 0


async def test_an_api_conflict_on_a_decision_is_announced_and_keeps_the_panel(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    fake.approve_error = (409, {"error": "Draft is not pending", "code": "DRAFT_NOT_PENDING"})
    response = await ui.post(
        f"/drafts/{DRAFT_ID}/approve",
        data={"body": BODY, "original_body": BODY, "review_ms": "5"},
        headers=HX,
    )

    assert response.status_code == 200
    assert response.headers["HX-Reswap"] == "none"
    assert "Error: Draft is not pending" in response.text


async def test_an_unknown_draft_renders_a_not_found_page(ui: httpx.AsyncClient) -> None:
    response = await ui.get(f"/drafts/{uuid4()}")
    assert response.status_code == 404
    assert "Draft not found" in response.text


async def test_missing_organization_renders_a_setup_error_and_calls_nothing(
    fake: FakeV1,
) -> None:
    app = create_app(
        _settings(org=None), transport=httpx.MockTransport(fake.handler), configure_logging=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui"
    ) as client:
        response = await client.get("/drafts")

    assert response.status_code == 503
    assert "FRONTEND__ORGANIZATION_ID" in response.text
    assert fake.requests == []


async def test_an_unreachable_api_renders_a_502_page() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = create_app(_settings(), transport=httpx.MockTransport(refuse), configure_logging=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui"
    ) as client:
        response = await client.get("/drafts")

    assert response.status_code == 502
    assert "The API could not be reached" in response.text


async def test_health_and_vendored_htmx_are_served(ui: httpx.AsyncClient) -> None:
    assert (await ui.get("/healthz")).status_code == 200
    script = await ui.get("/static/htmx.min.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    pinned = hashlib.sha256((STATIC_DIR / "htmx.min.js").read_bytes()).hexdigest()
    assert pinned == HTMX_SHA256, "htmx.min.js must be the vendored 2.0.11 release"
```

- [ ] **Step 10: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_frontend_review_ui.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.frontend.drafts'`.

- [ ] **Step 11: Vendor htmx and implement the app, routes, templates and static files**

Vendor htmx 2.0.11 (Zero-Clause BSD; no attribution required) and check its hash:

```bash
mkdir -p services/frontend/static services/frontend/templates/drafts
curl -fsSL https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js -o services/frontend/static/htmx.min.js
sha256sum services/frontend/static/htmx.min.js
```

Expected: `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717  services/frontend/static/htmx.min.js` (52182 bytes). Any other hash: stop, do not commit.

Create `services/frontend/web.py`:

```python
"""Shared helpers for the review UI routes (R23.4, R23.6; ADR-0009)."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from services.frontend.api_client import ApiError, ReviewApiClient

MISSING_ORGANIZATION = (
    "FRONTEND__ORGANIZATION_ID is not set. Set it in .env to the organization whose drafts "
    "you review, then restart the frontend."
)


def require_organization(request: Request) -> None:
    """Refuse to call /v1 without a tenant: the API requires X-Organization-Id (R23.6)."""
    if request.app.state.organization_id is None:
        raise ApiError(503, "ORGANIZATION_NOT_CONFIGURED", MISSING_ORGANIZATION)


def api_client(request: Request) -> ReviewApiClient:
    client: ReviewApiClient = request.app.state.api
    return client


def templates(request: Request) -> Jinja2Templates:
    loaded: Jinja2Templates = request.app.state.templates
    return loaded


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


async def api_error_handler(request: Request, exc: Exception) -> Response:
    """htmx: announce the error in the live region and swap nothing; pages: an error page."""
    if not isinstance(exc, ApiError):
        raise exc
    loaded = templates(request)
    if is_htmx(request):
        return loaded.TemplateResponse(
            request,
            "_status.html",
            {"status_message": f"Error: {exc.message}"},
            headers={"HX-Reswap": "none"},
        )
    return loaded.TemplateResponse(
        request, "error.html", {"error": exc}, status_code=exc.status_code
    )
```

Create `services/frontend/drafts.py`:

```python
"""Pending-draft queue, draft review and decisions (R23.4, R16.6, R16.7; design.md §5.8).

Every read and decision is a /v1 call. A decision carries the review time measured in the
browser (review.js, sent as review_ms); without JavaScript it falls back to the time since the
page was rendered. htmx requests get the re-rendered draft panel plus an out-of-band message
for the live region; plain form posts get a 303 back to the draft with a notice.
"""

from __future__ import annotations

import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.frontend.annotate import annotate_draft
from services.frontend.api_client import DraftDetail
from services.frontend.web import (
    api_client,
    is_htmx,
    optional_text,
    require_organization,
    templates,
)

NOTICES = {
    "saved": "Changes saved.",
    "approved": "Draft approved.",
    "rejected": "Draft rejected.",
}

drafts_router = APIRouter(dependencies=[Depends(require_organization)])

FormText = Annotated[str | None, Form()]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def normalize_body(text: str) -> str:
    """Browsers submit textarea line breaks as CRLF; drafts are stored with LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _non_negative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def resolve_review_ms(review_ms: str | None, rendered_at_ms: str | None, *, now: int) -> int:
    """Review time in ms: the browser's measurement, else time since render, else 0 (R16.7)."""
    measured = _non_negative_int(review_ms)
    if measured is not None:
        return measured
    rendered = _non_negative_int(rendered_at_ms)
    if rendered is not None:
        return max(0, now - rendered)
    return 0


def _detail_context(draft: DraftDetail, **extra: Any) -> dict[str, Any]:
    return {
        "draft": draft,
        "annotated": annotate_draft(draft.body, draft.citations, draft.business_facts),
        "rendered_at_ms": now_ms(),
        "nav": "drafts",
        **extra,
    }


async def _after_decision(request: Request, draft_id: UUID, notice: str) -> Response:
    if not is_htmx(request):
        return RedirectResponse(f"/drafts/{draft_id}?notice={notice}", status_code=303)
    draft = await api_client(request).get_draft(draft_id)
    return templates(request).TemplateResponse(
        request, "drafts/_panel.html", _detail_context(draft, status_message=NOTICES[notice])
    )


@drafts_router.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/drafts", status_code=303)


@drafts_router.get("/drafts", response_class=HTMLResponse)
async def draft_queue(
    request: Request, category: str | None = None, cursor: str | None = None
) -> Response:
    """Pending-draft queue (R23.4), one cursor page at a time (R23.6)."""
    chosen = optional_text(category)
    page = await api_client(request).list_drafts(category=chosen, cursor=optional_text(cursor))
    return templates(request).TemplateResponse(
        request, "drafts/queue.html", {"page": page, "category": chosen, "nav": "drafts"}
    )


@drafts_router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_detail(request: Request, draft_id: UUID, notice: str | None = None) -> Response:
    """Original email, thread summary, facts and the annotated draft (R23.4)."""
    draft = await api_client(request).get_draft(draft_id)
    context = _detail_context(draft, page_notice=NOTICES.get(notice or ""))
    return templates(request).TemplateResponse(request, "drafts/detail.html", context)


@drafts_router.post("/drafts/{draft_id}/edit")
async def save_edit(request: Request, draft_id: UUID, body: Annotated[str, Form()]) -> Response:
    await api_client(request).update_draft_body(draft_id, normalize_body(body))
    return await _after_decision(request, draft_id, "saved")


@drafts_router.post("/drafts/{draft_id}/approve")
async def approve(
    request: Request,
    draft_id: UUID,
    body: FormText = None,
    original_body: FormText = None,
    review_ms: FormText = None,
    rendered_at_ms: FormText = None,
    reviewer: FormText = None,
) -> Response:
    """Save an edited body first (PATCH), then approve (R16.6, R16.7)."""
    client = api_client(request)
    if body is not None and original_body is not None:
        edited = normalize_body(body)
        if edited != normalize_body(original_body):
            await client.update_draft_body(draft_id, edited)
    await client.approve_draft(
        draft_id,
        review_ms=resolve_review_ms(review_ms, rendered_at_ms, now=now_ms()),
        reviewer=optional_text(reviewer),
    )
    return await _after_decision(request, draft_id, "approved")


@drafts_router.post("/drafts/{draft_id}/reject")
async def reject(
    request: Request,
    draft_id: UUID,
    review_ms: FormText = None,
    rendered_at_ms: FormText = None,
    reviewer: FormText = None,
    comment: FormText = None,
) -> Response:
    await api_client(request).reject_draft(
        draft_id,
        review_ms=resolve_review_ms(review_ms, rendered_at_ms, now=now_ms()),
        reviewer=optional_text(reviewer),
        comment=optional_text(comment),
    )
    return await _after_decision(request, draft_id, "rejected")
```

Create `services/frontend/main.py`:

```python
"""Review UI service entrypoint (R23.4, R23.5, R23.7; design.md §5.8; ADR-0009).

Server-rendered FastAPI + Jinja2 + htmx pages. The service holds no database or broker
connection: every read and decision is a /v1 call carrying X-Organization-Id (R23.6). It has
no login, so docker-compose publishes it on 127.0.0.1 only (ADR-0009).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from packages.core.settings import FrontendServiceSettings
from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.logging import setup_logging
from services.frontend.api_client import ApiError, ReviewApiClient, build_http_client
from services.frontend.drafts import drafts_router
from services.frontend.web import api_error_handler

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(
    settings: FrontendServiceSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    configure_logging: bool = True,
) -> FastAPI:
    """Build the review UI; ``transport`` lets tests route /v1 calls in-process."""
    active = settings or FrontendServiceSettings()
    http = build_http_client(active.frontend, transport=transport)
    api = ReviewApiClient(http)
    health = HealthRegistry(service_name=active.service_name)

    async def check_api() -> tuple[bool, str]:
        try:
            response = await http.get("/healthz")
        except httpx.HTTPError as exc:
            return False, f"API unreachable: {exc}"
        return response.status_code == 200, f"API /healthz returned {response.status_code}"

    health.register_readiness_check("api", check_api)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if configure_logging:
            setup_logging(
                level=active.telemetry.log_level,
                json_format=active.telemetry.log_format == "json",
            )
        try:
            yield
        finally:
            await api.aclose()

    app = FastAPI(
        title="Email review UI",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = active
    app.state.api = api
    app.state.organization_id = active.frontend.organization_id
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_health_router(health))
    app.include_router(drafts_router)
    app.add_exception_handler(ApiError, api_error_handler)
    return app


app = create_app()
```

Create `services/frontend/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Review{% endblock %} · Email review</title>
  <link rel="stylesheet" href="/static/review.css">
  <script src="/static/htmx.min.js" defer></script>
  <script src="/static/review.js" defer></script>
</head>
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <header class="site-header">
    <nav aria-label="Main">
      <ul>
        <li><a href="/drafts"{% if nav == "drafts" %} aria-current="page"{% endif %}>Pending drafts</a></li>
      </ul>
    </nav>
  </header>
  <main id="main" tabindex="-1">
    {% block content %}{% endblock %}
  </main>
  <div id="status-region" class="status-region" role="status" aria-live="polite" aria-atomic="true">{{ page_notice or "" }}</div>
</body>
</html>
```

Create `services/frontend/templates/_status.html`:

```html
<div id="status-region" hx-swap-oob="innerHTML">{{ status_message }}</div>
```

Create `services/frontend/templates/error.html`:

```html
{% extends "base.html" %}
{% block title %}Error{% endblock %}
{% block content %}
<h1>{% if error.status_code == 404 %}Not found{% elif error.status_code == 503 %}Setup needed{% else %}Something went wrong{% endif %}</h1>
<p>{{ error.message }}</p>
<p class="note">Code {{ error.code }} (HTTP {{ error.status_code }})</p>
<p><a href="/drafts">Back to pending drafts</a></p>
{% endblock %}
```

Create `services/frontend/templates/drafts/queue.html`:

```html
{% extends "base.html" %}
{% block title %}Pending drafts{% endblock %}
{% block content %}
<h1>Pending drafts</h1>
<form class="filters" method="get" action="/drafts">
  <label for="category">Category</label>
  <input id="category" name="category" value="{{ category or '' }}">
  <div class="actions"><button type="submit">Filter</button></div>
</form>
{% if page.items %}
<table>
  <caption class="visually-hidden">Drafts waiting for review</caption>
  <thead>
    <tr><th scope="col">Subject</th><th scope="col">Category</th><th scope="col">Confidence</th><th scope="col">Created</th></tr>
  </thead>
  <tbody>
  {% for d in page.items %}
    <tr>
      <td><a href="/drafts/{{ d.id }}">{{ d.subject or "(no subject)" }}</a></td>
      <td>{{ d.category or "—" }}</td>
      <td>{{ "%.2f"|format(d.confidence) if d.confidence is not none else "—" }}</td>
      <td>{{ d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "—" }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p>No drafts are waiting for review.</p>
{% endif %}
{% if page.next_cursor %}
<p><a class="button-link" href="/drafts?cursor={{ page.next_cursor|urlencode }}{% if category %}&amp;category={{ category|urlencode }}{% endif %}">Next page</a></p>
{% endif %}
{% endblock %}
```

Create `services/frontend/templates/drafts/detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ draft.subject or "Draft" }}{% endblock %}
{% block content %}
<p><a href="/drafts">Back to pending drafts</a></p>
<h1>{{ draft.subject or "(no subject)" }}</h1>
<dl class="meta">
  <dt>Category</dt><dd>{{ draft.category or "—" }}</dd>
  <dt>Confidence</dt><dd>{{ "%.2f"|format(draft.confidence) if draft.confidence is not none else "—" }}</dd>
  <dt>Dispatch mode</dt><dd>{{ draft.dispatch_mode or "—" }}</dd>
  <dt>Job state</dt><dd>{{ draft.job_state or "—" }}</dd>
</dl>
<div class="layout">
  <div>
    <section class="panel" aria-labelledby="original-heading">
      <h2 id="original-heading">Original email</h2>
      {% set om = draft.original_message %}
      {% if om %}
      <dl class="meta">
        <dt>From</dt><dd>{{ (om.sender_name ~ " <" ~ om.sender_email ~ ">") if om.sender_name else (om.sender_email or "—") }}</dd>
        <dt>Subject</dt><dd>{{ om.subject or "(no subject)" }}</dd>
        <dt>Received</dt><dd>{{ om.received_at.strftime("%Y-%m-%d %H:%M") if om.received_at else "—" }}</dd>
      </dl>
      <div class="email-body">{{ om.body_text }}</div>
      {% else %}
      <p>The original email is not available.</p>
      {% endif %}
    </section>
    <section class="panel" aria-labelledby="summary-heading">
      <h2 id="summary-heading">Thread summary</h2>
      <p>{{ draft.thread_summary or "No summary yet: the thread is below the summarization threshold." }}</p>
    </section>
    <section class="panel" aria-labelledby="facts-heading">
      <h2 id="facts-heading">Business data</h2>
      {% if draft.business_facts %}
      <ul class="facts">
        {% for f in draft.business_facts %}
        <li><mark class="fact">{{ f.entity }}{% if f.reference %} {{ f.reference }}{% endif %}</mark>: {{ f.status }}{% if f.reason %} ({{ f.reason }}){% endif %}</li>
        {% endfor %}
      </ul>
      <p class="note">Highlighted text in the draft names one of these records.</p>
      {% else %}
      <p>No business data was looked up for this email.</p>
      {% endif %}
    </section>
  </div>
  <div>
    {% include "drafts/_panel.html" %}
  </div>
</div>
{% endblock %}
```

Create `services/frontend/templates/drafts/_panel.html`:

```html
<section id="draft-panel" class="panel" aria-labelledby="draft-heading">
  <h2 id="draft-heading" tabindex="-1">Draft reply</h2>
  {% if draft.citation_mismatch %}
  <p class="warning">The model cited knowledge it was not given; those citations were removed.</p>
  {% endif %}
  <div class="draft-body">
    {% for paragraph in annotated.paragraphs %}
    <p>{% for sentence in paragraph %}<span class="sentence">{% for seg in sentence.segments %}{% if seg.fact_reference %}<mark class="fact" title="Business data: {{ seg.fact_reference }}">{{ seg.text }}</mark>{% else %}{{ seg.text }}{% endif %}{% endfor %}{% for c in sentence.citations %}<a class="cite" href="#cite-{{ annotated.numbers[c.citation_id] }}" aria-label="Source {{ annotated.numbers[c.citation_id] }}: {{ c.title or c.citation_id }}">[{{ annotated.numbers[c.citation_id] }}]</a>{% endfor %}</span> {% endfor %}</p>
    {% endfor %}
  </div>
  {% if annotated.sources %}
  {% set unplaced_ids = annotated.unplaced | map(attribute="citation_id") | list %}
  <h3>Sources</h3>
  <ol class="sources">
    {% for c in annotated.sources %}
    <li id="cite-{{ annotated.numbers[c.citation_id] }}">
      <strong>{{ c.title or c.citation_id }}</strong>
      {% if c.text %}<blockquote>{{ c.text }}</blockquote>{% endif %}
      {% if c.citation_id in unplaced_ids %}<p class="note">Not matched to a sentence.</p>{% endif %}
    </li>
    {% endfor %}
  </ol>
  {% endif %}
  {% if draft.status == "draft" %}
  <form id="decision-form" method="post" action="/drafts/{{ draft.id }}/approve"
        hx-post="/drafts/{{ draft.id }}/approve" hx-target="#draft-panel" hx-swap="outerHTML"
        hx-vals='js:{review_ms: reviewElapsedMs()}'>
    <input type="hidden" name="original_body" value="{{ draft.body }}">
    <input type="hidden" name="rendered_at_ms" value="{{ rendered_at_ms }}">
    <label for="body">Reply text</label>
    <textarea id="body" name="body" rows="12">
{{ draft.body }}</textarea>
    <label for="reviewer">Reviewer (optional)</label>
    <input id="reviewer" name="reviewer" autocomplete="name">
    <div class="actions">
      <button type="submit" formaction="/drafts/{{ draft.id }}/edit" hx-post="/drafts/{{ draft.id }}/edit">Save changes</button>
      <button type="submit" class="primary">Approve</button>
    </div>
  </form>
  <form class="reject" method="post" action="/drafts/{{ draft.id }}/reject"
        hx-post="/drafts/{{ draft.id }}/reject" hx-target="#draft-panel" hx-swap="outerHTML"
        hx-include="#reviewer" hx-vals='js:{review_ms: reviewElapsedMs()}'>
    <input type="hidden" name="rendered_at_ms" value="{{ rendered_at_ms }}">
    <label for="comment">Reason for rejecting (optional)</label>
    <textarea id="comment" name="comment" rows="3"></textarea>
    <div class="actions"><button type="submit" class="danger">Reject</button></div>
  </form>
  {% else %}
  <p class="decided">This draft was {{ draft.status }}.</p>
  <div class="email-body">{{ draft.body }}</div>
  {% endif %}
</section>
{% if status_message %}{% include "_status.html" %}{% endif %}
```

(`annotated.sources` holds each citation once, in API order, so the list numbers match the `[n]` links in the draft.)

Create `services/frontend/static/review.js`:

```javascript
// Review UI helpers (task 6.8; R16.7, WCAG 2.2 AA 2.4.3).
"use strict";

// Milliseconds from page render to the decision, sent with approve/reject as review_ms
// through hx-vals='js:{review_ms: reviewElapsedMs()}'.
const reviewStartedAt = performance.now();

function reviewElapsedMs() {
  return Math.max(0, Math.round(performance.now() - reviewStartedAt));
}
window.reviewElapsedMs = reviewElapsedMs;

// After htmx replaces the draft panel the focused button is gone; put focus on the
// panel heading so keyboard and screen-reader users keep their place.
document.addEventListener("htmx:afterSettle", () => {
  const active = document.activeElement;
  if (active && active !== document.body) {
    return;
  }
  const heading = document.getElementById("draft-heading");
  if (heading) {
    heading.focus();
  }
});
```

Create `services/frontend/static/review.css`:

```css
/* Review UI (task 6.8). WCAG 2.2 AA basics: visible focus (2.4.7, 2.4.11), targets of at
   least 24 px (2.5.8; controls here are 44 px), text contrast >= 4.5:1 (1.4.3). */
:root {
  --text: #1b1b1f;
  --muted: #4a4a55;
  --bg: #ffffff;
  --panel: #f5f6f8;
  --border: #c9ccd3;
  --primary: #1a4fd6;
  --danger: #b3261e;
  --focus: #1a4fd6;
  --mark: #fff1a8;
  --warning-bg: #fff4e5;
  --warning-text: #663c00;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
}
* { box-sizing: border-box; }
body { margin: 0; color: var(--text); background: var(--bg); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.skip-link { position: absolute; left: -9999px; top: 0; display: inline-flex; align-items: center;
  min-height: 44px; padding: 0 1rem; background: var(--bg); color: var(--primary); }
.skip-link:focus { left: 1rem; top: 1rem; z-index: 10; }
.site-header { border-bottom: 1px solid var(--border); padding: 0 1.5rem; }
.site-header ul { display: flex; gap: 0.5rem; list-style: none; margin: 0; padding: 0; }
.site-header a { display: inline-flex; align-items: center; min-height: 44px; padding: 0 0.75rem;
  color: var(--primary); }
.site-header a[aria-current="page"] { font-weight: 700; }
main { max-width: 76rem; margin: 0 auto; padding: 1.5rem; }
main:focus { outline: none; }
h1 { font-size: 1.6rem; }
h2 { font-size: 1.2rem; margin-top: 0; }
a { color: var(--primary); }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.5rem; }
@media (max-width: 60rem) { .layout { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 0.25rem;
  padding: 1rem 1.25rem; margin-bottom: 1.5rem; }
.meta { display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1rem; }
.meta dt { font-weight: 600; }
.meta dd { margin: 0; }
.email-body, .draft-body p { white-space: pre-line; }
mark.fact { background: var(--mark); color: var(--text); padding: 0 0.15em;
  border-bottom: 2px solid #8a6d00; }
a.cite { font-size: 0.85em; margin-left: 0.15em; }
.sources blockquote { margin: 0.25rem 0 0.75rem; padding-left: 0.75rem;
  border-left: 3px solid var(--border); color: var(--muted); }
.note { color: var(--muted); }
.warning { background: var(--warning-bg); color: var(--warning-text); padding: 0.5rem 0.75rem; }
label { display: block; font-weight: 600; margin: 1rem 0 0.25rem; }
input, textarea, select { font: inherit; min-height: 44px; width: 100%; padding: 0.5rem;
  border: 1px solid var(--muted); border-radius: 0.25rem; background: var(--bg); color: var(--text); }
input[type="file"] { border: none; padding: 0.5rem 0; }
button, .button-link { display: inline-flex; align-items: center; justify-content: center;
  min-height: 44px; min-width: 44px; padding: 0 1rem; border: 1px solid var(--text);
  border-radius: 0.25rem; background: var(--bg); color: var(--text); font: inherit;
  cursor: pointer; text-decoration: none; }
button.primary { background: var(--primary); border-color: var(--primary); color: #ffffff; }
button.danger { background: var(--danger); border-color: var(--danger); color: #ffffff; }
.actions { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 1rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid var(--border);
  vertical-align: top; }
td a { display: inline-block; min-height: 24px; }
.status-region:not(:empty) { position: fixed; right: 1rem; bottom: 1rem; max-width: 28rem;
  padding: 0.75rem 1rem; border-radius: 0.25rem; background: var(--text); color: var(--bg); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; }
```

(Contrast: `#1a4fd6` on white 6.7:1; `#b3261e` on white 6.5:1; `#4a4a55` on `#f5f6f8` ≈ 8:1; white on `#1b1b1f` ≈ 17:1.)

- [ ] **Step 12: Run the review-page tests**

Run: `uv run pytest tests/unit/test_frontend_review_ui.py tests/unit/test_frontend_annotate.py -v`
Expected: PASS (14 + 10 passed).

- [ ] **Step 13: Write the failing compose, image and boundary tests**

In `tests/unit/test_runtime_image_contract.py`, in `_runtime_assets()` extend the `assets = [...]` list with the two UI directories (after the migrations entry):

```python
        "services/frontend/templates",
        "services/frontend/static",
```

and append:

```python
def test_review_ui_and_api_publish_on_loopback_only() -> None:
    """ADR-0009: the review UI has no login, so its port and the API's bind to 127.0.0.1."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["frontend"]["ports"] == ["127.0.0.1:3001:3001"]
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:8000:8000"]


def test_frontend_runs_the_review_ui_with_its_settings() -> None:
    """R23.6 / 6.8: the frontend container runs services.frontend with FRONTEND__* set."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    frontend = compose["services"]["frontend"]
    assert frontend["command"] == [
        "uvicorn",
        "services.frontend.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "3001",
    ]
    env = frontend["environment"]
    assert env["FRONTEND__API_BASE_URL"] == "http://api:8000"
    assert str(env["FRONTEND__ORGANIZATION_ID"]).startswith("${FRONTEND__ORGANIZATION_ID")
    assert frontend["healthcheck"]["test"][-1] == "http://localhost:3001/readyz"
```

In `tests/unit/test_dependency_rules.py`, append:

```python
def test_review_ui_reaches_the_system_only_over_http() -> None:
    """ADR-0009 / R23.6: services/frontend calls /v1 over HTTP; it never imports the API,
    the database layer or the broker."""
    forbidden = ("services.api", "packages.db", "packages.broker")
    violations: list[str] = []
    for py_file in (SERVICES_DIR / "frontend").rglob("*.py"):
        for name in get_imports(py_file):
            if any(name == root or name.startswith(f"{root}.") for root in forbidden):
                violations.append(f"{py_file.relative_to(REPO_ROOT)} imports {name}")
    assert not violations, "Review UI must use /v1 only:\n" + "\n".join(violations)
```

In `tests/unit/test_production_imports.py`, add `"services.frontend.main",` to `ENTRYPOINTS` after `"services.api.main",`.

- [ ] **Step 14: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_runtime_image_contract.py tests/unit/test_dependency_rules.py tests/unit/test_production_imports.py -v`
Expected: FAIL — `test_review_ui_and_api_publish_on_loopback_only` (`['3001:3001'] != ['127.0.0.1:3001:3001']`), `test_frontend_runs_the_review_ui_with_its_settings` (`KeyError: 'command'`), and `test_runtime_asset_is_not_dockerignored[services/frontend/templates]` / `[services/frontend/static]` (the bare `frontend` pattern in `.dockerignore` matches the path segment). `test_review_ui_reaches_the_system_only_over_http` and the production-import tests already PASS.

- [ ] **Step 15: Compose, Dockerfile, .dockerignore, image smoke**

In `docker-compose.yml`, change the api `ports` entry to:

```yaml
    ports:
      - "127.0.0.1:8000:8000"   # local only: the review UI and API have no login (ADR-0009)
```

Replace the whole `frontend:` service with:

```yaml
  frontend:
    build: *app-build
    container_name: rag-email-frontend
    restart: unless-stopped
    command: ["uvicorn", "services.frontend.main:app", "--host", "0.0.0.0", "--port", "3001"]
    ports:
      - "127.0.0.1:3001:3001"   # local only: the review UI has no login (ADR-0009)
    environment:
      SERVICE_NAME: frontend
      PORT: "3001"
      FRONTEND__API_BASE_URL: http://api:8000
      FRONTEND__ORGANIZATION_ID: ${FRONTEND__ORGANIZATION_ID:-}
    depends_on:
      api:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3001/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 10s
```

(The frontend deliberately does not merge `*app-env`: it needs no database, broker or provider settings.)

In `Dockerfile`, replace the two comment lines above `CMD ["python", "services/placeholder.py"]` with:

```dockerfile
# Every service sets its own command in docker-compose.yml; this health stub is only the
# image default.
```

(If the dispatch-worker task of this plan has already edited this comment, the final text is the one above.)

In `.dockerignore`, delete the line `frontend` (line 28). Delete the empty root directory: `git rm frontend/.gitkeep`.

In `scripts/image_smoke.py`, add `"services.frontend.main",` to the entrypoint tuple after `"services.api.main",`.

- [ ] **Step 16: Run the image and boundary tests**

Run: `uv run pytest tests/unit/test_runtime_image_contract.py tests/unit/test_dependency_rules.py tests/unit/test_production_imports.py -v`
Expected: PASS.

- [ ] **Step 17: Config docs, README and the design tree**

Append to `.env.example`:

```
# --- 21. Review UI (R23.4, R23.5, R23.6, R23.7, design.md §5.8, ADR-0009) ---
# Local use only: compose publishes the UI (3001) and the API (8000) on 127.0.0.1; there is
# no login. Inside compose the UI reaches the API at http://api:8000 (docker-compose.yml);
# FRONTEND__API_BASE_URL here is used when the UI runs on the host.
FRONTEND__API_BASE_URL=http://localhost:8000
# Tenant whose drafts the UI reviews (sent as X-Organization-Id): the seeded demo tenant.
FRONTEND__ORGANIZATION_ID=00000000-0000-0000-0000-000000000001
```

Append to `docs/configuration.md`:

```markdown

### 2.21 Review UI (`FRONTEND__*`)
*How the review UI (the `frontend` service) reaches the `/v1` API (R23.4–R23.7, design.md §5.8, ADR-0009).*

The review UI is server-rendered (FastAPI + Jinja2 + htmx) and calls only the `/v1` API; it holds no database or broker connection. Every request carries `FRONTEND__ORGANIZATION_ID` as the `X-Organization-Id` header (R23.6); while it is blank the pages show a setup error and call nothing. The UI has no login (ADR-0009, GEMINI.md §6), so Docker Compose publishes it and the API on `127.0.0.1` only (`http://localhost:3001`, `http://localhost:8000`); do not publish either port on a network interface. Inside Compose the UI reaches the API at `http://api:8000`, fixed in `docker-compose.yml`; `FRONTEND__API_BASE_URL` from `.env` applies when the UI runs on the host (`uv run uvicorn services.frontend.main:app --port 3001`). Compose forwards `FRONTEND__ORGANIZATION_ID` from the host `.env` into the `frontend` container only.

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `FRONTEND__API_BASE_URL` | `string` | `http://localhost:8000` | Starts with `http://` or `https://`; a trailing `/` is dropped | Base URL of the API the review UI calls; only `/v1` paths are used |
| `FRONTEND__ORGANIZATION_ID` | `UUID` | unset | UUID, or blank for unset | Tenant whose drafts are reviewed, sent as `X-Organization-Id` (R23.6). `.env.example` sets the seeded demo tenant `00000000-0000-0000-0000-000000000001` |
```

In `README.md` §7 "5. Access Management & Telemetry Consoles", add as the first bullet:

```markdown
- **Review UI (drafts, timelines, knowledge upload)**: [http://localhost:3001](http://localhost:3001) — bound to 127.0.0.1, no login (ADR-0009); set `FRONTEND__ORGANIZATION_ID` in `.env`
```

In `specs/design.md` §4 tree (divergence D1, recorded for the owner under "Open questions for the owner"): delete the line `├── frontend/                          # review UI` and replace `│   └── dispatch_worker/` with:

```
│   ├── dispatch_worker/
│   └── frontend/                      # review UI: FastAPI + Jinja2 + htmx (ADR-0009)
```

In the same tree, after the `│   ├── business/                      # BusinessDataProvider` line, add the package Task 2 created:

```
│   ├── dispatch/                      # build_outbound_reply, DispatchService (design §5.8)
```

- [ ] **Step 18: Full verification**

Run: `uv run ruff format services/frontend tests/unit/test_frontend_annotate.py tests/unit/test_frontend_review_ui.py packages/core/settings.py`
Run: `uv run ruff check . && uv run mypy packages services tests evaluation`
Expected: `All checks passed!` and `Success: no issues found in N source files`.
Run: `make test-unit`
Expected: all unit tests pass (the new frontend tests included; no existing test changes result).
Run: `uv run python -c "import services.frontend.main as m; print(sorted(r.path for r in m.app.routes))"`
Expected: the list contains `/`, `/drafts`, `/drafts/{draft_id}`, `/drafts/{draft_id}/approve`, `/drafts/{draft_id}/edit`, `/drafts/{draft_id}/reject`, `/healthz`, `/metrics`, `/readyz`, `/static`.

- [ ] **Step 19: Commit**

```bash
git add services/frontend packages/core/settings.py docker-compose.yml Dockerfile .dockerignore \
  scripts/image_smoke.py .env.example docs/configuration.md README.md specs/design.md \
  tests/unit/test_frontend_annotate.py tests/unit/test_frontend_review_ui.py \
  tests/unit/test_settings.py tests/unit/test_runtime_image_contract.py \
  tests/unit/test_dependency_rules.py tests/unit/test_production_imports.py
git status --short frontend   # expect: "D  frontend/.gitkeep" (staged by `git rm` in Step 15)
git commit -m "$(cat <<'EOF'
feat(frontend): review UI draft queue, detail and decisions [task 6.8] [R23.4, R23.6, R16.6, R16.7]

Server-rendered FastAPI + Jinja2 + htmx pages in services/frontend calling /v1 only with
X-Organization-Id: pending-draft queue, draft detail with the original email, thread
summary, citations beside the sentences they support and [BUSINESS DATA] references
highlighted; edit, approve and reject with a client-measured review_ms. FRONTEND__*
settings; compose binds frontend and api to 127.0.0.1 (ADR-0009).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Review UI part 2 — job timeline, knowledge upload, WCAG 2.2 AA checks, Playwright approve/edit flows [tasks.md 6.8]

Depends on Task 6 (task 6.1 drafts API, 6.2 feedback with `review_ms`, migration 0005) for Steps 9–14; Steps 1–8 depend only on Task 9 and existing endpoints.

**Files:**
- Create: `services/frontend/timeline.py`, `services/frontend/knowledge.py`
- Create: `services/frontend/templates/timeline/message.html`, `services/frontend/templates/timeline/_events.html`, `services/frontend/templates/knowledge/index.html`, `services/frontend/templates/knowledge/_documents.html`, `services/frontend/templates/knowledge/_row.html`
- Modify: `services/frontend/api_client.py` (models + 5 methods), `services/frontend/main.py` (2 routers), `services/frontend/templates/base.html` (nav), `services/frontend/templates/drafts/detail.html` (timeline link)
- Create: `tests/unit/test_frontend_timeline_knowledge.py`, `tests/e2e/review_stack.py`, `tests/e2e/conftest.py`, `tests/e2e/test_review_ui_flows.py`, `tests/e2e/test_review_ui_accessibility.py`
- Modify: `pyproject.toml` + `uv.lock` (`uv add --dev playwright`), `tests/unit/test_production_imports.py` (`DEV_ONLY_ROOTS`, line 23), `Makefile` (`.PHONY`, help, `test-e2e`, `ci`), `.github/workflows/ci.yml` (new `e2e-tests` job), `README.md` (§7.4 browser install)

**Interfaces:**
- Consumes: existing `GET /v1/messages/{id}` (`MessageDetailResponse`: `id, subject, sender{email,name}, received_at, direction`), `GET /v1/messages/{id}/timeline?limit=100` (`MessageTimelineResponse`: `message_id, current_state, total_events, events[ProcessingEventResponse]`), `GET /v1/knowledge/documents?limit=100` (`PaginatedResponse[KnowledgeDocumentResponse]`), `GET /v1/knowledge/documents/{id}`, `POST /v1/knowledge/documents` (multipart `file`, `title`, `category` → 202 `DocumentUploadResponse{document, job_id}`); Task 6's `/v1/drafts*` (Task 9 list) served by `services.api.main.create_app` with `app.state.db_pool` and `app.state.publisher` (`publisher.settings` = `BrokerSettings`, `await publisher.publish(exchange_name=…, routing_key=…, envelope=…)`); `feedback(decision, edited_body, edit_distance, review_ms)` and `generated_draft(status, body)` columns; `PostgresJobStore.create_job`, `PostgresDraftStore.create_draft`, `create_pool_from_settings`, `apply_migrations`, `tests.integration.isolation` helpers.
- Produces:
  - `api_client`: `SenderView`, `MessageHeader`, `TimelineEvent`, `MessageTimeline`, `KnowledgeDocumentView`, `DocumentPage`, `DocumentUpload`; `ReviewApiClient.get_message(message_id) -> MessageHeader`, `get_message_timeline(message_id) -> MessageTimeline`, `list_documents(*, limit=100) -> DocumentPage`, `get_document(document_id) -> KnowledgeDocumentView`, `upload_document(*, filename, content, content_type, title, category) -> DocumentUpload`; `TIMELINE_PAGE_SIZE = 100`, `DOCUMENT_PAGE_SIZE = 100`
  - `services.frontend.timeline`: `timeline_router`, `TERMINAL_JOB_STATES = frozenset({JobState.COMPLETED.value, JobState.DEAD_LETTER.value})`, `DETAIL_KEYS`; route `GET /messages/{message_id}/timeline` (htmx ⇒ `timeline/_events.html` fragment, polls every 5 s until terminal)
  - `services.frontend.knowledge`: `knowledge_router`, `TERMINAL_DOCUMENT_STATUSES = frozenset({"active", "failed", "superseded"})`; routes `GET /knowledge[?notice=uploaded]`, `POST /knowledge`, `GET /knowledge/documents/{document_id}/status` (row fragment, polls every 3 s until terminal, announces the final status)
  - `tests/e2e/review_stack.py`: `RecordingPublisher`, `ReviewStack(url, organization_id, publisher, db, run(coro), start(), stop())`, `SeededDraft`, `ReviewOutcome`, `seed_pending_draft(pool, org_id) -> SeededDraft`, `fetch_review_outcome(pool, org_id, draft_id) -> ReviewOutcome`, `delete_organization(pool, org_id)`, constants `SUBJECT`, `DRAFT_BODY`, `THREAD_SUMMARY`
  - `make test-e2e` (`uv run pytest tests/e2e -v`), included in `make ci`; CI job `e2e-tests`

- [ ] **Step 1: Write the failing timeline and knowledge page tests**

Create `tests/unit/test_frontend_timeline_knowledge.py`:

```python
"""Review UI job timeline and knowledge upload over the real /v1 API (task 6.8).

Requirements: R23.5 (job timeline and current state per message from processing_event),
R23.7 (knowledge upload with per-document ingestion status), R23.6 (tenant header).
The frontend's /v1 client is wired in-process (httpx.ASGITransport) to services.api with
in-memory stores, so these tests exercise the real API contract without a network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from packages.core.settings import FrontendServiceSettings, FrontendSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.job import InMemoryJobStore
from packages.db.knowledge import InMemoryKnowledgeStore
from packages.db.message import InMemoryMessageStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage
from packages.domain.knowledge import KnowledgeDocument
from packages.domain.state_machine import JobState
from services.api.main import create_app as create_api_app
from services.frontend.main import create_app as create_frontend_app

ORG = UUID("33333333-3333-4333-8333-333333333333")
HX = {"HX-Request": "true"}
PIPELINE = (
    JobState.NORMALIZED,
    JobState.CLASSIFIED,
    JobState.QUEUED,
    JobState.CONTEXT_READY,
    JobState.GENERATING,
    JobState.DRAFTED,
)


def _api(**state: Any) -> FastAPI:
    app = create_api_app(lifespan_enabled=False)
    app.state.storage_client = FakeObjectStorageClient()
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


@asynccontextmanager
async def _ui(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    settings = FrontendServiceSettings(
        _env_file=None, frontend=FrontendSettings(api_base_url="http://api", organization_id=ORG)
    )
    ui_app = create_frontend_app(
        settings, transport=httpx.ASGITransport(app=api_app), configure_logging=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_app), base_url="http://ui"
    ) as client:
        yield client


async def _message_with_job(
    states: tuple[JobState, ...],
) -> tuple[UUID, InMemoryMessageStore, InMemoryJobStore]:
    messages, jobs = InMemoryMessageStore(), InMemoryJobStore()
    msg_id, thread_id = uuid4(), uuid4()
    await messages.insert_message(
        NormalizedMessage(
            message_id=msg_id,
            thread_id=thread_id,
            mailbox_id=uuid4(),
            organization_id=ORG,
            provider="fake",
            provider_message_id=f"prov-{msg_id.hex[:8]}",
            sender=EmailAddress("alice.smith@clientcorp.com", "Alice Smith"),
            received_at=datetime(2026, 9, 28, 9, 0, tzinfo=UTC),
            subject="Where is order ORD-82915?",
            body_text="Hello",
        )
    )
    job, _ = await jobs.create_job(
        Job(
            organization_id=ORG,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.RECEIVED.value,
            idempotency_key=f"timeline-{uuid4()}",
        )
    )
    for state in states:
        await jobs.transition_job_state(ORG, job.id, state)
    return msg_id, messages, jobs


async def test_timeline_shows_every_event_and_the_current_state_and_keeps_polling() -> None:
    msg_id, messages, jobs = await _message_with_job(PIPELINE)
    async with _ui(_api(message_store=messages, job_store=jobs)) as ui:
        response = await ui.get(f"/messages/{msg_id}/timeline")

    assert response.status_code == 200
    html = response.text
    assert "Where is order ORD-82915?" in html
    assert "Current state: <strong>DRAFTED</strong>" in html
    for state in ("RECEIVED", *(s.value for s in PIPELINE)):
        assert f"<td>{state}</td>" in html
    assert 'hx-trigger="every 5s"' in html


async def test_a_finished_job_timeline_stops_polling() -> None:
    msg_id, messages, jobs = await _message_with_job((*PIPELINE, JobState.COMPLETED))
    async with _ui(_api(message_store=messages, job_store=jobs)) as ui:
        response = await ui.get(f"/messages/{msg_id}/timeline")

    assert "Current state: <strong>COMPLETED</strong>" in response.text
    assert "every 5s" not in response.text


async def test_a_timeline_poll_returns_only_the_events_fragment() -> None:
    msg_id, messages, jobs = await _message_with_job(PIPELINE)
    async with _ui(_api(message_store=messages, job_store=jobs)) as ui:
        response = await ui.get(f"/messages/{msg_id}/timeline", headers=HX)

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="timeline-events"' in response.text


async def test_an_unknown_message_timeline_is_not_found() -> None:
    async with _ui(_api(message_store=InMemoryMessageStore(), job_store=InMemoryJobStore())) as ui:
        response = await ui.get(f"/messages/{uuid4()}/timeline")
    assert response.status_code == 404
    assert "not found" in response.text


async def test_upload_forwards_the_file_and_lists_it_with_its_status() -> None:
    store = InMemoryKnowledgeStore()
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    async with _ui(_api(knowledge_store=store, publisher=publisher)) as ui:
        response = await ui.post(
            "/knowledge",
            data={"title": "Refund policy", "category": "policy"},
            files={"file": ("refund.md", b"# Refunds\n\nWithin 14 days.", "text/markdown")},
            headers=HX,
        )

    assert response.status_code == 200
    assert "Upload accepted: Refund policy is pending." in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "<td>Refund policy</td>" in response.text
    docs, total = await store.list_documents(organization_id=ORG)
    assert total == 1
    assert docs[0].title == "Refund policy"
    assert docs[0].category == "policy"
    publisher.publish.assert_awaited_once()


async def test_an_empty_file_is_refused_without_calling_the_api() -> None:
    store = InMemoryKnowledgeStore()
    async with _ui(_api(knowledge_store=store, publisher=MagicMock())) as ui:
        response = await ui.post(
            "/knowledge", files={"file": ("empty.md", b"", "text/markdown")}, headers=HX
        )

    assert response.status_code == 200
    assert response.headers["HX-Reswap"] == "none"
    assert "Error: Choose a non-empty file to upload." in response.text
    assert (await store.list_documents(organization_id=ORG))[1] == 0


async def test_a_document_row_polls_until_ingestion_finishes_then_announces_it() -> None:
    store = InMemoryKnowledgeStore()
    doc = await store.insert_document(
        KnowledgeDocument(organization_id=ORG, title="Refund policy", status="embedding")
    )
    async with _ui(_api(knowledge_store=store)) as ui:
        running = await ui.get(f"/knowledge/documents/{doc.id}/status", headers=HX)
        await store.update_document_status(ORG, doc.id, "active")
        finished = await ui.get(f"/knowledge/documents/{doc.id}/status", headers=HX)

    assert 'hx-trigger="every 3s"' in running.text
    assert "embedding" in running.text
    assert "hx-swap-oob" not in running.text
    assert "every 3s" not in finished.text
    assert "Refund policy: active" in finished.text


async def test_the_knowledge_page_shows_failures_with_their_reason() -> None:
    store = InMemoryKnowledgeStore()
    doc = await store.insert_document(
        KnowledgeDocument(organization_id=ORG, title="Scanned contract", status="pending")
    )
    await store.update_document_status(ORG, doc.id, "failed", failure_reason="No text layer")
    async with _ui(_api(knowledge_store=store)) as ui:
        response = await ui.get("/knowledge")

    assert response.status_code == 200
    assert "Scanned contract" in response.text
    assert "failed" in response.text
    assert "No text layer" in response.text
    assert '<label for="file">' in response.text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_frontend_timeline_knowledge.py -v`
Expected: FAIL — every test gets HTTP 404 from the frontend (`assert 404 == 200`), because `/messages/{id}/timeline` and `/knowledge` do not exist yet.

- [ ] **Step 3: Extend the API client**

In `services/frontend/api_client.py`, after `DRAFT_PAGE_SIZE = 25` add:

```python
TIMELINE_PAGE_SIZE = 100  # the API's page cap (services/api/pagination.py)
DOCUMENT_PAGE_SIZE = 100
```

After the `DraftDetail` class add:

```python
class SenderView(_ApiModel):
    email: str
    name: str | None = None


class MessageHeader(_ApiModel):
    """GET /v1/messages/{id}: what the timeline page shows about the email."""

    id: UUID
    subject: str = ""
    sender: SenderView | None = None
    received_at: datetime | None = None
    direction: str = "inbound"


class TimelineEvent(_ApiModel):
    """One processing_event row (R18.4, R23.5)."""

    id: int | None = None
    job_id: UUID | None = None
    event_type: str
    state_from: str | None = None
    state_to: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class MessageTimeline(_ApiModel):
    """GET /v1/messages/{id}/timeline (R23.5)."""

    message_id: UUID
    current_state: str | None = None
    total_events: int = 0
    events: list[TimelineEvent] = Field(default_factory=list)


class KnowledgeDocumentView(_ApiModel):
    """A knowledge document and its ingestion status (R9.10, R23.7)."""

    id: UUID
    title: str
    category: str | None = None
    version: int = 1
    status: str
    failure_reason: str | None = None
    updated_at: datetime | None = None


class DocumentPage(_ApiModel):
    items: list[KnowledgeDocumentView] = Field(default_factory=list)
    total_count: int = 0


class DocumentUpload(_ApiModel):
    """POST /v1/knowledge/documents 202 response."""

    document: KnowledgeDocumentView
    job_id: str
```

At the end of `ReviewApiClient` (after `reject_draft`) add:

```python
    async def get_message(self, message_id: UUID) -> MessageHeader:
        return MessageHeader.model_validate(
            await self._request("GET", f"/v1/messages/{message_id}")
        )

    async def get_message_timeline(self, message_id: UUID) -> MessageTimeline:
        payload = await self._request(
            "GET", f"/v1/messages/{message_id}/timeline", params={"limit": TIMELINE_PAGE_SIZE}
        )
        return MessageTimeline.model_validate(payload)

    async def list_documents(self, *, limit: int = DOCUMENT_PAGE_SIZE) -> DocumentPage:
        payload = await self._request("GET", "/v1/knowledge/documents", params={"limit": limit})
        return DocumentPage.model_validate(payload)

    async def get_document(self, document_id: UUID) -> KnowledgeDocumentView:
        return KnowledgeDocumentView.model_validate(
            await self._request("GET", f"/v1/knowledge/documents/{document_id}")
        )

    async def upload_document(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        title: str | None,
        category: str | None,
    ) -> DocumentUpload:
        data = {k: v for k, v in (("title", title), ("category", category)) if v is not None}
        files = {"file": (filename, content, content_type)}
        payload = await self._request("POST", "/v1/knowledge/documents", data=data, files=files)
        return DocumentUpload.model_validate(payload)
```

- [ ] **Step 4: Implement the timeline and knowledge routes**

Create `services/frontend/timeline.py`:

```python
"""Job timeline and current state for one message (R23.5; design.md §5.8 "Review UI").

Reads processing_event through GET /v1/messages/{id}/timeline. While the job is not in a
terminal state the events fragment re-polls every 5 s (htmx), so a reviewer who approved a
draft can watch DISPATCHED -> COMPLETED without reloading.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from packages.domain.state_machine import JobState
from services.frontend.web import api_client, is_htmx, require_organization, templates

TERMINAL_JOB_STATES = frozenset({JobState.COMPLETED.value, JobState.DEAD_LETTER.value})
DETAIL_KEYS = ("reason", "error", "category", "priority", "model_tier", "replayed")

timeline_router = APIRouter(dependencies=[Depends(require_organization)])


@timeline_router.get("/messages/{message_id}/timeline", response_class=HTMLResponse)
async def message_timeline(request: Request, message_id: UUID) -> Response:
    client = api_client(request)
    timeline = await client.get_message_timeline(message_id)
    context: dict[str, Any] = {
        "timeline": timeline,
        "live": timeline.current_state not in TERMINAL_JOB_STATES,
        "detail_keys": DETAIL_KEYS,
        "nav": "drafts",
    }
    if is_htmx(request):
        return templates(request).TemplateResponse(request, "timeline/_events.html", context)
    context["message"] = await client.get_message(message_id)
    return templates(request).TemplateResponse(request, "timeline/message.html", context)
```

Create `services/frontend/knowledge.py`:

```python
"""Knowledge upload with per-document ingestion status (R23.7, R9.10; design.md §5.8).

Uploads are forwarded to POST /v1/knowledge/documents. Each row whose status is not final
re-polls GET /v1/knowledge/documents/{id} every 3 s and announces the final status in the
live region.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.frontend.api_client import ApiError
from services.frontend.web import (
    api_client,
    is_htmx,
    optional_text,
    require_organization,
    templates,
)

TERMINAL_DOCUMENT_STATUSES = frozenset({"active", "failed", "superseded"})
UPLOADED_NOTICE = "Upload accepted. Ingestion status updates below."

knowledge_router = APIRouter(dependencies=[Depends(require_organization)])


@knowledge_router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, notice: str | None = None) -> Response:
    page = await api_client(request).list_documents()
    return templates(request).TemplateResponse(
        request,
        "knowledge/index.html",
        {
            "page": page,
            "terminal": TERMINAL_DOCUMENT_STATUSES,
            "nav": "knowledge",
            "page_notice": UPLOADED_NOTICE if notice == "uploaded" else None,
        },
    )


@knowledge_router.post("/knowledge")
async def upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    category: Annotated[str | None, Form()] = None,
) -> Response:
    content = await file.read()
    if not content:
        raise ApiError(400, "EMPTY_FILE", "Choose a non-empty file to upload.")
    client = api_client(request)
    result = await client.upload_document(
        filename=file.filename or "document",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        title=optional_text(title),
        category=optional_text(category),
    )
    if not is_htmx(request):
        return RedirectResponse("/knowledge?notice=uploaded", status_code=303)
    page = await client.list_documents()
    message = f"Upload accepted: {result.document.title} is {result.document.status}."
    return templates(request).TemplateResponse(
        request,
        "knowledge/_documents.html",
        {"page": page, "terminal": TERMINAL_DOCUMENT_STATUSES, "status_message": message},
    )


@knowledge_router.get("/knowledge/documents/{document_id}/status", response_class=HTMLResponse)
async def document_status(request: Request, document_id: UUID) -> Response:
    doc = await api_client(request).get_document(document_id)
    finished = doc.status in TERMINAL_DOCUMENT_STATUSES
    return templates(request).TemplateResponse(
        request,
        "knowledge/_row.html",
        {
            "doc": doc,
            "terminal": TERMINAL_DOCUMENT_STATUSES,
            "row_notice": f"{doc.title}: {doc.status}" if finished and is_htmx(request) else None,
        },
    )
```

In `services/frontend/main.py`, add after `from services.frontend.drafts import drafts_router`:

```python
from services.frontend.knowledge import knowledge_router
from services.frontend.timeline import timeline_router
```

and after `app.include_router(drafts_router)`:

```python
    app.include_router(timeline_router)
    app.include_router(knowledge_router)
```

- [ ] **Step 5: Templates for the timeline, knowledge pages, nav and the timeline link**

Create `services/frontend/templates/timeline/message.html`:

```html
{% extends "base.html" %}
{% block title %}Timeline{% endblock %}
{% block content %}
<p><a href="/drafts">Back to pending drafts</a></p>
<h1>Processing timeline</h1>
<dl class="meta">
  <dt>Subject</dt><dd>{{ message.subject or "(no subject)" }}</dd>
  <dt>From</dt><dd>{% if message.sender %}{{ (message.sender.name ~ " <" ~ message.sender.email ~ ">") if message.sender.name else message.sender.email }}{% else %}—{% endif %}</dd>
  <dt>Direction</dt><dd>{{ message.direction }}</dd>
</dl>
{% include "timeline/_events.html" %}
{% endblock %}
```

Create `services/frontend/templates/timeline/_events.html`:

```html
<div id="timeline-events"{% if live %} hx-get="/messages/{{ timeline.message_id }}/timeline" hx-trigger="every 5s" hx-swap="outerHTML"{% endif %}>
  <p>Current state: <strong>{{ timeline.current_state or "no events yet" }}</strong>{% if live %} <span class="note">(refreshes every 5 seconds)</span>{% endif %}</p>
  {% if timeline.events %}
  <table>
    <caption class="visually-hidden">Processing events, oldest first</caption>
    <thead>
      <tr><th scope="col">Time</th><th scope="col">Event</th><th scope="col">From</th><th scope="col">To</th><th scope="col">Details</th></tr>
    </thead>
    <tbody>
    {% for e in timeline.events %}
      <tr>
        <td>{{ e.created_at.strftime("%Y-%m-%d %H:%M:%S") }}</td>
        <td>{{ e.event_type }}</td>
        <td>{{ e.state_from or "—" }}</td>
        <td>{{ e.state_to }}</td>
        <td>{% for key in detail_keys if key in e.payload %}{{ key }}={{ e.payload[key] }}{% if not loop.last %}; {% endif %}{% endfor %}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% if timeline.total_events > timeline.events|length %}
  <p class="note">Showing the first {{ timeline.events|length }} of {{ timeline.total_events }} events.</p>
  {% endif %}
  {% else %}
  <p>No processing events are recorded for this message.</p>
  {% endif %}
</div>
```

Create `services/frontend/templates/knowledge/index.html`:

```html
{% extends "base.html" %}
{% block title %}Knowledge{% endblock %}
{% block content %}
<h1>Knowledge documents</h1>
<section class="panel" aria-labelledby="upload-heading">
  <h2 id="upload-heading">Upload a document</h2>
  <form method="post" action="/knowledge" enctype="multipart/form-data"
        hx-post="/knowledge" hx-encoding="multipart/form-data" hx-target="#documents" hx-swap="outerHTML"
        hx-on::after-request="if (event.detail.successful) this.reset()">
    <label for="file">File (PDF, DOCX, HTML, Markdown or text)</label>
    <input id="file" name="file" type="file" required accept=".pdf,.docx,.html,.htm,.md,.txt">
    <label for="title">Title (optional)</label>
    <input id="title" name="title">
    <label for="doc-category">Category (optional)</label>
    <input id="doc-category" name="category">
    <div class="actions"><button type="submit" class="primary">Upload</button></div>
  </form>
</section>
{% include "knowledge/_documents.html" %}
{% endblock %}
```

Create `services/frontend/templates/knowledge/_documents.html`:

```html
<section id="documents" aria-labelledby="documents-heading">
  <h2 id="documents-heading">Documents</h2>
  {% if page.items %}
  <table>
    <caption class="visually-hidden">Knowledge documents and their ingestion status</caption>
    <thead>
      <tr><th scope="col">Title</th><th scope="col">Category</th><th scope="col">Version</th><th scope="col">Status</th><th scope="col">Updated</th></tr>
    </thead>
    <tbody>
    {% for doc in page.items %}{% include "knowledge/_row.html" %}{% endfor %}
    </tbody>
  </table>
  {% else %}
  <p>No documents have been uploaded yet.</p>
  {% endif %}
</section>
{% if status_message %}{% include "_status.html" %}{% endif %}
```

Create `services/frontend/templates/knowledge/_row.html`:

```html
<tr id="doc-{{ doc.id }}"{% if doc.status not in terminal %} hx-get="/knowledge/documents/{{ doc.id }}/status" hx-trigger="every 3s" hx-swap="outerHTML"{% endif %}>
  <td>{{ doc.title }}</td>
  <td>{{ doc.category or "—" }}</td>
  <td>{{ doc.version }}</td>
  <td>{{ doc.status }}{% if doc.failure_reason %}<br><span class="note">{{ doc.failure_reason }}</span>{% endif %}</td>
  <td>{{ doc.updated_at.strftime("%Y-%m-%d %H:%M") if doc.updated_at else "—" }}</td>
</tr>
{% if row_notice %}<div id="status-region" hx-swap-oob="innerHTML">{{ row_notice }}</div>{% endif %}
```

In `services/frontend/templates/base.html`, replace

```html
        <li><a href="/drafts"{% if nav == "drafts" %} aria-current="page"{% endif %}>Pending drafts</a></li>
```

with

```html
        <li><a href="/drafts"{% if nav == "drafts" %} aria-current="page"{% endif %}>Pending drafts</a></li>
        <li><a href="/knowledge"{% if nav == "knowledge" %} aria-current="page"{% endif %}>Knowledge</a></li>
```

In `services/frontend/templates/drafts/detail.html`, replace

```html
  <dt>Job state</dt><dd>{{ draft.job_state or "—" }}</dd>
```

with

```html
  <dt>Job state</dt><dd>{{ draft.job_state or "—" }}{% if draft.message_id %} · <a href="/messages/{{ draft.message_id }}/timeline">Job timeline</a>{% endif %}</dd>
```

- [ ] **Step 6: Run the timeline and knowledge tests**

Run: `uv run pytest tests/unit/test_frontend_timeline_knowledge.py tests/unit/test_frontend_review_ui.py -v`
Expected: PASS (8 + 14 passed).

- [ ] **Step 7: Add Playwright and the browser**

```bash
uv add --dev playwright
uv run playwright install chromium
```

Expected: `pyproject.toml` `[dependency-groups] dev` gains `"playwright>=1.63.0"`, `uv.lock` updates, and Chromium downloads to `~/.cache/ms-playwright` (on a non-Debian host Playwright may print "BEWARE: your OS is not officially supported by Playwright; downloading fallback build" — that build is fine; if launching it fails for missing system libraries, see the open question at the end of this part).

In `tests/unit/test_production_imports.py` extend `DEV_ONLY_ROOTS`:

```python
DEV_ONLY_ROOTS = (
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "pytest_cov",
    "pytest_mock",
    "mypy",
    "playwright",
)
```

Run: `uv run pytest tests/unit/test_production_imports.py -v`
Expected: PASS (no shipped module imports playwright).

- [ ] **Step 8: Commit the timeline and knowledge pages**

```bash
git add services/frontend tests/unit/test_frontend_timeline_knowledge.py \
  tests/unit/test_production_imports.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
feat(frontend): job timeline and knowledge upload pages [task 6.8] [R23.5, R23.7]

Per-message processing_event timeline with the current state, re-polled every 5 s until
COMPLETED or DEAD_LETTER; knowledge upload forwarded to /v1 with per-document ingestion
status rows that re-poll every 3 s and announce the final status. Playwright added as a
dev dependency.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 9: Write the browser-test harness**

Create `tests/e2e/review_stack.py`:

```python
"""A real review UI + real /v1 API on the isolated test database, for browser tests (6.8).

The frontend (services.frontend) serves on 127.0.0.1 from a background uvicorn thread; its
/v1 client runs in-process (httpx.ASGITransport) against services.api with the asyncpg pool
of the rag_email_test database. Only the broker edge is recorded instead of published:
the approve -> email.dispatch publish itself is asserted by the 6.1 integration tests.
Database seeding and assertions run on the stack's own event loop (run()), so the test
thread never runs asyncio while Playwright's sync API is active. No credentials (R24.5).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from collections.abc import Coroutine
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, TypeVar

import asyncpg
import httpx
import uvicorn

from packages.broker.envelope import JobEnvelope
from packages.core.settings import (
    AppSettings,
    BrokerSettings,
    FrontendServiceSettings,
    FrontendSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.state_machine import JobState
from services.api.main import create_app as create_api_app
from services.frontend.main import create_app as create_frontend_app

T = TypeVar("T")

SUBJECT = "Re: Where is order ORD-82915?"
DRAFT_BODY = (
    "Your order ORD-82915 was dispatched on 26 September and should arrive within two "
    "working days. If anything is missing, reply to this email and we will help."
)
THREAD_SUMMARY = "Alice asks for the delivery status of ORD-82915."


class RecordingPublisher:
    """Stands in for MessagePublisher at the broker edge; records what approve publishes."""

    def __init__(self, settings: BrokerSettings) -> None:
        self.settings = settings
        self.published: list[tuple[str, str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((exchange_name, routing_key, envelope))


@dataclass(frozen=True)
class SeededDraft:
    draft_id: uuid.UUID
    job_id: uuid.UUID
    message_id: uuid.UUID


@dataclass
class ReviewOutcome:
    draft_status: str
    draft_body: str
    feedback: list[dict[str, Any]] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class ReviewStack:
    """Frontend on http://127.0.0.1:<port> wired to the real API on the test database."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="review-ui-stack", daemon=True
        )
        self.organization_id = uuid.uuid4()
        self.publisher = RecordingPublisher(AppSettings().broker)
        self.url = ""
        self._pool: asyncpg.Pool | None = None
        self._server: uvicorn.Server | None = None
        self._serving: Future[None] | None = None

    @property
    def db(self) -> asyncpg.Pool:
        assert self._pool is not None, "ReviewStack.start() has not run"
        return self._pool

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def start(self) -> None:
        self._thread.start()
        self._pool = self.run(create_pool_from_settings(AppSettings().database))
        api_app = create_api_app(lifespan_enabled=False)
        api_app.state.db_pool = self._pool
        api_app.state.publisher = self.publisher
        settings = FrontendServiceSettings(
            _env_file=None,
            frontend=FrontendSettings(
                api_base_url="http://api", organization_id=self.organization_id
            ),
        )
        frontend = create_frontend_app(
            settings, transport=httpx.ASGITransport(app=api_app), configure_logging=False
        )
        port = _free_port()
        config = uvicorn.Config(
            frontend, host="127.0.0.1", port=port, log_level="warning", log_config=None
        )
        self._server = uvicorn.Server(config)
        self._serving = asyncio.run_coroutine_threadsafe(self._server.serve(), self.loop)
        deadline = time.monotonic() + 10
        while not self._server.started:
            if self._serving.done():
                self._serving.result()  # surfaces a startup error
            if time.monotonic() > deadline:
                raise RuntimeError("review UI did not start within 10 s")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None and self._serving is not None:
            self._server.should_exit = True
            self._serving.result(timeout=10)
        if self._pool is not None:
            self.run(self._pool.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)


async def seed_pending_draft(pool: asyncpg.Pool, org_id: uuid.UUID) -> SeededDraft:
    """One tenant, mailbox, thread (with summary), inbound email, DRAFTED job and draft."""
    mbx_id, thread_id, msg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Support', 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            "INSERT INTO thread_state (thread_id, organization_id, summary) VALUES ($1, $2, $3)",
            thread_id,
            org_id,
            THREAD_SUMMARY,
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'alice.smith@clientcorp.com',
                      'Alice Smith', '[]', 'Where is order ORD-82915?',
                      'Hello, where is my order ORD-82915? Thanks, Alice', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.DRAFTED.value,
            idempotency_key=f"review-ui-{uuid.uuid4()}",
        )
    )
    draft = await PostgresDraftStore(pool).create_draft(
        GeneratedDraft(
            organization_id=org_id,
            job_id=job.id,
            message_id=msg_id,
            thread_id=thread_id,
            subject=SUBJECT,
            body=DRAFT_BODY,
            confidence=0.82,
            model_name="fake-model",
            model_tier="routine",
            prompt_version="billing.v2",
        )
    )
    return SeededDraft(draft_id=draft.id, job_id=uuid.UUID(str(job.id)), message_id=msg_id)


async def fetch_review_outcome(
    pool: asyncpg.Pool, org_id: uuid.UUID, draft_id: uuid.UUID
) -> ReviewOutcome:
    async with pool.acquire() as conn:
        draft = await conn.fetchrow(
            "SELECT status, body FROM generated_draft WHERE id = $1 AND organization_id = $2",
            draft_id,
            org_id,
        )
        rows = await conn.fetch(
            "SELECT decision, edited_body, edit_distance, review_ms FROM feedback"
            " WHERE draft_id = $1 AND organization_id = $2",
            draft_id,
            org_id,
        )
    assert draft is not None
    return ReviewOutcome(
        draft_status=draft["status"], draft_body=draft["body"], feedback=[dict(r) for r in rows]
    )


async def delete_organization(pool: asyncpg.Pool, org_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organization WHERE id = $1", org_id)
```

Create `tests/e2e/conftest.py`:

```python
"""Fixtures for the review UI browser tests (task 6.8; R23.4, R24.5).

The database is isolated exactly as tests/integration does it (rag_email_test, reset and
migrated once for this package). Playwright's sync API is started inside a package fixture
that depends on that isolation, so no asyncio loop runs in the test thread while it is live.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from packages.core.settings import AppSettings
from packages.db.migrator import apply_migrations
from tests.e2e.review_stack import ReviewStack, delete_organization
from tests.integration.isolation import (
    install_connection_guards,
    isolation_enabled,
    reset_database,
    resolve_test_database,
    resolve_test_vhost,
)


@pytest.fixture(scope="package", autouse=True)
def isolated_database() -> Iterator[None]:
    """Point the browser tests at a fresh, migrated rag_email_test database."""
    if not isolation_enabled():
        yield
        return
    database, vhost = resolve_test_database(), resolve_test_vhost()
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("DATABASE__NAME", database)
        mp.setenv("BROKER__VHOST", vhost)
        settings = AppSettings()
        asyncio.run(reset_database(settings.database, database))
        asyncio.run(apply_migrations(dsn=settings.database.asyncpg_dsn))
        install_connection_guards(mp, vhost=vhost, database=database)
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="package")
def browser(isolated_database: None) -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch()
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    opened = context.new_page()
    opened.set_default_timeout(10_000)
    try:
        yield opened
    finally:
        context.close()


@pytest.fixture
def stack(isolated_database: None) -> Iterator[ReviewStack]:
    review_stack = ReviewStack()
    review_stack.start()
    try:
        yield review_stack
    finally:
        try:
            review_stack.run(delete_organization(review_stack.db, review_stack.organization_id))
        finally:
            review_stack.stop()
```

- [ ] **Step 10: Write the browser tests**

Create `tests/e2e/test_review_ui_flows.py`:

```python
"""Approve and edit flows in a real browser (task 6.8; R23.4, R16.6, R16.7).

Chromium drives the real review UI, which calls the real /v1 drafts API backed by the
isolated rag_email_test database (tests/e2e/review_stack.py).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from packages.core.settings import AppSettings
from tests.e2e.review_stack import (
    SUBJECT,
    THREAD_SUMMARY,
    ReviewStack,
    SeededDraft,
    fetch_review_outcome,
    seed_pending_draft,
)

EDITED_BODY = (
    "Your order ORD-82915 left our warehouse on 26 September; the courier expects to "
    "deliver it tomorrow."
)


def _open_draft_from_queue(page: Page, stack: ReviewStack) -> SeededDraft:
    seeded = stack.run(seed_pending_draft(stack.db, stack.organization_id))
    page.goto(f"{stack.url}/drafts")
    page.get_by_role("link", name=SUBJECT).click()
    expect(page.get_by_role("heading", level=1)).to_have_text(SUBJECT)
    return seeded


def test_approve_from_the_queue_records_accepted_feedback_and_publishes_dispatch(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)
    expect(page.get_by_text(THREAD_SUMMARY)).to_be_visible()
    expect(page.get_by_text("Hello, where is my order ORD-82915? Thanks, Alice")).to_be_visible()

    page.get_by_role("button", name="Approve").click()

    expect(page.get_by_role("status")).to_have_text("Draft approved.")
    expect(page.get_by_role("button", name="Approve")).to_have_count(0)
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "approved"
    assert len(outcome.feedback) == 1
    row = outcome.feedback[0]
    assert row["decision"] == "accepted"
    assert row["edited_body"] is None
    assert row["review_ms"] is not None and row["review_ms"] > 0
    dispatch_queue = AppSettings().broker.queue_dispatch
    assert [(key, env.job_id) for _, key, env in stack.publisher.published] == [
        (dispatch_queue, str(seeded.job_id))
    ]


def test_edit_then_approve_records_edited_feedback_with_the_new_body(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)

    page.get_by_label("Reply text").fill(EDITED_BODY)
    page.get_by_role("button", name="Approve").click()

    expect(page.get_by_role("status")).to_have_text("Draft approved.")
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "approved"
    assert outcome.draft_body == EDITED_BODY
    assert len(outcome.feedback) == 1
    row = outcome.feedback[0]
    assert row["decision"] == "edited"
    assert row["edited_body"] == EDITED_BODY
    assert row["edit_distance"] is not None and row["edit_distance"] > 0
    assert row["review_ms"] is not None and row["review_ms"] > 0
    assert len(stack.publisher.published) == 1


def test_save_changes_persists_the_edit_without_a_decision(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)

    page.get_by_label("Reply text").fill(EDITED_BODY)
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_role("status")).to_have_text("Changes saved.")
    page.reload()

    expect(page.get_by_label("Reply text")).to_have_value(EDITED_BODY)
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "draft"
    assert outcome.feedback == []
    assert stack.publisher.published == []


def test_reject_with_a_reason_records_rejected_feedback_and_publishes_nothing(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)

    page.get_by_label("Reason for rejecting (optional)").fill("Wrong order number.")
    page.get_by_role("button", name="Reject").click()

    expect(page.get_by_role("status")).to_have_text("Draft rejected.")
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "rejected"
    assert [r["decision"] for r in outcome.feedback] == ["rejected"]
    assert stack.publisher.published == []
```

Create `tests/e2e/test_review_ui_accessibility.py`:

```python
"""WCAG 2.2 AA basics on the draft review screen (task 6.8).

2.4.7 / 2.4.11 visible focus, 2.5.8 target size (minimum 24 x 24 CSS px; inline links in
text are exempt), 4.1.3 status messages through a polite live region, 2.4.3 focus order
after an htmx swap.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page, expect

from tests.e2e.review_stack import SUBJECT, ReviewStack, seed_pending_draft

FOCUS_PROBE = """() => {
  const el = document.activeElement;
  const style = getComputedStyle(el);
  return {
    label: (el.textContent || "").trim() || el.getAttribute("name") || el.id || el.tagName,
    outline: style.outlineStyle,
    width: parseFloat(style.outlineWidth),
  };
}"""

TARGETS_PROBE = """els => els.map(el => {
  const box = el.getBoundingClientRect();
  const inline = el.tagName === "A" && el.closest("p, li, dd, td") !== null;
  return {
    label: (el.textContent || "").trim() || el.getAttribute("name") || el.id,
    width: box.width,
    height: box.height,
    inline: inline,
  };
})"""


def _open_seeded_draft(page: Page, stack: ReviewStack) -> None:
    seeded = stack.run(seed_pending_draft(stack.db, stack.organization_id))
    page.goto(f"{stack.url}/drafts/{seeded.draft_id}")
    expect(page.get_by_role("heading", level=1)).to_have_text(SUBJECT)


def test_every_control_reached_by_tab_shows_a_visible_focus_indicator(
    page: Page, stack: ReviewStack
) -> None:
    _open_seeded_draft(page, stack)
    seen: list[dict[str, Any]] = []
    for _ in range(40):
        page.keyboard.press("Tab")
        probe: dict[str, Any] = page.evaluate(FOCUS_PROBE)
        seen.append(probe)
        if probe["label"] == "Reject":
            break
    labels = [p["label"] for p in seen]
    assert "Skip to main content" in labels
    assert "Approve" in labels and "Reject" in labels
    invisible = [p for p in seen if p["outline"] == "none" or p["width"] < 2]
    assert not invisible, f"focus without a visible outline: {invisible}"


def test_interactive_targets_are_at_least_24_px(page: Page, stack: ReviewStack) -> None:
    _open_seeded_draft(page, stack)
    boxes: list[dict[str, Any]] = page.eval_on_selector_all(
        "header a, main a, main button, main input:not([type=hidden]), main textarea",
        TARGETS_PROBE,
    )
    assert len(boxes) >= 6
    too_small = [
        b for b in boxes if not b["inline"] and (b["width"] < 24 or b["height"] < 24)
    ]
    assert not too_small, f"targets under 24 px: {too_small}"


def test_status_messages_use_a_polite_live_region_and_focus_returns_to_the_panel(
    page: Page, stack: ReviewStack
) -> None:
    _open_seeded_draft(page, stack)
    region = page.get_by_role("status")
    expect(region).to_have_attribute("aria-live", "polite")

    page.get_by_role("button", name="Save changes").click()

    expect(region).to_have_text("Changes saved.")
    expect(page.locator("#draft-heading")).to_be_focused()
```

- [ ] **Step 11: Run the browser tests**

Run: `uv run pytest tests/e2e -v`
Expected before Task 6's drafts API exists: FAIL — the queue page shows the 404 error page (`/v1/drafts` not mounted), so `get_by_role("link", name=SUBJECT)` times out. Expected once Tasks 1 and 6 (6.1, 6.2, migration 0005) are committed: PASS (7 passed). If a flow test fails on a DB assertion, that is a real defect in the UI↔API contract: fix the side that diverges from the Interfaces list above, do not relax the assertion.

- [ ] **Step 12: Makefile, CI job and README**

In `Makefile`, add `test-e2e` to the `.PHONY` line (after `test-integration`), add the help line after the `test` help line:

```make
	@echo "  test-e2e - Browser tests of the review UI (Playwright; needs 'uv run playwright install chromium')"
```

add the target after `test-integration:`:

```make
test-e2e:
	$(UV) run pytest tests/e2e -v
```

and change `ci:` to:

```make
ci: fmt-check lint test-unit test-integration test-e2e
```

In `.github/workflows/ci.yml`, append this job under `jobs:` (after `integration-tests`):

```yaml
  e2e-tests:
    name: Review UI Browser Tests (Playwright, Zero Credentials)
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: rag_email
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 5s
          --health-timeout 5s
          --health-retries 5
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          version: "latest"

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: uv sync --all-extras --dev

      - name: Install Chromium for Playwright
        run: uv run playwright install --with-deps chromium

      - name: Run review UI browser tests
        run: uv run pytest tests/e2e -v
        env:
          DATABASE__HOST: localhost
          DATABASE__PORT: 5432
          DATABASE__USER: postgres
          DATABASE__PASSWORD: postgres
          DATABASE__NAME: rag_email
          OPENAI_API_KEY: ""
          ANTHROPIC_API_KEY: ""
          GOOGLE_API_KEY: ""
          GMAIL_ACCESS_TOKEN: ""
```

In `README.md` §7 "4. Run Automated Test Suites", after the `make test` block add:

```markdown
The review UI browser tests (`make test-e2e`, part of `make ci`) need Chromium once:
`uv run playwright install chromium`. They run the real UI and API against the isolated
`rag_email_test` database and never touch the running stack.
```

- [ ] **Step 13: Full verification**

Run: `uv run ruff format services/frontend tests/e2e tests/unit/test_frontend_timeline_knowledge.py`
Run: `make lint`
Expected: `All checks passed!` and mypy `Success: no issues found`.
Run: `make ci`
Expected: fmt-check, lint, unit, integration and e2e all pass (e2e: 7 passed).
Live stack (owner-run, not part of this task's automation): `make up`, then open `http://localhost:3001` — the queue lists the demo tenant's pending drafts; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3001/readyz` prints `200`; `ss -ltn | grep -E ':(3001|8000) '` shows both listening on `127.0.0.1` only.

- [ ] **Step 14: Commit**

```bash
git add tests/e2e Makefile .github/workflows/ci.yml README.md
git commit -m "$(cat <<'EOF'
test(frontend): Playwright approve/edit flows and WCAG 2.2 AA checks [task 6.8] [R23.4, R16.6, R16.7]

Chromium drives the real review UI against the real /v1 drafts API on the isolated
rag_email_test database: approve records accepted feedback with review_ms and publishes
the dispatch job; edit-then-approve records edited feedback with the new body; save and
reject flows. Accessibility checks cover visible focus, 24 px targets, the polite live
region and focus after htmx swaps. New make test-e2e (in make ci) and CI job.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

---

## Part E (Tasks 11–13): End-to-end on fakes, live Gmail gate, Phase 6 close

### Task 11: CI end-to-end test on fakes, fixture email to one provider draft or send [tasks.md 6.9]

**Files:**
- Create: `tests/integration/test_phase6_pipeline_e2e.py`
- Test: the file above is the test. No production code is written in this task. It proves that the pieces from 6.1–6.7 compose through their real entrypoints.

**Interfaces:**
- Consumes (existing code, verified):
  - `services.mail_connector.orchestrator.SyncOrchestrator(checkpoint_store=..., storage_client=..., publisher=..., mailbox_store=..., job_store=..., settings=...)` and `.sync_mailbox(mailbox, adapter=...) -> SyncOutcome` (`status`, `messages_synced`).
  - `services.email_worker.main.build_consumer(res: WorkerResources) -> EmailNormalizationConsumer`
  - `services.triage_worker.main.build_triage_consumer(settings, *, publisher, job_store, message_store, draft_store, connection=None, shutdown_coordinator=None, metrics=None) -> TriageConsumer`
  - `services.ai_worker.main.build_consumers(res, *, llm_provider=None, token_counter=None, embedder=None) -> list[AIWorkerConsumer]`
  - `packages.adapters.fake.FakeProviderAdapter.seed_message(provider_message_id, provider_thread_id=..., raw_payload=...)`
  - `packages.core.idempotency.derive_idempotency_key(organization_id=..., mailbox_id=..., provider_message_id=..., operation_type="dispatch")`
  - `packages.domain.taxonomy.get_default_registry() -> TaxonomyRegistry` (`.all_categories()`, `.get()`, `.register_category()`)
  - `services.api.main.create_app(lifespan_enabled=False)`, with `app.state.db_pool` and `app.state.publisher` set by the test.
- Consumes (from the earlier Phase 6 tasks; the binding contract, exact names):
  - 6.3a `FakeProviderAdapter`:
    - `create_draft(mailbox, reply) -> DraftRef` with `provider_draft_id` and `provider_message_id` both set;
    - `send_draft(mailbox, provider_draft_id: str) -> SentRef`;
    - `get_draft_status(...)` and `find_sent_message(...)`.
  - 6.3 `OutboundReply.thread_id: str`, which holds the provider thread id, and `OutboundReply.message_id: str | None`, in the form `<local@domain>` (see "Contract deviations" at the top of this plan).
  - 6.4 `CategoryDefinition.dispatch_mode: DispatchMode`, where `packages.domain.DispatchMode.SEND_REPLY == "send_reply"`.
  - 6.1 `POST /v1/drafts/{id}/approve`. It accepts the JSON body `{"reviewer": str}`, returns a 2xx status, commits first and then publishes a `JobEnvelope` to exchange `email.dispatch` with routing key `email.dispatch`.
  - 6.5 `services.dispatch_worker.main.build_consumer(res: WorkerResources, *, adapter_resolver: Callable[[Mailbox], MailProviderAdapter] | None = None) -> BaseConsumer` (Task 8 exposes exactly this keyword).
  - 6.5 and 6.7 columns: `generated_draft.provider_draft_id`, `provider_draft_message_id`, `dispatch_idempotency_key`, `status`; `email_message.direction='outbound'`, with `rfc822_message_id` and `in_reply_to` stored without angle brackets.
- Produces: `test_create_draft_mode_ends_with_one_provider_draft_and_no_outbound_message` and `test_send_reply_mode_ends_with_one_send_and_one_outbound_message`, both on a scratch vhost and the `rag_email_test` database. They need Postgres, RabbitMQ and MinIO, as `tests/integration/test_phase1_pipeline_e2e.py` does, and no live credentials (R24.5).

**Why one test drives every worker instead of calling services directly.** tasks.md 6.9 asks for a fixture email carried through ingest → normalize → triage → context → RAG → generate → approve → dispatch. No existing test runs that chain. The Phase 5 e2e starts from a hand-inserted `QUEUED` job (`tests/integration/test_business_data_e2e.py:230`). This test composes each service's own builder on one `WorkerResources`, so what it checks is the production wiring, not a re-assembly of it.

**Why the replay check wraps `process_job`.** A replay of a `COMPLETED` dispatch writes nothing (design §5.8 step 1: "already COMPLETED ⇒ ack, send nothing"). The test therefore cannot wait for a database change. It wraps the dispatch consumer's `process_job` on the instance, records each finished delivery, and waits until the replayed delivery has been processed before it asserts that nothing changed.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_phase6_pipeline_e2e.py`:

```python
"""Phase 6 end to end on fakes: fixture email to one provider draft or one send (6.9, R24.7).

The chain is fixture email -> SyncOrchestrator (fake provider) -> email-worker -> triage-worker
(rules put the email in billing) -> ai-worker (fake LLM, mock embedder; context and RAG over one
billing chunk) -> POST /v1/drafts/{id}/approve -> dispatch-worker (fake provider).

Each service is built by its own production builder on a scratch vhost and the isolated
rag_email_test database. The fake provider records every create_draft and send call, so
"exactly one" is counted at the provider boundary. It does not depend on how the fake stores
drafts after a send.

Requirements: R24.7, R17.1, R17.3, R17.7, R19.2, R19.3
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractQueue
from httpx import ASGITransport, AsyncClient

from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.protocol import MailProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import AIWorkerSettings, AppSettings, BrokerSettings, RetryLadderSettings
from packages.core.storage import MinioObjectStorageClient, get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.db.mailbox import PostgresMailboxStore
from packages.db.message import PostgresMessageStore
from packages.db.seed import deterministic_embed
from packages.domain import DispatchMode
from packages.domain.entities import DraftRef, Mailbox, OutboundReply, SentRef
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import CategoryDefinition, get_default_registry
from packages.knowledge.embedder import FakeEmbedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.main import build_consumers as build_ai_consumers
from services.api.main import create_app
from services.dispatch_worker.main import build_consumer as build_dispatch_consumer
from services.email_worker.main import build_consumer as build_email_consumer
from services.mail_connector.orchestrator import SyncOrchestrator
from services.triage_worker.main import build_triage_consumer
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
PIPELINE_TIMEOUT_S = 60.0
# The urgent-billing rule (config/triage_rules.yaml, id urgent-billing) classifies this email
# as billing without an LLM stage, so the category is deterministic.
SUBJECT = "Overdue payment failure on account"
BODY = "My account balance shows a payment failure and is now past due. Please advise."
BILLING_CHUNK = (
    "Overdue balances: when a card payment fails, the account enters a 7-day grace period. "
    "Customers can retry the payment from the billing portal or ask for a payment plan."
)


class RecordingFakeAdapter(FakeProviderAdapter):
    """The fake provider, recording every provider-side write (R19.3 counts at this boundary)."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[OutboundReply, DraftRef]] = []
        self.draft_sends: list[tuple[str, SentRef]] = []
        self.direct_sends: list[OutboundReply] = []

    async def create_draft(self, mailbox: Mailbox, reply: OutboundReply) -> DraftRef:
        ref = await super().create_draft(mailbox, reply)
        self.created.append((reply, ref))
        return ref

    async def send_draft(self, mailbox: Mailbox, provider_draft_id: str) -> SentRef:
        ref = await super().send_draft(mailbox, provider_draft_id)
        self.draft_sends.append((provider_draft_id, ref))
        return ref

    async def send_reply(self, mailbox: Mailbox, reply: OutboundReply) -> SentRef:
        self.direct_sends.append(reply)  # dispatch must go through the draft, never this
        return await super().send_reply(mailbox, reply)


@dataclasses.dataclass
class Tenant:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    provider_message_id: str
    provider_thread_id: str
    rfc822_id: str  # without angle brackets, as the parser stores it


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "p6e2e") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def dispatch_mode() -> Iterator[Callable[[DispatchMode], None]]:
    """Set every category's dispatch_mode for one test; restore the registry afterwards."""
    registry = get_default_registry()
    originals: dict[str, CategoryDefinition] = {}

    def apply(mode: DispatchMode) -> None:
        for name in registry.all_categories():
            definition = registry.get(name)
            assert definition is not None
            originals.setdefault(name, definition)
            registry.register_category(dataclasses.replace(definition, dispatch_mode=mode))

    try:
        yield apply
    finally:
        for definition in originals.values():
            registry.register_category(definition)


def _fixture_mime(to_address: str, rfc822_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Casey Customer <casey@customer.example.com>"
    msg["To"] = to_address
    msg["Subject"] = SUBJECT
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = f"<{rfc822_id}>"
    msg.set_content(BODY)
    return msg.as_bytes()


async def _seed_tenant(pool: asyncpg.Pool) -> Tenant:
    """A fresh org and mailbox, plus one billing chunk so the RAG step has a document."""
    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    doc_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"p6-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Support', 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            "INSERT INTO knowledge_document (id, organization_id, title, category, status)"
            " VALUES ($1, $2, 'Overdue balance procedure', 'billing', 'active')",
            doc_id,
            org_id,
        )
        await conn.execute(
            "INSERT INTO knowledge_chunk (id, document_id, organization_id, chunk_index, section,"
            " category, content, version, content_tsv)"
            " VALUES ($1, $2, $3, 0, 'Overdue balances', 'billing', $4, 1,"
            " to_tsvector('english', $4))",
            chunk_id,
            doc_id,
            org_id,
            BILLING_CHUNK,
        )
        await conn.execute(
            "INSERT INTO embedding_record (chunk_id, organization_id, model, dim, embedding)"
            " VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)",
            chunk_id,
            org_id,
            deterministic_embed(BILLING_CHUNK, dim=1536),
        )
    return Tenant(
        org_id=org_id,
        mailbox_id=mbx_id,
        provider_message_id=f"fake-msg-{uuid.uuid4().hex[:10]}",
        provider_thread_id=f"fake-thread-{uuid.uuid4().hex[:10]}",
        rfc822_id=f"e2e-{uuid.uuid4().hex}@customer.example.com",
    )


async def _resources(broker: BrokerSettings, pool: asyncpg.Pool) -> WorkerResources:
    settings = AIWorkerSettings(broker=broker, retry=FAST_RETRY)
    connection = await aio_pika.connect_robust(broker.url)
    return WorkerResources(
        settings=settings,
        db_pool=pool,
        connection=connection,
        publisher=MessagePublisher(
            broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
        ),
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=create_pipeline_metrics(),
    )


async def _only_pipeline_job(pool: asyncpg.Pool, org_id: uuid.UUID) -> uuid.UUID:
    rows = await pool.fetch(
        "SELECT id FROM processing_job WHERE organization_id = $1 AND job_type = 'email_pipeline'",
        org_id,
    )
    assert len(rows) == 1, f"expected one pipeline job, got {len(rows)}"
    return uuid.UUID(str(rows[0]["id"]))


async def _wait_for_state(
    pool: asyncpg.Pool, org_id: uuid.UUID, job_id: uuid.UUID, state: JobState
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PIPELINE_TIMEOUT_S
    current: str | None = None
    while loop.time() < deadline:
        current = await pool.fetchval(
            "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
            job_id,
            org_id,
        )
        if current == state.value:
            return
        await asyncio.sleep(0.2)
    events = await pool.fetch(
        "SELECT state_from, state_to, payload FROM processing_event"
        " WHERE job_id = $1 AND organization_id = $2 ORDER BY created_at",
        job_id,
        org_id,
    )
    raise AssertionError(
        f"job {job_id} ended {current}, expected {state.value}; events {[dict(e) for e in events]}"
    )


async def _wait_until(predicate: Callable[[], bool], what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PIPELINE_TIMEOUT_S
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def _run_to_approved(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    tenant: Tenant,
    fake: RecordingFakeAdapter,
    before_approve: Callable[[], None],
    body: Callable[[uuid.UUID, uuid.UUID, AbstractQueue, list[str], AsyncClient], Awaitable[None]],
) -> None:
    """Run every worker, sync the fixture email to DRAFTED, approve it, then hand over to `body`.

    `body(job_id, draft_id, dispatch_probe, dispatch_deliveries, api)` runs while the workers
    are still up; `dispatch_deliveries` gets one job id per finished dispatch delivery.
    """
    storage = get_storage_client(AppSettings().object_storage)
    assert isinstance(storage, MinioObjectStorageClient)
    await storage.bootstrap_buckets()
    res = await _resources(broker, pool)

    email_consumer = build_email_consumer(res)
    triage_consumer = build_triage_consumer(
        res.settings,
        publisher=res.publisher,
        job_store=PostgresJobStore(pool),
        message_store=PostgresMessageStore(pool),
        draft_store=PostgresDraftStore(pool),
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )
    ai_consumers = build_ai_consumers(
        res, llm_provider=FakeLLMProvider(), token_counter=TokenCounter(), embedder=FakeEmbedder()
    )

    def resolve(_mailbox: Mailbox) -> MailProviderAdapter:
        return fake

    dispatch_consumer = build_dispatch_consumer(res, adapter_resolver=resolve)
    deliveries: list[str] = []
    original = dispatch_consumer.process_job

    async def tracked(envelope: JobEnvelope, raw: AbstractIncomingMessage) -> None:
        try:
            await original(envelope, raw)
        finally:
            deliveries.append(envelope.job_id)

    dispatch_consumer.process_job = tracked  # type: ignore[method-assign]
    # The dispatch mode is read from the default taxonomy registry at dispatch time; set it
    # after every builder ran, so a builder that reloads config/categories.yaml cannot undo it.
    before_approve()

    probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
    await probe.bind(broker.exchange_email_dispatch, routing_key=broker.queue_dispatch)

    consumers = [email_consumer, triage_consumer, *ai_consumers, dispatch_consumer]
    for consumer in consumers:
        await consumer.start()
    try:
        fake.seed_message(
            provider_message_id=tenant.provider_message_id,
            provider_thread_id=tenant.provider_thread_id,
            raw_payload=_fixture_mime(f"support-{tenant.mailbox_id.hex[:6]}@example.com", tenant.rfc822_id),
        )
        mailbox = await PostgresMailboxStore(pool).get(tenant.mailbox_id)
        assert mailbox is not None
        outcome = await SyncOrchestrator(
            checkpoint_store=PostgresCheckpointStore(pool),
            storage_client=storage,
            publisher=res.publisher,
            mailbox_store=PostgresMailboxStore(pool),
            job_store=PostgresJobStore(pool),
            settings=res.settings,
        ).sync_mailbox(mailbox, adapter=fake)
        assert outcome.status == "success"
        assert outcome.messages_synced == 1

        job_id = await _only_pipeline_job(pool, tenant.org_id)
        await _wait_for_state(pool, tenant.org_id, job_id, JobState.DRAFTED)

        chunks = await pool.fetchval(
            "SELECT (payload->>'retrieved_chunks_count')::int FROM processing_event"
            " WHERE organization_id = $1 AND job_id = $2 AND state_to = 'CONTEXT_READY'"
            " ORDER BY created_at LIMIT 1",
            tenant.org_id,
            job_id,
        )
        assert chunks is not None and chunks >= 1, "the RAG step retrieved no chunk"
        draft_id = await pool.fetchval(
            "SELECT id FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
            tenant.org_id,
            job_id,
        )
        assert draft_id is not None

        app = create_app(lifespan_enabled=False)
        app.state.db_pool = pool
        app.state.publisher = res.publisher
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api:
            approved = await api.post(
                f"/v1/drafts/{draft_id}/approve",
                headers={"X-Organization-ID": str(tenant.org_id)},
                json={"reviewer": "e2e"},
            )
            assert approved.is_success, approved.text
            await body(job_id, uuid.UUID(str(draft_id)), probe, deliveries, api)
    finally:
        for consumer in consumers:
            await consumer.stop()
        await res.connection.close()


async def _replay_and_reapprove(
    broker: BrokerSettings,
    tenant: Tenant,
    draft_id: uuid.UUID,
    probe: AbstractQueue,
    deliveries: list[str],
    api: AsyncClient,
) -> None:
    """Replay the captured dispatch message, then approve again (6.1, R19.3)."""
    captured = await probe.get(no_ack=True, fail=False, timeout=5)
    assert captured is not None, "approve published nothing to email.dispatch"
    envelope = JobEnvelope.from_message(captured)
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(broker.exchange_email_dispatch, broker.queue_dispatch, envelope)
    finally:
        await publisher.close()
    await _wait_until(lambda: len(deliveries) >= 2, "the replayed dispatch delivery")
    # The probe is bound to the same exchange and key, so it holds a copy of the replay too.
    replayed = await probe.get(no_ack=True, fail=False, timeout=5)
    assert replayed is not None, "the probe did not see the replayed dispatch message"

    again = await api.post(
        f"/v1/drafts/{draft_id}/approve",
        headers={"X-Organization-ID": str(tenant.org_id)},
        json={"reviewer": "e2e"},
    )
    assert again.is_success, again.text
    await asyncio.sleep(0.5)
    # 6.1: a repeated approve re-publishes only while the job is not COMPLETED.
    assert await probe.get(no_ack=True, fail=False) is None


async def _state_transitions(pool: asyncpg.Pool, org_id: uuid.UUID, job_id: uuid.UUID) -> list[str]:
    rows = await pool.fetch(
        "SELECT state_to FROM processing_event WHERE organization_id = $1 AND job_id = $2"
        " AND event_type = 'state_transition' ORDER BY created_at",
        org_id,
        job_id,
    )
    return [str(r["state_to"]) for r in rows]


async def test_create_draft_mode_ends_with_one_provider_draft_and_no_outbound_message(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    dispatch_mode: Callable[[DispatchMode], None],
) -> None:
    """Default mode: one provider draft, no send, no outbound email_message (6.9, R17.1, R17.7)."""
    tenant = await _seed_tenant(pool)
    fake = RecordingFakeAdapter()
    try:

        async def body(
            job_id: uuid.UUID,
            draft_id: uuid.UUID,
            probe: AbstractQueue,
            deliveries: list[str],
            api: AsyncClient,
        ) -> None:
            await _wait_for_state(pool, tenant.org_id, job_id, JobState.COMPLETED)
            await _wait_until(lambda: len(deliveries) >= 1, "the first dispatch delivery")
            await _replay_and_reapprove(broker, tenant, draft_id, probe, deliveries, api)

            assert len(fake.created) == 1
            assert fake.draft_sends == [] and fake.direct_sends == []
            reply, ref = fake.created[0]
            assert reply.thread_id == tenant.provider_thread_id  # never our UUID (R17.2)
            assert reply.subject == f"Re: {SUBJECT}"
            assert reply.in_reply_to == f"<{tenant.rfc822_id}>"
            assert reply.references[-1] == f"<{tenant.rfc822_id}>"

            row = await pool.fetchrow(
                "SELECT status, provider_draft_id, provider_draft_message_id,"
                " dispatch_idempotency_key FROM generated_draft"
                " WHERE organization_id = $1 AND id = $2",
                tenant.org_id,
                draft_id,
            )
            assert row is not None
            assert row["status"] == "dispatched"
            assert row["provider_draft_id"] == ref.provider_draft_id
            assert row["provider_draft_message_id"] == ref.provider_message_id
            assert row["dispatch_idempotency_key"] == derive_idempotency_key(
                organization_id=tenant.org_id,
                mailbox_id=tenant.mailbox_id,
                provider_message_id=tenant.provider_message_id,
                operation_type="dispatch",
            )
            outbound = await pool.fetchval(
                "SELECT count(*) FROM email_message WHERE organization_id = $1"
                " AND direction = 'outbound'",
                tenant.org_id,
            )
            assert outbound == 0  # the customer has received nothing (design §5.8)
            feedback = await pool.fetchval(
                "SELECT count(*) FROM feedback WHERE organization_id = $1 AND draft_id = $2",
                tenant.org_id,
                draft_id,
            )
            assert feedback == 1
            transitions = await _state_transitions(pool, tenant.org_id, job_id)
            assert transitions.count(JobState.DISPATCHED.value) == 1
            assert transitions.count(JobState.COMPLETED.value) == 1
            assert transitions[-1] == JobState.COMPLETED.value

        await _run_to_approved(
            broker,
            channel,
            pool,
            tenant,
            fake,
            lambda: dispatch_mode(DispatchMode.CREATE_DRAFT),
            body,
        )
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", tenant.org_id)


async def test_send_reply_mode_ends_with_one_send_and_one_outbound_message(
    broker: BrokerSettings,
    channel: AbstractChannel,
    pool: asyncpg.Pool,
    dispatch_mode: Callable[[DispatchMode], None],
) -> None:
    """send_reply: one draft, one send of that draft, one outbound message (6.9, R17.3, R17.7)."""
    tenant = await _seed_tenant(pool)
    fake = RecordingFakeAdapter()
    try:

        async def body(
            job_id: uuid.UUID,
            draft_id: uuid.UUID,
            probe: AbstractQueue,
            deliveries: list[str],
            api: AsyncClient,
        ) -> None:
            await _wait_for_state(pool, tenant.org_id, job_id, JobState.COMPLETED)
            await _wait_until(lambda: len(deliveries) >= 1, "the first dispatch delivery")
            await _replay_and_reapprove(broker, tenant, draft_id, probe, deliveries, api)

            assert len(fake.created) == 1
            assert fake.direct_sends == []
            assert len(fake.draft_sends) == 1
            reply, draft_ref = fake.created[0]
            sent_draft_id, sent_ref = fake.draft_sends[0]
            assert sent_draft_id == draft_ref.provider_draft_id
            assert reply.message_id is not None

            rows = await pool.fetch(
                "SELECT m.provider_message_id, m.thread_id, m.rfc822_message_id, m.in_reply_to,"
                " m.subject FROM email_message m WHERE m.organization_id = $1"
                " AND m.direction = 'outbound'",
                tenant.org_id,
            )
            assert len(rows) == 1
            out = rows[0]
            inbound_thread = await pool.fetchval(
                "SELECT thread_id FROM email_message WHERE organization_id = $1"
                " AND provider_message_id = $2",
                tenant.org_id,
                tenant.provider_message_id,
            )
            assert out["provider_message_id"] == sent_ref.provider_message_id
            assert out["thread_id"] == inbound_thread
            assert out["rfc822_message_id"] == reply.message_id.strip("<>")
            assert out["in_reply_to"] == tenant.rfc822_id
            assert out["subject"] == f"Re: {SUBJECT}"

            draft = await pool.fetchrow(
                "SELECT status, provider_ref FROM generated_draft"
                " WHERE organization_id = $1 AND id = $2",
                tenant.org_id,
                draft_id,
            )
            assert draft is not None
            assert draft["status"] == "dispatched"
            assert draft["provider_ref"] == sent_ref.provider_message_id
            transitions = await _state_transitions(pool, tenant.org_id, job_id)
            assert transitions.count(JobState.DISPATCHED.value) == 1
            assert transitions[-1] == JobState.COMPLETED.value

        await _run_to_approved(
            broker,
            channel,
            pool,
            tenant,
            fake,
            lambda: dispatch_mode(DispatchMode.SEND_REPLY),
            body,
        )
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", tenant.org_id)
```

The `dispatch_mode` fixture is synchronous (the registry is in-process), so its restore runs in plain teardown. The organization is deleted in `finally`; the delete cascades.

- [ ] **Step 2: Run it and watch it fail for the right reason**

Run: `uv run pytest tests/integration/test_phase6_pipeline_e2e.py -v`

Expected before 6.1–6.7 are merged: FAIL at import, `ModuleNotFoundError: No module named 'services.dispatch_worker.main'` or `ImportError: cannot import name 'DispatchMode' from 'packages.domain'`.

Expected with 6.1–6.7 merged: both tests PASS. If a test fails, the assertion names the broken hop. For example:
- `job … ended DRAFTED, expected COMPLETED` means the approve publish or the dispatch consumer failed;
- `expected one pipeline job` means sync created more than one job.

Fix the owning 6.x task's code with a RED→GREEN test there, and do not loosen this test (GEMINI.md, "Implementation Integrity").

- [ ] **Step 3: Lint and type-check the new file**

Run:
```bash
uv run ruff format tests/integration/test_phase6_pipeline_e2e.py
uv run ruff check tests/integration/test_phase6_pipeline_e2e.py
uv run mypy tests/integration/test_phase6_pipeline_e2e.py
```
Expected: `1 file left unchanged` or `1 file reformatted`, then `All checks passed!` and `Success: no issues found in 1 source file`.

- [ ] **Step 4: Run the whole integration suite once**

Run: `uv run pytest tests/integration -q`

Expected: every test passes. The new tests use their own scratch vhost (`p6e2e`) and a fresh organization, so they do not touch the shared queues that `test_phase1_pipeline_e2e.py` purges.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_phase6_pipeline_e2e.py
git commit -m "$(cat <<'EOF'
test(dispatch): end-to-end fixture email to one provider draft or send on fakes [task 6.9] [R24.7, R17.1, R17.3, R17.7, R19.2, R19.3]

Drives sync, email-worker, triage-worker, ai-worker, the approve endpoint and the
dispatch-worker through their production builders on a scratch vhost. create_draft mode
ends with one provider draft and no outbound message; send_reply mode with one draft,
one send of that draft and one outbound email_message. Replaying the dispatch message and
approving again change nothing.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Connect a real Gmail mailbox, the live Phase 6 gate, and the runbook [tasks.md 6.10, 6.7 check]

**Files:**
- Modify: `tests/conftest.py`, lines 13–20 (`PROVIDER_SECRET_ENV_VARS`)
- Modify: `packages/adapters/gmail.py`, lines 270–345 (`synchronize`: INBOX filter and skipping vanished messages; see Step 3 and Open Question E1)
- Modify: `tests/unit/test_gmail_adapter.py` (append the tests)
- Create: `scripts/connect_gmail.py`
- Create: `tests/unit/test_connect_gmail.py`
- Create: `tests/integration/test_connect_gmail_postgres.py`
- Create: `scripts/phase6_gate.py`
- Create: `tests/unit/test_phase6_gate.py`
- Modify: `Makefile`, line 1 (`.PHONY`), the help block (lines 5–21), and targets after `phase5-gate` (line 97)
- Modify: `pyproject.toml`, the `[dependency-groups] dev` list (line 31): add `python-dotenv`
- Modify: `docker-compose.yml`, the `mail-connector` environment (line ~198) and the `dispatch-worker` environment (as rewritten by 6.5)
- Modify: `tests/unit/test_runtime_image_contract.py` (append the compose test)
- Modify: `.env.example` (a new last section) and `docs/configuration.md` (§2.4 table and prose)
- Modify: `docs/demo-runbook.md`: §3.4 (line 137), §5.3 (line 190), §6 (lines 196–210) and §7 (the troubleshooting table)
- Modify: `specs/tasks.md`, the 6.7 entry (lines 744–747): add the check result as a note

**Interfaces:**
- Consumes:
  - Gmail REST (checked against current docs on 2026-09-28):
    - `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` returns `{emailAddress, messagesTotal, threadsTotal, historyId}`. It accepts the `gmail.modify` scope.
    - `users.history.list` takes `labelId` ("Only return messages with a label matching the ID"). A stale `startHistoryId` returns 404.
  - The existing `stack_smoke` helpers: `SmokeFailure`, `check_services`, `wait_for_state`, `wait_for_job_message`, and module attribute `TIMEOUT_S`.
  - `packages.adapters.gmail.GmailProviderAdapter(access_token=...)` and `.get_thread(mailbox, provider_thread_id) -> RawThread`.
  - `packages.broker.routing.load_categories_from_yaml(path, registry)`, `packages.domain.taxonomy.TaxonomyRegistry`, and `AppSettings().routing.categories_config_path`.
  - From 6.1:
    - `GET /v1/drafts?status=draft` returns JSON with `items: [{"id": ...}, ...]`;
    - `GET /v1/drafts/{id}` returns 200;
    - `POST /v1/drafts/{id}/approve` accepts `{"reviewer": str}`.
  - From 6.4: `CategoryDefinition.dispatch_mode`.
  - From 6.5/6.7: the outbound `email_message` row. `provider_message_id` is the `drafts.send` response id, and `rfc822_message_id` / `in_reply_to` are stored without brackets.
- Produces:
  - In `scripts/connect_gmail.py`:
    - `GMAIL_TOKEN_VAR = "GMAIL_ACCESS_TOKEN"` and `CREDENTIALS_REF = "env:GMAIL_ACCESS_TOKEN"`
    - `class ConnectError(RuntimeError)`
    - `resolve_gmail_token(environ: Mapping[str, str], dotenv: Mapping[str, str | None]) -> str`
    - `async fetch_profile(http: httpx.AsyncClient, token: str) -> dict[str, Any]`
    - `check_profile(profile: Mapping[str, Any], address: str) -> str` (returns the historyId)
    - `async register_mailbox(pool, *, organization_id: UUID, address: str, history_id: str) -> UUID`
    - `async run(address: str, organization_id: UUID) -> UUID` and `main(argv: list[str] | None = None) -> int`
  - In `scripts/phase6_gate.py`:
    - `gate_subject(token: str) -> str`
    - `check_dispatch_mode(registry: TaxonomyRegistry, category: str) -> None`
    - `check_api_dispatch_mode(detail: Mapping[str, Any]) -> None` (the API's own `dispatch_mode` field on `GET /v1/drafts/{id}`, which Task 6 fills from the YAML the API loads at startup)
    - `check_dispatch_consumer(consumers: Mapping[str, int], queue: str) -> None`
    - `find_listed_draft(page: Mapping[str, Any], draft_id: str) -> Mapping[str, Any]`
    - `check_outbound_row(row: Mapping[str, Any], *, original_rfc822_id: str, original_subject: str) -> None`
    - `check_gmail_thread(messages: Sequence[tuple[str, bytes]], *, sent_provider_id: str, reply_rfc822_id: str, original_rfc822_id: str, original_subject: str) -> None`
    - `@dataclass(frozen=True) class ReplaySnapshot(job_state: str, outbound_rows: int, thread_messages: int, transitions: int, feedback_rows: int)`
    - `check_replay(before: ReplaySnapshot, after: ReplaySnapshot) -> None`
    - `async run() -> None` and `main() -> int`
  - Make targets `connect-gmail ADDRESS=...` and `phase6-gate`.

**The 6.7 check, done by reading the code (the result goes into tasks.md in Step 20):**

```
Gmail mailbox                         our stack
─────────────                         ─────────
INBOX  customer email ──history.list─▶ sync ─▶ normalize (direction='inbound') ─▶ triage ─▶ …
DRAFT  our create_draft ──history.list─▶ sync ─▶ normalize (direction='inbound'!) ─▶ triage ─▶ a new draft to ourselves
SENT   drafts.send copy ──history.list─▶ sync ─▶ dedup on provider_message_id ─▶ dropped only if step 5 already wrote it
(DRAFT deleted by drafts.send) ─▶ get_message 404 inside synchronize ─▶ the whole sync fails, the cursor never advances
```

Here is what the code does today:
- `gmail.py:273`: `history.list` has no `labelId`. The initial `messages.list` at `:339` has no `labelIds` either.
- `normalizer.py:116` hard-codes `direction="inbound"`.
- Triage never looks at `direction`.
- `gmail.py:321` fetches every id outside the `try`, and the orchestrator does not catch `NotFound`.

Sync therefore does ingest our own mail. In `create_draft` mode (the default for every category) it re-triages our own draft as a new inbound email. After a `send_reply` dispatch, the next sync fails permanently on the deleted draft. That is the "does not" case of tasks.md 6.7, so the gap is recorded in the 6.7 notes.

Without a fix, the live gate can run once but not twice, and demo step 6 (create_draft) replies to itself. Steps 2–3 implement the smallest fix: sync only `INBOX`, and skip a message that vanished between `history.list` and `messages.get`. It changes which provider messages are ingested, which is public behaviour, so it needs the owner's approval (Open Question E1). Everything else in this task works without it.

- [ ] **Step 1: Strip the Gmail and Graph tokens from every test process**

`tests/integration/test_phase1_pipeline_e2e.py:158` calls Google whenever `GMAIL_ACCESS_TOKEN` is exported. This task tells the owner to put that token in `.env`, so the guard must strip it first (R24.5).

Check first: `grep -n "GMAIL_ACCESS_TOKEN" tests/conftest.py`. Expected: no output. If an earlier Phase 6 task already added it, skip to Step 2.

In `tests/conftest.py`, replace the list:

```python
PROVIDER_SECRET_ENV_VARS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "GMAIL_CLIENT_SECRET",
    "MS_GRAPH_CLIENT_SECRET",
    # Short-lived mailbox tokens for the owner-run live gate (6.10); the registry falls back to
    # them for any mailbox without credentials_ref (packages/adapters/registry.py).
    "GMAIL_ACCESS_TOKEN",
    "GRAPH_ACCESS_TOKEN",
]
```

Run: `GMAIL_ACCESS_TOKEN=ya29.live-looking uv run pytest tests/integration/test_phase1_pipeline_e2e.py::test_real_gmail_mailbox_adapter_capability -v`

Expected: PASS without any network call. The token is deleted before the test reads it, so the test takes its no-token branch.

- [ ] **Step 2 (needs Open Question E1): Failing tests, Gmail sync reads only INBOX and skips vanished messages**

Append to `tests/unit/test_gmail_adapter.py`:

```python
@pytest.mark.asyncio
async def test_incremental_sync_asks_only_for_inbox_messages() -> None:
    """6.7: our own drafts and sent copies carry DRAFT/SENT, not INBOX; sync must not ingest them."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/history" in str(request.url):
            return httpx.Response(200, json={"history": [], "historyId": "501"}, request=request)
        return httpx.Response(404, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mailbox = Mailbox(id="mbx-inbox", organization_id="org-01", provider="gmail", address="a@b.c")
    await adapter.synchronize(mailbox, Checkpoint(mailbox_id="mbx-inbox", history_id="500"))

    history_calls = [u for u in seen if "/history" in u]
    assert len(history_calls) == 1
    assert "labelId=INBOX" in history_calls[0]


@pytest.mark.asyncio
async def test_initial_sync_lists_only_inbox_messages() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []}, request=request)
        return httpx.Response(404, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mailbox = Mailbox(id="mbx-init", organization_id="org-01", provider="gmail", address="a@b.c")
    await adapter.synchronize(mailbox, Checkpoint(mailbox_id="mbx-init", history_id=None))

    assert any("labelIds=INBOX" in u for u in seen), seen


@pytest.mark.asyncio
async def test_sync_skips_a_message_deleted_after_history_listed_it() -> None:
    """drafts.send deletes the draft; a 404 on one message must not fail the whole sync (6.7)."""
    raw_ok = encode_urlsafe_b64(b"Subject: kept\r\n\r\nbody")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/history" in url:
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "gone-1"}}]},
                        {"messagesAdded": [{"message": {"id": "kept-1"}}]},
                    ],
                    "historyId": "700",
                },
                request=request,
            )
        if "/messages/kept-1" in url:
            return httpx.Response(
                200, json={"id": "kept-1", "threadId": "th-k", "raw": raw_ok}, request=request
            )
        return httpx.Response(404, json={"error": {"code": 404}}, request=request)

    adapter = GmailProviderAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mailbox = Mailbox(id="mbx-gone", organization_id="org-01", provider="gmail", address="a@b.c")
    res = await adapter.synchronize(mailbox, Checkpoint(mailbox_id="mbx-gone", history_id="650"))

    assert [m.provider_message_id for m in res.messages] == ["kept-1"]
    assert res.requires_full_resync is False
    assert res.new_checkpoint.history_id == "700"
```

Run: `uv run pytest tests/unit/test_gmail_adapter.py -k "inbox or deleted_after" -v`

Expected: FAIL.
- `assert "labelId=INBOX" in ...` fails.
- `assert any("labelIds=INBOX" ...)` fails.
- The third test raises `packages.adapters.exceptions.NotFound`.

- [ ] **Step 3 (needs Open Question E1): Implement the INBOX filter and the skip**

In `packages/adapters/gmail.py`, add a module logger under the imports. Check first with `grep -n "^logger" packages/adapters/gmail.py`; add it only if that prints nothing:

```python
import logging

logger = logging.getLogger(__name__)
```

In `synchronize`, change the history URL:

```python
            base_history_url = (
                f"{self.base_url}/history?startHistoryId={cp.history_id}"
                "&historyTypes=messageAdded&labelId=INBOX"
            )
```

Replace the fetch loop after the history pages:

```python
            fetched_messages: list[RawMessage] = []
            for mid in msg_ids:
                try:
                    fetched_messages.append(await self.get_message(mailbox, mid))
                except NotFound:
                    # Deleted between history.list and messages.get (a draft that drafts.send
                    # replaced, or mail the user deleted): nothing left to ingest (6.7).
                    logger.warning(
                        "Gmail message %s vanished before fetch; skipped",
                        mid,
                        extra={"mailbox_id": str(mailbox.id)},
                    )
```

Change the initial-sync URL:

```python
        url = f"{self.base_url}/messages?maxResults=50&labelIds=INBOX"
```

Update the `synchronize` docstring to read: `"""Incrementally synchronize Gmail INBOX additions using history.list (R1.1, R2.5, R17.7)."""`

Run: `uv run pytest tests/unit/test_gmail_adapter.py -v`

Expected: every test PASSES. That includes the existing pagination test at `:320–383`, whose handler matches on the `/history` substring and `pageToken=page-tok-2`, both still present.

Commit:

```bash
git add packages/adapters/gmail.py tests/unit/test_gmail_adapter.py tests/conftest.py
git commit -m "$(cat <<'EOF'
fix(adapters): sync only Gmail INBOX and skip vanished messages; strip mailbox tokens in tests [task 6.7] [R17.7, R2.5, R24.5]

history.list and the initial messages.list now filter on INBOX, so our own provider
drafts and sent copies are not ingested and re-triaged as inbound mail, and a message
deleted between history.list and messages.get (a draft replaced by drafts.send) is
skipped instead of failing the whole sync. The test guard now strips
GMAIL_ACCESS_TOKEN and GRAPH_ACCESS_TOKEN.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

If the owner declines E1, commit only `tests/conftest.py`, with subject `test(guard): strip mailbox access tokens from test processes [task 6.10] [R24.5]`, and skip Steps 2–3.

- [ ] **Step 4: Add python-dotenv as a direct dev dependency**

The scripts read `GMAIL_ACCESS_TOKEN` from the host `.env`, as `AppSettings` does for its own keys. The token is not a settings field: it belongs only to the adapters (`packages/adapters/registry.py`). `python-dotenv` is already locked (`uv.lock:1749`) as a pydantic-settings dependency. Declaring it keeps the scripts from relying on a transitive package.

Run: `uv add --dev python-dotenv`

Expected: `pyproject.toml` `dev` gains `"python-dotenv>=1.2.3"`, and `uv.lock` changes only in the dev group. The runtime image stays `--no-dev`.

- [ ] **Step 5: Failing unit tests for connect_gmail's pure parts**

Create `tests/unit/test_connect_gmail.py`:

```python
"""scripts/connect_gmail.py registers the live test mailbox safely (6.10; R17.1, R24.5)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TOKEN = "ya29.test-token-never-printed"


def _load() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("connect_gmail", SCRIPTS / "connect_gmail.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cg = _load()


def test_credentials_ref_points_at_the_env_var() -> None:
    assert cg.CREDENTIALS_REF == "env:GMAIL_ACCESS_TOKEN"


def test_token_comes_from_the_environment_first() -> None:
    assert cg.resolve_gmail_token({"GMAIL_ACCESS_TOKEN": TOKEN}, {"GMAIL_ACCESS_TOKEN": "x"}) == TOKEN


def test_token_falls_back_to_the_dotenv_file() -> None:
    assert cg.resolve_gmail_token({}, {"GMAIL_ACCESS_TOKEN": f"  {TOKEN} "}) == TOKEN


@pytest.mark.parametrize("dotenv", [{}, {"GMAIL_ACCESS_TOKEN": ""}, {"GMAIL_ACCESS_TOKEN": None}])
def test_a_blank_token_is_refused(dotenv: dict[str, str | None]) -> None:
    with pytest.raises(cg.ConnectError, match="GMAIL_ACCESS_TOKEN"):
        cg.resolve_gmail_token({"GMAIL_ACCESS_TOKEN": "  "}, dotenv)


def test_profile_of_the_named_account_returns_its_history_id() -> None:
    profile = {"emailAddress": "Demo.Box@gmail.com", "historyId": "123456", "messagesTotal": 3}
    assert cg.check_profile(profile, "demo.box@gmail.com") == "123456"


def test_profile_of_another_account_is_refused() -> None:
    with pytest.raises(cg.ConnectError, match="other@gmail.com"):
        cg.check_profile({"emailAddress": "other@gmail.com", "historyId": "1"}, "demo@gmail.com")


def test_profile_without_history_id_is_refused() -> None:
    with pytest.raises(cg.ConnectError, match="historyId"):
        cg.check_profile({"emailAddress": "demo@gmail.com"}, "demo@gmail.com")


async def test_fetch_profile_sends_the_bearer_token_to_the_profile_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"emailAddress": "demo@gmail.com", "historyId": "9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        profile = await cg.fetch_profile(http, TOKEN)
    assert profile["historyId"] == "9"
    assert str(seen[0].url) == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_an_expired_token_names_the_fix_and_hides_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 401}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(cg.ConnectError) as exc_info:
            await cg.fetch_profile(http, TOKEN)
    assert "expired" in str(exc_info.value)
    assert "§3.2" in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)
```

Run: `uv run pytest tests/unit/test_connect_gmail.py -v`

Expected: FAIL at collection with `FileNotFoundError: ... scripts/connect_gmail.py`.

- [ ] **Step 6: Failing Postgres test for register_mailbox**

Create `tests/integration/test_connect_gmail_postgres.py`:

```python
"""connect_gmail.register_mailbox upserts the mailbox and arms its checkpoint (6.10)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("connect_gmail", SCRIPTS / "connect_gmail.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cg = _load()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


async def test_register_twice_keeps_one_mailbox_and_moves_the_cursor(pool: asyncpg.Pool) -> None:
    org_id = uuid.uuid4()
    address = f"demo-{org_id.hex[:6]}@gmail.com"
    await pool.execute("INSERT INTO organization (id, name) VALUES ($1, 'cg')", org_id)
    try:
        # A mailbox marked needs_reauth after a 401 must come back active (runbook §7).
        first = await cg.register_mailbox(
            pool, organization_id=org_id, address=address, history_id="100"
        )
        await pool.execute(
            "UPDATE mailbox SET status = 'needs_reauth' WHERE id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        await pool.execute(
            "UPDATE mailbox_checkpoint SET sync_state = 'error', pending_followup = true"
            " WHERE mailbox_id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        second = await cg.register_mailbox(
            pool, organization_id=org_id, address=address.upper(), history_id="250"
        )
        assert second == first

        mbx = await pool.fetchrow(
            "SELECT provider, address, credentials_ref, status FROM mailbox"
            " WHERE id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        assert mbx is not None
        assert dict(mbx) == {
            "provider": "gmail",
            "address": address,
            "credentials_ref": "env:GMAIL_ACCESS_TOKEN",
            "status": "active",
        }
        cp = await pool.fetchrow(
            "SELECT history_id, sync_state, pending_followup FROM mailbox_checkpoint"
            " WHERE mailbox_id = $1 AND organization_id = $2",
            first,
            org_id,
        )
        assert cp is not None
        assert dict(cp) == {"history_id": "250", "sync_state": "idle", "pending_followup": False}
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_register_into_a_missing_organization_is_refused(pool: asyncpg.Pool) -> None:
    with pytest.raises(cg.ConnectError, match="make seed"):
        await cg.register_mailbox(
            pool, organization_id=uuid.uuid4(), address="x@gmail.com", history_id="1"
        )
```

Run: `uv run pytest tests/integration/test_connect_gmail_postgres.py -v`

Expected: FAIL at collection with `FileNotFoundError: ... scripts/connect_gmail.py`.

- [ ] **Step 7: Implement scripts/connect_gmail.py**

Create `scripts/connect_gmail.py`:

```python
"""Register the Gmail test account as a watched mailbox (specs/tasks.md 6.10; R17.1, R2.1).

Owner-run on the host after `make up` and `make seed`, with a fresh GMAIL_ACCESS_TOKEN in .env
(docs/demo-runbook.md §3.2–3.3):

    make connect-gmail ADDRESS=ragemail.demo.<you>@gmail.com

What it does, stopping at the first failure:
  1. Reads GMAIL_ACCESS_TOKEN from the environment or .env. It never prints the token.
  2. Calls Gmail users.getProfile and refuses a token that belongs to another account.
  3. Upserts the mailbox into the demo tenant (Acme) with provider gmail,
     credentials_ref env:GMAIL_ACCESS_TOKEN and status active.
  4. Sets the mailbox checkpoint to the account's current historyId, so the first sync
     imports only mail that arrives from now on (not the last 50 messages, and never the
     literal 'initial' cursor that the initial-sync path would store).
Re-running it is safe. It also re-activates a mailbox that a 401 marked needs_reauth, and it
moves the start point to "now".
No push notifications reach localhost, so new mail is pulled with
POST /v1/mailboxes/<id>/resync (runbook §6 step 4; scripts/phase6_gate.py does it for you).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from dotenv import dotenv_values

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import DEMO_ORG_ID

GMAIL_TOKEN_VAR = "GMAIL_ACCESS_TOKEN"
CREDENTIALS_REF = f"env:{GMAIL_TOKEN_VAR}"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class ConnectError(RuntimeError):
    """Connecting the mailbox failed; the message says why and what to do."""


def resolve_gmail_token(environ: Mapping[str, str], dotenv: Mapping[str, str | None]) -> str:
    """The token from the process environment, else from .env; blank counts as missing."""
    for source in (environ.get(GMAIL_TOKEN_VAR), dotenv.get(GMAIL_TOKEN_VAR)):
        if source and source.strip():
            return source.strip()
    raise ConnectError(
        f"{GMAIL_TOKEN_VAR} is not set: mint a token (docs/demo-runbook.md §3.2) "
        "and put it in .env (§3.3)"
    )


async def fetch_profile(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    """Gmail users.getProfile for the token's account."""
    resp = await http.get(PROFILE_URL, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        raise ConnectError(
            "Gmail says the access token is invalid or expired (HTTP 401): mint a new one "
            "(docs/demo-runbook.md §3.2), update .env, then run make up"
        )
    if resp.status_code != 200:
        raise ConnectError(f"Gmail getProfile returned HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if not isinstance(body, dict):
        raise ConnectError(f"Gmail getProfile returned a non-object body: {body!r}")
    return body


def check_profile(profile: Mapping[str, Any], address: str) -> str:
    """The token must belong to `address`; returns the mailbox's current historyId."""
    owner = str(profile.get("emailAddress", "")).strip().lower()
    if owner != address.strip().lower():
        raise ConnectError(
            f"the token belongs to {owner or '<unknown>'}, not {address}: sign in to the "
            "OAuth Playground as the test account only (docs/demo-runbook.md §3.2)"
        )
    history_id = str(profile.get("historyId") or "").strip()
    if not history_id:
        raise ConnectError("Gmail getProfile returned no historyId")
    return history_id


async def register_mailbox(
    pool: asyncpg.Pool[Any], *, organization_id: UUID, address: str, history_id: str
) -> UUID:
    """Upsert the mailbox (active, env credentials) and point its checkpoint at `history_id`."""
    normalized = address.strip().lower()
    async with pool.acquire() as conn, conn.transaction():
        org = await conn.fetchval("SELECT id FROM organization WHERE id = $1", organization_id)
        if org is None:
            raise ConnectError(f"organization {organization_id} does not exist: run make seed first")
        mailbox_id = await conn.fetchval(
            """
            INSERT INTO mailbox (
                id, organization_id, provider, address, display_name, status, credentials_ref
            ) VALUES (gen_random_uuid(), $1, 'gmail', $2, 'Gmail test account', 'active', $3)
            ON CONFLICT (organization_id, address) DO UPDATE SET
                provider = 'gmail',
                status = 'active',
                credentials_ref = EXCLUDED.credentials_ref
            RETURNING id
            """,
            organization_id,
            normalized,
            CREDENTIALS_REF,
        )
        await conn.execute(
            """
            INSERT INTO mailbox_checkpoint (
                mailbox_id, organization_id, history_id, sync_state, last_sync_at,
                pending_followup
            ) VALUES ($1, $2, $3, 'idle', now(), false)
            ON CONFLICT (mailbox_id) DO UPDATE SET
                history_id = EXCLUDED.history_id,
                sync_state = 'idle',
                last_sync_at = now(),
                pending_followup = false
            WHERE mailbox_checkpoint.organization_id = EXCLUDED.organization_id
            """,
            mailbox_id,
            organization_id,
            history_id,
        )
    return UUID(str(mailbox_id))


async def run(address: str, organization_id: UUID) -> UUID:
    token = resolve_gmail_token(os.environ, dotenv_values(".env"))
    async with httpx.AsyncClient(timeout=10.0) as http:
        history_id = check_profile(await fetch_profile(http, token), address)
    print(f"ok   token belongs to {address}; current historyId {history_id}")
    settings = AppSettings()
    pool = await create_pool_from_settings(settings.database)
    try:
        mailbox_id = await register_mailbox(
            pool, organization_id=organization_id, address=address, history_id=history_id
        )
    finally:
        await pool.close()
    print(
        f"ok   mailbox {mailbox_id} in organization {organization_id}: provider gmail, "
        f"credentials_ref {CREDENTIALS_REF}, status active, watching from now"
    )
    return mailbox_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the Gmail test account (task 6.10).")
    parser.add_argument("--address", required=True, help="the Gmail test account's address")
    parser.add_argument(
        "--org-id", type=UUID, default=DEMO_ORG_ID, help="tenant to register into (demo: Acme)"
    )
    args = parser.parse_args(argv)
    if not args.address.strip():
        print("FAIL usage: make connect-gmail ADDRESS=<test account address>", file=sys.stderr)
        return 2
    try:
        mailbox_id = asyncio.run(run(args.address, args.org_id))
    except ConnectError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"CONNECT GMAIL OK mailbox_id={mailbox_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The address is stored lower-cased, so a re-run in a different case hits the same `UNIQUE (organization_id, address)` row, and the Postgres test checks this. `gen_random_uuid()` is built into PostgreSQL 13 and later (the stack runs 16). The checkpoint upsert's `WHERE` keeps it tenant-scoped.

Run:
```bash
uv run pytest tests/unit/test_connect_gmail.py -v
uv run pytest tests/integration/test_connect_gmail_postgres.py -v
```
Expected: all PASS (9 unit tests and 2 Postgres tests). `test_a_blank_token_is_refused` is parametrized 3 ways, so the unit summary counts 11 items.

- [ ] **Step 8: Makefile targets**

In `Makefile`, append ` connect-gmail phase6-gate` to the `.PHONY` line (line 1). Add two help lines after the `phase5-gate` echo:

```make
	@echo "  connect-gmail ADDRESS=... - Register the Gmail test account as a watched mailbox, owner-run (task 6.10)"
	@echo "  phase6-gate - Live Phase 6 gate: real email -> draft -> approve -> threaded Gmail reply, owner-run (task 6.10)"
```

After the `phase5-gate` target, add:

```make
connect-gmail:
	@test -n "$(ADDRESS)" || { echo "usage: make connect-gmail ADDRESS=<test account address>"; exit 2; }
	$(UV) run python scripts/connect_gmail.py --address $(ADDRESS)

phase6-gate:
	$(UV) run python scripts/phase6_gate.py
```

Run: `make -n connect-gmail ADDRESS=demo@gmail.com && make -n phase6-gate && make connect-gmail; echo "exit $$?"`

Expected: the two dry-run recipes print, then `usage: make connect-gmail ADDRESS=<test account address>` and `exit 2`. The runbook's Phase 6 detector must also count 1 or more: `grep -c 'phase6-gate' Makefile` prints `3` or more.

- [ ] **Step 9: Failing compose test, the token reaches only the services that call Gmail**

Append to `tests/unit/test_runtime_image_contract.py`:

```python
def test_compose_forwards_the_gmail_token_only_to_the_services_that_call_gmail() -> None:
    """6.10: mail-connector syncs and dispatch-worker drafts/sends; nothing else gets the token."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    holders = sorted(
        name
        for name, service in compose["services"].items()
        if "GMAIL_ACCESS_TOKEN" in (service.get("environment") or {})
    )
    assert holders == ["dispatch-worker", "mail-connector"]
    for name in holders:
        env = compose["services"][name]["environment"]
        assert env["GMAIL_ACCESS_TOKEN"] == "${GMAIL_ACCESS_TOKEN:-}", name
```

Run: `uv run pytest tests/unit/test_runtime_image_contract.py -k gmail_token -v`

Expected: FAIL with `assert ['dispatch-worker'] == ['dispatch-worker', 'mail-connector']` (Task 8 already forwards the token to the dispatch-worker).

- [ ] **Step 10: Forward the token in docker-compose.yml**

In the `mail-connector` service's `environment`, after `LEASE_REAPER__ENABLED: "false"`, add:

```yaml
      # Short-lived Gmail token for the live test mailbox (6.10); blank keeps it offline.
      GMAIL_ACCESS_TOKEN: ${GMAIL_ACCESS_TOKEN:-}
```

The `dispatch-worker` service already forwards it: Task 8 wrote `GMAIL_ACCESS_TOKEN: ${GMAIL_ACCESS_TOKEN:-}` into its `environment`. Do not add it a second time (a duplicate YAML key); check with `grep -c 'GMAIL_ACCESS_TOKEN' docker-compose.yml` (expected after this step: `2`).

Do not add it to `x-app-env`: that block reaches every app service, and the registry's env fallback (`packages/adapters/registry.py:58–61`) would then point every `gmail` mailbox without a `credentials_ref` at the real account in more processes than need it.

Run: `uv run pytest tests/unit/test_runtime_image_contract.py -v && docker compose config --quiet`

Expected: all PASS, and `docker compose config` exits 0 with no output.

- [ ] **Step 11: Config documentation**

Append a new last section to `.env.example`. Use the next free number after the sections added by the earlier Phase 6 tasks (the `FRONTEND__*` section from 6.8). If that is 21, this is 22:

```bash
# --- 22. Live Gmail test mailbox (R17.1, task 6.10, docs/demo-runbook.md §3) ---
# Short-lived OAuth access token (scope gmail.modify) for the owner-run live demo and
# make phase6-gate. Leave blank for offline use and CI: tests strip it (R24.5). Compose
# forwards it to mail-connector and dispatch-worker only. The stack never refreshes it.
# For the gate, set billing's dispatch_mode to send_reply in config/categories.yaml
# (dispatch_mode is a per-category YAML key, not an env var; default create_draft, task 6.4).
GMAIL_ACCESS_TOKEN=
```

In `docs/configuration.md` §2.4, add a row to the table:

```markdown
| `GMAIL_ACCESS_TOKEN` | `string` (secret) | blank | A `ya29.` access token, about 1 hour lifetime | Token read at call time by the Gmail adapter for mailboxes with `credentials_ref=env:GMAIL_ACCESS_TOKEN` (set by `make connect-gmail`), and as the registry fallback for `gmail` mailboxes with no `credentials_ref`. Never refreshed by the stack (ADR-0009). Blank in CI; the test guard strips it (R24.5). |
```

Below the table, add:

```markdown
- **Docker Compose.** `GMAIL_ACCESS_TOKEN` is forwarded from the host `.env` only to `mail-connector` (sync) and `dispatch-worker` (drafts and sends), not through `x-app-env`. Containers read it at start: after minting a new token, run `make up`.
- **Connecting the test account.** `make connect-gmail ADDRESS=<address>` checks that the token belongs to that address, then registers it in the demo tenant with `credentials_ref=env:GMAIL_ACCESS_TOKEN`, starting from the account's current `historyId` (`docs/demo-runbook.md` §3.4).
- **Dispatch mode** is not an environment variable: it is set per category as `dispatch_mode: create_draft | send_reply` in `config/categories.yaml` (default `create_draft`, task 6.4). The image copies `config/`, so a change needs `make up`.
```

Run: `grep -n "GMAIL_ACCESS_TOKEN" .env.example docs/configuration.md`

Expected: one hit in `.env.example` (plus its comment lines) and at least two in `docs/configuration.md`.

- [ ] **Step 12: Commit the connect command and the forwarding**

```bash
git add scripts/connect_gmail.py tests/unit/test_connect_gmail.py tests/integration/test_connect_gmail_postgres.py Makefile pyproject.toml uv.lock docker-compose.yml tests/unit/test_runtime_image_contract.py .env.example docs/configuration.md
git commit -m "$(cat <<'EOF'
feat(mailbox): make connect-gmail registers the live test account; forward GMAIL_ACCESS_TOKEN [task 6.10] [R17.1, R2.1, R24.5]

connect_gmail.py refuses a token of another account (users.getProfile), upserts the
mailbox into the demo tenant with credentials_ref env:GMAIL_ACCESS_TOKEN and status
active, and arms its checkpoint at the current historyId. Compose forwards the token
to mail-connector and dispatch-worker only; .env.example and docs/configuration.md
document it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 13: Failing unit tests for the gate's pure checks**

Create `tests/unit/test_phase6_gate.py`:

```python
"""scripts/phase6_gate.py checks what the Phase 6 gate claims (6.10; R17.1–R17.7)."""

from __future__ import annotations

import importlib.util
import sys
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from packages.domain.taxonomy import TaxonomyRegistry

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ORIGINAL_ID = "cust-123@customer.example.com"
REPLY_ID = "reply-9@rag-email.local"
SUBJECT = "Overdue payment failure on account [gate-abc123]"


def _load_gate() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the gate imports its siblings stack_smoke, connect_gmail
    spec = importlib.util.spec_from_file_location("phase6_gate", SCRIPTS / "phase6_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _registry(mode: str) -> TaxonomyRegistry:
    registry = TaxonomyRegistry()
    registry.register_from_dict(
        {
            "category": "billing",
            "description": "Billing",
            "default_reply_required": True,
            "default_retrieval_required": True,
            "default_workflow_hint": "ai",
            "dispatch_mode": mode,
        }
    )
    return registry


def _mime(msg_id: str, subject: str, in_reply_to: str | None, references: str | None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "demo@gmail.com"
    msg["To"] = "casey@customer.example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{msg_id}>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content("body")
    return msg.as_bytes()


def _snapshot(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "job_state": "COMPLETED",
        "outbound_rows": 1,
        "thread_messages": 2,
        "transitions": 9,
        "feedback_rows": 1,
    }
    values.update(overrides)
    return gate.ReplaySnapshot(**values)


def test_gate_subject_hits_the_urgent_billing_rule_and_carries_the_token() -> None:
    subject = gate.gate_subject("abc123")
    assert "[gate-abc123]" in subject
    assert "payment failure" in subject.lower() and "account" in subject.lower()


def test_dispatch_mode_send_reply_is_accepted() -> None:
    gate.check_dispatch_mode(_registry("send_reply"), "billing")


def test_dispatch_mode_create_draft_names_the_file_to_edit() -> None:
    with pytest.raises(gate.SmokeFailure, match="config/categories.yaml"):
        gate.check_dispatch_mode(_registry("create_draft"), "billing")


def test_unknown_category_is_refused() -> None:
    with pytest.raises(gate.SmokeFailure, match="support"):
        gate.check_dispatch_mode(_registry("send_reply"), "support")


def test_api_dispatch_mode_must_say_send_reply() -> None:
    """The review UI shows the API's field; a stale API would tell the reviewer 'draft'."""
    gate.check_api_dispatch_mode({"dispatch_mode": "send_reply"})
    with pytest.raises(gate.SmokeFailure, match="make up"):
        gate.check_api_dispatch_mode({"dispatch_mode": "create_draft"})
    with pytest.raises(gate.SmokeFailure, match="dispatch_mode"):
        gate.check_api_dispatch_mode({})


def test_dispatch_queue_needs_a_consumer() -> None:
    gate.check_dispatch_consumer({"email.dispatch": 1}, "email.dispatch")
    with pytest.raises(gate.SmokeFailure, match="email.dispatch"):
        gate.check_dispatch_consumer({"email.dispatch": 0}, "email.dispatch")
    with pytest.raises(gate.SmokeFailure, match="email.dispatch"):
        gate.check_dispatch_consumer({}, "email.dispatch")


def test_listed_draft_is_found_by_id() -> None:
    page = {"items": [{"id": "d-1"}, {"id": "d-2"}], "next_cursor": None}
    assert gate.find_listed_draft(page, "d-2") == {"id": "d-2"}
    with pytest.raises(gate.SmokeFailure, match="d-3"):
        gate.find_listed_draft(page, "d-3")


def test_outbound_row_must_reply_to_the_original() -> None:
    good = {
        "rfc822_message_id": REPLY_ID,
        "in_reply_to": ORIGINAL_ID,
        "subject": f"Re: {SUBJECT}",
        "provider_message_id": "gm-sent-1",
    }
    gate.check_outbound_row(good, original_rfc822_id=ORIGINAL_ID, original_subject=SUBJECT)
    for key, bad in [
        ("rfc822_message_id", None),
        ("in_reply_to", "other@x"),
        ("subject", f"Re: Re: {SUBJECT}"),
    ]:
        with pytest.raises(gate.SmokeFailure, match=key):
            gate.check_outbound_row(
                {**good, key: bad}, original_rfc822_id=ORIGINAL_ID, original_subject=SUBJECT
            )


def test_gmail_thread_holds_our_threaded_reply() -> None:
    messages = [
        ("gm-orig", _mime(ORIGINAL_ID, SUBJECT, None, None)),
        ("gm-sent-1", _mime(REPLY_ID, f"Re: {SUBJECT}", f"<{ORIGINAL_ID}>", f"<{ORIGINAL_ID}>")),
    ]
    gate.check_gmail_thread(
        messages,
        sent_provider_id="gm-sent-1",
        reply_rfc822_id=REPLY_ID,
        original_rfc822_id=ORIGINAL_ID,
        original_subject=SUBJECT,
    )


def test_gmail_thread_without_our_message_fails() -> None:
    with pytest.raises(gate.SmokeFailure, match="gm-sent-1"):
        gate.check_gmail_thread(
            [("gm-orig", _mime(ORIGINAL_ID, SUBJECT, None, None))],
            sent_provider_id="gm-sent-1",
            reply_rfc822_id=REPLY_ID,
            original_rfc822_id=ORIGINAL_ID,
            original_subject=SUBJECT,
        )


def test_gmail_replacing_our_message_id_fails_with_the_reason() -> None:
    """Research §8 Q1: if Gmail rewrites Message-ID, our stored id cannot thread replies."""
    messages = [
        ("gm-sent-1", _mime("gmail-made@mail.gmail.com", f"Re: {SUBJECT}", f"<{ORIGINAL_ID}>", None))
    ]
    with pytest.raises(gate.SmokeFailure, match="Message-ID"):
        gate.check_gmail_thread(
            messages,
            sent_provider_id="gm-sent-1",
            reply_rfc822_id=REPLY_ID,
            original_rfc822_id=ORIGINAL_ID,
            original_subject=SUBJECT,
        )


def test_gmail_reply_must_carry_in_reply_to() -> None:
    messages = [("gm-sent-1", _mime(REPLY_ID, f"Re: {SUBJECT}", None, None))]
    with pytest.raises(gate.SmokeFailure, match="In-Reply-To"):
        gate.check_gmail_thread(
            messages,
            sent_provider_id="gm-sent-1",
            reply_rfc822_id=REPLY_ID,
            original_rfc822_id=ORIGINAL_ID,
            original_subject=SUBJECT,
        )


def test_replay_that_changes_nothing_passes() -> None:
    gate.check_replay(_snapshot(), _snapshot())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_state", "DISPATCHED"),
        ("outbound_rows", 2),
        ("thread_messages", 3),
        ("transitions", 10),
        ("feedback_rows", 2),
    ],
)
def test_replay_that_changes_anything_fails(field: str, value: Any) -> None:
    with pytest.raises(gate.SmokeFailure, match=field):
        gate.check_replay(_snapshot(), _snapshot(**{field: value}))
```

Run: `uv run pytest tests/unit/test_phase6_gate.py -v`

Expected: FAIL at collection with `FileNotFoundError: ... scripts/phase6_gate.py`.

- [ ] **Step 14: Implement scripts/phase6_gate.py**

Create `scripts/phase6_gate.py`:

```python
"""Live-stack check for the Phase 6 gate (specs/tasks.md 6.10; R17.1–R17.7, R16.6, R19.3).

Run on the host by the owner, never under pytest (it needs a live Gmail token, R24.5):

    # .env: GMAIL_ACCESS_TOKEN=<fresh token, docs/demo-runbook.md §3.2>
    # config/categories.yaml: dispatch_mode: send_reply on the billing category (gate only)
    make up
    make seed
    make connect-gmail ADDRESS=<test account>
    make phase6-gate            # then send the email it asks for, from another address

Checks, stopping at the first failure:
  1. GMAIL_ACCESS_TOKEN is set (never printed); the categories file routes billing to
     send_reply; the API is ready; every hosted queue and email.dispatch has a consumer.
  2. Exactly one mailbox connected by make connect-gmail exists in the demo tenant.
  3. The owner's email, whose subject carries this run's token, is pulled by
     POST /v1/mailboxes/{id}/resync and reaches DRAFTED through the pipeline.
  4. The draft is reviewable: listed by GET /v1/drafts?status=draft and readable by
     GET /v1/drafts/{id}; its category's dispatch_mode is send_reply.
  5. POST /v1/drafts/{id}/approve takes the job to COMPLETED; the draft is dispatched with a
     provider ref; exactly one outbound email_message replies to the original (R17.7).
  6. In Gmail, the thread holds the sent reply with our Message-ID, In-Reply-To and
     References naming the original, and exactly one "Re: " (R17.2).
  7. Replaying the captured dispatch message and approving again change nothing: same job
     state, outbound rows, Gmail thread size, state transitions and feedback rows (R19.3).
Nothing is deleted afterwards: the mail is real, and the demo tenant keeps the conversation.
Set billing back to create_draft and run make up after the gate (runbook §5.3).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email import message_from_bytes
from email.policy import default as default_policy
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import httpx
import stack_smoke
from connect_gmail import CREDENTIALS_REF, ConnectError, resolve_gmail_token
from dotenv import dotenv_values
from stack_smoke import SmokeFailure, check_services, wait_for_job_message, wait_for_state

from packages.adapters.exceptions import ProviderError
from packages.adapters.gmail import GmailProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.broker.routing import load_categories_from_yaml
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import DEMO_ORG_ID
from packages.domain import DispatchMode
from packages.domain.entities import Mailbox
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry

API = "http://localhost:8000/v1"
EMAIL_TIMEOUT_S = 600.0  # the owner sends the email by hand
RESYNC_EVERY_S = 10.0
DRAFT_TIMEOUT_S = 120.0
DISPATCH_TIMEOUT_S = 60.0
REPLAY_SETTLE_S = 15.0
GATE_BODY = (
    "Hello, my account shows a payment failure and the balance is now past due. "
    "What should I do?"
)


@dataclass(frozen=True)
class ReplaySnapshot:
    """Everything a second dispatch could change (R19.3)."""

    job_state: str
    outbound_rows: int
    thread_messages: int
    transitions: int
    feedback_rows: int


def gate_subject(token: str) -> str:
    """A subject the urgent-billing rule classifies as billing, carrying this run's token."""
    return f"Overdue payment failure on account [gate-{token}]"


def check_dispatch_mode(registry: TaxonomyRegistry, category: str) -> None:
    definition = registry.get(category)
    if definition is None:
        raise SmokeFailure(f"category {category!r} is not in config/categories.yaml")
    if definition.dispatch_mode != DispatchMode.SEND_REPLY:
        raise SmokeFailure(
            f"category {category!r} has dispatch_mode {definition.dispatch_mode!s}: set "
            f"dispatch_mode: send_reply on it in config/categories.yaml for the gate, then make up"
        )


def check_api_dispatch_mode(detail: Mapping[str, Any]) -> None:
    """The API reports the mode the dispatch-worker will use (both load the same YAML)."""
    mode = detail.get("dispatch_mode")
    if mode != DispatchMode.SEND_REPLY.value:
        raise SmokeFailure(
            f"GET /v1/drafts/{{id}} reports dispatch_mode {mode!r}, expected 'send_reply': the "
            "API has not loaded the edited config/categories.yaml; run make up"
        )


def check_dispatch_consumer(consumers: Mapping[str, int], queue: str) -> None:
    if consumers.get(queue, 0) < 1:
        raise SmokeFailure(f"no consumer on {queue}: is dispatch-worker healthy? (runbook §4)")


def find_listed_draft(page: Mapping[str, Any], draft_id: str) -> Mapping[str, Any]:
    for item in page.get("items") or []:
        if str(item.get("id")) == draft_id:
            return dict(item)
    raise SmokeFailure(f"draft {draft_id} is not in GET /v1/drafts?status=draft")


def check_outbound_row(
    row: Mapping[str, Any], *, original_rfc822_id: str, original_subject: str
) -> None:
    if not row.get("rfc822_message_id"):
        raise SmokeFailure("outbound email_message has no rfc822_message_id")
    if row.get("in_reply_to") != original_rfc822_id:
        raise SmokeFailure(
            f"outbound in_reply_to {row.get('in_reply_to')!r}, expected {original_rfc822_id!r}"
        )
    if row.get("subject") != f"Re: {original_subject}":
        raise SmokeFailure(f"outbound subject {row.get('subject')!r}, expected one 'Re: '")


def _ids(value: object) -> list[str]:
    return [part.strip("<>") for part in str(value or "").split() if part.strip("<>")]


def check_gmail_thread(
    messages: Sequence[tuple[str, bytes]],
    *,
    sent_provider_id: str,
    reply_rfc822_id: str,
    original_rfc822_id: str,
    original_subject: str,
) -> None:
    """Gmail's copy of our reply is in the thread and threads onto the original (R17.2)."""
    raw = next((payload for pid, payload in messages if pid == sent_provider_id), None)
    if raw is None:
        raise SmokeFailure(
            f"Gmail thread has no message {sent_provider_id} (ids: {[p for p, _ in messages]})"
        )
    parsed = message_from_bytes(raw, policy=default_policy)
    got_id = str(parsed.get("Message-ID", "")).strip().strip("<>")
    if got_id != reply_rfc822_id:
        raise SmokeFailure(
            f"Gmail's copy has Message-ID {got_id!r}, not ours {reply_rfc822_id!r}: our stored "
            "id cannot thread the customer's next reply (research §8 Q1)"
        )
    if _ids(parsed.get("In-Reply-To")) != [original_rfc822_id]:
        raise SmokeFailure(f"In-Reply-To {parsed.get('In-Reply-To')!r}, expected the original")
    if original_rfc822_id not in _ids(parsed.get("References")):
        raise SmokeFailure(f"References {parsed.get('References')!r} lacks the original")
    if str(parsed.get("Subject", "")) != f"Re: {original_subject}":
        raise SmokeFailure(f"Gmail subject {parsed.get('Subject')!r}, expected one 'Re: '")


def check_replay(before: ReplaySnapshot, after: ReplaySnapshot) -> None:
    for field in ("job_state", "outbound_rows", "thread_messages", "transitions", "feedback_rows"):
        if getattr(before, field) != getattr(after, field):
            raise SmokeFailure(
                f"replay changed {field}: {getattr(before, field)!r} -> {getattr(after, field)!r}"
            )


async def queue_consumers(settings: AppSettings) -> dict[str, int]:
    vhost = quote(settings.broker.vhost, safe="")
    async with httpx.AsyncClient(timeout=5.0) as http:
        resp = await http.get(
            f"http://{settings.broker.host}:15672/api/queues/{vhost}",
            auth=(settings.broker.user, settings.broker.password),
            params={"columns": "name,consumers"},
        )
        resp.raise_for_status()
    return {q["name"]: int(q["consumers"]) for q in resp.json()}


async def connected_mailbox(pool: asyncpg.Pool[Any]) -> Mailbox:
    rows = await pool.fetch(
        "SELECT id, address FROM mailbox WHERE organization_id = $1 AND provider = 'gmail'"
        " AND credentials_ref = $2 AND status = 'active'",
        DEMO_ORG_ID,
        CREDENTIALS_REF,
    )
    if len(rows) != 1:
        raise SmokeFailure(
            f"{len(rows)} connected Gmail mailboxes in the demo tenant, expected 1: "
            "run make connect-gmail ADDRESS=<test account> (runbook §3.4)"
        )
    return Mailbox(
        id=rows[0]["id"],
        organization_id=DEMO_ORG_ID,
        provider="gmail",
        address=rows[0]["address"],
        credentials_ref=CREDENTIALS_REF,
    )


async def wait_for_owner_email(
    http: httpx.AsyncClient, pool: asyncpg.Pool[Any], mailbox: Mailbox, token: str
) -> asyncpg.Record:
    headers = {"X-Organization-ID": str(DEMO_ORG_ID)}
    deadline = time.monotonic() + EMAIL_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = await http.post(f"{API}/mailboxes/{mailbox.id}/resync", headers=headers, json={})
        if resp.status_code == 409:
            raise SmokeFailure(
                f"resync refused (409): {resp.text[:200]}. After a 401 the mailbox is "
                "needs_reauth: mint a token, make up, make connect-gmail again (runbook §7)"
            )
        if resp.status_code != 202:
            raise SmokeFailure(f"resync returned {resp.status_code}: {resp.text[:200]}")
        row = await pool.fetchrow(
            "SELECT id, thread_id, provider_message_id, rfc822_message_id, subject"
            " FROM email_message WHERE organization_id = $1 AND mailbox_id = $2"
            " AND direction = 'inbound' AND subject LIKE $3",
            DEMO_ORG_ID,
            mailbox.id,
            f"%[gate-{token}]%",
        )
        if row is not None:
            return row
        await asyncio.sleep(RESYNC_EVERY_S)
    raise SmokeFailure(f"no email with [gate-{token}] arrived within {EMAIL_TIMEOUT_S:.0f}s")


async def snapshot(
    pool: asyncpg.Pool[Any],
    adapter: GmailProviderAdapter,
    mailbox: Mailbox,
    job_id: UUID,
    draft_id: UUID,
    thread_id: UUID,
    provider_thread_id: str,
) -> ReplaySnapshot:
    org = DEMO_ORG_ID
    gmail_thread = await adapter.get_thread(mailbox, provider_thread_id)
    return ReplaySnapshot(
        job_state=str(
            await pool.fetchval(
                "SELECT state FROM processing_job WHERE id = $1 AND organization_id = $2",
                job_id,
                org,
            )
        ),
        outbound_rows=int(
            await pool.fetchval(
                "SELECT count(*) FROM email_message WHERE organization_id = $1"
                " AND thread_id = $2 AND direction = 'outbound'",
                org,
                thread_id,
            )
        ),
        thread_messages=len(gmail_thread.messages),
        transitions=int(
            await pool.fetchval(
                "SELECT count(*) FROM processing_event WHERE organization_id = $1"
                " AND job_id = $2 AND event_type = 'state_transition'",
                org,
                job_id,
            )
        ),
        feedback_rows=int(
            await pool.fetchval(
                "SELECT count(*) FROM feedback WHERE organization_id = $1 AND draft_id = $2",
                org,
                draft_id,
            )
        ),
    )


async def run() -> None:
    settings = AppSettings()
    gmail_token = resolve_gmail_token(os.environ, dotenv_values(".env"))
    print("ok   GMAIL_ACCESS_TOKEN is set (not printed)")
    registry = TaxonomyRegistry()
    load_categories_from_yaml(settings.routing.categories_config_path, registry)
    check_dispatch_mode(registry, "billing")
    print("ok   config/categories.yaml: billing dispatch_mode send_reply")
    await check_services(settings)
    check_dispatch_consumer(await queue_consumers(settings), settings.broker.queue_dispatch)
    print(f"ok   consumer on {settings.broker.queue_dispatch}")

    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    try:
        mailbox = await connected_mailbox(pool)
        print(f"ok   connected mailbox {mailbox.address} ({mailbox.id})")
        adapter = GmailProviderAdapter(access_token=gmail_token)
        token = uuid4().hex[:8]
        subject = gate_subject(token)
        print(
            f"\n>>> From ANOTHER address, send an email to {mailbox.address} now.\n"
            f">>> Subject: {subject}\n>>> Body:    {GATE_BODY}\n"
            f">>> Waiting up to {EMAIL_TIMEOUT_S / 60:.0f} minutes...\n"
        )
        headers = {"X-Organization-ID": str(DEMO_ORG_ID)}
        async with httpx.AsyncClient(timeout=15.0) as http:
            inbound = await wait_for_owner_email(http, pool, mailbox, token)
            print(f"ok   email {inbound['provider_message_id']} ingested into our thread")
            job_id = await pool.fetchval(
                "SELECT id FROM processing_job WHERE organization_id = $1 AND message_id = $2",
                DEMO_ORG_ID,
                inbound["id"],
            )
            if job_id is None:
                raise SmokeFailure(f"message {inbound['id']} has no processing job")
            stack_smoke.TIMEOUT_S = DRAFT_TIMEOUT_S
            await wait_for_state(pool, DEMO_ORG_ID, job_id, {JobState.DRAFTED.value})
            category = await pool.fetchval(
                "SELECT category FROM classification_result WHERE organization_id = $1"
                " AND message_id = $2 ORDER BY created_at DESC LIMIT 1",
                DEMO_ORG_ID,
                inbound["id"],
            )
            check_dispatch_mode(registry, str(category))
            draft_id = await pool.fetchval(
                "SELECT id FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
                DEMO_ORG_ID,
                job_id,
            )
            listed = await http.get(f"{API}/drafts", headers=headers, params={"status": "draft"})
            if listed.status_code != 200:
                raise SmokeFailure(f"GET /v1/drafts returned {listed.status_code}")
            find_listed_draft(listed.json(), str(draft_id))
            detail = await http.get(f"{API}/drafts/{draft_id}", headers=headers)
            if detail.status_code != 200:
                raise SmokeFailure(f"GET /v1/drafts/{draft_id} returned {detail.status_code}")
            check_api_dispatch_mode(detail.json())
            print(f"ok   job DRAFTED ({category}); draft {draft_id} listed, readable, send_reply")

            channel = await connection.channel(on_return_raises=True)
            probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
            await probe.bind(
                settings.broker.exchange_email_dispatch, routing_key=settings.broker.queue_dispatch
            )
            approved = await http.post(
                f"{API}/drafts/{draft_id}/approve", headers=headers, json={"reviewer": "phase6-gate"}
            )
            if not approved.is_success:
                raise SmokeFailure(f"approve returned {approved.status_code}: {approved.text}")
            captured = await wait_for_job_message(probe, str(job_id))
            stack_smoke.TIMEOUT_S = DISPATCH_TIMEOUT_S
            await wait_for_state(pool, DEMO_ORG_ID, job_id, {JobState.COMPLETED.value})
            draft = await pool.fetchrow(
                "SELECT status, provider_ref FROM generated_draft"
                " WHERE organization_id = $1 AND id = $2",
                DEMO_ORG_ID,
                draft_id,
            )
            if draft is None or draft["status"] != "dispatched" or not draft["provider_ref"]:
                raise SmokeFailure(f"draft after dispatch: {dict(draft) if draft else None}")
            outbound = await pool.fetch(
                "SELECT provider_message_id, rfc822_message_id, in_reply_to, subject"
                " FROM email_message WHERE organization_id = $1 AND thread_id = $2"
                " AND direction = 'outbound'",
                DEMO_ORG_ID,
                inbound["thread_id"],
            )
            if len(outbound) != 1:
                raise SmokeFailure(f"{len(outbound)} outbound messages in the thread, expected 1")
            check_outbound_row(
                dict(outbound[0]),
                original_rfc822_id=str(inbound["rfc822_message_id"]),
                original_subject=subject,
            )
            print("ok   approved -> COMPLETED; one outbound email_message replies to the original")

            provider_thread_id = await pool.fetchval(
                "SELECT provider_thread_id FROM email_thread WHERE organization_id = $1 AND id = $2",
                DEMO_ORG_ID,
                inbound["thread_id"],
            )
            gmail_thread = await adapter.get_thread(mailbox, str(provider_thread_id))
            check_gmail_thread(
                [(m.provider_message_id, bytes(m.raw_payload)) for m in gmail_thread.messages],
                sent_provider_id=str(outbound[0]["provider_message_id"]),
                reply_rfc822_id=str(outbound[0]["rfc822_message_id"]),
                original_rfc822_id=str(inbound["rfc822_message_id"]),
                original_subject=subject,
            )
            print(f"ok   Gmail thread {provider_thread_id} holds the threaded reply")

            before = await snapshot(
                pool, adapter, mailbox, job_id, draft_id, inbound["thread_id"], str(provider_thread_id)
            )
            exchange = await channel.get_exchange(settings.broker.exchange_email_dispatch)
            replay = JobEnvelope.from_message(captured)
            await exchange.publish(replay.to_message(), routing_key=settings.broker.queue_dispatch)
            again = await http.post(
                f"{API}/drafts/{draft_id}/approve", headers=headers, json={"reviewer": "phase6-gate"}
            )
            if not again.is_success:
                raise SmokeFailure(f"repeated approve returned {again.status_code}: {again.text}")
            await asyncio.sleep(REPLAY_SETTLE_S)
            after = await snapshot(
                pool, adapter, mailbox, job_id, draft_id, inbound["thread_id"], str(provider_thread_id)
            )
            check_replay(before, after)
            print(
                f"ok   replayed dispatch + repeated approve: nothing sent "
                f"(Gmail thread {after.thread_messages} messages, job {after.job_state})"
            )
    finally:
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except (SmokeFailure, ConnectError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    except ProviderError as exc:
        print(f"FAIL Gmail: {exc} (a 401 means the token expired: runbook §3.2)", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"FAIL HTTP {exc.response.status_code} from {exc.request.url}", file=sys.stderr)
        return 1
    print("PHASE 6 GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`resolve_gmail_token` raises `ConnectError`, and the Gmail adapter raises `ProviderError` subclasses (a 401 is `AuthExpired`). `main` reports both as one `FAIL` line, like a `SmokeFailure`.

Run:
```bash
uv run pytest tests/unit/test_phase6_gate.py -v
uv run ruff format scripts/connect_gmail.py scripts/phase6_gate.py tests/unit/test_phase6_gate.py tests/unit/test_connect_gmail.py tests/integration/test_connect_gmail_postgres.py
uv run ruff check scripts tests/unit/test_phase6_gate.py tests/unit/test_connect_gmail.py tests/integration/test_connect_gmail_postgres.py
uv run mypy tests/unit/test_phase6_gate.py tests/unit/test_connect_gmail.py tests/integration/test_connect_gmail_postgres.py
```
Expected: 17 test items PASS (13 functions; the replay-failure test is parametrized 5 ways). ruff reports `All checks passed!`, and mypy reports `Success`.

mypy's configured targets are `packages services tests evaluation`, and `scripts/` is covered through the test imports only as `ModuleType`. Also run `uv run mypy scripts/connect_gmail.py scripts/phase6_gate.py` once. Expected: `Success: no issues found in 2 source files`. If mypy cannot find `stack_smoke`/`connect_gmail`, run it with `MYPYPATH=scripts`.

- [ ] **Step 15: Prove the gate cannot run under pytest by accident**

Run: `uv run pytest --collect-only -q scripts 2>&1 | tail -1`

Expected: `no tests ran` (testpaths is `tests`; `scripts/` holds no `test_*` files). The live gate is owner-run only (R24.5).

- [ ] **Step 16: Commit the gate**

```bash
git add scripts/phase6_gate.py tests/unit/test_phase6_gate.py
git commit -m "$(cat <<'EOF'
feat(gate): make phase6-gate, a real Gmail email to a threaded reply that replays to nothing [task 6.10] [R17.1, R17.2, R17.3, R17.7, R16.6, R19.3]

Owner-run. Pulls the owner's email by resync, waits for DRAFTED, checks the draft is
listed and readable through /v1/drafts, approves it with billing set to send_reply,
checks the outbound email_message and Gmail's copy of the reply (Message-ID,
In-Reply-To, References, one Re:), then replays the captured dispatch message and
approves again and asserts nothing changed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 17: Fill runbook §3.4**

In `docs/demo-runbook.md`, replace the §3.4 body (line 139, "Known gap: …") with:

````markdown
Run this once after `make up` and `make seed` (§4), and again whenever you want the stack to start watching "from now":

```bash
make connect-gmail ADDRESS=ragemail.demo.<yourname>@gmail.com
```

Expected:

```
ok   token belongs to ragemail.demo.<yourname>@gmail.com; current historyId 1234567
ok   mailbox <id> in organization 00000000-0000-0000-0000-000000000001: provider gmail, credentials_ref env:GMAIL_ACCESS_TOKEN, status active, watching from now
CONNECT GMAIL OK mailbox_id=<id>
```

- It checks that the token in `.env` belongs to that address, so a token minted while signed in to another account is refused.
- The mailbox joins the demo tenant (Acme), so the seeded customers, orders and knowledge apply to its emails.
- Only mail that arrives after this command is imported. Older mail in the test inbox is left alone.
- Copy the mailbox id: §6 step 4 uses it. Nothing pushes new mail to a stack on `localhost`, so you pull it with one command (§6 step 4); `make phase6-gate` pulls it for you.
- If Gmail calls failed with 401, the stack marks the mailbox `needs_reauth`. After minting a new token (§3.2–3.3) and `make up`, run this command again to re-activate it.
````

- [ ] **Step 18: Fill runbook §5.3**

Replace the §5.3 body (line 192, "The command and its expected output …") with:

````markdown
The gate sends a real reply from the test account, so it needs the billing category set to send directly. Do this only for the gate:

1. In `config/categories.yaml`, under `- category: billing`, set `dispatch_mode: send_reply`.
2. Run `make up`, which rebuilds the image with the changed file.
3. Run:

   ```bash
   make phase6-gate
   ```

4. When it prints `>>> From ANOTHER address, send an email to … now.`, send that email exactly, from an address other than the test account. The subject carries a code like `[gate-1a2b3c4d]` that the gate looks for. You have 10 minutes.

Expected, ending in `PHASE 6 GATE OK`:

```
ok   GMAIL_ACCESS_TOKEN is set (not printed)
ok   config/categories.yaml: billing dispatch_mode send_reply
ok   api ready; consumers on mail.sync.requested, email.normalize, email.triage, knowledge.ingest
ok   consumer on email.dispatch
ok   connected mailbox ragemail.demo.<yourname>@gmail.com (<id>)
ok   email <gmail id> ingested into our thread
ok   job DRAFTED (billing); draft <id> listed, readable, send_reply
ok   approved -> COMPLETED; one outbound email_message replies to the original
ok   Gmail thread <gmail thread id> holds the threaded reply
ok   replayed dispatch + repeated approve: nothing sent (Gmail thread 2 messages, job COMPLETED)
PHASE 6 GATE OK
```

Your sending address receives the reply in the same conversation. Afterwards, set billing back to `dispatch_mode: create_draft` and run `make up`, so the demo (§6) creates drafts, as it does by default.

If it prints `FAIL …`, the message names the check. `FAIL Gmail: … 401` means the token expired: repeat §3.2–3.3, `make up`, `make connect-gmail`, then rerun.
````

- [ ] **Step 19: Fill runbook §6 and §7**

In §6, replace the line "About 10 minutes. Steps 1–3 work now; steps 4–7 need Phase 6." with:

```markdown
About 10 minutes. Steps 4–7 need Phase 6 (see the check at the top) and a connected mailbox (§3.4).
```

Replace step 4 with:

````markdown
4. **[after Phase 6] Send a real email (1 min).** From another address, email the Gmail test account asking about an order (for example Alice's "What is the status of order 82915?"; send it from `alice.smith@clientcorp.com` only if you control that address, otherwise the sender is an unknown customer and the draft says so). Then pull it into the stack, with the mailbox id from §3.4:

   ```bash
   curl -s -X POST -H 'X-Organization-ID: 00000000-0000-0000-0000-000000000001' \
     -H 'Content-Type: application/json' -d '{}' \
     http://localhost:8000/v1/mailboxes/<mailbox id>/resync
   ```

   Expected: a JSON body with `"status"` accepted. The draft appears in the review UI within about a minute.
````

Leave steps 5–7 as they are: they already describe the Phase 6 behaviour (create_draft default, the review UI at `http://localhost:3001`, the timeline to `COMPLETED`).

In §7, add three rows to the table:

```markdown
| **[after Phase 6]** `make connect-gmail` says the token belongs to another account | The Playground was signed in to a different Google account | Mint the token in a private window signed in only to the test account (§3.2) |
| **[after Phase 6]** Resync returns 409, or the gate says `needs_reauth` | A 401 marked the mailbox for re-authentication | New token (§3.2–3.3), `make up`, then `make connect-gmail ADDRESS=…` again |
| **[after Phase 6]** An approved draft ends `DEAD_LETTER` | A permanent provider error (expired token, the email's thread was deleted in Gmail) | `curl -H 'X-Organization-ID: …' http://localhost:8000/v1/jobs/<job id>` shows `last_error`; fix the cause, then `POST /v1/jobs/<job id>/replay` |
```

Run: `grep -n "Known gap\|are added when Phase 6 is built" docs/demo-runbook.md`

Expected: no output.

The "Last verified" line is updated in Task 13 Step 7, after the owner's run. It must not claim a verification that has not happened.

- [ ] **Step 20: Record the 6.7 check result in tasks.md**

In `specs/tasks.md`, under the 6.7 entry (after its second bullet, line 746), add a note sub-bullet. Use the variant that matches the owner's answer to E1.

If the owner approved the fix (Steps 2–3 committed):

```markdown
  - Check result (2026-MM-DD, by code reading and recorded-response tests): Gmail sync did ingest the mailbox's own mail. `history.list` and the initial `messages.list` had no label filter (`packages/adapters/gmail.py`), the normalizer marks everything `inbound`, and triage does not look at `direction`. So in `create_draft` mode our own provider draft came back as a new inbound email, and after a `send_reply` dispatch the draft deleted by `drafts.send` made the next sync fail on a 404. Fixed in 6.7: sync reads `labelId=INBOX` only, and a message deleted between listing and fetch is skipped (`tests/unit/test_gmail_adapter.py::test_incremental_sync_asks_only_for_inbox_messages`, `::test_sync_skips_a_message_deleted_after_history_listed_it`). A sent copy is also deduplicated by `provider_message_id` because step 5 records it. Left as a gap: an email the account sends to itself carries `INBOX` and would still be triaged; the Graph adapter's delta sync was not checked (verified by recorded responses only, ADR-0009).
```

If the owner declined E1:

```markdown
  - Check result (2026-MM-DD, by code reading): Gmail sync ingests the mailbox's own mail and re-triages it. `history.list` and the initial `messages.list` have no label filter (`packages/adapters/gmail.py:273, :339`), the normalizer marks everything `inbound` (`services/email_worker/normalizer.py:116`), and triage does not look at `direction`. In `create_draft` mode our own provider draft is triaged as a new inbound email; after a `send_reply` dispatch the draft deleted by `drafts.send` makes `get_message` 404 inside `synchronize`, which fails every later sync of that mailbox. Gap left open by owner decision; `make connect-gmail` re-arms the cursor at "now" as a workaround between gate runs.
```

- [ ] **Step 21: Full unit and lint pass for this task**

Run: `make fmt-check && make lint && make test-unit`

Expected: exit 0. `ruff format --check` lists no files. mypy prints `Success`. The unit summary shows no failures and includes `test_phase6_gate.py`, `test_connect_gmail.py` and the new `test_runtime_image_contract.py` test.

- [ ] **Step 22: Commit the documentation**

```bash
git add docs/demo-runbook.md specs/tasks.md
git commit -m "$(cat <<'EOF'
docs(runbook): connect-gmail, the Phase 6 gate and the live demo steps; record the 6.7 sync check [task 6.10, 6.7] [R17.1, R17.7]

Fills runbook §3.4 (make connect-gmail), §5.3 (make phase6-gate with billing set to
send_reply for the gate only, and its expected output), §6 step 4 (pulling the demo
email by resync) and three §7 rows. The 6.7 note records that Gmail sync ingested our
own drafts and sent copies, and what was done about it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Phase 6 close: `make ci`, completion audit, tasks.md statuses and the gate evidence format [tasks.md 6.1–6.10, Phase 6 gate]

**Files:**
- Modify: `specs/tasks.md`:
  - the Phase 6 task entries, lines 706–767 (checkbox and closing note per task);
  - a new gate-evidence block right after the `> **Phase 6 gate:**` paragraph (line 769).
- Modify: `docs/demo-runbook.md`, line 6 ("Last verified"), after the owner's run only.

**Interfaces:**
- Consumes:
  - Tasks 1–12 committed.
  - The owner's live run of `make up`, `make seed`, `make connect-gmail` and `make phase6-gate`.
  - The owner's answers to the open questions of every Phase 6 part, listed under "Open questions for the owner" at the end of this plan.
- Produces: flipped checkboxes, per-task closing notes, the Phase 6 gate evidence block, and the runbook's "Last verified" line.

- [ ] **Step 1: Full CI**

Run: `make ci`

Expected: exit 0. It covers `ruff format --check .`, `ruff check .`, strict mypy over `packages services tests evaluation`, `pytest tests/unit` and `pytest tests/integration` (in `rag_email_test`). If 6.8 added Playwright tests under `tests/e2e`, also run `uv run pytest tests/e2e -v`, with the browser installed by `uv run playwright install chromium`. Expected: PASS.

Record the unit and integration (and e2e) pass counts from the pytest summary lines. If any step fails, fix the cause in the owning task's code with a RED→GREEN test and re-run. Do not edit tests to go green.

- [ ] **Step 2: Migration round trip**

Run: `uv run pytest tests/integration/test_database_schema.py::test_migration_reversibility -v && make migrate`

Expected: PASS, so 0005 down fully undoes 0005. `make migrate` then reports every migration applied, with nothing pending, against the configured database. It touches only the host `.env` database; the live stack's restart stays with the owner.

- [ ] **Step 3: Completion audit, one per task 6.1–6.10**

Dispatch a reviewer (`superpowers:requesting-code-review`) over `git diff bb00bfc..HEAD`. The brief for each task:
- Trace every bullet in its `specs/tasks.md` entry and every cited requirement ID to code, and to a test that fails without that code.
- GEMINI.md §3 DoD items 1–6:
  - new keys are in both `.env.example` and `docs/configuration.md`: `FRONTEND__API_BASE_URL`, `FRONTEND__ORGANIZATION_ID` (6.8), `GMAIL_ACCESS_TOKEN` (6.10), and the `dispatch_mode` note (6.4);
  - `draft_decisions_total{decision, category}` is exported and documented with its PromQL in `docs/observability.md` (6.2).
- Specific checks:
  - 6.5: the forced-redelivery tests crash after each of the five steps and assert exactly one provider draft and one send. The claim, the RETRY_PENDING → DISPATCHED edge, the reaper skip and the no-lease rule are each pinned by a test.
  - 6.6: a `Retry-After` picks the first ladder tier ≥ its value, capped at the last tier. 400/404/auth and a null provider thread id dead-letter with `last_error` kept.
  - 6.7: the outbound row is written in step 5's transaction. The Step 20 note in tasks.md matches what the code does.
  - 6.9: `tests/integration/test_phase6_pipeline_e2e.py` runs every hop through production builders (no hand-inserted `QUEUED` job).
  - 6.10: no test needs `GMAIL_ACCESS_TOKEN`, and the guard strips it.
- Rules:
  - every tenant query carries `organization_id`;
  - every state change goes through `transition_job` / `transition_job_state(_on)`;
  - `packages/domain` imports only the stdlib and `packages.core`;
  - `services/*` holds no `gmail`/`graph`/`imap` literal (`tests/unit/test_dependency_rules.py`);
  - `frontend` and `api` are bound to `127.0.0.1`.

Record the verdict per task as PASS, PASS WITH NOTES or FAIL. For each finding, fix it with a RED→GREEN test and re-run `make ci`. Do this before any checkbox flips.

- [ ] **Step 4: Flip the checkboxes the audit allows**

In `specs/tasks.md`, flip a task to `[x]` only if three things hold: its audit passed, it has no owner-run leg outstanding, and no owner decision on its written bullets is outstanding (GEMINI.md §3, §7).

Mark these tasks `[~]` until their condition clears:
- 6.10, until the owner has run `make phase6-gate`. Sub-bullet: `Left: the owner runs make phase6-gate against the Gmail test account (see the Phase 6 gate evidence below).`
- 6.7, while Open Question E1 is unanswered. Sub-bullet: `Left: owner decision on E1 (Gmail sync label filter); the check result is recorded above.`
- 6.3a, if the owner has not accepted recorded-response-only verification of Graph as its closing evidence. ADR-0009 already says so; confirm the audit cites it.
- Any task whose part lists an unanswered open question on its written bullets. Name that question in the `Left:` line.

Under each flipped task, append a closing sub-bullet in the repo's form (see 3.16, `specs/tasks.md:374`):

```markdown
  - Closed 2026-MM-DD after a completion audit (PASS WITH NOTES; `make ci` green, unit N, integration M). The fix pass before flipping:
    - <one line per audit finding and the test that pinned it, or "none">
```

For 6.9, the closing line also names the two tests: `test_create_draft_mode_ends_with_one_provider_draft_and_no_outbound_message` and `test_send_reply_mode_ends_with_one_send_and_one_outbound_message`.

- [ ] **Step 5: Commit the audit close**

Name only the tasks this commit closes in the subject and the `[task …]` tag. Adjust the requirement list to those tasks' `_Requirements:_` lines.

```bash
git add specs/tasks.md
git commit -m "$(cat <<'EOF'
docs(tasks): close Phase 6 tasks after their completion audit [task 6.1–6.9] [R16.6, R16.7, R16.8, R17.1, R17.2, R17.3, R17.4, R17.5, R17.6, R17.7, R18.3, R18.7, R19.2, R19.3, R21.4, R23.2, R23.4, R23.5, R23.6, R23.7, R24.7]

Records the audit verdicts, the fix pass and the make ci counts for tasks 6.1–6.9, and
flips the tasks the audit allows. 6.10 stays [~] until the owner runs make phase6-gate;
6.7 stays [~] while E1 is open. Adjust the tag and this body to the tasks actually closed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: The owner runs the live gate**

The owner runs this; Claude does not restart the live stack or call Gmail. Hand over these steps:

1. Mint a Gmail token (runbook §3.2), put it in `.env` as `GMAIL_ACCESS_TOKEN=…` (§3.3), and keep the Gemini settings from Phase 5.
2. In `config/categories.yaml`, set `dispatch_mode: send_reply` on `billing` (runbook §5.3 step 1).
3. `make up`, `make seed`, then `make connect-gmail ADDRESS=<test account>`.
4. `make phase6-gate`. Send the email it asks for from another address.
5. Set billing back to `create_draft` and run `make up`.
6. Regression: `make smoke`, `make phase5-gate`.

Expected: `PHASE 6 GATE OK` after the ten `ok` lines listed in runbook §5.3.

If it prints `FAIL …`, debug the named check with `superpowers:systematic-debugging` and fix the cause with a RED→GREEN test; the owner then re-runs the gate. Record each defect under "Defects the gate found and fixed".

A `Message-ID` failure in `check_gmail_thread` means Gmail replaced our header, which answers research §8 Q1 in the negative. That is a real finding. Record it, and do not loosen the check. The fix belongs in 6.7's write-back: store Gmail's header from the sent copy.

- [ ] **Step 7: Write the Phase 6 gate evidence block and the runbook's "Last verified" line**

Add the block after the `> **Phase 6 gate:**` paragraph (`specs/tasks.md:769`). Mirror the Phase 5 block (`specs/tasks.md:692–698`): a dated header naming the script and who ran the stack, one bullet per gate clause, then "Not live", then "Defects", then the CI line.

Take every value from the owner's gate output, the database rows it names, and the Step 1 counts. Add no value the output does not show.

```markdown
>
> **Gate evidence (2026-MM-DD, live stack, `scripts/phase6_gate.py` / `make phase6-gate`; the owner ran `make up`, `make seed` and `make connect-gmail` with a fresh `GMAIL_ACCESS_TOKEN`, `billing` set to `send_reply` for the gate, LLM `<provider>` / `<fast model>`):**
> - Real email → reviewable draft: the owner's email "`<subject with [gate-…]>`" from `<sender domain only>` was pulled by resync into our thread, classified `<category>` by `<decided_by>`, and reached `DRAFTED`; the draft was listed by `GET /v1/drafts?status=draft` and read by `GET /v1/drafts/{id}`.
> - Approve → threaded reply: `POST /v1/drafts/{id}/approve` took the job `DRAFTED → DISPATCHED → COMPLETED`; the draft is `dispatched` with provider ref `<gmail message id>`. In Gmail, thread `<gmail thread id>` holds the reply with our `Message-ID`, `In-Reply-To` = the original, `References` ending with it, and subject `Re: <subject>` (one `Re: `).
> - Reply visible in our thread: one `email_message` row, `direction='outbound'`, in the original's thread, `in_reply_to` = the original's Message-ID (R17.7).
> - Replay sends nothing: the captured dispatch message was republished and approve was called again; job state, outbound rows (1), Gmail thread size (`<n>`), state transitions (`<n>`) and feedback rows (1) were unchanged after `15 s`.
> - CI on fakes: `tests/integration/test_phase6_pipeline_e2e.py` (create_draft: one provider draft, no outbound message; send_reply: one draft, one send, one outbound message; replay and repeated approve change nothing) and the 6.5 forced-redelivery tests.
> - Not live: the create_draft mode against Gmail (shown in the demo, runbook §6 step 6); Graph (recorded HTTP responses only, ADR-0009); rate-limit and 5xx retries (6.6 tests with injected faults).
> - Regression: `make smoke` and `make phase5-gate` still pass.
> - Defects the gate found and fixed:
>   - <one line per defect with its RED→GREEN test, or "none">
>
>   `make ci` passed afterwards (unit N, integration M).
```

Do not write the sender's full address into the evidence (it is the owner's personal address); the domain is enough.

Then flip 6.10 to `[x]` with its closing sub-bullet, and 6.7 if E1 is answered.

In `docs/demo-runbook.md`, line 6, replace the "Last verified" bullet with:

```markdown
- **Last verified:** 2026-MM-DD at commit `<short sha of the evidence commit's parent>` (branch `RAG_Email_System`), through §5.3 (`make phase6-gate`).
```

- [ ] **Step 8: Commit the gate evidence**

```bash
git add specs/tasks.md docs/demo-runbook.md
git commit -m "$(cat <<'EOF'
docs(tasks): record the Phase 6 gate evidence and close 6.10 [task 6.10] [R17.1, R17.2, R17.3, R17.4, R17.5, R17.6, R17.7]

The owner ran make phase6-gate against the Gmail test account: a real email became a
reviewable draft, approving it delivered a threaded reply visible in Gmail and in our
thread, and replaying the dispatch sent nothing. The runbook's Last verified line now
covers §5.3.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

If 6.7 also closes here, add `6.7` to the `[task …]` tag and `R17.7` is already listed.

---

---

## Open questions for the owner

Each question carries a recommended answer. Questions marked **(blocks a step)** gate the step named. The other questions record behaviour this plan implements, and an answer changes a later task, not this plan. Task 13's audit leaves any task with an unanswered blocking question at `[~]`.

**E1 (blocks Task 12 Steps 2–3): Gmail sync ingests our own drafts and sent mail.**
- `history.list` and `messages.list` have no label filter, and normalization marks every message `inbound`.
- In `create_draft` mode, the provider draft is therefore re-triaged as a new customer email.
- After a `send_reply` dispatch, the draft that `drafts.send` deleted makes `get_message` 404, and every later sync fails.
- The proposed fix is `labelId=INBOX` / `labelIds=INBOX`, plus skipping a `NotFound` for a single message. It changes which provider messages are ingested, so it is public behaviour.
- **Recommendation: approve.** Without the fix, the live gate runs once and not twice, and the default demo mode drafts a reply to its own draft.

**D1 (blocks Task 9 Steps 15 and 17): the review UI lives in `services/frontend/`, not the root `frontend/` of design §4.**
- The Docker image and the wheel package only `services/`, and `.dockerignore` excludes `frontend`.
- Task 9 would make four changes:
  - move `frontend/` under `services/` in the design tree;
  - delete the empty `frontend/.gitkeep`;
  - drop the `.dockerignore` line;
  - add the `packages/dispatch/` package, new in Task 2, to the same tree.
- **Recommendation: approve.** GEMINI.md §7 requires your approval before the design is edited.

**E5: the `GMAIL_ACCESS_TOKEN` env fallback in `packages/adapters/registry.py:58–61`.**
- The adapter registry gives the token to every `gmail` mailbox without a `credentials_ref`, including the seeded `support@acme.com`.
- With the token exported, approving a seeded demo draft calls the real account. That call most likely 404s on the thread and dead-letters.
- **Recommendation:** drop the fallback, set `credentials_ref` explicitly (which `make connect-gmail` already does), and update `tests/unit/test_mail_read_api.py:378`. Do this as a small follow-up task added to `specs/tasks.md`, not inside 6.10.
- Until then, the runbook says not to approve seeded drafts while the token is set.

**D6: `make ci` gains `test-e2e`, which needs Chromium (`uv run playwright install chromium`).**
- Playwright does not officially support Arch Linux. It downloads a fallback build.
- **Recommendation:** keep `test-e2e` in `make ci`. This branch never runs the GitHub workflow, so `make ci` is the only place the UI flows are checked.

**E4: switching `billing` to `send_reply` for the live gate means editing `config/categories.yaml`, running `make up` (a rebuild), and reverting afterwards.**
- **Recommendation:** accept. No env or API override of the mode is added, because neither the spec nor the contract has one.

**E7: "replaying the dispatch job sends nothing" is proven by republishing the captured dispatch message and approving again.**
- `POST /v1/jobs/{id}/replay` accepts only `DEAD_LETTER` jobs, so it cannot replay a `COMPLETED` dispatch.
- **Recommendation:** accept this reading of the gate sentence.

**E8 / research §8 Q1: it is not documented whether Gmail keeps our `Message-ID` on `drafts.send`.**
- The gate fails on purpose if it does not.
- In that case, after an ambiguous send failure, `find_sent_message` cannot find the Gmail copy, and the job dead-letters instead of sending twice.
- **Recommendation:** accept, and let the live gate settle it. If Gmail rewrites the id, the fix is to read the header back from the sent copy in the step-5 write-back.

**Research §8 Q2 and Graph ids.** Graph is verified only by recorded responses (ADR-0009).
- (a) `createReply` may add its own quoted original, which would duplicate the quote from `build_outbound_reply`.
- (b) Graph ids stored at ingestion were fetched without `Prefer: IdType="ImmutableId"`. A `createReply` on an original that was moved since ingestion then 404s and dead-letters.
- **Recommendation:** accept both as known limits, and record them in the 6.3a closing note.

**D2 (blocks Task 6 Step 12a; Task 13 keeps 6.1 at `[~]` until answered): design §5.8 contradicts itself on a repeated approve.**
- `specs/design.md:686` (Review API) says a repeated approve "returns the first result and publishes nothing new". The §5.8 dispatch text and tasks.md 6.1 say it re-publishes the dispatch job while the job is not `COMPLETED`.
- This plan implements the re-publish reading (it lets a reviewer recover a lost dispatch message), and Task 6's tests assert it. The per-job dispatch lock (Task 7) makes the extra envelopes harmless.
- **Recommendation: approve** the Step 12a rewrite of line 686. GEMINI.md §7 requires your approval before the design is edited.

**D3 (blocks Task 5 Step 5: the `internetMessageId` line of Graph `create_draft` and Graph `find_draft`): Graph drafts carry our `Message-ID`.**
- Design §5.8 says Graph's `createReply` sets `internetMessageId` itself. Then the orphan-draft lookup (tasks.md 6.5 "exactly one provider draft") has nothing to match on Graph.
- The plan sets `internetMessageId` = our `Message-ID` in the `createReply` body (Graph documents it as updatable while `isDraft`), and `find_draft` matches it on the conversation's drafts.
- The orphan-draft lookup itself replaces the earlier "accept a second provider draft" limit: it costs one provider call, and only on a redelivery or replay that finds no stored handle, not on every dispatch.
- **Recommendation: approve**, and amend design §5.8's Graph sentence in the same change. Graph is verified only by recorded responses (ADR-0009), so the 6.3a closing note records this as unverified live.

**Mode switch mid-flight.**
- Suppose a job crashes after its provider draft was recorded in `create_draft` mode and before finish, and the category is then switched to `send_reply`. The redelivery sends the approved draft once.
- **Recommendation:** accept. The reviewer approved the draft, the send uses the same provider draft, and a job that already completed is never re-sent. Review Focus 5 pins that last case.

**Business facts on the review screen are statuses, not values.**
- Only `entity`, `reference`, `status` and `reason` are persisted, on the `CONTEXT_READY` event. The UI can show "ORD-82915 — FOUND" but not the order's attribute values.
- **Recommendation:** accept for 6.8. Persisting the rendered `[BUSINESS DATA]` block would be a design change.

**No automatic dispatch trigger.**
- `DispatchService` honours `auto_send_eligible`, but only approve publishes a dispatch job. Every category is `false`.
- **Recommendation:** confirm that leaving out an automatic trigger is in scope (R17.6 holds trivially).

**Approve on a dead-lettered job publishes nothing.** The operator path is `POST /v1/jobs/{id}/replay`.
- **Recommendation:** confirm this split between reviewer and operator.

**Reply text details.**
- Replies go to the original sender. A `Reply-To` header is not honoured, because nothing on the dispatch path persists it (`email_message` has no headers column and `_row_to_message` never fills `NormalizedMessage.headers`). Persisting `Reply-To` (migration column, parser, message store) is a follow-up task, not part of 6.3.
- The quoted original uses `original.body_text`, which may include earlier quoted history.
- `reply_subject` collapses only `Re:` prefixes. It leaves `Fwd:`, `AW:` and `Re[2]:` as they are.
- **Recommendation:** accept both.

**python-dotenv as a direct dev dependency.** The connect and gate scripts use it to read the token from `.env`, without a settings field that would hold a secret.
- **Recommendation:** accept.

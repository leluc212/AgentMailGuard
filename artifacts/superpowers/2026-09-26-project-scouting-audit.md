# Project Scouting Audit — `rag-email`

**Date:** 2026-09-26 · **Branch:** `RAG_Email_System` @ `1ecf7b1` · **Scope:** whole repository, read-only
**Companion file:** `2026-09-26-audit-register.md` holds every finding with its file, line, verified claim and evidence.

---

## 1. What this project is

`rag-email` is a final-year project that builds the core engine of an enterprise email assistant. It receives mail from Gmail and Microsoft Graph, classifies each message, retrieves organizational knowledge only when a reply needs it, and drafts a grounded reply for human review. Its governing rule is *"classify first, retrieve only when required, generate only when necessary."*

The repository is spec-driven. `specs/requirements.md` holds 208 numbered criteria, `specs/design.md` is the blueprint, and `specs/tasks.md` is a 110-task queue across Phases 0–8. `GEMINI.md` is the binding constitution for every coding agent.

It sits inside a larger system called AgentMailGuard. Security controls such as prompt-injection defence, DLP and phishing filtering are deliberately out of scope here. They live in a sibling project on the remote branch `origin/feature/mailguard-defense-stack`, which shares no git history with this branch.

### Designed architecture

```
 Gmail / Graph ──webhook──▶ api ──▶ mail.sync.requested ──▶ mail-connector (sync loop, checkpoints)
                                                                  │ raw MIME → MinIO
                                                                  ▼
                                   email.normalize ──▶ email-worker (MIME → canonical message, threading, dedup)
                                                                  │
                                                                  ▼
                                   email.triage ──▶ triage-worker (rules ▶ ML ▶ small LLM)
                                                      │            │             │
                                         no reply ◀───┘   template ◀┘   AI path ──┘
                                        (COMPLETED)     (DRAFTED, 0 AI)      │
                                                                             ▼
                               email.<category>.<lane> ──▶ ai-worker: context ▶ hybrid RAG ▶ 1 generation call
                                                                             │ validated, citation-checked draft
                                                                             ▼
                                                  review UI ──approve──▶ dispatch-worker ──▶ provider mailbox
```

---

## 2. Current state in one picture

The components are built and tested. The processes that would run them mostly are not.

```
                    BUILT & TESTED                         RUNNING TODAY (docker ps, rabbitmqctl)
 api              ✅ 7 routers                          ⚠ image from task 1.14; lacks jobs/knowledge/search routes
 mail-connector   ✅ orchestrator, renewal               ✗ placeholder.py stub
 email-worker     ✅ full normalizer + main.py          ✗ container Exited(0) since 2026-09-23
 triage-worker    ✅ rules, ML, LLM cascade, gate        ✗ placeholder.py stub (no main.py exists)
 knowledge-worker ✅ pipeline + main.py                 ✗ placeholder.py stub (no compose command)
 ai-worker        ⚠ packages only, service empty        ✗ placeholder.py stub
 dispatch-worker  ✗ not started (Phase 6)               ✗ placeholder.py stub
 frontend         ✗ not started (Phase 6)               ✗ placeholder.py stub

 RabbitMQ: every queue has 0 consumers · 2 messages stranded in email.general_inquiry.normal
 Prometheus: 6 targets scrape a constant `up 1` from the stub · email_worker target is down
```

The stub answers `/healthz` and `/readyz` with 200 regardless of anything, so `docker compose ps` reports a healthy stack that processes nothing.

### Measured numbers

| Check | Result |
|---|---|
| Unit tests | 1,041 passed |
| Integration tests | 117 passed, against the live dev stack |
| `mypy packages/` (strict) | clean |
| `make lint` (mypy over tests too) | 14 errors in 6 test files |
| `ruff check` | clean |
| `ruff format --check` | 40 files fail |
| Tasks marked done | 66 of 110 |
| Of those, audited as fully done | 45 |
| Unpushed commits | 72, covering all of Phases 3–4 |

---

## 3. Task claims versus evidence

Five auditors checked every `[x]` task in Phases 0–4 against its bullets, its tests and its runtime host.

| Phase | Audited done | Partial | Gate runnable today |
|---|---:|---:|---|
| 0 Foundation | 8 / 13 | 0.4, 0.6, 0.9, 0.11, 0.13 | partly, only with an untracked `.env` |
| 1 Mail pipeline | 12 / 14 | 1.6, 1.7 | no |
| 2 Triage & queues | 8 / 15 | 2.2, 2.5, 2.8, 2.10, 2.13, 2.14, 2.15 | no |
| 3 Knowledge RAG | 11 / 15 | 3.5, 3.8, 3.11, 3.13 | partly, as pytest only |
| 4 Context & generation | 6 / 10 | 4.4, 4.7, 4.8, 4.9 `[~]` | no |

No phase gate from 1 onward has been executed as written, yet each following phase started. Task 4.9 is the only honest partial marker. The other 21 partial tasks are marked `[x]`.

---

## 4. Findings, grouped by root cause

The auditors produced 386 raw findings, and the completeness critic added a handful more. All 140 blockers and majors were checked by verifiers who tried to refute the fact and then judged it against the spec. 131 finding records survived, one was refuted, and 8 turned out to be intentionally scheduled for later phases. Many records describe the same root cause from different angles, so they are grouped below. The companion register lists each one.

### 4.1 Blockers: the system cannot run, or would lose messages once it does

1. **Production code crashes on import without pytest.** `packages/llm/__init__.py:55` and `packages/retrieval/__init__.py:43` import test-contract modules that `import pytest`. The Docker image installs no dev dependencies. With pytest absent, `services.api.main`, `services.email_worker.main`, `services.knowledge_worker.main`, the triage consumer and the context builder all fail with `ModuleNotFoundError`. Reproduced this session. Today's api container only works because its image predates the change.
2. **The Docker image is incomplete and stale.** The `Dockerfile` copies only `packages/` and `services/`. It leaves out `config/`, `prompts/`, `schemas/`, `migrations/`, `artifacts/models/` and `evaluation/`, all of which the workers load at runtime. It also ignores `uv.lock`. `make up` never rebuilds, so the running images are 89 of 92 commits old and lack scikit-learn, jinja2 and tiktoken.
3. **Six of eight services have no real process.** `services/triage_worker` and `services/mail_connector` have no `main.py`. The knowledge worker has one, but compose never runs it. The AI and dispatch workers are empty. Nothing hosts the sync loop, subscription renewal, lease reaper, queue monitor or embedding-dimension startup check, although their tasks are marked done.
4. **No running process declares the broker topology.** `setup_topology` is called only from tests, which breaks R3.2. The queues exist today only because integration tests declared them on the shared broker.
5. **The retry ladder drops messages.** All three retry queues dead-letter to the `email.route` topic exchange (`packages/broker/topology.py:190-193`). That exchange binds only `email.<category>.<lane>` keys. A retried message from `email.normalize`, `email.triage`, `knowledge.ingest` or `mail.sync.requested` is discarded when its delay expires. The only retry test publishes from `email.route` itself, so it cannot catch this.

### 4.2 Major: correctness bugs that would surface once the pipeline runs

**Broker and delivery**
- `BaseConsumer` acknowledges unparseable messages without dead-lettering them (`packages/broker/consumer.py:174-180`), though the inline comment says it does. This breaks R3.5.
- Backoff jitter is computed but never applied (R19.5).
- Operator replay publishes to a routing key nothing is bound to. It also commits `RETRY_PENDING` before publishing and swallows publish failures while returning HTTP 200 (`services/api/routers/jobs.py:199,231`).
- Graceful shutdown closes the channel before in-flight jobs can acknowledge.

**Transactions and idempotency**
- No store exposes a connection or transaction seam, so a state transition cannot commit together with its side effect. This violates a `GEMINI.md` §4 correctness rule and R18.5. The email worker commits `NORMALIZED` in a separate transaction and swallows failures.
- A redelivered email-worker message is treated as a duplicate and never re-dispatches triage, so the job stalls silently.
- The triage consumer commits state before publishing. On redelivery it raises an illegal-transition error into the dead-letter queue. The `execute_once` helper from task 0.8 is built but unused.

**Ingestion (R1, R2)**
- Coalesced follow-up notifications are wiped by the checkpoint upsert, which breaks R2.9 (`services/mail_connector/orchestrator.py:300` with `packages/db/checkpoint.py:118`).
- Gmail's initial sync stores a page token as `history_id`, which causes a full-resync loop (`packages/adapters/gmail.py:347`).
- The mailbox lock is released during a full resync. A crash leaves `sync_state='syncing'` forever. Graph syncs over 50 pages restart from scratch.
- Gmail HTTP 403 maps to `AuthExpired`, but Gmail also uses 403 for quota errors, so a busy mailbox gets parked as needing re-auth. Retry-after is never honoured by a caller (R1.6). No production path creates a provider subscription.

**Knowledge ingestion (R9)**
- Both the upload API and the pipeline bump the version, so a re-ingest lands at N+2.
- The document is unsearchable during re-ingest and stays unsearchable after a failed one. Old chunks remain searchable.
- The upload endpoint returns 202 even when the ingest job was never published.

**Retrieval (R10–R12)**
- The lexical branch combines every extracted keyword and identifier with AND through `websearch_to_tsquery` (`packages/retrieval/postgres.py:171`), so it rarely matches a real email.
- The category filter uses email categories that no knowledge document carries (`packages/retrieval/query_builder.py:72`), so `general_inquiry`, `sales`, `administration` and `scheduling` retrieve nothing.
- The query is never persisted with the job (R12.6, task 3.13 marked done).
- The cross-encoder reranker cannot load because `sentence-transformers` is not a dependency.
- The debug endpoint's production wiring uses the fake embedder and stub reranker.
- The `RETRIEVAL__*` settings are dead config, and the branch timeout is hard-coded to 2 s against a documented 500 ms.

**Triage (R6)**
- With the shipped configuration, no stage can emit the template outcome.
- The ML stage is skipped unless a caller injects a classifier, and none does.
- Per-organization thresholds have no runtime source.
- Classification results are never persisted, which breaks R6.7. The live table is empty.
- File-backed template bodies render as the literal path string.

**Context and generation (R8, R14)**
- `ContextBuilder` never calls the summarizer.
- The summarizer's LLM call is untagged, so the call budget would count it as the job's one generation call and block the real draft (`packages/context/summarizer.py:132`). The triage LLM bypasses the budget entirely.
- The complexity router is wired to the generator only inside tests.
- The first-summary save can silently overwrite a concurrent worker's summary (`packages/db/thread_state.py:350-360`). `GEMINI.md` §9 names this exact trap. Summary output is persisted without schema validation.

**Tenancy**
- Mailbox routes, `PostgresMailboxStore.get` and `update_status`, and every checkpoint query run without an `organization_id` predicate. `GEMINI.md` §4 says every tenant-scoped query carries one.

**Observability**
- The cost counter is registered, but nothing in production increments it, so SC9 cannot be measured.
- Trace continuity breaks on the sync path.
- Queue depth is never exported because the queue monitor has no host.
- The api's `/readyz` reports ready when the database pool failed to start.

### 4.3 CI and test infrastructure

- **CI has never run on this branch.** `.github/workflows/ci.yml` triggers only on `master` and `main`, and `main` is a one-commit orphan. At HEAD it would fail on the 14 mypy errors and 40 format failures.
- **The CI integration job would also fail.** It maps Postgres to 5432 while a test hard-codes 5433. It exports `STORAGE__*` variables that no setting reads.
- **Integration tests share the live dev stack.** They depend on an untracked `.env`. One test drops every table of the dev database on each run. The deployed email worker consumed test messages. One test makes an opportunistic live Gmail call, which `GEMINI.md` §8 forbids.

### 4.4 Evidence validity

- **The triage model's macro-F1 of 1.0 is not a held-out number.** The classification set has only 64 unique bodies. All 64 test bodies also appear in the training split. Measured this session.
- **The seed knowledge corpus has no meaningful vectors.** It is embedded with SHA-256-derived vectors, so a hybrid-versus-vector comparison on it cannot support H1. The Phase 3 benchmark clause was never run.

### 4.5 Configuration drift

- **Some documented settings have no effect.** The `API__*` group is inert, because `APISettings` reads bare `PORT`, `HOST` and `CORS_ORIGINS`. The `RETRIEVAL__*` and `AGENT_PROFILES__*` groups and the categories config path have no consumer.
- **`.env.example` does not match the compose stack.** It says ports 5432 and 9000, while compose publishes 5433 and 9010. A clean checkout following the README cannot reach the stack.

### 4.6 Process and tracking

- **72 commits exist only on this laptop.** That is all of Phases 3 and 4. The remote branch stops at task 2.15 and is 2 README-only commits ahead.
- **Missing work was never added to the queue.** Worker entrypoints, compose commands and the topology call are missing work. They were never added to `specs/tasks.md` as `GEMINI.md` §7 requires. `design.md` was not updated where the implementation diverged, and `docs/adr/` is empty.
- **The README is stale.** It claims 395 tests and zero lint errors.
- **Claude Code does not load the constitution automatically.** The repo has no `CLAUDE.md` or `AGENTS.md`, so `GEMINI.md` auto-loads only for Gemini.
- **Hygiene.** The credential registry accepts plaintext `ya29.` tokens in `credentials_ref`. Compose hard-codes Grafana `admin/admin`. `docs/artifacts/` is an untracked byte-identical copy of `artifacts/`, and `files.zip` and `docs/architecture.zip` are stale snapshots.

---

## 5. What holds

These load-bearing properties were checked and pass:

- **Dependency rules hold.** `packages/domain` imports only the standard library. No package imports `services/`. No LLM SDK appears outside `packages/llm`.
- **Inbound email is never embedded.** Only `embedding_record` has a vector column.
- **Each prompt carries one email.** Micro-batching processes jobs one by one.
- **Checkpoints advance only after persist and publish,** which satisfies R2.8.
- **There is no runtime DDL.** Multi-tenant fixtures seed three organizations. pgvector 0.8.6 supports the iterative scans that the under-fill mitigation uses.
- **The generator enforces its call limits.** It allows exactly one generation call and at most one repair, validates the draft before returning it, and checks citations against supplied chunks.
- **Commit messages carry task and requirement IDs** almost throughout.

The code is careful at the component level. What is missing is the layer that assembles components into running processes, plus the transaction seam between stores.

---

## 6. What needs to be done

The five workstreams, in recommended order:

```
 W0 protect work ──▶ W1 make it runnable ──▶ W2 stop message loss ──▶ W3 transaction seam
   push 72 commits     imports, image,          retry DLX, unparseable     unit of work,
                       entrypoints, topology,    ack, replay, jitter,       idempotent
                       migrate/bootstrap job     drain order                redelivery
                                    │
                                    ▼
             W4 subsystem fixes (many bounded) ──▶ W5 truthful CI & evidence ──▶ resume queue: 4.11, 4.12, Phase 5 …
```

| Workstream | Contents | Process path |
|---|---|---|
| W0 | Push `RAG_Email_System` after reconciling the 2 remote README commits | your action or approval |
| W1 | Move the test contract suites out of production packages. Fix the Dockerfile to copy runtime assets, use the lock and skip dev deps. Add `main.py` for triage and mail-connector, host the renewal loop, lease reaper and queue monitor, wire compose commands and an init job for migrations and buckets. Call `setup_topology` at startup and make `make up` rebuild. | architectural: spec, then plan |
| W2 | Fix the retry dead-letter routing, dead-letter unparseable messages, fix replay routing and failure handling, apply jitter, fix drain order | bounded, with regression tests |
| W3 | Give stores a shared connection or transaction seam. Make the email and triage consumers idempotent under redelivery with `execute_once`. | architectural: changes store interfaces |
| W4 | Ingestion, knowledge versioning, retrieval query shape and category vocabulary, triage wiring, summarizer wiring and the thread-state race, tenant scoping | bounded, one per root cause |
| W5 | Run CI on the branch and fix the 14 mypy errors and 40 format failures. Isolate integration tests from the dev stack. Re-split the classification set by body. Re-open the 21 partial `[x]` tasks as `[~]` and add the discovered work to `specs/tasks.md`. Refresh the README. | bounded, plus tracking edits you approve |

W1 goes further than the "Option B" in the earlier assessment: an image built from HEAD would not start even with the compose commands added.

---

## 7. Method and coverage limits

- **Mapping.** Eighteen subsystem mappers and five task-claim auditors read the code, specs, git history and the live stack, all read-only.
- **Verification.** Each blocker and major finding got an adversarial fact check and a spec-context judgement. 99 were checked by two separate agents. The other 41 were checked by one agent doing both, after a usage limit stopped the first run.
- **Critic.** A completeness critic looked for uncovered paths and contradictions, and ran cheap runtime checks.
- **Not verified.** 246 minor and nit findings were checked by nobody. They appear in the register's section E as leads only.
- **Nothing changed.** No file, git state or container was modified during the audit. The only writes are this report and its register.

---

> **Problem:** the code is well built piece by piece, but the system cannot run. Six of eight services are health-check stubs. An image built from today's code would crash on start because production modules import pytest. Once the pipeline does run, the retry path would silently drop messages. On top of that, 72 commits of work exist only on this laptop.
>
> **Need you to:** first, push or approve pushing the branch, so Phases 3–4 are not one disk failure from gone. Second, pick the order. My recommendation is to make the system runnable and stop the message loss (W1–W2) before task 4.11. The alternative is to continue the queue as written. W1 is not in `specs/tasks.md`, so adding it is your call under `GEMINI.md` §7.

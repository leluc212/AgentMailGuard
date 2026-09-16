# AGENTS.md — Project Constitution

**Project:** Enterprise RAG-Based Intelligent Email Management and Response System (`rag-email`)
**Applies to:** every AI coding agent working in this repository — Claude Code, Cursor, Copilot, Windsurf, Aider, or any other.

This file is loaded on every turn. It is short on purpose. Read it, then read the spec.

---

## 1. This project is spec-driven

The specification is the source of truth. Code serves the spec, not the other way around.

| File | Role | When you read it |
|---|---|---|
| `specs/requirements.md` | The acceptance contract — 208 numbered criteria (`R1.1`…`R24.7`), NFRs, success criteria | Before starting any task; re-read the specific IDs your task cites |
| `specs/design.md` | The blueprint — architecture, interfaces, schema, contracts | Before writing code in an area you haven't touched |
| `specs/tasks.md` | The work queue — 110 tasks across Phases 0–8 | Every session; it is your working file |

**If code and spec disagree, the spec wins until a human says otherwise.**

---

## 2. The working loop

1. Open `specs/tasks.md`, take the **next unchecked task in order**. Do not skip ahead.
2. Read every `_Requirements:_` ID that task cites, in `specs/requirements.md`. They are the definition of done, not a footnote.
3. Read the relevant `specs/design.md` section. Use the interfaces and schema as written.
4. Implement **one task**. Not the phase. Not "while I'm here."
5. Write the tests that prove the cited criteria hold.
6. Verify the Definition of Done (§3). Then mark `[x]`.
7. Commit with the task number and requirement IDs in the message:
   `feat(triage): cascade orchestration [task 2.5] [R6.2, R6.7, R6.9, R6.11]`

Context discipline matters: one task at a time produces better results than one phase at a time, because the model stays focused on a single goal.

---

## 3. Definition of Done

A task is complete only when **all** hold:

1. Every cited criterion is implemented.
2. Automated tests cover those criteria.
3. `make up` still brings the stack to a healthy state.
4. New config keys are in `.env.example` **and** `docs/configuration.md`.
5. Structured logs and the metrics named in R21 are emitted for any new processing step.
6. CI is green — nothing outside the task's scope regressed.

Do not mark `[x]` on partial work. Mark `[~]` and say what's left.

---

## 4. Non-negotiable rules

**Architecture**

- Provider names (`gmail`, `graph`, `imap`) appear **only** inside `packages/adapters/`. Everywhere else, use `MailProviderAdapter`.
- `services/*` may import `packages/*`. `packages/*` must never import `services/*`. `packages/domain` imports stdlib + `packages/core` only.
- All retrieval goes through `SearchBackend`. No direct SQL from pipeline code.
- All model calls go through `LLMProvider`. No SDK imports outside `packages/llm/`.
- State transitions go through the state machine in `packages/domain/`. Never hand-write a state string.

**Data**

- Every tenant-scoped query carries `organization_id`. No exceptions.
- Schema changes are versioned migrations. No runtime DDL.
- Blobs go to object storage. Never into a relational column.

**Correctness**

- At-least-once delivery + idempotent consumers. Never assume exactly-once.
- Ack only after side effects are committed.
- State transitions commit in the same transaction as the side effect that caused them.
- Never persist an LLM response that failed schema validation.

**Cost**

- Exactly **one generation call** per job. Budget ceiling per job: ≤1 triage + ≤1 summarization + 1 generation + ≤1 repair.
- `reply_required=false` ⇒ zero AI work. `workflow_hint=template` ⇒ zero retrieval, zero generation.

---

## 5. Anti-goals — stop if you find yourself doing these

These are not style preferences. Each one breaks a load-bearing property of the design.

| Don't | Why |
|---|---|
| Build a multi-agent chain (planner → critic → writer) | Multiplies latency and cost per hop. Specialization is a config problem — use agent profiles (R14.4) |
| Batch multiple emails into one prompt | Destroys email independence. Micro-batch *workers*, never prompts (R3.7) |
| Embed every inbound email | The entire cost argument depends on not doing this (R6.5) |
| Add OpenSearch, MongoDB, Elasticsearch, or a vector DB | PostgreSQL-first is ADR-0001. Migrate only when exp08 measures insufficiency (R10.7) |
| Summarize a thread on every message | Threshold-triggered only (R8.4) |
| Skip the queue and call a service directly | Async decoupling is the reason bursts don't break the system (R3) |
| "Improve" retrieval by returning more chunks | More context ≠ better answers. Top-K is settled by exp03, not by intuition (R11.3) |
| Invent a security control | See §6 |

---

## 6. Hard stop — out of scope

The following are **deliberately excluded** from this project and are designed as separate cross-cutting subsystems:

authentication architecture · authorization · access-control enforcement · encryption architecture · DLP · prompt-injection defences · malware detection · phishing detection · secret detection · content security classification · security auditing · regulatory compliance · threat modelling

**If a task appears to require one of these, stop and raise it with the human.** Do not improvise a security design, do not add a "basic" version, do not leave a TODO that implies one exists. Provider OAuth credentials are assumed to exist as an integration prerequisite; store references, never secrets (`mailbox.credentials_ref`).

---

## 7. Divergence protocol

Reality will contradict the spec. That is expected. What is not acceptable is silent divergence.

**When the design is wrong or infeasible:**
1. Stop implementing.
2. State the contradiction plainly: what the spec says, what reality requires, what it costs either way.
3. Wait for a human decision.
4. On approval: update `specs/design.md`, write an ADR in `docs/adr/`, sync `specs/tasks.md` if task scope changed, **then** implement.

**When a requirement is ambiguous:** ask. Do not pick an interpretation and proceed — a wrong interpretation implemented confidently is more expensive than a question.

**When you discover missing work:** add a task to `specs/tasks.md` with requirement IDs. Do not silently expand the task you're on.

**Never** delete or renumber a requirement ID. Withdrawn requirements are marked `WITHDRAWN`, not removed — task references must keep resolving.

---

## 8. Testing

- No test requires live credentials or network access to a provider. Ever.
- External systems are faked: `FakeProviderAdapter`, stub `LLMProvider`, stub embedder.
- Integration tests use ephemeral PostgreSQL and RabbitMQ containers.
- Multi-tenant fixtures seed **≥3 tenants with overlapping content** — single-tenant fixtures hide the filtered-ANN under-fill bug (`design.md §5.5`).
- Every pure component gets unit tests: chunker, RRF, rules engine, state machine, query builder, idempotency, subject normalizer, quoted-history splitter, cost calculator.

Integration tests are the strongest signal that the system actually works. Prefer one real integration test over five mocked unit tests when the risk is in the wiring.

---

## 9. Known traps

Things that have already bitten this design on paper. Check these before debugging blindly.

- **Filtered HNSW under-returns.** Tenant-scoped vector search can silently return fewer than top-N and RRF will fuse the short list without error. Detect `count < top_n`, set `retrieval_underfilled=true`, widen `ef_search`. (`R10.10`)
- **Checkpoint ordering.** Advance the sync checkpoint only *after* messages are durably persisted and published. Re-running a window is safe; losing one is not. (`R2.8`)
- **Cost is a counter, not a gauge.** A gauge can't be summed over a window, which is exactly what SC9 needs.
- **`thread_state` is versioned.** Concurrent workers on one thread must use optimistic concurrency or you will lose summary updates. (`R8.6`)
- **Citations must be verified.** A model citing a chunk it was never shown is a hallucination signal worth measuring, not a formatting nit. (`R16.5`)

---

## 10. Reference numbers

Keep these in your head; they explain why the architecture is shaped this way.

```
100,000 emails/day  →  45,000 no reply      (zero AI cost)
                    →  20,000 template      (zero retrieval, zero generation)
                    →  35,000 AI generated  (~24,500 need RAG)

Average arrival 1.16 msg/s · 20× burst ≈ 23 msg/s
Typical end-to-end 2–6 s · p95 < 10 s
10,000 mailboxes ≠ 10,000 concurrent agents
```

The governing sentence, and the reason every gate exists:

> **Classify first. Retrieve only when required. Generate only when necessary.**

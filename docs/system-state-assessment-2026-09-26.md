# System State Assessment — `rag-email`

**Date:** 2026-09-26 · **Branch:** `RAG_Email_System` @ `1ecf7b1` · **Unpushed commits:** 72 (`git rev-list --count origin/RAG_Email_System..HEAD`)

**Method.** Every figure is measured against the working tree at that commit. Where this document cites a requirement, a gate, or a design rule, the text of that artifact is quoted inline so the claim is auditable without opening another file. Commands are given so each measurement can be reproduced.

---

## 1. Findings summary

| Dimension | Measurement | Command |
|---|---|---|
| Task completion | 66 done, 1 in progress, 43 open (110 total) | `grep -cE '^- \[x\]' specs/tasks.md` |
| Production LOC | 35,459 — `packages/` 26,974, `services/` 8,485 | `find … -name '*.py' \| xargs cat \| wc -l` |
| Test LOC | 35,731 across 122 test modules (87 unit, 35 integration) | same, over `tests/` |
| Unit suite | **1,041 passed**, 0 failed, 21.0 s | `uv run pytest tests/unit` |
| Integration suite | **117 passed**, 0 failed, 46.5 s (live PostgreSQL + RabbitMQ) | `uv run pytest tests/integration` |
| Type check, production | **clean**, 108 source files, `strict = true` | `uv run mypy packages/` |
| Type check, full lint target | **14 errors in 6 files**, 297 files checked | `uv run mypy packages services tests evaluation` |
| Lint | clean | `uv run ruff check .` |
| Format | 39 files would be reformatted | `uv run ruff format --check .` |
| **Executable services** | **2 of 8** containers run service code | compose audit, §3 |
| Experiment artifacts | 0 | `ls evaluation/results/` → `.gitkeep` |

The code is in good condition. The gap is between **built** and **executable**: the intelligence layers are implemented and tested, and the process supervision that would run them is largely absent.

---

## 2. Build inventory

### 2.1 Packages — all implemented, all covered by tests

```
packages/db             7,222   asyncpg pools, migration runner, 12 entity stores
packages/llm            3,043   provider protocol + 3 impls, budget tracker, router,
                                profiles, schema validation, citation verifier, generator
packages/knowledge      2,979   parsers (pdf/docx/html/md), chunker, embedder
packages/retrieval      2,730   SearchBackend, pgvector + FTS, RRF fusion, rerank, query builder
packages/broker         2,505   AMQP topology, envelope, publisher, BaseConsumer, DLX
packages/adapters       2,223   gmail, graph, imap, fake — provider names confined here
packages/domain         2,002   entities, value objects, state machine (stdlib + core only)
packages/core           1,792   Pydantic settings, object storage, idempotency, logging
packages/observability  1,556   metrics registry, span helpers, health, shutdown, cost table
packages/context          921   thread assembly, summarization policy, ContextBuilder
packages/business           1   __init__.py only — not started
```

`packages/llm` module-level breakdown (`wc -l`):

```
generator.py 536   router.py 433   profile.py 309   validation.py 300   budget.py 270
client.py 254   anthropic.py 207   testing.py 177   factory.py 129   fake.py 123
citations.py 108   __init__.py 111   protocol.py 86
```

### 2.2 Services — implemented unevenly

```
services/api             2,924   FastAPI: 7 routers, 7 schema modules, pagination, errors
services/triage_worker   2,804   gate 758 · cascade 551 · training 269 · llm_classifier 267
                                 thresholds 223 · consumer 215 · classifier 197 · rules 154
services/email_worker    1,932   normalizer, parser, threading, attachments, persister, main
services/mail_connector    568   orchestrator, renewal
services/knowledge_worker  255   consumer, main
services/ai_worker           1   __init__.py only
services/dispatch_worker     1   __init__.py only
```

The layout matches the designed tree. The design document specifies:

```
├── services/
│   ├── api/                           # FastAPI app, routers, OpenAPI
│   ├── mail_connector/
│   ├── email_worker/
│   ├── triage_worker/
│   ├── ai_worker/
│   ├── knowledge_worker/
│   └── dispatch_worker/
```

All seven directories exist; two contain only `__init__.py`.

The dependency rule it states — *"`services/*` may import `packages/*`. `packages/*` must not import `services/*`. `packages/domain` imports nothing but stdlib and `packages/core`"* — holds. `packages/llm/citations.py` imports only `__future__`, `collections.abc`, `dataclasses`, `typing`, and `packages.domain.entities`; no `packages/*` module imports `services/*`.

---

## 3. Runtime topology: what actually executes

`docker-compose.yml` defines 8 services built from the repository `Dockerfile`. That Dockerfile ends:

```dockerfile
ENV PYTHONPATH=/app
ENV PORT=8000
ENV SERVICE_NAME=service
EXPOSE 8000
CMD ["python", "services/placeholder.py"]
```

A compose service therefore runs real code **only if it overrides `command:`**. Audit of the 8 built services:

| Compose service | `command:` override | Process actually running |
|---|---|---|
| `api` | `uvicorn services.api.main:app --host 0.0.0.0 --port 8000` | **service code** |
| `email-worker` | `python -m services.email_worker.main` | **service code** |
| `mail-connector` | — | `services/placeholder.py` |
| `triage-worker` | — | `services/placeholder.py` |
| `ai-worker` | — | `services/placeholder.py` |
| `knowledge-worker` | — | `services/placeholder.py` |
| `dispatch-worker` | — | `services/placeholder.py` |
| `frontend` | — | `services/placeholder.py` |

`services/placeholder.py` is a 57-line `http.server` handler. Its `/metrics` route:

```python
elif self.path == "/metrics":
    self.send_response(200)
    self.send_header("Content-Type", "text/plain; version=0.0.4")
    self.end_headers()
    metrics = (
        f"# HELP up Status of {service_name}\n"
        f"# TYPE up gauge\n"
        f'up{{service="{service_name}"}} 1\n'
    )
    self.wfile.write(metrics.encode("utf-8"))
```

Its own module docstring states the intent: *"Placeholder HTTP server for service containers during Phase 0 bootstrap. Provides /healthz, /readyz, and /metrics endpoints so container health checks and Prometheus scraping function properly before full worker logic is deployed."*

Consequence: `docker compose ps` reports all containers healthy, and for six of them "healthy" means the stub answered `/healthz`. Prometheus scrapes a constant `up 1` from those six and no service metrics whatsoever. **Container health is not a signal about this system's function.**

### 3.1 The entrypoint gap, precisely

Two workers have complete, tested logic and no process to host it.

`services/email_worker/main.py` is the working pattern — it constructs dependencies, starts the consumer alongside a health server, and coordinates shutdown:

```python
from packages.broker.publisher import MessagePublisher
from packages.db.connection import create_pool_from_settings
from packages.observability.health import HealthRegistry, create_health_router
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import init_tracer
from services.email_worker.consumer import EmailNormalizationConsumer

async def run_worker() -> None:
    """Initialize resources, start consumer and health server, and coordinate shutdown."""
    settings = AppSettings()
    ...
```

`services/triage_worker/` has the consumer but no such module:

```
services/triage_worker/consumer.py:46   class TriageConsumer(BaseConsumer):
services/triage_worker/main.py          ← does not exist
```

`find services -name 'main.py'` returns exactly three paths: `services/api/main.py`, `services/email_worker/main.py`, `services/knowledge_worker/main.py`. `knowledge_worker` has a `main.py` but no compose `command:`, so it also runs the stub.

Net pipeline reachability:

```
mailbox ──▶ api /webhook ──▶ email.normalize ──▶ [email_worker] ──▶ email.triage ──▶ ⊘
             ✅ running        queue declared       ✅ running        published to     nothing
                                                                     a queue with     consumes
                                                                     no consumer
```

`TriageConsumer` would drain `email.triage`; nothing instantiates it. Downstream of that point — retrieval, generation, persistence, dispatch — no process exists at all: `ai_worker` and `dispatch_worker` are single-line packages.

---

## 4. Requirement implementation status

Status is judged against the criterion text, quoted.

### Implemented and covered by tests

| ID | Criterion (verbatim) | Implementation |
|---|---|---|
| R16.1 | *"SHALL require the model to return a schema-conformant object: `action, draft, confidence, knowledge_chunks[], thread_summary_updated, model_tier`"* | `schemas/reply.v1.json` (all 6 in `required`, `additionalProperties: false`); `DraftReplyPayload` in `packages/llm/validation.py` with `ConfigDict(extra="forbid")` |
| R16.2 | *"SHALL validate the response against the schema before persistence"* | `validate_draft_payload()` called from `SinglePassGenerator.generate_draft` before `GenerationResult` is constructed |
| R16.3 | *"IF validation fails, THEN SHALL retry once with a repair instruction, and on second failure SHALL fail the job into the retry/DLQ path — never persist an unvalidated draft"* | `_validate_with_repair()` + `_repair_draft()`, bounded by `CallBudgetTracker.MAX_REPAIR = 1`; raises `UnvalidatedDraftError` on second failure. Generator half complete; the DLQ hop has no consumer to exercise — see §5 |
| R16.5 | *"SHALL reject citations that do not correspond to chunks actually supplied in the context, recording a `citation_mismatch` flag"* | `packages/llm/citations.py::verify_citations`, 108 lines, 100 % statement coverage, 16 dedicated tests |
| R14.9 | per-job call ceiling | `CallBudgetTracker` with `MAX_TRIAGE/SUMMARIZE/GENERATE/REPAIR = 1` and `BUDGET_CEILING = 4`; `assert_generation_budget(require_generation=True)` enforces exactly one generation call |
| R5.* | data platform | 3 forward+reverse migrations; `pgvector`, `pg_trgm`; tenant scoping verified by integration tests against live PostgreSQL |
| R24.* | engineering baseline | strict mypy on `packages/`, ruff, 122 test modules, pinned `uv.lock` |

The call-budget design the implementation enforces:

| Call | When | Max |
|---|---|---:|
| Triage LLM | stage 3 only, after rules and ML both abstain | 1 |
| Thread summarization | threshold-triggered only | 1 |
| **Generation** | always, for AI-path emails | **exactly 1** |
| Schema repair | only on validation failure | 1 |

Ceiling 4, common case 1 — asserted in `CallBudgetTracker.assert_generation_budget`, and the invariant the test suite pins is "one *generation* call per job", not "one LLM call per job".

### Implemented but not reachable at runtime

R1/R2/R4 (ingestion adapters), R3/R6/R7/R18/R19 (triage cascade, queue routing, state machine, idempotency), R9/R10/R11 (knowledge ingestion, hybrid retrieval, reranking). All have code and tests; none have a running host process except the parts inside `api` and `email_worker`.

### Not started

- **R13** business data — `packages/business` is one line.
- **R17** dispatch — `services/dispatch_worker` is one line.
- **R22** evaluation harness — datasets and loaders exist; `evaluation/experiments/` and `evaluation/results/` contain only `.gitkeep`.
- **R23** review UI — `frontend/` contains only `.gitkeep`.

---

## 5. Phase gate executability

Each gate below is quoted from the work queue. The right column states whether it can be executed against the current stack.

| Gate | Text (abridged verbatim) | Executable now |
|---|---|---|
| 0 | *"`make up && make migrate && make seed && make test` succeeds from a clean checkout. Every service reports ready, exposes `/metrics`, and logs a trace id."* | **Yes**, though "exposes `/metrics`" is satisfied by the stub for 6 services |
| 1 | *"a real Gmail mailbox (or the fake adapter in CI) delivers a notification; the message appears in `email_message`, associated with a thread, with attachments in MinIO, the checkpoint advanced…"* | **Partly** — `api` + `email_worker` run; `mail_connector` does not, so notification intake is not exercised end to end |
| 2 | *"a newsletter fixture terminates at `COMPLETED` with zero AI calls; an acknowledgement fixture produces a template reply…; a support fixture lands in `email.support.normal`"* | **No** — no `triage_worker` process |
| 3 | *"a support email retrieves the correct procedure chunk; an invoice-identifier email retrieves the correct billing chunk via the lexical branch…"* | **No** as a live path; retrieval is proven only by integration tests |
| 4 | *"a support email with a 12-message thread produces a schema-valid, citation-verified draft in `DRAFTED`; a short thread triggers no summarization; a low-confidence job escalates exactly once; forcing single-tier mode still works end to end"* | **No** — no `ai_worker`; `DRAFTED` transition is task 4.11 |
| 7 | *"every hypothesis H1–H5 has a reproducible artifact with a run manifest, and SC1–SC10 are reported with measured values"* | **No** — zero artifacts exist |
| 8 | *"a 20× burst is absorbed without job loss; adding AI-worker replicas measurably raises throughput…"* | **No** |

No gate from 2 onward has been executed in its stated end-to-end form. Phases 1–3 are marked complete on component and integration evidence.

---

## 6. Observability inventory

The observability criterion enumerates required metrics: *"SHALL expose Prometheus metrics: `emails_received_total, emails_classified_total, emails_generated_total, classification_latency_ms, retrieval_latency_ms, generation_latency_ms, end_to_end_latency_ms, queue_depth, queue_wait_ms, retrieval_hit_rate, retrieval_top_k, input_tokens_total, output_tokens_total, embedding_tokens_total, estimated_ai_cost, failed_jobs_total, retry_jobs_total`."*

Checked by name against `packages/observability/metrics.py`: **all 17 are registered.** Each was added by the task that needed it rather than deferred, so the metric *registry* is effectively complete even though its phase shows 0 of 17 tasks done. Additional metrics registered beyond the enumerated list include `triage_funnel_outcomes_total`, `model_escalations_total`, `llm_calls_total`, `llm_calls_per_job`, `draft_repairs_total`, `draft_validation_failures_total`, `citations_verified_total`, `citation_mismatches_total`, `retrieval_underfilled_total`, `rerank_fallback_total`.

What remains genuinely unbuilt in this area: OpenTelemetry span coverage across all nine stages, the five provisioned Grafana dashboards, cost aggregation per email/category/day, and the early-exit funnel panel.

Caveat: six of eight containers export only `up 1`, so the registry is complete while the *scrape surface* is not.

---

## 7. Evidence deficit

The project defines five hypotheses and ten success criteria. None has a measured value.

| ID | Hypothesis (verbatim) |
|---|---|
| H1 | *"Hybrid lexical+semantic retrieval beats vector-only for email knowledge"* |
| H2 | *"Cascaded triage cuts unnecessary generation without hurting routing accuracy"* |
| H3 | *"Thread summarization cuts tokens while preserving reply quality"* |
| H4 | *"Model cascading cuts cost vs a single strong model at acceptable quality"* |
| H5 | *"The async queue architecture absorbs bursts and scales with stateless workers"* |

Success criteria include macro-F1 ≥ 0.90 (SC1), Recall@5 ≥ 0.85 (SC2), useful-draft acceptance ≥ 80 % (SC3), typical end-to-end ≤ 6 s (SC4), p95 ≤ 10 s (SC5), zero duplicate logical processing (SC6). The latency SLO targets range from *"Notification → ingestion < 1 s"* through *"End-to-end p95 < 10 s"*, against a reference workload of *"10,000 mailboxes / 100,000 emails per day"* and a sustained ingest floor of *"≥ 25 msg/s"*.

Present: `evaluation/datasets/classification/{train,test}.jsonl`, `evaluation/datasets/retrieval/queries.jsonl`, generator and loader modules, and a trained classifier at `docs/artifacts/models/triage_ml_v1.joblib` with a metrics sidecar.
Absent: every experiment runner, every result artifact, and the end-to-end path any latency or throughput measurement would require.

---

## 8. Defect and debt register

| # | Item | Location | Impact |
|---|---|---|---|
| 1 | Six services run the Phase-0 stub | `docker-compose.yml`, `Dockerfile` `CMD` | pipeline not executable; container health misleading |
| 2 | `TriageConsumer` has no launcher | `services/triage_worker/consumer.py:46`; no `main.py` | 2,804 tested lines unreachable |
| 3 | `mail_connector` has no launcher | `services/mail_connector/{orchestrator,renewal}.py` | notification intake unreachable |
| 4 | `knowledge_worker` has `main.py`, no compose `command:` | `docker-compose.yml` | KB ingestion unreachable |
| 5 | 14 mypy errors in test files | `tests/unit/test_queue_metrics.py` (4), `tests/unit/test_draft_repair_orchestration.py` (3), `tests/integration/test_queue_metrics_integration.py` (3), `tests/unit/test_citation_verification_generation.py` (2), `tests/unit/test_thread_context_assembly.py` (1), `tests/integration/test_thread_context_assembly_postgres.py` (1) | `make lint` runs `mypy packages services tests evaluation`, so the defined CI target is red while production code is clean |
| 6 | 39 files fail `ruff format --check` | `packages/retrieval/*`, `packages/knowledge/*`, `packages/context/*`, several test modules, and markdown files whose embedded Python this ruff version reformats | `make fmt-check` fails; none of the affected files were touched by recent work |
| 7 | Zero ADRs | `docs/adr/` holds only `.gitkeep` | decisions taken this phase are unrecorded outside commit messages |
| 8 | README figures stale | claims "395 Tests Passing"; actual 1,158. Claims zero lint errors; true of `packages/`, not of `make lint` | misleads a new contributor |
| 9 | `up{service=…} 1` is the only metric from 6 services | `services/placeholder.py` | a Prometheus alert on service liveness would be satisfied by a dead pipeline |

Item 5 detail — the two newest errors are a `PipelineMetrics` argument-type mismatch where tests pass a stub metrics object to `SinglePassGenerator(metrics=…)`; the parameter is typed `PipelineMetrics | None`, and the stubs are structural duck-types rather than instances.

---

## 9. Remaining work

**Phase 4 — 2 open tasks, 1 to close**

- `4.11 Draft persistence` — *"Persist body, citations, model, tier, escalation reason, prompt version, token counts, estimated cost. Transition `GENERATING → DRAFTED`."* The target schema already exists:

  ```sql
  CREATE TABLE IF NOT EXISTS generated_draft (
      id UUID PRIMARY KEY,
      organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
      job_id UUID REFERENCES processing_job(id) ON DELETE SET NULL,
      message_id UUID NOT NULL REFERENCES email_message(id) ON DELETE CASCADE,
      thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
      action TEXT NOT NULL, subject TEXT, body TEXT NOT NULL,
      confidence NUMERIC(4,3),
      citations JSONB NOT NULL DEFAULT '[]',
      citation_mismatch BOOLEAN NOT NULL DEFAULT false,
      model_name TEXT, model_tier TEXT, escalation_reason TEXT, prompt_version TEXT,
      input_tokens INT, output_tokens INT, cost_estimate NUMERIC(10,6),
      status TEXT NOT NULL DEFAULT 'draft'
          CHECK (status IN ('draft','approved','rejected','dispatched')),
      provider_ref TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  ```

  `packages/db/draft.py` already reads and writes both `citations` and `citation_mismatch`. Implementation note: persist `GenerationResult.citation_verdict.citations` (accepted only) and `citation_mismatch`; `content["knowledge_chunks"]` intentionally retains rejected ids as a record of the model's claim and must not be used as the persisted citation list.
- `4.12 Generation metrics` — likely reduces to verification given §6.
- `4.9` remains in progress pending a consumer that can exercise the DLQ hop.

**Phases 5–8 — 41 tasks**

| Phase | Tasks | Substance |
|---|---|---|
| 5 | 6 | `packages/business` from zero: schema + seed, `BusinessDataProvider`, sender→customer resolution, intent-driven fact fetching, missing-entity handling |
| 6 | 9 | draft management API, feedback capture, outbound construction, dispatch modes, idempotent dispatch, write-back, review UI, end-to-end smoke. **`ai_worker` and `dispatch_worker` get built here — this is where the pipeline first runs end to end** |
| 7 | 17 | spans, dashboards, cost accounting, benchmark expansion, experiment runner, exp01–exp09, quality rubric, success-criteria report |
| 8 | 9 | scale profile, autoscaling signal, quorum queues, resilience suite, backpressure, OpenSearch migration path, ADRs, runbook, evolution doc |

**Off the task list**

Entrypoints and compose `command:` lines for `triage_worker` and `mail_connector` (register items 2–4). These are not tasks in the queue; the work is a `main.py` per worker following the `email_worker` pattern plus one compose line each, and it is the precondition for executing the Phase 1–4 gates as written.

---

## 10. Assessment

**What is sound.** Requirement traceability is carried in commit messages (`[task 4.8] [R15.1-R15.6]`) and holds across 72 commits. Integration tests run against live PostgreSQL and RabbitMQ rather than mocks — 117 of them. The architectural constraints are enforced in fact, not just stated: `packages/domain` imports only stdlib and `packages/core`; provider names appear only under `packages/adapters`. The cost-avoidance design is implemented and asserted rather than assumed — rules and ML classify before any LLM call, `CallBudgetTracker` enforces exactly one generation call per job with a hard ceiling of four, and tier escalation replaces the generation call rather than adding one. Where implementation contradicted the plan, the plan was corrected: task 4.9 was moved from `[x]` to `[~]` with the residual explicitly named.

**What is not.** Completion is tracked by checkbox, and the checkbox measures component completion, not operability. Six of eight containers report healthy while executing a 57-line stub. Two fully implemented workers — 3,372 lines combined — have no host process. No phase gate from 2 onward has been executed in its stated form. The defined CI target (`make lint`) is red on 14 test-file type errors while production code type-checks clean under strict mode.

**Quantified position.** 60 % of tasks are complete. Retired risk is lower than that, because the unretired risk is concentrated in integration and measurement: whether the full chain holds under the reference workload, and whether H1–H5 survive contact with data. Component quality predicts success; nothing yet demonstrates it.

---

> **Problem:** the system is implemented to a high standard through Phase 4 and cannot run end to end. Six of eight containers execute a health stub, `triage_worker` and `mail_connector` have complete tested logic with no `main.py`, and `ai_worker`/`dispatch_worker` do not exist yet. Consequently every phase gate from 2 onward, all eleven latency targets, and all ten success criteria are unverified.
>
> **Need you to:** choose the order. Option A — continue the queue as written: 4.11, 4.12, then Phases 5–8, with a runnable pipeline arriving partway through Phase 6. Option B — insert an unqueued block now to add `main.py` for `triage_worker` and `mail_connector` plus three compose `command:` lines, which makes the Phase 1–4 gates executable and converts container health into a real signal. Option B is not in the task list, so it is your call; I can proceed with 4.11 immediately either way.

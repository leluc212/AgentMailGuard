# ADR-0018: a demo console for the thesis defence, with a demo ingest door, per-job run options, and AgentMailGuard in the live ai-worker

- **Status:** Accepted, 2026-11-16
- **Date:** 2026-11-16
- **Decided by:** project owner (decisions Q1–Q10 and C1–C9 of `PLAN_UI.md` §8, 2026-11-16)
- **Number:** 0018, because ADR-0015 decision 11 reserves ADR-0016 for the host hooks of task 7.32
- **Tasks:** `specs/tasks.md` Phase 9 (9.0–9.15)
- **Requirements:** new group R25 (R25.1–R25.12); touches R3.1, R14.7, R16.6, R18.1, R21.7, R23.1, R23.6
- **Amends:** ADR-0009 (its rejected alternative "a React single-page app"), ADR-0010 decision 3 (wiring the guard into the live ai-worker), the `.dockerignore` rule "no container imports `agentmailguard/`" (task 7.24). **Does not change** ADR-0011, ADR-0015 or ADR-0017: the benchmark keeps its own guard-worker, configs and pin
- **Plan:** `PLAN_UI.md`, progress in `TRACKING_UI.md`

## Context

The project is presented at a thesis defence. The committee must see, on the running stack:

1. rag-email's cost gates working on real emails: no-reply exits, template exits, retrieval only when needed, one generation call (AGENTS.md §10).
2. AgentMailGuard stopping attacks inside rag-email: the same email under `C0` (no guard) and `C3` (full guard), side by side.

The owner asked for a live demo that can run every scenario, is clear and visual, and looks modern; bilingual (Vietnamese and English); with a switch between the offline fake model and a real GPT model.

What the stack offers today:

- The review UI (`services/frontend`, Jinja2 + htmx, `127.0.0.1:3001`, ADR-0009) covers drafts, the job timeline and knowledge upload (R23.4, R23.5, R23.7). It has no way to start a run and no pipeline view.
- An email enters only through the mail-connector (Gmail) or through the benchmark feeder (`evaluation/mailguard_bench/live/feeder.py`), which calls `SyncOrchestrator.sync_mailbox` (`services/mail_connector/orchestrator.py`) with an adapter holding one message. The API has no ingest endpoint.
- The model is chosen per process at start-up (`LLM__PROVIDER`, `packages/llm/factory.py`), so a switch during a demo is impossible.
- The guard runs only in the benchmark: `evaluation/mailguard_bench/live/guard_worker.py` starts a host process that replaces the ai-worker and swaps `GuardedDraftingService` in through `build_consumers(drafting_factory=...)`. That code imports the benchmark's case adapters, result store and tree-hash pin. `.dockerignore` keeps `agentmailguard/` and `evaluation/` out of every image.
- `requirements.md` §0.5 keeps prompt-injection defences out of rag-email. ADR-0010 makes AgentMailGuard that separate subsystem, called only through its own integration adapters, and leaves wiring it into the live ai-worker to "a later, separate decision". The owner made that decision on 2026-11-16 (Q2).

## Decision

1. **A second front end, `demo-ui`.** A single-page app in `services/demo_ui/`: React, Vite, TypeScript, Tailwind CSS, shadcn/ui, React Flow (pipeline diagram), Recharts (charts), react-i18next (Vietnamese and English; email text, state names, layer and rule ids are not translated). The build is static and served by nginx, which also reverse-proxies `/v1` to the `api` service, so the browser sees one origin and the API needs no CORS change. Compose binds it to `127.0.0.1:3002`. It calls only `/v1`. The review UI stays as it is and keeps R23.4, R23.5, R23.7 until the console's review screen is accepted (owner decision Q5). ADR-0009 rejected a React app *for the review UI* (three form screens); the console's animated pipeline view, side-by-side comparison and charts are the reason it is accepted here.

2. **No login, local only.** The console can start runs, approve drafts and spend model credit. Authentication stays out of scope (AGENTS.md §6). Like ADR-0009, the port is bound to `127.0.0.1` and nothing is added.

3. **A demo ingest door that keeps the queue.** A router `/v1/demo` is mounted only when `DEMO__ENABLED=true` (default `false`; when false every `/v1/demo` path answers 404):
   - `GET /v1/demo/capabilities`: whether the live profile is configured (and its model name), whether the guard is available, whether the L1 classifier is loaded.
   - `GET /v1/demo/scenarios`: the catalog (decision 5).
   - `POST /v1/demo/messages`: one email (from, subject, body, optional thread messages) plus run options (decision 4); answers 202 with a `demo_request_id`.
   - `POST /v1/demo/scenarios/{id}/run`: the same, with the scenario's email and options; a comparison scenario creates one request per guard mode.
   - `GET /v1/demo/requests/{id}`: the `message_id` and `job_id` once ingested, else `pending`.

   The API builds a text/plain MIME message, gives it a fresh `Message-ID`, and publishes a `mail.demo.inject` envelope. The mail-connector consumes it and calls `SyncOrchestrator.sync_mailbox` with an inline one-message adapter, exactly as the feeder does, so the archive in MinIO, the idempotency key, the `processing_job` row and the publish to `email.normalize` are the connector's own code. The inline adapter lives in `packages/adapters/` (AGENTS.md §4). `make seed` creates one demo mailbox per seeded tenant (≥3 tenants). Every request carries `X-Organization-ID` (R23.6).

4. **Run options per job.** A demo request carries `run_options = {model_profile: fake|live, guard_mode: C0|C3}`. They are stored on the job (`processing_job.run_options`, JSONB, nullable, migration `0006`) and copied into every envelope of that job (design §7.3), so retries and replays (`POST /v1/jobs/{id}/replay`) keep them. A job without `run_options` behaves exactly as today.
   - **Model switch (owner decision C1, "M1").** The triage-worker and the ai-worker build two `LLMProvider`s at start-up: `fake` (`FakeLLMProvider`) and `live` (from the `LLM__*` settings). Each job picks one by `model_profile`; the absent default is the configured provider. If the live provider is not configured (`LLM__PROVIDER=fake`), `POST /v1/demo/...` with `live` answers 422 before anything is enqueued.
   - **Live model (owner decision C3: GPT).** `LLM__PROVIDER=openai`, and `gpt-4o-mini` for every role: the fast, strong and fallback tiers, the summarizer, and the guard's five judges, as in the benchmark (owner decision C3a).
   - **Embedding (owner decision C2, "E2").** The switch never changes the embedding: stored vectors and query vectors must come from one model (R5.10). Both profiles use a real `text-embedding-3-small` (OpenAI, 1536 dimensions). The fallback when there is no network is a separate offline profile (`LLM__PROVIDER=fake`, `EMBEDDING__MOCK=true`), brought up with `make up` and `make seed` *before* the defence, not switched from the UI.
   - Cost: live calls are priced from `LLM__PRICE_TABLE` as today; fake calls cost 0. The R21 cost counters gain a `model_profile` label.

5. **Scenario catalog as configuration.** `config/demo_scenarios.yaml` holds S01–S14 and G01–G06 of `PLAN_UI.md` §4: the emails (or thread), the tenant, the run options, and the expected outcome (final state, category, `reply_required`, `workflow_hint`, inbound and final guard action, number of model calls). The console compares expected with actual. Guard scenarios copy a few case texts from `evaluation/datasets/mailguard/cases.jsonl` into the YAML, because no image contains `evaluation/`; each copied case keeps its case id and source. LLMail-Inject texts are MIT. For RAG poisoning the catalog uses the project's own seed cases (MIT), not PoisonedRAG questions taken from MS MARCO (non-commercial only); attribution follows `evaluation/mailguard_bench/pinned/NOTICE.md`.

6. **AgentMailGuard in the live ai-worker (owner decision Q2).**
   - Setting `GUARD__ENABLED` (default `false`) makes the guard available; a job runs it when its `guard_mode` is `C3`. `GUARD__DEFAULT_MODE` (default `C0`) applies to jobs without `run_options`. Only `C0` and `C3` exist (owner decision C5, as ADR-0017).
   - A new `services/ai_worker/guarded_drafting.py` wraps the one generation call. It calls only AgentMailGuard's public API (`mailguard.pipeline.MailGuardPipeline` with `GuardConfig.preset("C3")`, and `mailguard.integration.adapters`), as ADR-0010 decision 1 requires: rag-email's code gains no rule, classifier, sanitiser or policy. Its outcome mapping is the benchmark's: an inbound `block`/`quarantine` makes an escalated draft with an empty body and no reply call; `human_approval` keeps the draft with reason `agentmailguard:human_approval:<rule>`; otherwise the body as the output guard left it.
   - The benchmark code (`evaluation/mailguard_bench/live/*`) and the guard subtree (pin `d95e542`, tree hash `MAILGUARD_V3_TREE`) are **not changed**, so published runs stay reproducible. A parity test runs the same fixture emails through both wrappers with fake providers and asserts the same decisions and draft bodies.
   - Guard models: with `model_profile=fake` the five judges are `fake` (no LLM stage is built); with `live` they use the live model.
   - **Call budget (owner decision C7).** AGENTS.md §4 caps a job at ≤1 triage + ≤1 summarization + 1 generation + ≤1 repair. A `C3` job adds up to five guard judge calls (the guard's shared budget). As in the benchmark, the guard's calls are counted and priced in the guard report and in a separate `guard_llm_calls_total` counter, outside the job's `CallBudgetTracker`, and only `C3` jobs make them. This is an accepted exception to AGENTS.md §4 for `C3` jobs only; `C0` jobs keep the budget unchanged.
   - Image: `agentmailguard/` is installed into the app image (`.dockerignore` stops excluding it; `evaluation/` stays excluded). The private L1 classifier is never baked in or committed: compose mounts it read-only from `GUARD__L1_MODEL_PATH` on the host. Without it L1's classifier stage is off, `GET /v1/demo/capabilities` says so, and the console shows a warning.
   - **Display only.** The guard's decision does not change `dispatch_mode` and never enables `auto_send`; every draft still goes through review (ADR-0009). Consuming the Doc Scanner's `trust` in rag-email (badges, `trust_level`, exclusion, re-retrieval) remains ADR-0016 / task 7.32 and is not part of this decision.

7. **Guard report storage.** Table `guard_report` (migration `0006`): `organization_id`, `job_id` (unique), `guard_mode`, `schema_version` (3), `inbound_action`, `final_action`, `report` (JSONB: the per-layer verdicts, the chunk trust list, the isolation info, the judges called), `guard_llm_calls`, `timings_ms`, `created_at`. It is written in the same transaction as `persist_drafted` (AGENTS.md §4). `GET /v1/jobs/{id}/guard` returns it, organization-scoped; 404 for a `C0` job.

8. **Live progress by polling (owner decision Q6).** The console polls `GET /v1/jobs/{id}/timeline` (existing, R23.5) about every 500 ms while the job is not terminal, then stops. No stream endpoint. Step details come from the existing endpoints (`messages`, `threads`, `drafts`, `search/debug`) and from timeline event payloads; where a step's data is missing from the payload (to be listed in task 9.1), the step that emits the event adds it.

9. **Observability summary (owner decision Q7).** `GET /v1/metrics/summary?window=<duration>` reads the R21 metrics from Prometheus (`OBSERVABILITY__PROMETHEUS_URL`, default `http://prometheus:9090`) and returns the early-exit funnel, p50/p95 end-to-end latency, queue depths and the cost increase over the window. The console draws them with Recharts; Grafana stays for operators. Some R21 metrics carry an `organization` label (the funnel, `packages/observability/funnel.py`) and some may not (to be listed in task 9.8). The endpoint requires `X-Organization-ID` (R23.6), filters by it where the label exists, and marks each panel `scope: "organization"` or `scope: "stack"`.

10. **Benchmark results, read-only.** A script (`scripts/export_bench_for_demo.py`) copies the committed `summary.json` and `metrics.csv` of the three v3 1,800-case runs (`evaluation/results/mailguard_bench/2026-10-19-*-v3-1800`) into the console's static assets at build time. The console never runs a benchmark case.

11. **Controlled faults for the retry and fail-closed scenarios (S10, G06).** When `DEMO__FAULT_MARKERS_ENABLED=true` (requires `DEMO__ENABLED`; default `false`), `FakeLLMProvider` fails on a marker in the prompt (`[[demo-fault:timeout]]`, `[[demo-fault:invalid-json]]`). Live providers never read markers.

12. **Order of work.** Phase 9 depends on Phases 0–6 (all done except 6.10, the live Gmail gate, which the console does not need: demo emails enter through the inline adapter) and not on the open Phase 7 and 8 tasks. By owner decision (C8) its tasks may start before the Phase 7 and 8 gates. Inside Phase 9 the order rule of AGENTS.md §2 holds.

## Consequences

- rag-email gains a second front end and a Node build stage in the Dockerfile; CI gains a UI job (lint, type-check, unit tests, build) and a Playwright end-to-end test against the stack with fake providers (R24.5: no live credentials in any test).
- rag-email's production path can now run the guard. ADR-0010's rule still holds: the defence logic stays in `agentmailguard/`, and rag-email only calls its public API. `requirements.md` §0.5 is unchanged; R25.5 states the call, not a defence.
- Two guard wrappers exist (benchmark and live), kept equal by the parity test, so the pinned benchmark is not touched.
- `processing_job` gains a column and the schema gains a table (migration `0006`, with its `down`).
- AGENTS.md and CLAUDE.md §1 now count 220 criteria (`R1.1`…`R25.12`) and the tasks of Phases 0–9 (owner decision C9).
- With E2, even the fake profile needs network and an embedding key; the offline profile is the fallback.

## Alternatives considered

- **Upgrade the Jinja2 + htmx review UI.** One toolchain, but an animated pipeline view, side-by-side comparison and charts are hard to make look modern there (owner decision Q4).
- **Write demo emails straight into the database from the API.** Skips the connector and the queue (AGENTS.md §5 anti-goal).
- **Restart the workers to switch the model ("M2").** 30–60 s per switch, a start-up failure risk during the defence, and the UI would need control of Docker (owner decision C1).
- **A server-sent-events stream.** Smoother, but a new endpoint and connection handling; polling the existing timeline is enough (owner decision Q6).
- **Reuse the benchmark's `GuardedDraftingService` in the ai-worker.** It imports benchmark-only modules and the tree-hash pin; changing it would change the published runs' code.

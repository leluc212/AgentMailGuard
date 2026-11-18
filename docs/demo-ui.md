# Demo console

The console for the thesis defence (ADR-0018, `specs/design.md` §5.9, requirements R25). It starts any scenario on the running stack and shows every step, with or without AgentMailGuard, on the fake or the live model. It runs beside the Jinja2 review UI (`http://localhost:3001`), which stays as it is.

## Start it

1. In `.env`:

   ```dotenv
   DEMO__ENABLED=true
   DEMO__FAULT_MARKERS_ENABLED=true   # S10 (retry, replay)
   GUARD__ENABLED=true                # C3 runs
   MAILGUARD_L1_HOST_DIR=./evaluation/mailguard_bench/pinned   # folder with l1_injection_clf_v1.joblib
   ```

   For the live model (`gpt-4o-mini`) also set `LLM__PROVIDER=openai`, `LLM__OPENAI_API_KEY` and the price table (`docs/configuration.md` §2). With `LLM__PROVIDER=fake` the model switch is greyed out.
2. Optional, for the Benchmark screen: `uv run python scripts/export_bench_for_demo.py` (copies the committed v3 results into `services/demo_ui/public/bench/`).
3. `docker compose up -d --build`, then `make seed` (one demo mailbox per tenant). If `init` exits because RabbitMQ was not ready yet, run `docker compose up -d` again.
4. Open `http://localhost:3003`. The port is bound to `127.0.0.1` and there is no login (R25.12): do not publish it.

Development without Docker: `cd services/demo_ui && npm install && npm run dev` (the dev server proxies `/v1` to `http://localhost:8000`, or `API_URL`).

## Screens

| Screen | What it shows | Requirement |
|---|---|---|
| Live Pipeline | Inbox of the console's runs; the pipeline diagram, animated from the job timeline (polled every 500 ms until the job stops); each step's event payload, the retrieved chunks with their BM25, vector, RRF and rerank scores, tokens, cost and latency | R25.7 |
| Scenarios | S01–S14 and G01–G06 from `config/demo_scenarios.yaml`, run one or all (the burst S13 only on its own), expected vs actual with pass/fail; a free email composer with C0/C3 | R25.2 |
| Guard Inspector | For a C3 job: the decision, the four layers, the chunk trust, the isolation data, the raw report; C0 ⇄ C3 columns for a G scenario | R25.6, R25.8 |
| Review & Dispatch | Drafts awaiting review: edit, approve, reject. A demo mailbox records the reply and sends nothing | R23.4 |
| Timeline | Every event of a job; replay of a dead-lettered job | R23.5 |
| Knowledge | Upload a text document, watch its status move to `active` | R23.7 |
| Observability | Funnel and received count (this tenant), p50/p95 latency, queue depths and cost by model profile (whole stack), from `GET /v1/metrics/summary` | R25.9 |
| Benchmark | The committed 1,800-case results, read-only | R25.10 |

Top bar: tenant (the three seeded tenants), model switch (`fake` / `live`, per run), language (Tiếng Việt / English), theme (dark / light). Warnings say when the demo door is off, the guard is off, or the L1 classifier is not mounted.

## Notes

- Runs are kept in the browser (`localStorage`), per tenant.
- G06 (fail-closed judge) runs on the live model only (owner decision C13); its outcome depends on the judge's answer.
- The guard's decision is display only: dispatch mode and review do not change (ADR-0018 decision 6).
- Regenerate the API types from the running API: `npm run gen:api` (writes `src/api/schema.d.ts`).
- Tests: `npm test` (unit), `tests/e2e/test_demo_console.py` (Playwright against a running stack with `DEMO_CONSOLE_URL`).

# ADR-0014: Full-cloud route for the v2 benchmark, the guard pin it needs, and the runner's embedding

- **Status:** Accepted (owner decision 2026-10-01); this branch (`wp-cloud`) carries it out and awaits
  the owner's review before it reaches `main`
- **Date:** 2026-10-01
- **Decided by:** project owner (team meeting of 2026-10-01; instructions of 2026-10-01 ~09:20 and ~09:24)
- **Requirements:** R22.12, R14.7, R5.10, R21.4, R21.6, R24.5
- **Amends:** ADR-0012 decision 9 (the route of Qwen2.5-7B and Llama-3.1-8B, deferred there to this
  meeting) and decision 3 (the v2 guard pin, `1a3ef62`); ADR-0011 decision 1 (the embedding that every
  run shares); Amendment 2 of `docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md`, through
  its Amendment 4
- **Changes:** `specs/tasks.md` task 7.29; `docs/BENCHMARK.md`, `docs/benchmark-windows-native.md`,
  `docs/demo-runbook.md` §9.9 and §9.10, `docs/configuration.md`, `.env.example`, `README.md`

## Context

ADR-0012 decision 9 deferred one question to the team meeting of 2026-10-01: whether the teammate's
Friday run (2026-10-02) serves Qwen2.5-7B and Llama-3.1-8B locally on a 6 GB laptop GPU, or through
OpenRouter's API. The OpenRouter support existed only on two unmerged branches: `wp-r4-openrouter`
(rag-email: provider pin, provenance, route breaker, canary) and `mailguard-openrouter` (the guard's
OpenAI provider sends the same pin and reports who served each call). The guard copy in this repository
(`agentmailguard/`, a subtree at `1a3ef62`) ignores the pin, so the guard-worker refuses to build a
pinned guard (`route.guard_pin_problem`): the route cannot run without moving the guard pin.

The embedding was fixed to Google's `gemini-embedding-001` at 1536 dimensions for every run (ADR-0011
decision 1), with its key taken from `LLM__OPENAI_API_KEY`. A runner without a billed Gemini project
cannot finish a run (about 6,000 to 10,000 embedding calls per model; the last published free limit
was 1,000 a day).

## Decision

1. **Full cloud for the v2 benchmark.** `gpt-4o-mini` through the OpenAI API (`BENCH_OPENAI_API_KEY`);
   Qwen2.5-7B and Llama-3.1-8B through OpenRouter (`BENCH_OPENROUTER_API_KEY`). The teammate uses
   their own keys, read by name only. No local GPU is used. One model serves every LLM role of its run
   in both systems, as before (ADR-0011 decision 1).
2. **One pinned provider per OpenRouter model, fallbacks off, the served provider recorded.**

   | Profile | Model id | Pin (`provider.order`) | Precision filter | Price per 1M (in / out) |
   |---|---|---|---|---|
   | `qwen2.5-7b-openrouter` | `qwen/qwen-2.5-7b-instruct` | `phala` | none (Phala states `unknown`) | $0.10 / $0.20 |
   | `llama-3.1-8b-openrouter` | `meta-llama/llama-3.1-8b-instruct` | `coreweave` | `bf16` | $0.22 / $0.22 |

   Every request sends `provider: {order: [pin], allow_fallbacks: false, require_parameters: true[,
   quantizations]}` and `X-OpenRouter-Metadata: enabled`. rag-email's client refuses a call another
   provider served, a fallback attempt, or a response that names no provider
   (`LLMProviderMismatchError`); the guard's judges report their provenance and rag-email's
   `CountingProvider` turns a mismatch into a `guard_layer_error` row. OpenRouter's public listing,
   read 2026-10-01, still supports both pins: Phala is Qwen2.5-7B's only provider; CoreWeave is the
   only Llama-3.1-8B endpoint that lists structured outputs, and its only `bf16` one. The pins are not
   changed by this decision; choosing another provider is the owner's call and a new `RUN`.
3. **Results are not comparable with the v1 runs on serving.** v1 served Qwen2.5-7B and Llama-3.1-8B
   locally in 4-bit builds on Ollama; v2 serves them on another provider, another precision and
   another runtime. Every routed report says so (`## Run setup`), next to the pin and the providers
   that served the calls.
4. **The guard pin moves from `1a3ef62` to `915cb1e`** (tree `f659748a611340e093d41189dc1f445a2f842134`),
   brought into `agentmailguard/` by a subtree merge with full history (ADR-0012 decision 6). The two
   commits change the guard's transport only: the OpenAI provider sends the routing object and the
   metadata header and reports `provenance`; no layer, prompt, rule, threshold or contract changes,
   and the training code is unchanged (the L1 classifier is the same kind of model). All three Friday
   models run at the new pin, `gpt-4o-mini` included, so the guard commit is the same across models.
   v1 keeps `81df5d07`. Run folders started at `1a3ef62` (local preflight and smoke folders only;
   none is a result) cannot be resumed at the new pin.
5. **The embedding is the runner's choice.** One embedding model of the runner's choice, 1536
   dimensions, the same for every run of a comparison, recorded with each run. Any OpenAI-compatible
   `/embeddings` endpoint that returns 1536-dimension vectors serves, natively or through the
   `dimensions` parameter the embedder sends (for example OpenAI `text-embedding-3-small`, or Gemini
   `gemini-embedding-001` at 1536). The settings are `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL`,
   `EMBEDDING__API_KEY` (the embedding key, never an LLM key) and `EMBEDDING__DIMENSION=1536` (the
   `VECTOR(1536)` column; another width needs a migration, out of scope), with `EMBEDDING__MOCK=false`
   forced. The stack env, the guard-worker and the doctor refuse a missing setting by name. Each
   config's meta and fingerprint record the model, the width and the endpoint's host, so a resumed or
   retried run refuses another embedding; `manifest.json` and the report's `## Run setup` repeat it.
   No code reads several runs, so the minimal sound check across models is that each run states its
   embedding and the guides require one model for a comparison; within a run the report already
   refuses configs whose embedding differs.
6. **The meaning reader is the runner's choice too**, served by `LLM__*` given on the `bench-report`
   command line; it may not be a benchmarked model (ADR-0012 decision 7), which now also covers the
   OpenRouter ids and profile names.
7. **What a failing route does.** The live runner stops after three consecutive provider mismatches or
   HTTP 404/502/503 rows, and at once on a 402 that is not OpenRouter's transient in-flight budget; it
   exits 3, and the kit then stops the whole campaign (no next config, no retry pass) and says how to
   resume. A router error inside an HTTP 200 body is classified by its code, and the error text puts
   the status and OpenRouter's `limit_source` first, so the 200-character cut of an error row keeps
   them.
8. **Where each call's served provider is recorded.** Guarded rows carry `generation.provenance` and
   `guard_llm.provenance`. Every model call's `llm_inference` log line carries `provenance` (triage,
   summarize, generate, repair), and the kit saves the triage-worker's and the ai-worker's log lines of
   each config to `raw/services.<config>.log`: that is the record for triage, the summarizer and C0's
   generation, which reach no result row. No database column is added (a migration needs its own
   approval).

## Consequences

- The teammate's guides describe the cloud route: three keys (OpenAI, OpenRouter, the embedding's),
  OpenRouter credit with a per-key limit and privacy settings that allow both pinned providers, a
  canary before each OpenRouter run (`make bench-canary`, `make mailguard-probe`), a credit check
  before every run, and what a `STOP` means. The local Ollama route stays documented as an appendix and
  in the runbook; it is not used on Friday.
- **Single points of failure.** With fallbacks off, a Phala outage stops every Qwen2.5-7B call; there
  is no equivalent second provider. Llama-3.1-8B's other providers serve `fp8` or an unknown precision
  without structured outputs.
- **Quota.** A Tier 1 OpenAI account caps `gpt-4o-mini` at 10,000 requests a day, close to one full run
  (about 8,000 to 11,000); a free Gemini key cannot carry the embeddings of a run. The guides say so.
- **Cost.** Estimated per full run: about $1.5 to $2 for `gpt-4o-mini`, $0.9 to $1.3 for Qwen2.5-7B,
  $1.6 to $2.1 for Llama-3.1-8B, plus the embedding and the reader; the run records what the router
  reported per call (`cost_usd` in the provenance totals).
- **Time.** The two OpenRouter models were not measured; the guides plan 4 to 8 hours each, and about
  15 to 25 hours for the three models and the reader.

## Known limits, open for the owner

- **The guard's judges ask for `json_object`, not a strict JSON schema** (the guard's registry entries;
  ADR-0012 decision 9 asked for strict schema). `require_parameters` guarantees the provider supports
  `response_format`, and the guard validates each answer; changing the guard's mode would change guard
  behaviour, so it is recorded as a limit, not changed.
- **A guard judge's HTTP failure on the pinned route** (402, 404, 5xx) is the guard's visible fallback
  (`llm_fallback`), and the row stays scored, as for any model (ADR-0012 decision 4); the report counts
  fallbacks per config. Turning such rows into error rows would change what is scored, which this
  decision does not do.
- **No route-specific `error.kind`.** A provider mismatch in triage is a `triage_stage_failure` row and
  in the ai-worker a dead-lettered job; both are retried and excluded like every error row, and the
  breaker reads the text.
- **Retries.** The ai-worker retries a 404/502/503 on its ladder, so a case on a dead route waits its
  full case timeout before it becomes an error row; three such rows stop the run.

## Alternatives rejected

- **Keep the local route on the 6 GB laptop GPU.** The owner chose cloud for Friday; the numbers would
  have been slower, partly CPU-served and not comparable with the 12 GB desktop's.
- **Run the guard from a separate `AgentMailGuard-openrouter` worktree with a `MAILGUARD_COMMIT`
  override**, as the branch's runbook proposed. The teammate has one clone; the subtree is the layout
  `main` uses (ADR-0012 decision 6), and a pin the Makefile does not know cannot be verified in a
  shallow clone.
- **Let OpenRouter fall back to another provider.** The served model would vary within a run, in
  precision and in structured-output support, and nothing would show it per row.
- **Keep Gemini embeddings hard-wired.** It ties every runner to a billed Google project and to the LLM
  key's name; the vector column, not the vendor, is the real constraint.

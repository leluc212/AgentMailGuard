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
- **Amended:** decisions 9 to 15 below (owner decisions of 2026-10-01, A to G), after the first
  version of this record

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
   none is a result) cannot be resumed at the new pin. On branch `wp-cloud` the subtree merge
   `04efd43` and the merge `4ba1b81` carry the new guard tree with the old pin constants (the pin
   moves in `366d8ad`), so the pin check fails at those two commits, which matters only to
   `git bisect`. A unit test now requires `HEAD:agentmailguard` to be the pinned tree, so CI catches
   such a gap.
5. **The embedding is the runner's choice.** One embedding model of the runner's choice, 1536
   dimensions, the same for every run of a comparison, recorded with each run. Any OpenAI-compatible
   `/embeddings` endpoint serves if it accepts the `dimensions` parameter, which the embedder sends
   with every request, and returns 1536-dimension vectors (for example OpenAI `text-embedding-3-small`,
   or Gemini `gemini-embedding-001` at 1536). A model that is 1536 wide natively but rejects the
   parameter (OpenAI `text-embedding-ada-002`; OpenAI documents `dimensions` for `text-embedding-3` and
   later only) fails every call; omitting the parameter for such models would be a code change of its
   own. The settings are `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL`, `EMBEDDING__API_KEY` (the
   embedding key, never an LLM key) and `EMBEDDING__DIMENSION=1536` (the `VECTOR(1536)` column; another
   width needs a migration, out of scope), with `EMBEDDING__MOCK=false` forced. The stack env, the
   guard-worker and the doctor refuse a missing setting by name. Each config's meta and fingerprint
   record the model, the width and the endpoint's host, so a resumed or retried run refuses another
   embedding; `manifest.json` and the report's `## Run setup` repeat it. No code reads several runs, so
   the minimal sound check across models is that each run states its embedding and the guides require
   one model for a comparison; within a run the report already refuses configs whose embedding differs.
6. **The meaning reader is the runner's choice too**, served by `LLM__*` given on the `bench-report`
   command line; it may not be a benchmarked model (ADR-0012 decision 7), which now also covers the
   OpenRouter ids and profile names.
7. **What a failing route does.** The live runner stops after three consecutive provider mismatches or
   HTTP 404/502/503 rows, and at once on a 402 that is not OpenRouter's transient in-flight budget; it
   exits 3, and the kit then stops the whole campaign (no next config, no retry pass) and says how to
   resume. (Extended to every provider's used-up quota or credit by decision 10.) A router error inside an HTTP 200 body is classified by its code, and the error text puts
   the status and OpenRouter's `limit_source` first, so the 200-character cut of an error row keeps
   them.
8. **Where each call's served provider is recorded.** Guarded rows carry `generation.provenance` and
   `guard_llm.provenance`. Every model call's `llm_inference` log line carries `provenance` (triage,
   summarize, generate, repair), and the kit saves the triage-worker's and the ai-worker's log lines of
   each config to `raw/services.<config>.log`: that is the record for triage, the summarizer and C0's
   generation, which reach no result row. No database column is added (a migration needs its own
   approval).

### Owner decisions of 2026-10-01 on failures, limits and the trial (A to G)

9. **A guard route failure is a retried error row (A).** A guard LLM call (the L1 judge, the L2
   extractor, the L3b scanner, the L4 judge) that failed on the route or the service itself (HTTP
   402, 404, 408, 409, 429 or 5xx, a timeout, a connection error, a provider mismatch, or a router
   error carried in an HTTP 200 body) is not scored. rag-email's `CountingProvider` records each such
   call per case from the guard provider's chained transport exception (and the guard's own message
   when nothing is chained), without changing `agentmailguard/`; the case executor puts them in
   `guard_route_failures` (the guard-worker's audit line carries them) and the runner makes the row an
   error of kind `guard_route_failure` (a provider mismatch keeps `guard_layer_error`). The retry pass
   and a resume rerun it, and the report lists it under its errors row ("of which guard route
   failure"). A model that answers badly (no JSON, a schema mismatch, a refusal) stays the guard's
   real behaviour: a scored fallback with `metadata.llm_fallback` (ADR-0012 decision 4). It applies to
   every profile, OpenAI and Gemini included. A per-minute 429 is still raised as a rate limit, so the
   ai-worker's retry ladder (or the v1 runner's back-off) runs the case again.
10. **Quota and credit exhaustion stop every run (B).** A 429 that names a used-up quota, prepaid
    balance, spend limit or daily cap is told from a per-minute rate limit by what the providers
    document (`packages/core/provider_limits.py`, read 2026-10-01): OpenAI's error codes
    `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`,
    `organization_usage_limit_exceeded` and the type `insufficient_quota` ("Retrying billing, spend,
    or quota errors won't restore API access", https://developers.openai.com/api/docs/guides/error-codes);
    a message that names a per-day measure, RPD or TPD
    (https://developers.openai.com/api/docs/guides/rate-limits); Gemini's per-day or spend-based
    `RESOURCE_EXHAUSTED` (https://ai.google.dev/gemini-api/docs/rate-limits). rag-email's client raises
    `LLMQuotaExhaustedError` with `quota_exhausted: <what>` at the head of its text, the ai-worker
    dead-letters it, the embedder raises `EmbeddingQuotaExhaustedError` at once, and the benchmark's
    back-off never retries either. The route breaker now runs for every profile: a used-up quota or
    no credit (OpenRouter's 402) stops the run at once, whatever the provider and whether the model,
    a guard judge, triage, the embedding (a knowledge upload, or the query embedding, whose cause the
    `context_built` event now records as `retrieval_vector_error`) or the meaning reader hit it; the
    streak stops (mismatches, 404/502/503) stay for a pinned route only. The stopped rows are error
    rows; the STOP line and the kit tell the runner to rerun the same `make bench-run` later, which
    resumes and retries them. The meaning step stops the same way (`STOP meaning`, exit 3).
11. **The kit's meaning step passes `--retry-errors` (C)**, so the same `make bench-report` run again
    reads the drafts a failed read left unread. The guide says so.
12. **One embedding call before any model spend (D).** `make bench-run` embeds one fixed line
    through `packages.knowledge.embedder.get_embedder` with the runner's `EMBEDDING__*` (the shell
    over `.env`, the settings the host processes read), retries off, right after the stack env is
    rendered and before the stack is touched, and refuses to start unless one 1536-dimension vector
    comes back: an endpoint that ignores or rejects `dimensions`, a wrong key, model or URL, a rate
    limit or a used-up quota each say so. `make bench-doctor` stays free of calls; the call runs on
    the runner's machine with the runner's key, and tests use a fake transport.
13. **A trial that includes RAG cases (E).** `make bench-run ... CASE_IDS=<id>,...` (`--case-ids`)
    runs only those cases of each config, in their order, refuses an id the config does not run, and
    is refused unless the `RUN` starts with `trial`: a trial folder is never a result. The pinned case
    file and the full-run selection do not change. The guide's trial runs one LLMail attack, one
    benign email and one RAG case, so it embeds knowledge documents.
14. **A run stopped by a limit resumes after another model's run (F).** Nothing in the code
    prevented it: the stack env and the fingerprint are rendered from the profile and `.env`
    alone, and the profile always states its routing keys, so the resumed run's settings equal the
    stopped run's. Two tests prove it (the kit re-renders `.env.stack` identically; `live.run`
    accepts the resume with no mismatch and retries the stopped rows).
15. **The usage tier is not discussed (G).** The guides state what one model run needs, so the
    runner can check that their accounts cover it: about 8,000 to 11,000 model requests per run
    (estimated from the calls per case: at most one reply, the triage model when the rules and the
    classifier are unsure, the guard's calls in `C1`, `C2`, `C4`, `C5`, `C7`), about 4 to 8 million
    input and 1 to 1.5 million output tokens (the v1 `gpt-4o-mini` run measured about 640 input and
    175 output tokens per reply and about 440 and 100 per guard call), about 6,000 to 10,000 embedding
    requests per model run (567 knowledge documents in each of 9 configs, about 5,100, plus up to
    about 4,950 queries; about 1 to 1.5 million tokens), and the costs below. If a limit or a credit
    runs out anyway, the run stops cleanly and the same command resumes it later; other models may run
    in between. The Tier-1 split-run plan is withdrawn.

## Consequences

- The teammate's guides describe the cloud route: three keys (OpenAI, OpenRouter, the embedding's),
  OpenRouter credit with a per-key limit and privacy settings that allow both pinned providers, a
  canary before each OpenRouter run (`make bench-canary`, `make mailguard-probe`), a credit check
  before every run, and what a `STOP` means. The local Ollama route stays documented as an appendix and
  in the runbook; it is not used on Friday.
- **Single points of failure.** With fallbacks off, a Phala outage stops every Qwen2.5-7B call; there
  is no equivalent second provider. Llama-3.1-8B's other providers serve `fp8` or an unknown precision
  without structured outputs.
- **Quota.** One model run needs about 8,000 to 11,000 model requests and 6,000 to 10,000 embedding
  requests (decision 15); a free Gemini key cannot carry the embeddings of a run. A limit or credit
  that runs out anyway stops the run cleanly, and the same command resumes it (decisions 10, 14).
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
- **Route-specific error kinds outside the guard.** A guard call's route failure has its own kind,
  `guard_route_failure` (decision 9). A provider mismatch in triage is a `triage_stage_failure` row and
  in the ai-worker a dead-lettered job; both are retried and excluded like every error row, and the
  breaker reads the text.
- **Retries.** The ai-worker retries a 404/502/503 on its ladder, so a case on a dead route waits its
  full case timeout before it becomes an error row; three such rows stop a pinned run. An unpinned
  (OpenAI) run does not stop on a streak of 5xx rows; they are error rows the retry pass reruns.
- **A daily cap the provider does not name.** The stop reads the provider's documented codes and the
  per-day measure in the message (decision 10); a 429 that says neither is treated as a per-minute
  rate limit and retried, so it shows as error rows after the case timeout, not as a stop.
- **A canary `strict_json` failure on the pinned provider** has no rule beyond "stop and ask the
  owner" in the guide; whether such a provider may run (and be recorded) is the owner's call.

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

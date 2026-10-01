# ADR-0013: A category retrieval floor in the triage gate, and a benchmark switch for the retrieval category filter

- **Status:** Accepted (owner, 2026-10-01 00:42; ADR-0012 decision 12)
- **Date:** 2026-10-01
- **Decided by:** the owner, 2026-10-01 00:42, in ADR-0012 decision 12: both changes (A and B) are
  taken, with the two sub-decisions recorded under "Decisions" below. Built on branch `wp-rag-routing`
  (worktree `v2-rag-routing`, based on `a199848`) and merged with `v2-integration` at `f3f0ca1`; the two
  feature commits cite `[task 7.20]`, which the task list renumbered to 7.28.
- **Relates to:** R6.5, R6.6, R6.9, R6.12, R6.13, R6.14, R10.4, R12.4; `specs/design.md` §5.3 (the three
  gates), §5.4, §5.5; task 7.28; ADR-0011, ADR-0012

## Context

The live v2 smoke of 2026-09-30 and 2026-10-01 (gpt-4o-mini, real services) found that the RAG path
was never exercised for the company-policy cases, for two independent reasons:

1. **Triage skipped retrieval.** The stage-3 model answered `retrieval_required=false` for 9 of 9
   company-policy questions (warranty period, refund fee, password reset, shipping redirect, discount,
   policy), so hybrid retrieval and the reranker never ran. `config/categories.yaml` declares
   `default_retrieval_required` per category (true for support, sales, billing, administration and
   general_inquiry; false for scheduling, automated_notification, acknowledgement and no_response),
   but nothing applied those defaults to a stage's result. (The ML stage derives its flag from the same
   table already; a stage-1 rule takes its own `retrieval_required`, which defaults to its
   `reply_required`; the stage-3 answer is the model's own.)
2. **The category filter hid the documents.** The benchmark feeder uploads every case's knowledge
   documents under the case's own category (`kb_category`, a leftover of v1's fixed classification; for
   these cases `support`). `RetrievalQueryBuilder` filters by the category live triage assigns (R12.4,
   R10.4) and the SQL excludes every other category. Live triage never answered `support` for these
   emails, so even the cases routed to retrieval found 0 documents.

```
                                                  ┌─ reply_required=false ──────▶ COMPLETED   (R6.5)
stage 1/2/3 ──▶ classification ──▶ gate ──────────┼─ template hint, match ──────▶ DRAFTED     (R6.13)
  (rule/ml/llm)   retrieval_required?             └─ everything else: AI ───────▶ QUEUED ──▶ route envelope
                                                        │                                        │
                                   (A) the floor ──────▶│ retrieval_required :=                  ▼
                                                        │   stage OR category default      ai-worker ContextBuilder
                                                        ▼                                        │ retrieval_required?
                                                  event payload + raw                            ▼
                                                  say the floor raised it        RetrievalQueryBuilder
                                                                                 filters: organization, status,
                                                                                 category ◀── (B) the switch
```

## Decisions

The two changes are separate commits on the branch. Either can be taken alone, but the benchmark needs
both to exercise the RAG path: the floor makes the retrieval happen, and the switch lets it find the
documents. The owner took both.

### A. Category retrieval floor (commit "Category retrieval floor")

When a message requires a reply and is routed to AI generation, the routed `retrieval_required` is the
stage's own answer **or** the category's `default_retrieval_required` from the taxonomy
(`config/categories.yaml`), for every stage (rule, ML, LLM) and for the R6.11 safe default (which stays
`retrieval_required=true`).

- **Where: the gate's third outcome** (`services/triage_worker/gate.py`, both the in-memory and the
  persisted path, through one method). That is the only place that knows the final routing: a
  template-hinted message goes to AI only when no template matches (R6.14), and that is decided in the
  gate. Because the floor runs after the no-reply exit and the matched-template reply, **R6.5 (no reply,
  no retrieval) and R6.13 (template, no retrieval, no generation) are untouched by construction.**
- **What it records:** the gate's `QUEUED` event payload always carries `retrieval_required_from_category`
  (true when the floor raised the flag), and the routed classification's `raw` carries it when true. A
  structured `retrieval_floor_applied` log line names the category and the deciding stage, and the counter
  `retrieval_floor_applied_total{organization,category,decided_by}` counts the jobs the floor raised, after the
  `QUEUED` transition has committed (`docs/observability.md`), so the share of `rag` jobs the floor made can be
  read from `/metrics`. The benchmark's raw rows carry the same fact as `triage.retrieval_floor`.
- **What it does not rewrite:** the classification row the cascade persists (R6.7) keeps the stage's own
  answer. The benchmark's `triage.retrieval_required` therefore shows what the stage said, its
  `gate_outcome` (read from the `QUEUED` event) shows what ran, and `triage.retrieval_floor` (the `QUEUED`
  event's marker; null when the gate recorded none) says whether the floor made the difference.
- **Switch:** `TRIAGE__CATEGORY_RETRIEVAL_FLOOR` (default `true`; `false` restores each stage's own
  answer). Compose forwards it to the triage worker when set. The category defaults are per category in
  `config/categories.yaml`, so an operator changes one without code (R6.9).
- **Unknown category:** has no default, so the stage's answer stands. An alias resolves to its category.

### B. Benchmark category filter switch (commit "Benchmark category filter switch")

`RETRIEVAL__CATEGORY_FILTER_ENABLED` (default `true`, so production is unchanged) sets
`QueryBuilderConfig.category_filter_enabled` (through `QueryBuilderConfig.from_settings`) in the
ai-worker, and so in the benchmark's guard-worker, which runs the same `build_consumers`. Only the
category filter goes; the organization and status filters stay (R10.4). In the live benchmark every case
runs in a throwaway organization that holds only that case's documents, so the tenant is the isolation
boundary and the category filter adds nothing there except the mismatch.

Two places that also build a retrieval query are left as they are on purpose: `/v1/search/debug`, where a
`category` in the request is an explicit filter of the operator's that the switch would not override
(the switch governs the filter derived from the classification, R12.4), and v1's in-process host, whose
documents are filed under the case's category by construction and whose settings fingerprint stays as
it is.

The benchmark's `stack_env` writes `RETRIEVAL__CATEGORY_FILTER_ENABLED=false` for every model profile
and its host check requires the host `.env` to say the same, so C0 (the containers) and the guarded
configs (the host guard-worker) always run with the same filter. The run fingerprint records it
(`retrieval.category_filter`), in the runner's meta and in the guard-worker's, which the runner compares
key by key. The guard-worker's L3b echo check builds its own query, and it gets a builder made from the
same retrieval settings (`guard_worker.build_guarded_components`), so the benchmark has one configuration.

`stack_env` also writes `TRIAGE__CATEGORY_RETRIEVAL_FLOOR=true` for every profile: Compose forwards the
floor from `.env` and a leftover `false` there would silently take the RAG path out of the run. Compose
reads `.env.stack` after `.env`, and a shell value that differs is refused. Only the triage worker
container reads it, so there is no host check. Runbook §9.9 step 5 prints `catfilter=` and `floor=` for
every service; only the ai-worker's `catfilter` and the triage-worker's `floor` change what the run does.

## Alternatives considered

- **Change the stage-3 prompt** so the model asks for retrieval more often. Rejected: prompts are frozen
  for the benchmark, the answer would still depend on the model, and the category table already says
  what each category needs.
- **Apply the floor where the cascade finalizes the classification.** Rejected: the cascade does not
  know whether a template will match, so it would mark template-routed jobs, and the persisted row would
  no longer be the stage's own result.
- **Floor only the stage-3 answer.** Rejected: one rule for every stage is simpler, and a rule or a
  custom category can under-ask too.
- **Replace the stage's answer with the category default** (not OR). Rejected: a stage may know better
  that a category that does not retrieve needs a document; OR only ever raises.
- **(B) Upload each case's documents under the category triage will choose.** Impossible: the documents
  are uploaded and active before the email is fed, and the category is chosen later by a model that
  differs per run.
- **(B) Upload every case's documents under every category.** Multiplies embedding calls by the number
  of categories for identical text.
- **(B) Force the triage category to the case's category.** Bypasses the live triage the benchmark is
  measuring.

## Consequences

- **More jobs retrieve.** Each one pays an embedding call, a hybrid search and a rerank, and the reference
  funnel's 70/30 RAG / no-RAG split becomes a function of the category mix: with the floor on, AI
  generation without retrieval is left to the categories whose default is false (scheduling, and custom
  categories that declare it). `docs/observability.md` says so.
- **Possible extra escalations.** The complexity router escalates when retrieval was required and too
  few relevant chunks came back (R15.3, capped at one per job, R15.5). A job newly marked as requiring
  retrieval, whose knowledge base holds nothing relevant, now escalates where it used to draft without
  looking.
- **Rules.** No shipped rule in `config/triage_rules.yaml` sets `retrieval_required: false` on a message
  that needs a reply (every such rule also has `reply_required: false`, so it exits before the floor; a unit
  test holds this), so the floor changes nothing for them. A rule author who does write it for a
  category that retrieves by default is overruled; the category's default is the place to change that.
- **Four existing tests** used a default-retrieving category for their "no retrieval" case (three unit
  tests and one integration test, `tests/integration/test_funnel_metrics_integration.py`, which could not be
  run without containers and is still unrun: run it with `make test-integration` when the stack is free). They now use `scheduling`,
  the replying category whose default is not to retrieve; their assertions are unchanged.
- **Benchmark runs.** A run started before the switch has no `retrieval.category_filter` in its
  fingerprint, so a resume across the change is refused, as intended.
- **Not changed:** prompts, guard code, thresholds, cases, and anything under `evaluation/results`.

## Decisions the owner took (2026-10-01 00:42, ADR-0012 decision 12)

- **A and B, both.** Accepted. (If one is later withdrawn, edit this file: its status becomes Rejected.)
- **An explicit `retrieval_required: false` in a rule may be overruled by the floor:** yes, one rule for every
  stage. The shipped rules are unaffected, and a unit test holds that: no rule in `config/triage_rules.yaml`
  has `reply_required: true` with `retrieval_required: false`, so a rule edit that would be overruled fails
  the test instead of being overruled unnoticed.
- **A category the taxonomy does not know keeps the stage's answer**, not retrieval.

## Rollout and rollback

Both are configuration: `TRIAGE__CATEGORY_RETRIEVAL_FLOOR=false` restores the previous routing
(restart the triage worker), and `RETRIEVAL__CATEGORY_FILTER_ENABLED=true`, the default, keeps the
category filter that production has today. Neither changes the schema, the queues or the job states.
Nothing in this branch was run against live services: the owner's live smoke is the check. After the
floor, a job that used to skip retrieval shows `retrieval_required_from_category: true` on its `QUEUED`
event and a non-empty `retrieved` list on its `context_built` event when the knowledge base holds an
answer.

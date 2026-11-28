# ADR-0021: Soft category scoping for retrieval, and one meaning for a document's category

- **Status:** Accepted (owner, 2026-11-28)
- **Date:** 2026-11-28
- **Decided by:** the owner, 2026-11-28: approach E (soft scoping); the phase 3 acceptance gate below;
  the duplicated demo documents are deleted; the live judge schema fix runs in parallel.
- **Relates to:** R6.4, R9.5, R10.2, R10.3, R10.4, R10.8, R10.9, R10.10, R12.4, R21.4, R23.7;
  `specs/design.md` §5.5; task 9.20 (supersedes 7.18); ADR-0001, ADR-0005, ADR-0013

## Context

Every live demo job that retrieved so far got **zero chunks** (2026-11-18 to 2026-11-28, tenant
Acme: S03, S08, S10, G03, G06 and others; `retrieved_chunks_count = 0` on every live
`CONTEXT_READY` event). The cause is one word used for two things:

| | Email category | Document category |
|---|---|---|
| Meaning | the lane a message is routed to (R6.4, 9 values) | what kind of document it is |
| Set by | triage, when the email arrives | the uploader, before any email exists |
| Validated | yes, against the taxonomy | no: free text; the upload API's own description suggests `policy` |
| In the data | 9 lanes | seed: `billing_procedure`, `catalog`, `fulfillment`, `policy`, `runbook`; demo: `support` |

Retrieval equates them: both SQL branches filter `d.category = <triage category>` (R12.4, R10.4;
`packages/retrieval/postgres.py`). So:

- No lane is called `policy`, `fulfillment` or `catalog`: the seeded knowledge is unreachable from
  every lane (recorded as task 7.18 on 2026-09-28, never done).
- Company-policy questions (return window, warranty, shipping, opening hours) have no lane of their
  own. Live triage (gpt-4o-mini) files them under `general_inquiry`, and could as well file a refund
  question under `billing` or a discount question under `sales`. A document filed under one lane is
  filtered out before ranking whenever triage picks another.
- The fake triage returns `support` for every email (`packages/llm/fake.py`), which is why the demo
  documents, uploaded as `support`, appeared to work offline.

ADR-0013 met the same mismatch in the benchmark and switched the category filter off there only
(`RETRIEVAL__CATEGORY_FILTER_ENABLED=false`). Production and the demo keep the hard filter.

### External evidence (checked 2026-11-28)

- *When More Documents Hurt RAG* (arXiv 2606.11350, 2026, preprint; 200 human-validated queries, five
  LLM backbones, six corpora): hard partitioning, which commits each query to one scope, lowered answer
  correctness by up to 21.8 points where the routing signal was weak; soft scoping, which fuses a scoped
  list with an unscoped global list by reciprocal-rank fusion, never significantly lowered correctness
  and helped where the scope structure is real. Because the global list always takes part, its worst
  case is unscoped retrieval. The authors keep hard scoping for corpora with a reliable routing signal.
- A metadata filter excludes a document before similarity ranking; raising `k` cannot recover it
  (OneUptime, "How to debug empty SelfQueryRetriever results", 2026-11). This is the G03 symptom.
- Access boundaries are hard filters (AWS, "Secure multi-tenant RAG with Amazon Bedrock and Verified
  Permissions"); relevance preferences are boosts, e.g. the tag function of Azure AI Search scoring
  profiles raises the score of matching documents without removing the others.
- pgvector applies a `WHERE` after the HNSW walk; from 0.8, `hnsw.iterative_scan` keeps scanning
  until the filtered `LIMIT` is met (pgvector README). A global list also bounds the under-fill of
  the category-filtered vector branch (R10.10).

Limits of this evidence: the paper is a 2026 preprint, measured mostly on 7–8B models and on
non-email corpora. It sets the direction; the decision on the default is taken by measurement on this
system (phase 3 below).

Our routing signal is weak in exactly the paper's sense: triage classifies the **email** into a lane,
not the **document** a question needs.

## Decision

### 1. A document's category is a lane or nothing

`knowledge_document.category` is either one of the taxonomy's categories (aliases resolved,
`packages/domain/taxonomy.py`) or `NULL`. `NULL` means *shared*: the document belongs to no lane. The
upload API refuses any other value with 422. A versioned data migration maps the existing values:
`billing_procedure` → `billing`, `catalog` → `sales`, `runbook` → `support`, `fulfillment` and
`policy` → `NULL`; its down step restores them. The seed fixtures follow.

### 2. Soft category scoping in the hybrid retriever

`RETRIEVAL__CATEGORY_SCOPE` takes `hard`, `soft` or `off`:

| Mode | Lists fused by RRF | Equals |
|---|---|---|
| `hard` | lexical and vector, both filtered by the lane | today's `RETRIEVAL__CATEGORY_FILTER_ENABLED=true` |
| `soft` | lexical and vector filtered by the lane, **plus** lexical and vector over the whole tenant | new |
| `off` | lexical and vector over the whole tenant | today's `RETRIEVAL__CATEGORY_FILTER_ENABLED=false` (benchmark) |

- The organization and status filters apply to every list, inside the SQL (R10.4). Tenant isolation is
  unchanged.
- The lane filter still comes from the classification (R12.4) and is applied inside the branch that
  uses it, never after fusion (R10.4).
- A lane document appears in both the lane list and the tenant list, so RRF ranks it above an equally
  close document of another lane: the boost needs no weight to calibrate (ADR-0005).
- One query embedding serves both vector lists; no LLM or embedding call is added. The reranker and
  the top-K passed to generation (R11.3) are unchanged: only the candidate pool before reranking grows.
- Every candidate records which list found it (`lane`, `tenant` or both) next to its ranks (R10.8),
  in the job's retrieval payload and in `/v1/search/debug`.
- `RETRIEVAL__CATEGORY_FILTER_ENABLED` keeps working when `RETRIEVAL__CATEGORY_SCOPE` is unset
  (`true` → `hard`, `false` → `off`), so the benchmark's stack env and fingerprint stay valid.

### 3. The default is chosen by measurement (gate)

The default stays `hard` until an evaluation of about 30 queries, each with its answer-bearing
document and the category live triage gave it, has run all three modes. `soft` becomes the default
only if:

- its Recall@5 of the answer-bearing document is ≥ that of `off` and ≥ that of `hard`;
- its zero-result rate is 0 for queries whose tenant holds a relevant document;
- its retrieval p95 stays inside the branch budget (`RETRIEVAL__RETRIEVAL_TIMEOUT_MS`, 3,000 ms, R10.9).

Otherwise the demo runs `off` and this ADR records why. The results are committed with the git SHA and
the embedding model.

## Alternatives considered

- **A. Fix the data only, one lane per document.** Hard partitioning: a policy document asked about
  from another lane is still lost. This is what happened to G03.
- **B. Several lanes per document (`categories text[]`).** Still hard partitioning; the uploader must
  predict every lane a future question may be routed to. ADR-0013 rejected the equivalent "upload
  under every category" for the benchmark.
- **C. Shared documents only (`d.category = :lane OR d.category IS NULL`).** Half soft: shared
  documents survive a mis-route, lane documents do not.
- **D. Switch the category filter off everywhere.** Safe, but gives up the precision a real lane
  structure provides; the paper finds soft scoping at least as correct as unscoped retrieval.
- **A second LLM call to pick the document scope.** Adds cost and latency per job and contradicts
  R12.5; the lane already exists.

## Consequences

- In `soft` mode each retrieval runs four SQL queries instead of two, concurrently, inside the same
  branch budget. Latency is measured by the gate.
- Shared documents compete with lane documents in the tenant lists; RRF and the reranker order them.
- Seeded and demo knowledge becomes reachable; the evaluation tasks 7.15–7.17 then run on reachable
  knowledge.
- A new metric `retrieval_scope_source_total{organization,source}` shows how often the tenant list
  supplies a chunk the lane list missed: the rate of mis-routes that soft scoping recovered.
- Not changed: prompts, triage, the guard, the benchmark's configuration and results, tenant isolation.

## Rollout and rollback

Configuration only, apart from the document-category migration: `RETRIEVAL__CATEGORY_SCOPE=hard`
restores today's behaviour; the migration's down step restores the old category values. The order of
work, and what the owner checks at each step, is task 9.20 in `specs/tasks.md`.

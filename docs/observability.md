# Observability & Funnel Instrumentation Reference

**Spec Alignment:** `specs/requirements.md §R6.10, §R6.15, §R21.4, §NFR14` · `specs/design.md §5.3, §10`

The system provides comprehensive Prometheus metrics covering end-to-end processing, latency histograms, token costs, and strict triage funnel accounting without a residual bucket.

---

## 1. Triage Funnel Architecture

Every incoming email triaged by `triage_worker` is accounted for across exactly three mutually exclusive outcomes, guaranteeing zero residual unclassified traffic:

```
                    Inbound Email (100,000 / day)
                                 │
                         triage_worker
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
            ▼                    ▼                    ▼
       Early Exit         Template Reply        AI Generation
     (≈45,000 / 45%)      (≈20,000 / 20%)      (≈35,000 / 35%)
            │                    │                    │
            │                    │            ┌───────┴───────┐
            ▼                    ▼            ▼               ▼
      emails_early_      emails_templated_   PROCEED_RAG   PROCEED_NO_RAG
      exit_total         total              (≈24,500/70%) (≈10,500/30%)
            │                    │            │               │
            └────────────────────┼────────────┴───────────────┘
                                 ▼
                  triage_funnel_outcomes_total
                  [Residual = 0, Exact 100%]
```

### 1.1 Funnel Outcomes

1. **Early Exit (`early_exit`, ~45%)**:
   - Condition: `reply_required == false` or `workflow_hint == 'none'`.
   - Action: Job transitions immediately to `COMPLETED`.
   - Cost: Zero embeddings, zero retrieval, zero LLM generation (R6.5).
2. **Template Reply (`template`, ~20%)**:
   - Condition: `workflow_hint == 'template'` with approved template matching `(category, intent)`.
   - Action: Deterministic template rendered; Job transitions to `DRAFTED`.
   - Cost: Zero embeddings, zero retrieval, zero LLM generation (R6.12, R6.13).
3. **AI Generation (`ai_generation`, ~35%)**:
   - Condition: `workflow_hint == 'ai'` (or template missing fallback per R6.14).
   - Sub-dimension `rag_mode`:
     - `rag` (~70% of AI = ~24,500/day): `retrieval_required == true`, executes hybrid RAG.
     - `no_rag` (~30% of AI = ~10,500/day): `retrieval_required == false`, utilizes thread and business context only.
     - `retrieval_required` here is the value the gate routed with. With the category retrieval floor (`TRIAGE__CATEGORY_RETRIEVAL_FLOOR`, ADR-0013, proposed) a stage's `false` for a category that retrieves by default counts as `rag`, so the measured split follows the category mix rather than the ~70/30 planning figure.

### 1.2 Mathematical Reconciliation Formula (R6.15)

$$\text{total\_triaged} = \text{early\_exit} + \text{template} + \text{ai\_generation}$$
$$\text{residual} = \text{total\_triaged} - (\text{early\_exit} + \text{template} + \text{ai\_generation}) = 0$$
$$\text{rag\_share} = \frac{\text{ai\_generation}\{\text{rag\_mode} = \text{"rag"}\}}{\text{ai\_generation}} \approx 70\%$$

---

## 2. Prometheus Metric Instruments

### 2.1 Funnel Accounting Counters

| Metric | Type | Labels | Description |
|---|---|---|---|
| `triage_funnel_outcomes_total` | Counter | `organization, category, outcome, rag_mode` | Primary accounting counter across `early_exit`, `template`, and `ai_generation`. |
| `emails_early_exit_total` | Counter | `organization, category, reason` | Emails completed at early exit gate. |
| `emails_templated_total` | Counter | `organization, template_id` | Deterministic template replies drafted without LLM. |
| `emails_generated_total` | Counter | `organization, category, model_tier` | Persisted AI drafts; counted once per job, never on redelivery (R21.4). |
| `emails_classified_total` | Counter | `organization, category, priority, decided_by` | Total classifications executed by triage cascade. |
| `citations_verified_total` | Counter | `category` | Drafts whose citations were checked against the supplied context (R16.5). |
| `citation_mismatches_total` | Counter | `category` | Drafts citing at least one chunk absent from the context (R16.5). |

#### Hallucinated-citation rate (R16.5)

`citation_mismatch` is exported as two monotonic counters rather than a gauge, so the rate is
computed at query time and no worker has to hold accumulator state:

```promql
sum(rate(citation_mismatches_total[5m])) / sum(rate(citations_verified_total[5m]))
```

Per category, for the retrieval-quality dashboard panel (task 7.4):

```promql
sum by (category) (rate(citation_mismatches_total[5m]))
  / sum by (category) (rate(citations_verified_total[5m]))
```

A draft is counted in `citations_verified_total` whenever a schema-valid draft was produced,
including one that cited nothing — so the denominator is "drafts that could have cited" and the
ratio is the share of drafts containing at least one ungrounded citation.

### 2.2 Operational & Cost Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `classification_latency_ms` | Histogram | `stage` | Triage latency per stage (`rule`, `ml`, `llm`, `default`). |
| `input_tokens_total` | Counter | `model, tier` | Cumulative LLM prompt tokens consumed. |
| `output_tokens_total` | Counter | `model, tier` | Cumulative LLM completion tokens generated. |
| `estimated_ai_cost_total` | Counter | `model, tier` | Monotonic cumulative AI cost in USD. |
| `failed_jobs_total` | Counter | `queue, job_type, error_type` | Terminal job failures routed to dead-letter queue. |
| `llm_context_tokens` | Histogram | `kind`, `tier` | Final assembled context size per inference request, counted before the call (R11.7). |
| `generated_draft_cost_total` | Counter | `category`, `model_tier` | Estimated USD cost of persisted AI drafts; unpriced models add nothing (R21.6). |
| `business_lookups_total` | Counter | `entity`, `status` | One increment per business fact the business step produced: `entity` is `order`, `ticket` or `invoice`; `status` is `FOUND`, `NOT_FOUND`, `NOT_LOOKED_UP` or `UNAVAILABLE`. Customer resolution is not counted here (R13.6, R13.7). |
| `business_lookup_latency_ms` | Histogram | — | Duration of one bounded business-data provider call, timeouts included (R13.7). |
| `draft_decisions_total` | Counter | `decision`, `category` | One increment per draft's first reviewer decision: `accepted` (approved unchanged), `edited` (approved after edits) or `rejected`. A repeated approve or reject does not count again; `category` is the email's latest triage category, `unknown` if none (R16.7, R21.4, SC3). |

#### Generation cost, context size and latency (R11.7, R21.4–R21.6, NFR8)

Every model request (triage, summarize, generate, repair) passes through one helper (triage is wired today; summarize, generate and repair are wired in the ai-worker composition, task 4.13),
`packages/llm/inference_metrics.py::record_inference`. It observes `llm_context_tokens`,
adds provider-reported tokens to `input_tokens_total` / `output_tokens_total`, adds priced
cost to `estimated_ai_cost_total`, and writes one JSON log line `llm_inference` whose
`fields` carry `kind, tier, model, context_tokens, input_tokens, output_tokens, latency_ms,
outcome, estimated_cost_usd`. The line also carries the correlation ids (`job_id`, `trace_id`),
which is how context length is joined to a draft's feedback (quality) per request.

```promql
# Cost per generated email over a day (SC9): all AI cost / generated emails
sum(increase(estimated_ai_cost_total[1d])) / sum(increase(emails_generated_total[1d]))

# Draft cost per category per day (R21.6)
sum by (category) (increase(generated_draft_cost_total[1d]))

# p95 context size by call kind (R11.7)
histogram_quantile(0.95, sum by (le, kind) (rate(llm_context_tokens_bucket[5m])))

# p95 generation latency against the NFR8 target of 1-5 s
histogram_quantile(0.95, sum by (le) (rate(generation_latency_ms_bucket[5m]))) > 5000
```

Per-email cost is the `generated_draft.cost_estimate` column; `NULL` means the model had no
price in `LLM__PRICE_TABLE`, and such drafts are excluded from cost sums rather than counted as free.

#### Business data lookups (R13.7, R21.3)

`packages/business/fetch.py::fetch_business_context` wraps the one provider call per job. A
non-empty plan opens a `business.fetch` span (attributes `organization_id`, `planned_refs`,
`snapshot`, `timeout_ms`, `outcome` = `ok`/`timeout`/`error`, `customer_status`,
`business_data_degraded`; status ERROR on timeout or error), increments
`business_lookups_total` once per fact and observes `business_lookup_latency_ms`. Every job
writes exactly one JSON log line `business_fetch`, also when nothing was planned
(`outcome=not_planned`, no span, no metrics). Its `fields` carry `planned, refs, snapshot,
customer_status, facts[{entity, reference, status, reason}], business_data_degraded, outcome,
latency_ms`, and the line carries `trace_id`, `job_id` and `organization_id`. The sender
address and provider error text are never logged. The same plan, statuses and
`business_data_degraded` are stored on the job's `CONTEXT_READY` event for replay.

```promql
# Share of business facts that degraded to UNAVAILABLE (timeouts and errors, R13.7)
sum(rate(business_lookups_total{status="UNAVAILABLE"}[5m])) / sum(rate(business_lookups_total[5m]))

# p95 lookup latency against the 500 ms default deadline
histogram_quantile(0.95, sum by (le) (rate(business_lookup_latency_ms_bucket[5m])))
```

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

---

## 3. PromQL Queries for Grafana Funnel Dashboard (R21.7)

### 3.1 Funnel Outcome Distribution (Percentages)

```promql
# Early Exit Rate (Target: ~45%)
sum(rate(triage_funnel_outcomes_total{outcome="early_exit"}[5m]))
/
sum(rate(triage_funnel_outcomes_total[5m])) * 100

# Template Reply Rate (Target: ~20%)
sum(rate(triage_funnel_outcomes_total{outcome="template"}[5m]))
/
sum(rate(triage_funnel_outcomes_total[5m])) * 100

# AI Generation Rate (Target: ~35%)
sum(rate(triage_funnel_outcomes_total{outcome="ai_generation"}[5m]))
/
sum(rate(triage_funnel_outcomes_total[5m])) * 100
```

### 3.2 RAG Share of AI Traffic (Target: ~70%)

```promql
sum(rate(triage_funnel_outcomes_total{outcome="ai_generation", rag_mode="rag"}[5m]))
/
sum(rate(triage_funnel_outcomes_total{outcome="ai_generation"}[5m])) * 100
```

### 3.3 Zero-Residual Invariant Alert

```promql
# Alert triggers if residual is non-zero
(
  sum(rate(triage_funnel_outcomes_total[5m]))
  - (
      sum(rate(triage_funnel_outcomes_total{outcome="early_exit"}[5m]))
      + sum(rate(triage_funnel_outcomes_total{outcome="template"}[5m]))
      + sum(rate(triage_funnel_outcomes_total{outcome="ai_generation"}[5m]))
    )
) != 0
```

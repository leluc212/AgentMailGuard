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

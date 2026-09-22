# Implementation Plan — Phase 2, Task 2.9: Funnel Instrumentation

Instruments the triage pipeline and early-exit gate with Prometheus counters to verify the realized email processing funnel against the architectural 45% (early exit) / 20% (template reply) / 35% (AI generation) assumption without a residual bucket, tracks the ~70% RAG share of AI traffic, and exports `emails_templated_total` alongside `emails_generated_total` per `specs/tasks.md` Task 2.9, `specs/requirements.md` (`R6.10, R6.15, R21.4, NFR14`), `specs/design.md §5.3, §10`, and the Technical Proposal.

---

## User Review Required

> [!IMPORTANT]
> **Zero Residual Guarantee (R6.15)**: Every triaged email must map to exactly one of the three primary outcomes:
> 1. `early_exit` (`reply_required == false` or `workflow_hint == 'none'`, ~45%)
> 2. `template` (`workflow_hint == 'template'` with matched template, ~20%)
> 3. `ai_generation` (`workflow_hint == 'ai'`, ~35%)
>
> Within `ai_generation`, the sub-dimension `rag_mode` (`rag` vs `no_rag`) tracks the ~70% RAG share of AI traffic. The funnel reconciliation formula guarantees:
> $$\text{early\_exit} + \text{template} + \text{ai\_generation} = \text{total\_triaged} \quad (\text{residual} = 0)$$

---

## Open Questions

None. The specifications (`R6.10`, `R6.15`, `R21.4`, `NFR14`, and `design.md §10`) are exhaustive and provide explicit contract definitions.

---

## Proposed Changes

### Observability Layer (`packages/observability`)

#### [MODIFY] [metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/observability/metrics.py)
- Add `emails_early_exit_total` Counter with labels `["organization", "category", "reason"]`.
- Add `triage_funnel_outcomes_total` Counter with labels `["organization", "category", "outcome", "rag_mode"]`.
- Ensure `emails_templated_total` (`["organization", "template_id"]`) and `emails_generated_total` (`["organization", "model_tier"]`) remain registered and exported on the `/metrics` endpoint.

#### [NEW] [funnel.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/observability/funnel.py)
- Define `FunnelOutcome` enum (`EARLY_EXIT`, `TEMPLATE`, `AI_GENERATION`) and `RAGMode` enum (`NONE`, `RAG`, `NO_RAG`).
- Define `FunnelReport` frozen dataclass capturing raw counts, proportions, RAG share, and boolean `is_reconciled`.
- Implement `compute_funnel_reconciliation(metrics: PipelineMetrics, organization: str | None = None) -> FunnelReport` that aggregates `triage_funnel_outcomes_total` samples from Prometheus registry and verifies zero residual.
- Implement helper `record_funnel_outcome(...)` for standardized metric emission.

#### [MODIFY] [__init__.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/observability/__init__.py)
- Export `FunnelReport`, `FunnelOutcome`, `RAGMode`, and `compute_funnel_reconciliation`.

---

### Triage Worker (`services/triage_worker`)

#### [MODIFY] [gate.py](file:///home/ple/Documents/antigravity/dazzling-bose/services/triage_worker/gate.py)
- Inject optional `metrics: PipelineMetrics | None = None` into `EarlyExitGate.__init__` (defaulting to `get_metrics()`).
- In `EarlyExitGate.evaluate_decision`:
  - On `GateAction.EARLY_EXIT`: Increment `triage_funnel_outcomes_total(outcome="early_exit", rag_mode="none")` and `emails_early_exit_total(reason=...)`.
  - On `GateAction.TEMPLATE_REPLY`: Increment `triage_funnel_outcomes_total(outcome="template", rag_mode="none")` and `emails_templated_total(template_id=...)`.
  - On `GateAction.PROCEED_RAG`: Increment `triage_funnel_outcomes_total(outcome="ai_generation", rag_mode="rag")`.
  - On `GateAction.PROCEED_NO_RAG`: Increment `triage_funnel_outcomes_total(outcome="ai_generation", rag_mode="no_rag")`.

#### [MODIFY] [cascade.py](file:///home/ple/Documents/antigravity/dazzling-bose/services/triage_worker/cascade.py)
- Inject optional `metrics: PipelineMetrics | None = None` into `CascadingTriageEngine.__init__`.
- Pass `metrics` down to `EarlyExitGate`.
- In `CascadingTriageEngine.triage`:
  - Increment `emails_classified_total` with `organization`, `category`, `priority`, `decided_by`.
  - Record `classification_latency_ms` with `stage=decided_stage`.

---

### Tests & Verification (`tests/`)

#### [NEW] [test_funnel_metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_funnel_metrics.py)
- Unit tests validating:
  1. `PipelineMetrics` registers all required funnel instruments on isolated registry.
  2. `EarlyExitGate` emits correct labels for each of the 4 gate actions (`EARLY_EXIT`, `TEMPLATE_REPLY`, `PROCEED_RAG`, `PROCEED_NO_RAG`).
  3. `compute_funnel_reconciliation` mathematical reconciliation on reference 100k email distribution:
     - 45,000 Early Exit (45.0%)
     - 20,000 Template Reply (20.0%)
     - 35,000 AI Generation (35.0%) with 24,500 RAG (70.0% of AI) and 10,500 No-RAG (30.0% of AI).
     - Asserts `residual == 0` and `is_reconciled == True`.
  4. Multi-tenant filtering isolates counts by `organization`.
  5. Zero-count edge cases and invalid state handling.

#### [NEW] [test_funnel_metrics_integration.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/integration/test_funnel_metrics_integration.py)
- Integration test executing `triage_and_gate` end-to-end against PostgreSQL test DB:
  1. Process messages resulting in early exit, template reply, and AI generation (with and without RAG).
  2. Scrape Prometheus exposition payload via `generate_metrics_payload()`.
  3. Verify presence and formatting of:
     - `triage_funnel_outcomes_total`
     - `emails_early_exit_total`
     - `emails_templated_total`
     - `emails_generated_total`
     - `emails_classified_total`
  4. Verify Prometheus text format matches OpenMetrics / Prometheus specs.

---

### Documentation (`docs/`)

#### [NEW] [observability.md](file:///home/ple/Documents/antigravity/dazzling-bose/docs/observability.md)
- Document all Prometheus metrics names, types, and label dimensions.
- Document funnel outcome reconciliation formulas, PromQL queries for Grafana Funnel Dashboard (`R21.7`), and alerting thresholds.

---

## Verification Plan

### Automated Tests
```bash
# 1. Run unit tests for funnel metrics
.venv/bin/pytest -v tests/unit/test_funnel_metrics.py

# 2. Run existing observability unit tests
.venv/bin/pytest -v tests/unit/test_observability_metrics.py

# 3. Run integration tests for funnel metrics and observability
.venv/bin/pytest -v tests/integration/test_funnel_metrics_integration.py tests/integration/test_observability_e2e.py

# 4. Run linting and type checks
.venv/bin/ruff check .
.venv/bin/mypy packages services
```

### Manual Verification
- Verify Prometheus exposition output format against standard scraping expectations.

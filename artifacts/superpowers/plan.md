# Implementation Plan: Task 3.14 Retrieval Latency Metrics

## Goal
Implement and verify **Task 3.14: Retrieval latency metrics**, fulfilling requirements **R11.6, R21.4, R21.5, NFR5, NFR6**:
- Record `retrieval_latency_ms` and `rerank_latency_ms` separately as Prometheus histograms (R11.6, R21.4, R21.5).
- Ensure latency bucket distributions cleanly bracket project SLO targets:
  - NFR5: Hybrid retrieval target (50–250 ms).
  - NFR6: Reranking target (20–200 ms).
- Ensure comprehensive observation across all execution paths: normal hybrid execution, degraded single-branch execution, full branch failure, rerank success, rerank timeout, and rerank error/fallback.
- Provide helper instrumentation functions for safe, uniform latency recording.

---

## Architecture & Data Flow

```
HybridRetriever.retrieve()                 RerankService.rerank()
         │                                          │
         ├────────────────────────┐                 ├────────────────────────┐
         ▼                        ▼                 ▼                        ▼
  lexical + vector         total_latency_ms   CrossEncoder / Stub       elapsed_ms
         │                        │                 │                        │
         ▼                        ▼                 ▼                        ▼
  RetrievalResult           Prometheus       RerankResult              Prometheus
                     retrieval_latency_ms                         rerank_latency_ms
                     (labels: mode)                               (separate histogram)
```

---

## Assumptions
1. `PipelineMetrics` (`packages/observability/metrics.py`) manages the authoritative Prometheus registry instruments.
2. `retrieval_latency_ms` and `rerank_latency_ms` are distinct histograms, enabling independent p50/p95/p99 latency calculations without conflating database retrieval time with model inference time (R11.6).
3. Metric emission never blocks or crashes pipeline execution (guaranteed via `contextlib.suppress(Exception)`).
4. `retrieval_latency_ms` tracks `mode` (`hybrid`, `degraded`, `failed`), while `rerank_latency_ms` captures overall rerank stage duration.

---

## Plan

### Step 1: Enhance Latency Recording across All Paths
- **Files:** `packages/retrieval/retriever.py`, `packages/retrieval/rerank.py`, `packages/observability/metrics.py`
- **Changes:**
  - In `packages/observability/metrics.py`:
    - Add helper functions: `record_retrieval_latency(metrics: PipelineMetrics, latency_ms: float, mode: str)` and `record_rerank_latency(metrics: PipelineMetrics, latency_ms: float)`.
    - Verify `RETRIEVAL_BUCKETS` (10, 25, 50, 100, 250, 500, 1000, 2500, 5000) and `RERANK_BUCKETS` (10, 25, 50, 100, 250, 500, 1000, 2500) accurately encompass NFR5 and NFR6 ranges.
  - In `packages/retrieval/retriever.py`:
    - Ensure `retrieval_latency_ms` with `mode="failed"` is observed even when `raise_on_both_failed=True`.
  - In `packages/retrieval/rerank.py`:
    - Ensure `rerank_latency_ms` is observed on timeout and exception fallback paths so latency is not missing when operations degrade.
- **Verify:** `uv run ruff check packages/ && uv run mypy packages/observability/ packages/retrieval/`

### Step 2: Implement Comprehensive Unit & Latency Metrics Tests
- **Files:** `tests/unit/test_retrieval_metrics.py`
- **Changes:**
  - Verify `retrieval_latency_ms` and `rerank_latency_ms` exist as distinct instruments in Prometheus registry.
  - Test independent recording:
    - Running `HybridRetriever` increments only `retrieval_latency_ms` samples.
    - Running `RerankService` increments only `rerank_latency_ms` samples.
    - Running both updates both independently with their respective durations (proving R11.6).
  - Test degraded modes:
    - Single-branch failure records `retrieval_latency_ms{mode="degraded"}` and `retrieval_degraded_total`.
    - Dual-branch failure with exception records `retrieval_latency_ms{mode="failed"}`.
    - Reranker timeout records `rerank_latency_ms` and `rerank_fallback_total{reason="timeout"}`.
    - Reranker error records `rerank_latency_ms` and `rerank_fallback_total{reason="error"}`.
  - Test NFR bucket conformity (50ms, 100ms, 250ms, 500ms) and p50/p95 quantile computability.
- **Verify:** `uv run pytest tests/unit/test_retrieval_metrics.py --cov=packages.observability.metrics --cov-report=term-missing -v`

### Step 3: Run Full Regression & Architecture Validation
- **Files:** N/A
- **Changes:**
  - Run architectural boundary checks: `uv run pytest tests/unit/test_dependency_rules.py -v`.
  - Run full regression suite: `uv run pytest -m "not slow" -q`.
  - Run static analysis: `uv run ruff check packages/ tests/ && uv run mypy packages/ tests/unit/test_retrieval_metrics.py`.
- **Verify:** All tests pass with zero regressions.

### Step 4: Update Task Queue & Git Commit
- **Files:** `specs/tasks.md`
- **Changes:**
  - Mark Task 3.14 `[x]` in `specs/tasks.md`.
  - Git commit: `feat(observability): retrieval and rerank latency histograms [task 3.14] [R11.6, R21.4, R21.5, NFR5, NFR6]`.
- **Verify:** `git log -1 --stat`

---

## Risks & Mitigations
- **Risk:** High-cardinality label explosion on latency histograms.
  - *Mitigation:* `retrieval_latency_ms` uses low-cardinality `mode` (`"hybrid"`, `"degraded"`, `"failed"`), and `rerank_latency_ms` has zero extraneous labels. Tenant scoping is tracked on error/degradation counters instead of multi-bucket histograms.
- **Risk:** Metric recording exceptions causing pipeline failures.
  - *Mitigation:* All metric observations are guarded with `with contextlib.suppress(Exception):`.

---

## Rollback Plan
If issues arise, modifications to `retriever.py`, `rerank.py`, and `metrics.py` can be reverted via `git checkout`.

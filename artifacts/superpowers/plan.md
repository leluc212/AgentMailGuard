# Implementation Plan — Phase 3, Task 3.10: Concurrent Branch Execution & Degradation

### Goal
Implement concurrent lexical and vector branch execution with per-branch timeout management, surviving branch degradation (`retrieval_degraded=true`), Prometheus metrics emission, and RRF result fusion in `packages/retrieval/retriever.py`, fulfilling requirements **R10.5, R10.6, R10.9**.

### Assumptions
- `HybridRetriever` wraps any `SearchBackend` implementation conforming to `packages/retrieval/protocol.py`.
- Concurrent branch execution uses `asyncio.create_task` and `asyncio.wait_for`.
- Degraded operations set `retrieval_degraded=True` and record metrics on `PipelineMetrics`.

### Plan
1. Register Observability Metric
   - Files: `packages/observability/metrics.py`
   - Change: Add `retrieval_degraded_total` Counter with `tenant` and `failed_branch` labels to `PipelineMetrics`.
   - Verify: `uv run pytest tests/unit/test_observability_metrics.py -v`

2. Implement `HybridRetriever` and `RetrievalResult`
   - Files: `packages/retrieval/retriever.py`, `packages/retrieval/__init__.py`
   - Change: Implement concurrent branch execution with `asyncio.wait_for`, per-branch timers, exception isolation, surviving branch fallback via `fuse_lexical_and_vector`, and Prometheus metrics emission.
   - Verify: `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/`

3. Implement Unit Test Suite for Concurrency and Degradation
   - Files: `tests/unit/test_retriever.py`
   - Change: Test true concurrency timing, normal fusion, lexical timeout degradation, vector timeout degradation, branch exceptions, double branch failure, task cancellation, and metrics.
   - Verify: `uv run pytest tests/unit/test_retriever.py -v`

4. Verify Architecture Rules & Full Regression Suite
   - Files: `specs/tasks.md`
   - Change: Run dependency rules tests and mark Task 3.10 as complete `[x]`.
   - Verify: `uv run pytest tests/unit/test_dependency_rules.py -v && uv run pytest -m "not slow" -q`

### Risks & mitigations
- **Risk:** Slow coroutine continues running in background after timeout.
  - **Mitigation:** Use `asyncio.wait_for` which automatically cancels the underlying task upon timeout.
- **Risk:** Double branch failure causing unhandled exception in email worker.
  - **Mitigation:** Gracefully handle dual-failure by returning empty candidates and recording both errors in `RetrievalResult`.

### Rollback plan
Revert changes to `packages/observability/metrics.py`, `packages/retrieval/__init__.py`, delete `packages/retrieval/retriever.py` and `tests/unit/test_retriever.py`.

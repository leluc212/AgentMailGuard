# Implementation Plan — Phase 3, Task 3.11: Cross-Encoder Reranker

### Goal
Implement cross-encoder semantic reranker wrapper, per-organization and per-category disable policy, RRF order fallback on unavailability, and separate latency recording in `packages/retrieval/rerank.py` fulfilling requirements **R11.1, R11.2, R11.5, R11.6**.

### Assumptions
- In testing and lightweight environments, no external model download or GPU is required (R24.5).
- Reranking can be enabled or disabled globally, per organization ID, and per category (R11.2).
- When unavailable or failing, candidates remain in their original fused RRF order with fallback recorded (R11.5).

### Plan
1. Register Metric `rerank_fallback_total`
   - Files: `packages/observability/metrics.py`, `tests/unit/test_observability_metrics.py`
   - Change: Add `rerank_fallback_total: Counter` with labels `["tenant", "reason"]` to `PipelineMetrics`.
   - Verify: `uv run pytest tests/unit/test_observability_metrics.py -v`

2. Implement Reranker Module and Policy
   - Files: `packages/retrieval/rerank.py`, `packages/retrieval/__init__.py`, `tests/stubs/rerank.py`, `tests/stubs/__init__.py`
   - Change: Implement `Reranker` protocol, `CrossEncoderReranker`, `StubReranker`, `RerankPolicy`, `RerankResult`, and `RerankService` with policy checks, timeout handling, RRF fallback, and Prometheus metrics.
   - Verify: `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/`

3. Implement Unit Test Suite for Reranking
   - Files: `tests/unit/test_rerank.py`
   - Change: Test semantic score updates, reordering, policy-based disabling (per-org and per-category), unavailability fallback, timeout fallback, exception fallback, top-k truncation, and metrics emission.
   - Verify: `uv run pytest tests/unit/test_rerank.py -v`

4. Verify Architecture Rules & Full Regression Suite
   - Files: `specs/tasks.md`
   - Change: Run dependency rules tests and mark Task 3.11 as complete `[x]`.
   - Verify: `uv run pytest tests/unit/test_dependency_rules.py -v && uv run pytest -m "not slow" -q`

### Risks & mitigations
- **Risk:** Slow cross-encoder inference stalling email pipeline workers.
  - **Mitigation:** Wrap rerank calls in `asyncio.wait_for` with configurable timeout, falling back cleanly to RRF order (R11.5).
- **Risk:** Missing ML dependencies (`torch` / `transformers`) causing crashes.
  - **Mitigation:** Catch import/initialization errors in `CrossEncoderReranker`, mark as unavailable, and fall back to RRF without crashing.

### Rollback plan
Revert changes in `packages/observability/metrics.py`, `packages/retrieval/__init__.py`, `tests/stubs/__init__.py`, and delete `packages/retrieval/rerank.py`, `tests/stubs/rerank.py`, `tests/unit/test_rerank.py`.

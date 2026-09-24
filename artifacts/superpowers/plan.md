# Implementation Plan — Phase 3, Task 3.9: RRF Fusion

### Goal
Implement pure Reciprocal Rank Fusion (RRF) algorithm in `packages/retrieval/rrf.py` fulfilling requirements **R10.3, R10.8, R24.3** with configurable `k` (default 60), deterministic tie-breaking, branch rank and score preservation, metadata merging, and a comprehensive unit test suite in `tests/unit/test_rrf.py`.

### Assumptions
- `packages/retrieval/rrf.py` is a pure function module with stdlib + `packages/retrieval/models.py` dependencies only.
- `Candidate` carries both ranks and both scores, fused score, content, and metadata per R10.8.
- Default $k = 60$ per R10.3 and specs/design.md §5.5.

### Plan
1. Implement Pure RRF Module
   - Files: `packages/retrieval/rrf.py`, `packages/retrieval/__init__.py`
   - Change: Implement `compute_rrf_score`, `reciprocal_rank_fusion`, and `fuse_lexical_and_vector` with input validation, intra-list deduplication, score calculation, deterministic tie-breaking, and candidate field preservation.
   - Verify: `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/`

2. Implement Pure-Function Unit Test Suite
   - Files: `tests/unit/test_rrf.py`
   - Change: Test disjoint lists, identical lists, overlapping lists, single-branch survival/degradation, empty inputs, ties, custom $k$, weights, intra-list deduplication, field preservation (R10.8), and Postgres SQL CTE mathematical parity.
   - Verify: `uv run pytest tests/unit/test_rrf.py -v`

3. Verify Architectural Compliance and Regressions
   - Files: `specs/tasks.md`
   - Change: Run dependency rules test, full unit test suite, and mark task 3.9 as complete in `specs/tasks.md`.
   - Verify: `uv run pytest tests/unit/test_dependency_rules.py -v && uv run pytest -m "not slow" -q`

### Risks & mitigations
- **Risk:** Floating point imprecision causing non-deterministic ordering when fused scores are close.
  - **Mitigation:** Sort by `(-fused_score, chunk_id)` providing 100% deterministic tie-breaking.
- **Risk:** Intra-branch duplicates inflating RRF scores.
  - **Mitigation:** Only count the first (best) occurrence of a `chunk_id` within any single branch.

### Rollback plan
Revert new files `packages/retrieval/rrf.py` and `tests/unit/test_rrf.py`, and revert exports in `packages/retrieval/__init__.py`.

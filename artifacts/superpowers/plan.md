# Implementation Plan: Phase 2 Task 2.7 — Early-Exit Gate — The Cost Lever

### Goal
Implement the Early-Exit Gate per `specs/tasks.md` Task 2.7, `specs/requirements.md` criteria `R6.5` and `R6.6`, and `specs/design.md §5.3` and `§8`.

### Assumptions
1. If `reply_required == false`: The job transitions directly from `CLASSIFIED` to `COMPLETED`. No embedding, retrieval, reranking, or LLM generation is ever called (`R6.5`).
2. If `retrieval_required == false`: The job transitions to `QUEUED`, but the hybrid RAG retrieval pipeline is bypassed; context is constructed from thread history and business metadata only (`R6.6`).
3. State transitions must strictly go through `packages.domain.state_machine.transition_job` (`GEMINI.md §4`, `R18.3`).
4. Database updates go through `JobStore` (`packages/db/job.py`).

### Plan

1. **Implement Early-Exit Gate (`services/triage_worker/gate.py`, `services/triage_worker/__init__.py`)**
   - Files: `services/triage_worker/gate.py`, `services/triage_worker/__init__.py`
   - Change:
     - Define `GateAction(StrEnum)` (`EARLY_EXIT`, `PROCEED_NO_RAG`, `PROCEED_RAG`).
     - Define `GateDecision` dataclass with action, job, event, and execution flags (`should_embed`, `should_retrieve`, `should_rerank`, `should_generate`).
     - Implement `EarlyExitGate` with `evaluate_decision` (sync/pure) and `evaluate_and_persist` (async DB-backed).
     - Define `DownstreamPipelineHooks(Protocol)` and `GatedPipelineRunner` asserting zero calls on early exit or retrieval bypass.
     - Export symbols in `services.triage_worker`.
   - Verify: `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`

2. **Wire Early-Exit Gate into Cascading Triage Engine (`services/triage_worker/cascade.py`)**
   - Files: `services/triage_worker/cascade.py`
   - Change:
     - Add `triage_and_gate` and `triage_and_gate_sync` methods to `CascadingTriageEngine` to integrate classification with gate evaluation.
   - Verify: `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`

3. **Author Gate Unit Tests (`tests/unit/test_early_exit_gate.py`)**
   - Files: `tests/unit/test_early_exit_gate.py`
   - Change:
     - Test early-exit on `reply_required=False` with `GatedPipelineRunner`, asserting mock call counts for embed, retrieve, rerank, and generate are strictly 0.
     - Test retrieval bypass on `retrieval_required=False`, asserting retrieve, rerank, and embed call counts are 0, while generate is called.
     - Test full RAG path on `retrieval_required=True`, asserting all steps run.
     - Test legal vs illegal state transitions and telemetry events.
   - Verify: `uv run pytest tests/unit/test_early_exit_gate.py -v`

4. **Author PostgreSQL Gate Integration Tests (`tests/integration/test_early_exit_gate_postgres.py`)**
   - Files: `tests/integration/test_early_exit_gate_postgres.py`
   - Change:
     - Author integration tests using `PostgresJobStore` against live PostgreSQL container.
     - Verify database row updates to `COMPLETED` and `processing_event` audit logs.
     - Verify multi-tenant isolation.
   - Verify: `uv run pytest tests/integration/test_early_exit_gate_postgres.py -v`

5. **Full Regression Verification & Task 2.7 Sign-Off**
   - Files: `specs/tasks.md`, `artifacts/superpowers/execution.md`, `artifacts/superpowers/finish.md`
   - Change:
     - Run full test suite, ruff, and mypy.
     - Mark Task 2.7 complete in `specs/tasks.md`.
     - Update execution and finish documentation.
   - Verify: `uv run pytest tests/unit tests/integration -q && uv run ruff check . && uv run mypy packages services tests evaluation`

### Risks & mitigations
- **Risk**: A job at `NORMALIZED` state cannot directly transition to `COMPLETED` according to `TRANSITIONS` table (`NORMALIZED -> CLASSIFIED -> COMPLETED`).
  - **Mitigation**: `EarlyExitGate` detects `job.state == NORMALIZED` and legally sequences `NORMALIZED -> CLASSIFIED -> COMPLETED`.
- **Risk**: Concurrent workers or state machine mismatch.
  - **Mitigation**: Use formal `transition_job` and atomic transactions via `JobStore`.

### Rollback plan
- Revert git changes via `git checkout -- services/ tests/ specs/tasks.md`.

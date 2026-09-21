# Implementation Plan — Phase 2 Task 2.5: Cascade Orchestration & Thresholds

Implement the unified cascading triage engine orchestrating Stage 1 (Rules), Stage 2 (ML), and Stage 3 (Small-LLM fallback), adhering to `R6.2, R6.7, R6.9, R6.11`, `specs/design.md §5.3`, and the Technical Proposal.

---

### Goal

1. Implement `ClassificationStore` protocol, `PostgresClassificationStore`, and `InMemoryClassificationStore` in `packages/db/classification.py` for durable persistence of all triage outputs to `classification_result` (`R6.7`).
2. Implement `ThresholdManager` in `services/triage_worker/thresholds.py` supporting dynamic runtime threshold resolution across organization and category dimensions without redeploy (`R6.9`).
3. Implement `CascadingTriageEngine` in `services/triage_worker/cascade.py` orchestrating Stage 1 (Rules) $\to$ Stage 2 (ML) $\to$ Stage 3 (LLM) with strict early short-circuiting on confidence thresholds (`R6.2`), safe default fallback with review flags on failure (`R6.11`), and per-stage latency tracking.
4. Author comprehensive unit tests in `tests/unit/test_triage_cascade.py` and PostgreSQL integration tests in `tests/integration/test_classification_store_postgres.py`.
5. Mark Task 2.5 `[x]` in `specs/tasks.md`.

---

### Assumptions

1. Stage 1 executes declarative rules (`HotReloadableRuleEngine`). If confidence $\ge \tau_1$, halts and emits.
2. Stage 2 executes lightweight ML (`MLClassifier`). If confidence $\ge \tau_2$, halts and emits without invoking Stage 3.
3. Stage 3 executes small LLM (`LLMTriageClassifier`). If confidence $\ge \tau_3$, halts and emits.
4. If all stages fail or return malformed/low confidence output, the engine assigns safe default (`category="general_inquiry"`, `priority="normal"`, `reply_required=True`, `confidence=0.0`, `decided_by="default"`, `review_flag=True`) per `R6.11`.
5. Every classification result is persisted with its stage, latency, model identifier (if any), and raw output to PostgreSQL `classification_result` table (`R6.7`).
6. Threshold precedence: Org + Category > Org default > Global category > Global default (`R6.9`).

---

### Plan

1. **Implement Classification Database Store**
   - Files: `packages/db/classification.py`, `packages/db/__init__.py`
   - Change:
     - Define `ClassificationResultRow` dataclass.
     - Define `ClassificationStore` Protocol (`save_classification`, `get_classification`, `get_latest_classification_by_message`).
     - Implement `PostgresClassificationStore` with parameterized asyncpg queries.
     - Implement `InMemoryClassificationStore` for offline tests.
   - Verify:
     - Run `uv run mypy packages/db/` and `uv run ruff check packages/db/`.

2. **Implement Configurable Threshold Manager**
   - Files: `services/triage_worker/thresholds.py`, `services/triage_worker/__init__.py`
   - Change:
     - Implement `ThresholdManager` with global defaults, per-category overrides, and per-organization overrides.
     - Add `get_threshold(stage, category=None, organization_id=None) -> float`.
     - Support runtime dynamic re-configuration without restart (`R6.9`).
   - Verify:
     - Run `uv run mypy services/triage_worker/` and `uv run ruff check services/triage_worker/`.

3. **Implement Cascading Triage Engine**
   - Files: `services/triage_worker/cascade.py`, `services/triage_worker/__init__.py`
   - Change:
     - Define `StageExecutionRecord` and `CascadeResult`.
     - Implement `CascadingTriageEngine`:
       - Stage 1 $\to$ Stage 2 $\to$ Stage 3 cascade.
       - Short-circuit on `confidence >= threshold` (`R6.2`).
       - Safe default fallback (`R6.11`).
       - Optional database persistence (`R6.7`).
       - Async `triage()` and sync `triage_sync()`.
   - Verify:
     - Run `uv run mypy services/triage_worker/` and `uv run ruff check services/triage_worker/`.

4. **Author Cascade Unit Test Suite**
   - Files: `tests/unit/test_triage_cascade.py`
   - Change:
     - Test Stage 1 early exit (assert ML and LLM not called).
     - Test Stage 2 early exit (assert LLM not called).
     - Test Stage 3 LLM execution.
     - Test Safe Default on complete failure / malformed output (`review_flag=True`).
     - Test threshold resolution hierarchy (Org + Category > Org default > Global category > Global default).
     - Test persistence recording (`R6.7`).
   - Verify:
     - Run `uv run pytest tests/unit/test_triage_cascade.py -v`.

5. **Author PostgreSQL Integration Test Suite**
   - Files: `tests/integration/test_classification_store_postgres.py`
   - Change:
     - Test `PostgresClassificationStore` CRUD against live PostgreSQL container (`:5433`).
     - Test tenant isolation (`organization_id`).
   - Verify:
     - Run `uv run pytest tests/integration/test_classification_store_postgres.py -v`.

6. **Update Task Checklist & Sign-Off**
   - Files: `specs/tasks.md`
   - Change:
     - Mark task 2.5 `[x]`.
   - Verify:
     - Review git diff, run full test suite, commit and push.

---

### Risks & Mitigations

- **Risk:** Unnecessary LLM invocation when rules or ML already have high confidence.
  - *Mitigation:* Strict short-circuit gate checked immediately after each stage. Tested with call counters on mock objects.
- **Risk:** Database write failure on persistence blocking the triage pipeline.
  - *Mitigation:* If persistence fails, log error clearly and retain classification in memory; return `CascadeResult` so message processing is not lost.
- **Risk:** Organization settings drift or invalid threshold values.
  - *Mitigation:* `ThresholdManager` bounds all thresholds to $[0.0, 1.0]$ and falls back safely to default `TriageSettings`.

---

### Rollback Plan

If issues occur:
1. Revert `packages/db/classification.py`, `services/triage_worker/thresholds.py`, and `services/triage_worker/cascade.py`.
2. Existing standalone Stage 1, Stage 2, and Stage 3 classifiers remain untouched and independently operable.

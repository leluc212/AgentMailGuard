# Phase 2 Task 2.8: Deterministic Template Reply Path — Finish Summary

## 1. Summary of Changes

- **Domain State Machine & Entities (`packages/domain/state_machine.py`, `packages/domain/entities.py`, `packages/domain/__init__.py`)**:
  - Added `JobState.DRAFTED` to allowed targets in `TRANSITIONS[JobState.CLASSIFIED]` for direct state machine transitions upon template reply rendering.
  - Defined `GeneratedDraft` dataclass entity in `packages/domain/entities.py` matching PostgreSQL `generated_draft` table (`id`, `organization_id`, `job_id`, `message_id`, `thread_id`, `action`, `subject`, `body`, `confidence`, `citations`, `citation_mismatch`, `model_name`, `model_tier`, `escalation_reason`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_estimate`, `status`, `provider_ref`, `created_at`).
  - Exported all symbols in `packages.domain`.
- **Pure Domain Templates & Variable Substitution Engine (`packages/domain/templates.py`, `tests/unit/test_templates_domain.py`)**:
  - Implemented `TemplateDefinition` and `TemplateRenderResult`.
  - Implemented mustache-style variable substitution (`{{ variable }}` and `{{ object.field }}`) for message attributes (`subject`, `sender_name`, `sender_email`, `message_id`, `thread_id`, etc.) and arbitrary `business_data` attributes with safe fallback for missing values.
  - Implemented `TemplateRegistry` with `(category, intent)` case-insensitive lookup, dictionary/JSON deserialization, and disk file resolution.
  - Verified pure standard library compliance (zero external dependencies in `packages/domain`).
- **Approved Versioned Template Files & Declarative Configuration (`prompts/templates/`, `config/templates.yaml`, `services/triage_worker/template_loader.py`)**:
  - Authored version 1 approved text template files:
    - `prompts/templates/acknowledgement.v1.txt` for `(acknowledgement, receipt_confirmation)`
    - `prompts/templates/scheduling_ack.v1.txt` for `(scheduling, meeting_accepted)`
  - Created declarative YAML registry `config/templates.yaml`.
  - Implemented `HotReloadableTemplateRegistry` in `services/triage_worker/template_loader.py` with mtime checking and error isolation.
- **Draft Persistence Store (`packages/db/draft.py`, `packages/db/__init__.py`, `tests/unit/test_draft_store.py`)**:
  - Defined `DraftStore` protocol exposing `create_draft`, `get_draft`, `list_drafts_for_job`, `list_drafts_for_thread`.
  - Implemented `InMemoryDraftStore` for unit testing and `PostgresDraftStore` using parameterized tenant-scoped queries on `generated_draft`.
- **Early-Exit Gate & Pipeline Runner Integration (`services/triage_worker/gate.py`, `services/triage_worker/cascade.py`, `tests/unit/test_template_gate.py`)**:
  - Added `GateAction.TEMPLATE_REPLY` to `GateAction`.
  - Integrated `TemplateRegistry` and `DraftStore` into `EarlyExitGate`:
    - If `workflow_hint == 'template'` and template matches: renders draft, sets zero-AI flags (`should_retrieve=False`, `should_generate=False`), transitions job to `DRAFTED` (`R6.13`), persists draft, returns `GateAction.TEMPLATE_REPLY`.
    - If `workflow_hint == 'template'` and no template matches: falls back to `workflow_hint='ai'` (`R6.14`), transitions job to `QUEUED`, never blocking actionable replies.
  - Updated `GatedPipelineRunner` to assert zero calls to retrieval, reranking, and generation on `TEMPLATE_REPLY`.
  - Proved that early exit (`COMPLETED`), template reply (`DRAFTED`), and AI generation (`QUEUED`) are mutually exclusive and exhaustive over all mail (`R6.15`).
- **PostgreSQL Integration Tests (`tests/integration/test_template_gate_postgres.py`)**:
  - Verified live database persistence of `processing_job` (`DRAFTED`), `processing_event`, and `generated_draft`.
  - Verified live fallback to `QUEUED` when template is missing.
  - Verified strict multi-tenant isolation across organizations.
- **Task Verification**: Marked Task 2.8 complete in `specs/tasks.md`.

---

## 2. Review Pass (Blocker / Major / Minor / Nit)

- **Blocker**: None.
- **Major**: None.
- **Minor**: None.
- **Nit**: None. All 183 source files pass `ruff check` and `mypy --strict`.

---

## 3. Verification Commands Run & Results

| Verification Target | Command | Result |
|---|---|---|
| State Machine Unit Tests | `uv run pytest tests/unit/test_state_machine.py -v` | PASS (23/23 passed in 0.12s) |
| Template Domain Unit Tests | `uv run pytest tests/unit/test_templates_domain.py -v` | PASS (10/10 passed in 0.10s) |
| Architectural Boundary Guard | `uv run pytest tests/unit/test_dependency_rules.py -v` | PASS (4/4 passed in 0.67s) |
| Draft Store Unit Tests | `uv run pytest tests/unit/test_draft_store.py -v` | PASS (1/1 passed in 0.10s) |
| Template Gate Unit Tests | `uv run pytest tests/unit/test_template_gate.py -v` | PASS (4/4 passed in 2.34s) |
| PostgreSQL Template Gate Integration | `uv run pytest tests/integration/test_template_gate_postgres.py -v` | PASS (3/3 passed in 2.50s) |
| Full Test Suite | `uv run pytest tests/unit tests/integration -q` | PASS (534/534 passed in 28.2s) |
| Code Style & Strict Types | `uv run ruff check . && uv run mypy packages services tests evaluation` | PASS (0 errors, 183 files clean) |

---

## 4. Manual Validation Steps

To verify the deterministic template reply path locally:
```bash
uv run python -c "
from packages.domain.entities import EmailAddress, Job, Classification, NormalizedMessage
from packages.domain.state_machine import JobState
from services.triage_worker.template_loader import load_templates_from_file
from services.triage_worker.gate import EarlyExitGate, GateAction

registry = load_templates_from_file('config/templates.yaml')
gate = EarlyExitGate(template_registry=registry)

job = Job(state=JobState.CLASSIFIED, organization_id='org-alpha')
cls = Classification(category='acknowledgement', intent='receipt_confirmation', reply_required=True, workflow_hint='template', retrieval_required=False)
msg = {'subject': 'Support Request', 'sender_name': 'Sarah Connor', 'message_id': 'msg-999'}
biz = {'order_id': 'ORD-777'}

dec = gate.evaluate_decision(job, cls, message=msg, business_data=biz)
assert dec.action == GateAction.TEMPLATE_REPLY
assert dec.job.state == JobState.DRAFTED
assert dec.should_retrieve is False
assert dec.should_generate is False
assert dec.rendered_draft is not None
assert 'Sarah Connor' in dec.rendered_draft.body
print('Deterministic template manual test passed successfully!')
"
```

---

## 5. Follow-Ups

- Next task in queue is **Phase 2 Task 2.9: Funnel instrumentation** (`R6.10, R6.15, R21.4, NFR14`) tracking counters for all three outcomes (`early_exit`, `templated`, `generated`) and `emails_templated_total` Prometheus metric.

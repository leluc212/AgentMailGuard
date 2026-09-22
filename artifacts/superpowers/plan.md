# Implementation Plan: Phase 2 Task 2.8 — Deterministic Template Reply Path

## Goal
Implement Phase 2 Task 2.8 ("Deterministic template reply path — the missing 20%") per `specs/tasks.md`, `specs/requirements.md` (R6.12, R6.13, R6.14, R6.15), `specs/design.md §5.3, §8`, and the Technical Proposal.

The triage engine emits `workflow_hint ∈ {template, ai, none}`. A versioned template registry keyed by `(category, intent)` substitutes variables from message and business data. When `workflow_hint='template'`, an approved deterministic draft is rendered and the job transitions directly to `DRAFTED` with **zero** retrieval and **zero** LLM generation calls. If no template matches, it falls back to `workflow_hint='ai'` so a reply is never blocked. The three outcomes (early exit / template reply / AI generation) are mutually exclusive and exhaustive over all mail.

## Architecture & Funnel Alignment

```
                                  [Inbound Message]
                                          │
                                          ▼
                              [Cascading Triage Engine]
                      (Rules -> Lightweight ML -> Small LLM)
                                          │
                        Emits Classification Snapshot
                (category, intent, reply_required, workflow_hint, retrieval_required)
                                          │
         ┌────────────────────────────────┼────────────────────────────────┐
         ▼                                ▼                                ▼
  [Outcome 1: ~45%]               [Outcome 2: ~20%]                [Outcome 3: ~35%]
reply_required == false       workflow_hint == 'template'        workflow_hint == 'ai'
         │                                │                                │
         ▼                                ▼                                ▼
  Early Exit Gate                 Template Registry                Actionable Queue
(0 AI / 0 Vector)          Match (category, intent)?                     (QUEUED)
         │                         ├── Yes ──▶ Render Draft &               │
         ▼                         │           Transition to DRAFTED        ▼
    [COMPLETED]                    │           (0 RAG / 0 Generation)   Hybrid RAG &
                                   │                                    Context Builder
                                   └── No (Missing Template) ──┐            │
                                                               │            ▼
                                               Fall back to AI ┘      AI Generation
                                            (never blocks a reply)     (GENERATING)
```

## Assumptions
1. `TRANSITIONS[JobState.CLASSIFIED]` in `packages/domain/state_machine.py` must permit `{JobState.QUEUED, JobState.COMPLETED, JobState.DRAFTED, JobState.FAILED}` so the template path can transition directly to `DRAFTED` without bypassing the domain state machine.
2. The `generated_draft` table already exists in PostgreSQL (`migrations/0001_core_schema.up.sql`). Persisting drafted template replies populates this table with `model_name='template'`, `model_tier='template'`, zero token usage, zero cost, and empty citations.
3. Variable substitution supports dot-notation access (e.g., `{{ subject }}`, `{{ sender.name }}`, `{{ sender_name }}`, `{{ business_data.order_id }}`) and safe fallback for missing variables without throwing unhandled exceptions.
4. If `workflow_hint == 'template'` but no matching template exists in the registry for `(category, intent)`, the gate seamlessly downgrades to `workflow_hint = 'ai'` and transitions the job to `QUEUED`.

---

## Detailed Step-by-Step Plan

### Step 1: Domain State Machine & Entities Update
- **Files**:
  - `packages/domain/state_machine.py`
  - `packages/domain/entities.py`
  - `tests/unit/test_state_machine.py`
- **Changes**:
  - Update `TRANSITIONS[JobState.CLASSIFIED]` in `packages/domain/state_machine.py` to include `JobState.DRAFTED`.
  - Define `GeneratedDraft` dataclass in `packages/domain/entities.py` with fields: `id`, `organization_id`, `job_id`, `message_id`, `thread_id`, `action`, `subject`, `body`, `confidence`, `citations`, `citation_mismatch`, `model_name`, `model_tier`, `escalation_reason`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_estimate`, `status`, `provider_ref`, `created_at`.
  - Update `tests/unit/test_state_machine.py`: assert total legal transitions is 25, verify `CLASSIFIED -> DRAFTED` is legal, and remove it from illegal transition parameters.
- **Verify**:
  - `uv run pytest tests/unit/test_state_machine.py -v`

### Step 2: Template Domain & Variable Substitution Engine
- **Files**:
  - `packages/domain/templates.py` (new)
  - `packages/domain/__init__.py`
  - `tests/unit/test_templates_domain.py` (new)
- **Changes**:
  - Define `TemplateDefinition` dataclass (`template_id`, `category`, `intent`, `subject_template`, `body_template`, `version`, `is_active`).
  - Define `TemplateRenderResult` dataclass (`template_id`, `version`, `subject`, `body`, `variables_used`).
  - Implement `substitute_template_variables(template_str: str, context: dict[str, Any]) -> str` supporting `{{ variable }}` and `{{ object.field }}` placeholders.
  - Implement `TemplateRegistry`:
    - Stores templates keyed by `(category, intent)`.
    - Methods: `register(template)`, `find_template(category, intent)`, `render(template, message, business_data)`.
    - Factory methods: `from_dict(data)`, `from_yaml(yaml_str)`, `from_file(path)`.
- **Verify**:
  - `uv run pytest tests/unit/test_templates_domain.py -v`

### Step 3: Approved Versioned Template Files & Default Configuration
- **Files**:
  - `prompts/templates/acknowledgement.v1.txt` (new)
  - `prompts/templates/scheduling_ack.v1.txt` (new)
  - `config/templates.yaml` (new)
- **Changes**:
  - Create versioned text template files in `prompts/templates/` for:
    - `acknowledgement.v1.txt`: acknowledgement of receipt for `(acknowledgement, receipt_confirmation)`.
    - `scheduling_ack.v1.txt`: meeting confirmation for `(scheduling, meeting_accepted)`.
  - Create `config/templates.yaml` referencing these template files with standard subject templates `"Re: {{ subject }}"`.
- **Verify**:
  - `uv run python -c "from packages.domain.templates import TemplateRegistry; r = TemplateRegistry.from_file('config/templates.yaml'); assert len(r.templates) >= 2; print('Templates loaded successfully')"`

### Step 4: Draft Persistence Store (PostgreSQL & In-Memory)
- **Files**:
  - `packages/db/draft.py` (new)
  - `packages/db/__init__.py`
  - `tests/unit/test_draft_store.py` (new)
- **Changes**:
  - Define `DraftStore` protocol exposing `create_draft`, `get_draft`, `list_drafts_for_job`, `list_drafts_for_thread`.
  - Implement `InMemoryDraftStore` for deterministic unit testing.
  - Implement `PostgresDraftStore` using parameterized queries on `generated_draft` table with strict `organization_id` tenant scoping.
- **Verify**:
  - `uv run pytest tests/unit/test_draft_store.py -v`

### Step 5: Gate Integration, Template Rendering & Fallback Path
- **Files**:
  - `services/triage_worker/gate.py`
  - `services/triage_worker/cascade.py`
  - `services/triage_worker/__init__.py`
  - `tests/unit/test_template_gate.py` (new)
- **Changes**:
  - Add `GateAction.TEMPLATE_REPLY = "template_reply"` to `GateAction`.
  - Add `rendered_draft: GeneratedDraft | None = None` and `draft_id: UUID | str | None = None` to `GateDecision`.
  - Integrate `TemplateRegistry` and optional `DraftStore` into `EarlyExitGate`:
    - If `reply_required == False` or `workflow_hint == 'none'`: `GateAction.EARLY_EXIT` -> `COMPLETED`.
    - If `workflow_hint == 'template'`:
      - Attempt `template_registry.find_template(classification.category, classification.intent)`.
      - If found: render template, build `GeneratedDraft` (0 tokens, 0 cost, citations=[], status='draft'), transition job to `DRAFTED` (`R6.13`), assert zero retrieval/generation (`should_retrieve=False`, `should_generate=False`), persist draft if store provided, return `GateDecision(action=GateAction.TEMPLATE_REPLY)`.
      - If NOT found: fall back to `workflow_hint = 'ai'` (`R6.14`), transition job to `QUEUED`, set `action = PROCEED_RAG` or `PROCEED_NO_RAG` based on `retrieval_required`.
    - If `workflow_hint == 'ai'`: transition to `QUEUED` (`PROCEED_RAG` or `PROCEED_NO_RAG`).
  - Extend `DownstreamPipelineHooks` & `GatedPipelineRunner` in tests to verify zero calls across embed, retrieve, rerank, and generate on `TEMPLATE_REPLY`.
  - Test mutual exclusivity and exhaustiveness of the 3 outcomes across all categories and workflows (R6.15).
- **Verify**:
  - `uv run pytest tests/unit/test_template_gate.py -v`

### Step 6: Integration Tests & Full Suite Regression Verification
- **Files**:
  - `tests/integration/test_template_gate_postgres.py` (new)
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
  - `artifacts/superpowers/finish.md`
- **Changes**:
  - Write integration tests against PostgreSQL:
    - Inbound email matching template -> renders draft -> persists in `generated_draft` -> updates `processing_job` to `DRAFTED` -> records audit `processing_event`.
    - Missing template -> falls back to `ai` -> job state `QUEUED` -> no draft created.
    - Multi-tenant isolation: draft rows segregated strictly by `organization_id`.
    - Exhaustive funnel reconciliation test (45% no-reply / 20% template / 35% AI) with 0 residual bucket.
  - Run full test suite (`pytest tests/unit tests/integration`), `ruff`, and `mypy --strict`.
  - Mark Task 2.8 complete in `specs/tasks.md`.
- **Verify**:
  - `uv run pytest tests/integration/test_template_gate_postgres.py -v`
  - `uv run pytest tests/unit tests/integration -q`
  - `uv run ruff check . && uv run mypy packages services tests evaluation`

---

## Risks & Mitigations
- **Risk 1**: Missing variables in template string causing unhandled exceptions during email processing.
  - *Mitigation*: The substitution engine replaces unresolved `{variable}` or `{object.field}` tokens with an empty string or gracefully ignores them, logging a warning.
- **Risk 2**: Template match failure stalling or dropping inbound actionable mail.
  - *Mitigation*: Strictly enforce R6.14 fallback to `workflow_hint='ai'` when `find_template` returns None, ensuring the email is always queued for AI generation.
- **Risk 3**: Illegal state transition in domain state machine when moving from `CLASSIFIED` to `DRAFTED`.
  - *Mitigation*: Explicitly declare `JobState.DRAFTED` as a legal destination from `JobState.CLASSIFIED` in `packages/domain/state_machine.py`.

## Rollback Plan
If any step fails or regresses existing functionality:
- Restore `packages/domain/state_machine.py` and `tests/unit/test_state_machine.py` to previous git HEAD.
- Remove untracked files with `git clean -fd`.

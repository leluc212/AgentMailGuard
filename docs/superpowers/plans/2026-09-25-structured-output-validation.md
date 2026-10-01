# Phase 4, Task 4.9: Structured Output & Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement structured output validation and 1-repair retry orchestration for draft reply synthesis (`R16.1`–`R16.3`, `design.md §5.7`), strictly enforcing the schema `{action, draft, confidence, knowledge_chunks[], thread_summary_updated, model_tier}`, retrying once on failure with repair instructions, and failing into the retry/DLQ path on second failure so that unvalidated drafts are never persisted.

**Architecture:** A dedicated validation module (`packages/llm/validation.py`) enforces strict Pydantic and JSON schema validation against `schemas/reply.v1.json`. `SinglePassGenerator` intercepts generation outputs, validates them, and on validation failure (invalid JSON or schema non-conformance) constructs a structured repair prompt and invokes the LLM once with `CallKind.REPAIR` under the strict `CallBudgetTracker` budget. On second failure, it raises `UnvalidatedDraftError` to ensure no invalid draft ever proceeds to storage or dispatch.

**Architecture Diagram:**

```mermaid
graph TD
    subgraph "SinglePassGenerator Workflow (R16.1-R16.3)"
        CTX[ContextPackage] --> GEN_CALL[LLMProvider.generate<br>call_kind=GENERATE]
        GEN_CALL --> VAL1{Validate Schema<br>R16.1, R16.2}
        VAL1 -->|Valid| SUCCESS[GenerationResult<br>is_repaired=False]
        VAL1 -->|Invalid| REP_CHECK{Can Call Repair?<br>Budget <= 1 repair}
        REP_CHECK -->|No| FAIL_DLQ[Raise UnvalidatedDraftError<br>Never persist unvalidated draft]
        REP_CHECK -->|Yes| BUILD_REP[build_repair_messages<br>error + invalid output + schema]
        BUILD_REP --> REP_CALL[LLMProvider.generate<br>call_kind=REPAIR]
        REP_CALL --> VAL2{Validate Repaired<br>Schema R16.3}
        VAL2 -->|Valid| REP_SUCCESS[GenerationResult<br>is_repaired=True]
        VAL2 -->|Invalid| FAIL_DLQ
    end

    subgraph "Observability"
        REP_CALL -.-> MET_CALL[llm_calls_total / llm_calls_per_job<br>kind='repair']
        VAL2 -.-> MET_REP[draft_repairs_total<br>status='succeeded' | 'failed']
    end
```

**Tech Stack:**
- Python 3.12+
- Pydantic v2 (`BaseModel`, `Field`, `ConfigDict`, `field_validator`)
- Standard library `json`, `dataclasses`, `enum`
- Prometheus client (`Counter`, `Histogram`)
- Pytest with `pytest-asyncio`

**Spec References:**
- Acceptance Contract: `specs/requirements.md` (`R16.1`, `R16.2`, `R16.3`, `R14.9`, `R14.10`, `R15.5`)
- Blueprint: `specs/design.md` §5.7 (*Reply Agent & Model Cascade*), §6.1 (*Data Models*)
- Work Queue: `specs/tasks.md` Task 4.9
- Technical Proposal: `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md` §24 (*Structured LLM Output*)

## Global Constraints
- Provider names (`gmail`, `graph`, `imap`) appear only inside `packages/adapters/`.
- All model calls go through `LLMProvider`. No direct SDK imports outside `packages/llm/`.
- Exactly one generation call per job; ≤1 schema repair retry (`R14.9`, `R16.3`, `CLAUDE.md §4`).
- Never persist an LLM response that failed schema validation (`R16.3`, `CLAUDE.md §4`).
- Multi-tenant and zero-network testing discipline: all tests must be 100% deterministic and offline (`CLAUDE.md §8`).

---

### Task 1: Schema Specification & Validation Engine (`schemas/reply.v1.json`, `packages/llm/validation.py`)

**Files:**
- Modify: `schemas/reply.v1.json`
- Create: `packages/llm/validation.py`
- Modify: `packages/llm/__init__.py`
- Test: `tests/unit/test_draft_validation.py`

**Interfaces:**
- Consumes: `schemas/reply.v1.json`, `packages/llm/protocol.py` (`ChatMessage`, `LLMSchemaValidationError`)
- Produces:
  - `DraftReplyPayload` (Pydantic model validating all 6 fields)
  - `validate_draft_payload(content: Any, schema: dict[str, Any] | None = None) -> DraftReplyPayload`
  - `DraftValidationError(LLMSchemaValidationError)`
  - `UnvalidatedDraftError(LLMSchemaValidationError)`
  - `build_repair_messages(original_messages: list[ChatMessage], invalid_content: Any, validation_error: str, schema: dict[str, Any] | None = None) -> list[ChatMessage]`

- [ ] **Step 1: Write failing tests for schema validation and repair message building**

```python
# tests/unit/test_draft_validation.py
import json
from pathlib import Path
import pytest
from packages.llm.protocol import ChatMessage
from packages.llm.validation import (
    DraftReplyPayload,
    DraftValidationError,
    UnvalidatedDraftError,
    validate_draft_payload,
    build_repair_messages,
)

def test_reply_schema_requires_all_six_fields() -> None:
    schema = json.loads(Path("schemas/reply.v1.json").read_text(encoding="utf-8"))
    assert set(schema["required"]) == {
        "action",
        "draft",
        "confidence",
        "knowledge_chunks",
        "thread_summary_updated",
        "model_tier",
    }

def test_validate_valid_draft_payload() -> None:
    valid_data = {
        "action": "reply",
        "draft": "Dear Alice, your password has been reset.",
        "confidence": 0.95,
        "knowledge_chunks": ["doc-123"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }
    payload = validate_draft_payload(valid_data)
    assert isinstance(payload, DraftReplyPayload)
    assert payload.action == "reply"
    assert payload.draft == "Dear Alice, your password has been reset."
    assert payload.confidence == 0.95
    assert payload.knowledge_chunks == ["doc-123"]
    assert payload.thread_summary_updated is False
    assert payload.model_tier == "routine"

def test_validate_missing_field_raises_draft_validation_error() -> None:
    invalid_data = {
        "action": "reply",
        "draft": "Missing confidence and other fields",
    }
    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(invalid_data)
    assert "confidence" in str(exc_info.value)

def test_validate_invalid_action_enum() -> None:
    data = {
        "action": "disallowed_action",
        "draft": "Some draft",
        "confidence": 0.8,
        "knowledge_chunks": [],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }
    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(data)
    assert "action" in str(exc_info.value)

def test_validate_extra_properties_rejected() -> None:
    data = {
        "action": "reply",
        "draft": "Draft text",
        "confidence": 0.9,
        "knowledge_chunks": [],
        "thread_summary_updated": False,
        "model_tier": "routine",
        "unexpected_extra_field": "disallowed",
    }
    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(data)
    assert "extra" in str(exc_info.value).lower() or "unexpected_extra_field" in str(exc_info.value)

def test_build_repair_messages_formatting() -> None:
    orig = [ChatMessage(role="user", content="Draft an email")]
    invalid = {"action": "invalid"}
    error = "Field 'draft' is required"
    repair_msgs = build_repair_messages(orig, invalid, error)
    assert len(repair_msgs) >= 2
    assert "Field 'draft' is required" in repair_msgs[-1].content
    assert "JSON schema" in repair_msgs[-1].content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_draft_validation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.llm.validation'`

- [ ] **Step 3: Update `schemas/reply.v1.json` and implement `packages/llm/validation.py`**

In `schemas/reply.v1.json`:
Update `"required"` array to include all 6 fields:
```json
  "required": [
    "action",
    "draft",
    "confidence",
    "knowledge_chunks",
    "thread_summary_updated",
    "model_tier"
  ]
```

In `packages/llm/validation.py`:
Implement `DraftReplyPayload(BaseModel)` with `ConfigDict(extra="forbid")`, field validators for `action` (`reply`, `forward`, `escalate`, `no_reply`), `confidence` (0.0 to 1.0), `knowledge_chunks` (list of strings), `thread_summary_updated` (bool), `model_tier` (str).
Implement `validate_draft_payload(content: Any, schema: dict[str, Any] | None = None) -> DraftReplyPayload`.
Implement `DraftValidationError` and `UnvalidatedDraftError`.
Implement `build_repair_messages(...)`.

Re-export symbols in `packages/llm/__init__.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_draft_validation.py tests/unit/test_agent_profile.py -v`
Expected: PASS

- [ ] **Step 5: Run formatting, lint, and mypy**

Run:
```bash
uv run ruff check packages/llm/ tests/unit/test_draft_validation.py
uv run ruff format --check packages/llm/ tests/unit/test_draft_validation.py
uv run mypy packages/llm/validation.py tests/unit/test_draft_validation.py
```

- [ ] **Step 6: Commit**

```bash
git add schemas/reply.v1.json packages/llm/validation.py packages/llm/__init__.py tests/unit/test_draft_validation.py
git commit -m "feat(llm): implement DraftReplyPayload, validation engine, and repair prompts [task 4.9] [R16.1, R16.2]"
```

---

### Task 2: Observability & Repair Metrics (`packages/observability/metrics.py`)

**Files:**
- Modify: `packages/observability/metrics.py`
- Test: `tests/unit/test_observability_metrics.py`

**Interfaces:**
- Consumes: Prometheus Client
- Produces:
  - `draft_repairs_total: Counter` with labels `["status"]` (`"succeeded"`, `"failed"`)
  - `draft_validation_failures_total: Counter` with labels `["stage"]` (`"initial"`, `"repair"`)

- [ ] **Step 1: Write failing tests for repair metrics**

In `tests/unit/test_observability_metrics.py`:
Add `test_draft_repairs_and_validation_failure_metrics()` asserting registration and incrementing of `draft_repairs_total` and `draft_validation_failures_total`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_observability_metrics.py -k test_draft_repairs -v`
Expected: FAIL with `AttributeError: 'PipelineMetrics' object has no attribute 'draft_repairs_total'`

- [ ] **Step 3: Update `packages/observability/metrics.py`**

Add fields to `PipelineMetrics` dataclass:
```python
draft_repairs_total: Counter
draft_validation_failures_total: Counter
```
Instantiate both in `create_pipeline_metrics`:
```python
draft_repairs_total=Counter(
    "draft_repairs_total",
    "Total schema repair retry outcomes for generated drafts (R16.3)",
    ["status"],
    registry=reg,
),
draft_validation_failures_total=Counter(
    "draft_validation_failures_total",
    "Total draft schema validation failures by pipeline stage (R16.2, R16.3)",
    ["stage"],
    registry=reg,
),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_observability_metrics.py -v`
Expected: PASS

- [ ] **Step 5: Run formatting, lint, and mypy**

Run:
```bash
uv run ruff check packages/observability/ tests/unit/test_observability_metrics.py
uv run ruff format --check packages/observability/ tests/unit/test_observability_metrics.py
uv run mypy packages/observability/metrics.py tests/unit/test_observability_metrics.py
```

- [ ] **Step 6: Commit**

```bash
git add packages/observability/metrics.py tests/unit/test_observability_metrics.py
git commit -m "feat(observability): add draft_repairs_total and validation failure counters [task 4.9] [R16.2, R16.3]"
```

---

### Task 3: SinglePassGenerator Validation & Repair Orchestration (`packages/llm/generator.py`)

**Files:**
- Modify: `packages/llm/generator.py`
- Modify: `tests/unit/test_single_pass_generator.py`
- Modify: `tests/unit/test_complexity_router_integration.py`
- Create: `tests/unit/test_draft_repair_orchestration.py`
- Modify: `specs/tasks.md`

**Interfaces:**
- Consumes:
  - `validate_draft_payload`, `build_repair_messages`, `DraftValidationError`, `UnvalidatedDraftError`
  - `BudgetedLLMProvider`, `CallBudgetTracker`, `CallKind.REPAIR`
  - `PipelineMetrics`
- Produces:
  - `SinglePassGenerator.generate_draft` validating schema, executing 1 repair retry on failure, raising `UnvalidatedDraftError` on double failure, returning `GenerationResult(is_repaired=...)`.
  - Check-off in `specs/tasks.md` Task 4.9.

- [ ] **Step 1: Write failing integration tests for validation and repair retry**

In `tests/unit/test_draft_repair_orchestration.py`:
- `test_generation_valid_output_first_attempt()`: output passes validation on first attempt, 0 repairs, exactly 1 `GENERATE` call.
- `test_generation_invalid_output_repaired_successfully()`: first attempt returns invalid JSON/schema, repair call returns valid schema -> `GenerationResult.is_repaired is True`, exactly 1 `GENERATE` + 1 `REPAIR` call, budget respected, `draft_repairs_total{status="succeeded"}` incremented.
- `test_generation_invalid_output_repair_also_fails()`: first attempt fails, repair attempt also fails -> raises `UnvalidatedDraftError`, never returns or persists unvalidated draft (R16.3), `draft_repairs_total{status="failed"}` incremented.
- `test_generation_repair_budget_exhaustion_guards()`: if repair call is not permitted by budget tracker, immediately raises `UnvalidatedDraftError` without executing extra call.
- `test_generation_preserves_escalated_tier_on_repair()`: if escalated tier was chosen, repair retry also runs on the escalated tier.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_draft_repair_orchestration.py -v`
Expected: FAIL (repair logic not yet implemented in `SinglePassGenerator`)

- [ ] **Step 3: Update `SinglePassGenerator` in `packages/llm/generator.py`**

1. Update `GenerationResult`:
   Add fields:
   `is_repaired: bool = False`
   `repair_attempts: int = 0`
   `validated_payload: DraftReplyPayload | None = None`
2. Update `generate_draft`:
   - After initial `budgeted.generate(call_kind=CallKind.GENERATE)`, validate `result.content` via `validate_draft_payload(result.content, schema)`.
   - If `DraftValidationError` or `LLMSchemaValidationError` occurs:
     - Record metric `draft_validation_failures_total.labels(stage="initial").inc()`.
     - Check `tracker.can_call(CallKind.REPAIR)`: if false, record `draft_repairs_total.labels(status="failed").inc()`, raise `UnvalidatedDraftError`.
     - Build repair prompt with `build_repair_messages(messages, result.content if hasattr(result, "content") else str(exc), str(exc), schema)`.
     - Execute repair call: `repair_result = await budgeted.generate(messages=repair_messages, schema=schema, tier=effective_tier, call_kind=CallKind.REPAIR)`.
     - Validate repair result: `validated = validate_draft_payload(repair_result.content, schema)`.
     - If repair validation succeeds:
       - Record `draft_repairs_total.labels(status="succeeded").inc()`.
       - Combine token usages and set `is_repaired = True`.
     - If repair validation fails:
       - Record `draft_validation_failures_total.labels(stage="repair").inc()`.
       - Record `draft_repairs_total.labels(status="failed").inc()`.
       - Raise `UnvalidatedDraftError` (never persist unvalidated draft - R16.3).
3. Ensure `canned_reply` in `tests/unit/test_single_pass_generator.py` and `tests/unit/test_complexity_router_integration.py` contains all 6 required fields so existing test suites remain clean.
4. Mark Task 4.9 `[x]` in `specs/tasks.md`.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/unit/test_draft_repair_orchestration.py tests/unit/test_draft_validation.py tests/unit/test_single_pass_generator.py tests/unit/test_complexity_router_integration.py tests/unit/test_generation_budget.py -v
```
Expected: PASS

- [ ] **Step 5: Run full test suite, format, lint, and mypy**

Run:
```bash
uv run ruff check packages/ tests/
uv run ruff format --check packages/ tests/
uv run mypy packages/
uv run pytest tests/unit/ -v
```

- [ ] **Step 6: Commit**

```bash
git add packages/llm/generator.py tests/unit/test_draft_repair_orchestration.py tests/unit/test_single_pass_generator.py tests/unit/test_complexity_router_integration.py specs/tasks.md
git commit -m "feat(llm): integrate schema validation and repair retry in SinglePassGenerator [task 4.9] [R16.1-R16.3]"
```

---

## 4. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Model fails initial generation with non-JSON text | `json.loads` fails before schema validation | Validator and generator handle raw string content gracefully and pass raw response text into repair prompt |
| Repair retry calls consume extra budget | Breaches `R14.9` budget invariant | Repair call uses `CallKind.REPAIR` which is strictly capped at 1; `tracker.assert_generation_budget(require_generation=True)` validates ≤1 repair |
| Model repeats schema failure on repair | Infinite retry loop | Hard stop at 1 repair retry (`R16.3`), raising `UnvalidatedDraftError` to terminal DLQ/retry path |
| Existing tests had 4-field canned replies | Breakage in existing generator tests | All test fixtures updated to standard 6-field conformant structure matching `R16.1` and `reply.v1.json` |

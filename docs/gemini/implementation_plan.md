# Phase 4, Task 4.9: Structured Output & Validation Implementation Plan

Implement structured output validation and 1-repair retry orchestration for draft reply synthesis (`R16.1`–`R16.3`, `design.md §5.7`), strictly enforcing the schema `{action, draft, confidence, knowledge_chunks[], thread_summary_updated, model_tier}`, retrying once on failure with repair instructions, and failing into the retry/DLQ path on second failure so that unvalidated drafts are never persisted.

## User Review Required

> [!IMPORTANT]
> - `schemas/reply.v1.json` is updated to include all 6 fields in its `"required"` array (`action, draft, confidence, knowledge_chunks, thread_summary_updated, model_tier`), bringing it into strict alignment with `R16.1`, `design.md §5.7`, and Technical Proposal §24.
> - Canned reply fixtures in existing tests (`test_single_pass_generator.py` and `test_complexity_router_integration.py`) are updated to provide all 6 conformant fields so they pass the stricter schema validation.

## Architecture Diagram

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

## Proposed Changes

### Component 1: Schema Specification & Validation Engine

Enforces schema properties and validation logic before draft persistence.

#### [MODIFY] [reply.v1.json](file:///home/ple/Documents/antigravity/dazzling-bose/schemas/reply.v1.json)
- Add `"thread_summary_updated"` and `"model_tier"` to `"required"` list alongside `"action"`, `"draft"`, `"confidence"`, and `"knowledge_chunks"`.

#### [NEW] [validation.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/validation.py)
- `DraftReplyPayload`: Pydantic model enforcing all 6 fields, valid action enum (`reply`, `forward`, `escalate`, `no_reply`), confidence bounds (0.0 to 1.0), and `ConfigDict(extra="forbid")`.
- `validate_draft_payload(content: Any, schema: dict[str, Any] | None = None) -> DraftReplyPayload`: Validates response dictionary or raw JSON against schema.
- `DraftValidationError(LLMSchemaValidationError)` & `UnvalidatedDraftError(LLMSchemaValidationError)`: Custom domain exceptions.
- `build_repair_messages(original_messages: list[ChatMessage], invalid_content: Any, validation_error: str, schema: dict[str, Any] | None = None) -> list[ChatMessage]`: Constructs prompt for 1-time schema repair retry.

#### [MODIFY] [__init__.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/__init__.py)
- Re-export `DraftReplyPayload`, `validate_draft_payload`, `DraftValidationError`, `UnvalidatedDraftError`, and `build_repair_messages`.

#### [NEW] [test_draft_validation.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_draft_validation.py)
- Unit tests verifying schema compliance, field validation, rejection of missing fields/extra properties/out-of-bound values, and repair message generation.

---

### Component 2: Observability & Repair Metrics

Emits Prometheus metrics for repair attempts and schema validation outcomes.

#### [MODIFY] [metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/observability/metrics.py)
- Register `draft_repairs_total: Counter` with labels `["status"]` (`"succeeded"`, `"failed"`).
- Register `draft_validation_failures_total: Counter` with labels `["stage"]` (`"initial"`, `"repair"`).

#### [MODIFY] [test_observability_metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_observability_metrics.py)
- Add unit tests verifying registration and emission of repair and validation metrics.

---

### Component 3: SinglePassGenerator Validation & Repair Orchestration

Connects validation and repair retry into generation lifecycle with budget enforcement.

#### [MODIFY] [generator.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/generator.py)
- Update `GenerationResult`: add `is_repaired: bool = False`, `repair_attempts: int = 0`, `validated_payload: DraftReplyPayload | None = None`.
- In `generate_draft`:
  1. Validate initial generation output (`R16.2`).
  2. If validation fails, check repair budget and execute exactly 1 repair retry (`call_kind=CallKind.REPAIR`) with repair instruction (`R16.3`, `R14.9`).
  3. Validate repair output.
  4. On second failure, raise `UnvalidatedDraftError` ensuring unvalidated draft is never persisted (`R16.3`).
  5. Record metrics for validation failures and repair outcomes.

#### [MODIFY] [test_single_pass_generator.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_single_pass_generator.py)
- Update `canned_reply` fixture to include `thread_summary_updated: False` and `model_tier: "routine"`.

#### [MODIFY] [test_complexity_router_integration.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_complexity_router_integration.py)
- Update `canned_reply` fixture to include `thread_summary_updated: False` and `model_tier: "routine"`.

#### [NEW] [test_draft_repair_orchestration.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_draft_repair_orchestration.py)
- End-to-end integration tests for:
  - Valid response on first attempt (0 repairs, 1 generate call).
  - Successful repair retry (1 generate + 1 repair call, budget ceiling respected, `is_repaired=True`).
  - Double failure raises `UnvalidatedDraftError` (never persists unvalidated draft).
  - Call budget exhaustion blocks unauthorized repair retry.

#### [MODIFY] [tasks.md](file:///home/ple/Documents/antigravity/dazzling-bose/specs/tasks.md)
- Check off Task 4.9.

---

## Verification Plan

### Automated Tests
```bash
# 1. Run validation engine unit tests
uv run pytest tests/unit/test_draft_validation.py -v

# 2. Run metrics unit tests
uv run pytest tests/unit/test_observability_metrics.py -v

# 3. Run repair orchestration integration tests
uv run pytest tests/unit/test_draft_repair_orchestration.py -v

# 4. Run existing generator and router regression tests
uv run pytest tests/unit/test_single_pass_generator.py tests/unit/test_complexity_router_integration.py tests/unit/test_generation_budget.py -v

# 5. Full test suite and lint/type checks
uv run pytest tests/unit/ -v
uv run ruff check packages/ tests/
uv run ruff format --check packages/ tests/
uv run mypy packages/
```

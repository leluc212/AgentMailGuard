# Complexity Router & Model Cascade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `ComplexityRouter` as a standalone pipeline stage to route easy emails to `routine` tier (~90%) and escalate complex emails to `high_capability` tier (~10%) based on 5 configurable triggers, with a single-tier ablation switch (`R15.1`–`R15.6`, `design.md §5.7`).

**Architecture:** A standalone `ComplexityRouter` in `packages/llm/router.py` evaluates an incoming `ContextPackage` and triage `Classification` against 5 deterministic criteria (low classification confidence, long/complex thread, insufficient retrieval evidence, multiple requested actions, oversized context). In the default path, it returns a `RoutingDecision` for `routine` tier. If any criterion holds, it escalates to `high_capability` tier, recording the exact `escalation_reason` and capping escalations to max 1 per job (`R15.5`). A config toggle `force_single_tier` forces single-tier operation for H4 empirical comparison (`R15.6`). The caller passes `decision.tier` and `decision.escalation_reason` directly into `SinglePassGenerator.generate_draft(...)`, maintaining clean stage decoupling without multi-agent chaining (`R14.4`).

**Architecture Diagram:**

```mermaid
graph TD
    CP[ContextPackage] --> CR[ComplexityRouter]
    CL[Classification] --> CR
    
    subgraph "Standalone Router Stage (packages/llm/router.py)"
        CR -->|1. check force_single_tier| T0{Forced?}
        T0 -->|Yes| HIGH_F[HIGH_CAPABILITY / single_tier_forced]
        T0 -->|No| T_CAP{Escalations >= Max?}
        T_CAP -->|Yes| ROUTINE_C[ROUTINE / none]
        T_CAP -->|No| T_CRIT{Evaluate 5 Criteria}
        T_CRIT -->|Any Match| HIGH[HIGH_CAPABILITY / reason]
        T_CRIT -->|None Match| ROUTINE[ROUTINE / none]
    end
    
    HIGH --> RD[RoutingDecision]
    HIGH_F --> RD
    ROUTINE --> RD
    ROUTINE_C --> RD
    
    subgraph "Generation Stage (packages/llm/generator.py)"
        RD -->|escalated_tier, escalation_reason| SPG[SinglePassGenerator]
        CP --> SPG
        SPG --> GR[GenerationResult]
    end
    
    CR -->|model_escalations_total{reason, tier}| PM[PipelineMetrics]
```

**Tech Stack:** Python 3.12, Pydantic V2, Tiktoken / TokenCounter, Prometheus Client, Asyncio.

**Spec:** [`specs/tasks.md` Task 4.8](file:///home/ple/Documents/antigravity/dazzling-bose/specs/tasks.md#L421-L426), [`specs/requirements.md` R15.1–R15.6](file:///home/ple/Documents/antigravity/dazzling-bose/specs/requirements.md#L295-L306), [`specs/design.md` §5.7](file:///home/ple/Documents/antigravity/dazzling-bose/specs/design.md#L570-L578).

## Global Constraints

- Routine tier by default (`R15.2`).
- Escalate to high_capability tier on any configured criterion (`R15.3`):
  1. `low_classification_confidence`: `classification.confidence < confidence_threshold`
  2. `complex_thread`: `message_count >= thread_messages_threshold` or `thread_tokens >= thread_tokens_threshold`
  3. `insufficient_retrieval_evidence`: `retrieval_required` and 0 chunks, or fewer than `min_retrieved_chunks` above `min_relevance_score`
  4. `multiple_requested_actions`: multiple questions / action directives detected (`>= multiple_actions_threshold`)
  5. `oversized_context`: total estimated context tokens `> context_tokens_threshold`
- Max one escalation per job (`R15.5`). Escalation **replaces** the generation call tier, never adds one (`design.md §5.7`).
- Expose configuration switch `force_single_tier` for H4 ablation comparison (`R15.6`).
- Record `tier` used and `escalation_reason` (or `"none"`) on every generation result (`R15.4`).
- No live network calls in tests (`CLAUDE.md §8`). All tests use deterministic fixtures and offline stubs.
- Code must pass `ruff check`, `ruff format --check`, and `mypy` strict typing.

---

### Task 1: Configuration Settings (`ComplexityRouterSettings` in `packages/core/settings.py`)

**Files:**
- Modify: `packages/core/settings.py:170-216, 480-530`
- Modify: `packages/core/__init__.py:1-40`
- Modify: `.env.example`
- Modify: `docs/configuration.md`
- Test: `tests/unit/test_complexity_router_settings.py`

**Interfaces:**
- Consumes: `pydantic.BaseModel`, `pydantic.Field`
- Produces: `ComplexityRouterSettings` mounted on `AppSettings.complexity_router`

- [ ] **Step 1: Write the failing tests for `ComplexityRouterSettings`**

Create `tests/unit/test_complexity_router_settings.py` covering:
- Default values: `enabled=True`, `force_single_tier=False`, `default_tier="routine"`, `escalated_tier="high_capability"`, `confidence_threshold=0.75`, `thread_messages_threshold=5`, `thread_tokens_threshold=2000`, `min_retrieved_chunks=2`, `min_relevance_score=0.50`, `multiple_actions_threshold=2`, `context_tokens_threshold=3500`, `max_escalations_per_job=1`.
- Custom environment variable overrides (e.g. `ROUTER_FORCE_SINGLE_TIER=true`, `ROUTER_CONFIDENCE_THRESHOLD=0.80`).
- AppSettings nesting: `app_settings.complexity_router` exists and validates properly.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_complexity_router_settings.py -v`
Expected: FAIL with `ImportError: cannot import name 'ComplexityRouterSettings' from 'packages.core.settings'`.

- [ ] **Step 3: Implement `ComplexityRouterSettings` and update documentation**

In `packages/core/settings.py`:
- Add `class ComplexityRouterSettings(BaseModel)` with all configurable thresholds and defaults.
- Mount onto `AppSettings`: `complexity_router: ComplexityRouterSettings = Field(default_factory=ComplexityRouterSettings)`.
- Re-export in `packages/core/__init__.py`.
- Add Section 19 to `.env.example` and Section 2.19 to `docs/configuration.md`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_complexity_router_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/core/settings.py packages/core/__init__.py .env.example docs/configuration.md tests/unit/test_complexity_router_settings.py
git commit -m "feat(settings): add ComplexityRouterSettings for model cascade [task 4.8] [R15.1, R15.6]"
```

---

### Task 2: EscalationReason, RoutingDecision, and Action Heuristics (`packages/llm/router.py`)

**Files:**
- Create: `packages/llm/router.py`
- Modify: `packages/llm/__init__.py`
- Test: `tests/unit/test_complexity_router.py`

**Interfaces:**
- Consumes: `packages.llm.protocol.ModelTier`
- Produces: `EscalationReason`, `RoutingDecision`, `count_requested_actions`

- [ ] **Step 1: Write the failing tests for data models and action heuristics**

In `tests/unit/test_complexity_router.py`:
- Test `EscalationReason` enum values: `none`, `low_classification_confidence`, `complex_thread`, `insufficient_retrieval_evidence`, `multiple_requested_actions`, `oversized_context`, `single_tier_forced`.
- Test `RoutingDecision` dataclass attributes: `tier`, `model`, `is_escalated`, `escalation_reason`, `details`.
- Test `count_requested_actions` heuristic function:
  - Single question: "Can you help me reset my password?" -> 1 action.
  - Multiple questions: "Where is my invoice? Also, can you update my payment method?" -> 2 actions.
  - Numbered list: "Please: 1. Cancel order 2. Issue refund 3. Delete account" -> 3 actions.
  - Action keywords: "Please send the logs. Additionally, please call me tomorrow." -> 2 actions.
  - Statement without actions: "Thank you for the update." -> 0 actions.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_complexity_router.py -k test_action_heuristics -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.llm.router'`.

- [ ] **Step 3: Implement data models and `count_requested_actions`**

In `packages/llm/router.py`:
- `EscalationReason(StrEnum)`
- `@dataclass(frozen=True) class RoutingDecision`
- `count_requested_actions(text: str) -> int`:
  - Counts question marks (`?`).
  - Counts numbered items (`r"^\s*\d+[\.\)]\s+"`) and bullet points (`r"^\s*[\-\*\•]\s+"`).
  - Counts action transition keywords (`"additionally"`, `"also"`, `"furthermore"`, `"secondly"`, `"as well as"`).
  - Returns deduplicated total action count.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_complexity_router.py -k "test_action_heuristics or test_routing_decision" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/llm/router.py tests/unit/test_complexity_router.py
git commit -m "feat(llm): implement EscalationReason, RoutingDecision, and action heuristics [task 4.8] [R15.3, R15.4]"
```

---

### Task 3: ComplexityRouter Engine & Invariants (`packages/llm/router.py`)

**Files:**
- Modify: `packages/llm/router.py`
- Modify: `packages/llm/__init__.py`
- Test: `tests/unit/test_complexity_router.py`

**Interfaces:**
- Consumes: `ContextPackage`, `Classification`, `ComplexityRouterSettings`, `LLMTiersSettings`, `TokenCounter`, `PipelineMetrics`
- Produces: `ComplexityRouter.route(...) -> RoutingDecision`

- [ ] **Step 1: Write the failing tests for `ComplexityRouter`**

In `tests/unit/test_complexity_router.py`:
- Test default routing to `routine` tier (`R15.2`).
- Test Trigger 1: low classification confidence (`confidence=0.60 < 0.75`) -> escalates with reason `low_classification_confidence` (`R15.3`).
- Test Trigger 2: complex thread (`len(recent_messages) >= 5` or tokens > 2000) -> escalates with reason `complex_thread` (`R15.3`).
- Test Trigger 3: insufficient retrieval evidence (fewer than 2 chunks >= 0.50 score, or 0 chunks when required) -> escalates with reason `insufficient_retrieval_evidence` (`R15.3`).
- Test Trigger 4: multiple requested actions (>= 2 actions detected) -> escalates with reason `multiple_requested_actions` (`R15.3`).
- Test Trigger 5: oversized context (total tokens > 3500) -> escalates with reason `oversized_context` (`R15.3`).
- Test Max Escalation Cap: if `escalations_performed >= 1`, routing returns `routine` tier and does not escalate (`R15.5`).
- Test Forced Single-Tier: if `force_single_tier=True`, returns `high_capability` tier with reason `single_tier_forced` regardless of context complexity (`R15.6`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_complexity_router.py -k "test_complexity_router" -v`
Expected: FAIL because `ComplexityRouter` is not yet implemented.

- [ ] **Step 3: Implement `ComplexityRouter`**

In `packages/llm/router.py`:
- Implement `ComplexityRouter`:
  - `__init__(settings: ComplexityRouterSettings | None = None, tiers_settings: LLMTiersSettings | None = None, token_counter: TokenCounter | None = None, metrics: PipelineMetrics | None = None)`
  - `route(context: ContextPackage, *, classification: Classification | None = None, profile: AgentProfile | None = None, escalations_performed: int = 0) -> RoutingDecision`
  - Logic:
    1. Check `force_single_tier`: if enabled, route to `settings.single_tier_override` (default `high_capability`) with reason `SINGLE_TIER_FORCED`.
    2. Check escalation cap: if `escalations_performed >= settings.max_escalations_per_job`, return default `routine` tier.
    3. Evaluate 5 criteria in deterministic order:
       - Confidence check.
       - Thread message count & thread tokens check.
       - Retrieved chunks check (count & relevance score).
       - Multiple requested actions check.
       - Total context tokens check using `TokenCounter`.
    4. If triggered, return `high_capability` decision and increment metric `metrics.model_escalations_total`.
    5. Else, return `routine` decision with reason `NONE`.
- Re-export `ComplexityRouter`, `EscalationReason`, `RoutingDecision` in `packages/llm/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_complexity_router.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/llm/router.py packages/llm/__init__.py tests/unit/test_complexity_router.py
git commit -m "feat(llm): implement ComplexityRouter and escalation triggers [task 4.8] [R15.1-R15.6]"
```

---

### Task 4: Pipeline Integration, Observability & Quality Gate

**Files:**
- Modify: `packages/observability/metrics.py:65-75, 185-195`
- Modify: `specs/tasks.md:421-426`
- Create: `tests/unit/test_complexity_router_integration.py`

**Interfaces:**
- Consumes: `ComplexityRouter`, `SinglePassGenerator`, `PipelineMetrics`, `ContextPackage`
- Produces: End-to-end routing into generation, metric emission, and `specs/tasks.md` completion

- [ ] **Step 1: Write integration tests connecting ComplexityRouter and SinglePassGenerator**

In `tests/unit/test_complexity_router_integration.py`:
- Test standalone stage pattern:
  - Caller invokes `decision = router.route(context, classification=classification)`.
  - Caller passes `escalated_tier=decision.tier` and `escalation_reason=decision.escalation_reason` into `SinglePassGenerator.generate_draft(...)`.
  - Normal email -> generated at `routine` tier with `escalation_reason="none"`.
  - Escalated email (e.g. low confidence or multiple questions) -> generated at `high_capability` tier with corresponding `escalation_reason`.
  - Exactly 1 generation call is made per job (`R14.9`, `R15.5`).
- Test `force_single_tier`: when enabled in router settings, decision is `high_capability` with `escalation_reason="single_tier_forced"`, and draft is generated at `high_capability`.
- Test metric emission: `model_escalations_total{reason, tier}` counter increments when escalation occurs.
- Test that `GenerationResult` carries `tier`, `escalation_reason`, and `prompt_version` (`R15.4`).

- [ ] **Step 2: Add `model_escalations_total` metric to `PipelineMetrics`**

In `packages/observability/metrics.py`:
- Add `model_escalations_total: Counter` with labels `["reason", "tier"]`.

- [ ] **Step 3: Run all unit and integration tests**

Run: `uv run pytest tests/unit/test_complexity_router_settings.py tests/unit/test_complexity_router.py tests/unit/test_complexity_router_integration.py tests/unit/test_single_pass_generator.py tests/unit/test_generation_budget.py -v`
Expected: All tests PASS.

- [ ] **Step 4: Run linters, formatters, and type checker**

Run:
```bash
uv run ruff check packages/ tests/unit/test_complexity_router*
uv run ruff format --check packages/ tests/unit/test_complexity_router*
uv run mypy packages/ tests/unit/test_complexity_router*
```
Expected: 0 errors across all tools.

- [ ] **Step 5: Mark Task 4.8 complete in `specs/tasks.md` and commit**

Change `- [ ] **4.8 Complexity router & model cascade**` to `- [x] **4.8 Complexity router & model cascade**`.
```bash
git add packages/observability/metrics.py specs/tasks.md tests/unit/test_complexity_router_integration.py
git commit -m "feat(llm): complete complexity router and model cascade integration [task 4.8] [R15.1-R15.6]"
```

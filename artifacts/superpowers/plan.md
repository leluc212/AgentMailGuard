# Implementation Plan: Phase 2 Task 2.6 — Category Taxonomy

### Goal
Implement the canonical enterprise email classification category taxonomy per `specs/tasks.md` Task 2.6, `specs/requirements.md` criterion `R6.4`, and `specs/design.md §5.3`, anchored on the Technical Proposal §11.

### Assumptions
1. `R6.4` requires at minimum the 9 categories: `support`, `sales`, `billing`, `administration`, `scheduling`, `general_inquiry`, `automated_notification`, `acknowledgement`, `no_response`.
2. Architecture rule: `packages/domain` imports standard library and `packages/core` only (zero dependencies on database, broker, or external frameworks).
3. The 9 canonical categories form the immutable baseline taxonomy, while `TaxonomyRegistry` allows tenant-specific extensions or intent customizations.
4. Downstream triage workers (Stage 2 ML, Stage 3 LLM, Cascade) and evaluation schemas reference `packages/domain/taxonomy.py` as single source of truth.

### Plan

1. **Implement Domain Category Taxonomy (`packages/domain/taxonomy.py`, `packages/domain/__init__.py`)**
   - Files: `packages/domain/taxonomy.py`, `packages/domain/__init__.py`
   - Change:
     - Define `Category(StrEnum)` with 9 canonical categories (`support`, `sales`, `billing`, `administration`, `scheduling`, `general_inquiry`, `automated_notification`, `acknowledgement`, `no_response`).
     - Define `CategoryDefinition` dataclass (`category`, `description`, `default_reply_required`, `default_retrieval_required`, `default_workflow_hint`, `default_priority`, `intents`, `auto_send_eligible`, `aliases`).
     - Define `TaxonomyRegistry` allowing tenant-level extensions while preserving the 9 canonical baseline categories.
     - Implement lookup and normalization functions: `validate_category`, `normalize_category`, `is_valid_category`, `get_category_definition`.
     - Export all symbols in `packages/domain/__init__.py`.
   - Verify: `uv run ruff check packages/domain/ && uv run mypy packages/domain/ && uv run pytest tests/unit/test_dependency_rules.py`

2. **Align Triage Worker Classifiers & Evaluation Schemas**
   - Files: `services/triage_worker/classifier.py`, `services/triage_worker/llm_classifier.py`, `evaluation/datasets/schemas.py`
   - Change:
     - Update `services/triage_worker/classifier.py` to import `NO_REPLY_CATEGORIES` and `RETRIEVAL_CATEGORIES` from `packages.domain.taxonomy`.
     - Update `services/triage_worker/llm_classifier.py` to use `CANONICAL_CATEGORIES` and `normalize_category` from `packages.domain.taxonomy`.
     - Update `evaluation/datasets/schemas.py` to alias or wrap `packages.domain.taxonomy.Category`.
   - Verify: `uv run ruff check services/triage_worker/ evaluation/ && uv run mypy services/triage_worker/ evaluation/`

3. **Author Taxonomy Unit Test Suite (`tests/unit/test_category_taxonomy.py`)**
   - Files: `tests/unit/test_category_taxonomy.py`
   - Change:
     - Validate all 9 canonical categories from R6.4 exist and have expected string values.
     - Validate category definitions, intent taxonomies, default routing flags, and aliases.
     - Validate category normalization and validation rules.
     - Validate `TaxonomyRegistry` operations (default registry, lookup, custom category registration, immutability of canonical set).
     - Validate domain boundary compliance (stdlib/core imports only).
   - Verify: `uv run pytest tests/unit/test_category_taxonomy.py -v`

4. **Regression Verification & Task 2.6 Sign-off**
   - Files: `specs/tasks.md`, `artifacts/superpowers/execution.md`, `artifacts/superpowers/finish.md`
   - Change:
     - Run full unit and integration test suites.
     - Verify 0 ruff errors, 0 mypy strict type errors.
     - Mark Task 2.6 complete (`[x]`) in `specs/tasks.md`.
     - Update execution and finish documentation.
   - Verify: `uv run pytest tests/unit tests/integration -q && uv run ruff check . && uv run mypy packages services tests`

### Risks & mitigations
- **Risk**: Hardcoded strings across existing tests or workers might conflict if casing or whitespace normalization isn't applied.
  - **Mitigation**: `normalize_category` strips whitespace, converts to lowercase, and maps aliases before validation.
- **Risk**: Domain boundary violation if non-stdlib packages are imported in `packages/domain/taxonomy.py`.
  - **Mitigation**: Use stdlib `enum.StrEnum`, `dataclasses`, and `typing`. Verified by `test_dependency_rules.py`.

### Rollback plan
- Revert git changes via `git checkout -- packages/ services/ evaluation/ tests/`.

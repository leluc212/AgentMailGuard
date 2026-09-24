# Implementation Plan: Task 3.13 Retrieval Query Builder

## Goal
Implement **Task 3.13: Retrieval query builder** (`packages/retrieval/query_builder.py`), fulfilling requirements **R12.1, R12.2, R12.3, R12.4, R12.5, R12.6**:
- Construct `RetrievalQuery` from `current email + thread summary + classification intent` without extra LLM calls (R12.1, R12.5).
- Produce semantic query text and discrete lexical keywords as separate outputs (R12.2).
- Extract structured identifiers (`INV-...`, `ORD-...`, `TICKET-...`, `SKU-...`, `CONT-...`, `INC-...`) via configurable regex and inject them into lexical query (R12.3).
- Derive tenant, category, and metadata filters from classification (R12.4).
- Support serialization to/from dictionary for durable job persistence and evaluation replay (R12.6).
- Support degraded Phase 3 operation (`thread_summary=None`) while providing parameter for Phase 4 wiring.
- Explicitly test that an identifier-bearing email (e.g. `INV-2026-01829`) retrieves the correct chunk where vector-only retrieval fails.

---

## Architecture & Data Flow

```
NormalizedMessage + Classification (+ thread_summary: str | None)
                               │
                               ▼
                ┌───────────────────────────────┐
                │ RetrievalQueryBuilder         │
                ├───────────────────────────────┤
                │ 1. Regex Identifier Extractor │ ──► identifiers: ["INV-2026-01829", ...]
                │ 2. Salient Keyword Extractor  │ ──► lexical_terms: ["discrepancy", "addon"]
                │ 3. Semantic Composer          │ ──► semantic_text: "Intent: ... Email: ..."
                │ 4. Filter Synthesizer         │ ──► filters: {org_id, category, status}
                └───────────────────────────────┘
                               │
                               ▼
                         RetrievalQuery
                   (to_dict / from_dict enabled)
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
      Lexical Branch                        Vector Branch
   (Exact Match on INV-...)             (Semantic Similarity)
```

---

## Assumptions
1. Phase 3 executes in degraded mode where `thread_summary` is optional (`thread_summary: str | None = None`), ready to be provided by `thread_state` in Phase 4 without breaking the interface.
2. The default query construction path is purely deterministic (regex + stopword filtering + string assembly), requiring 0 LLM calls (R12.5).
3. Identifier patterns default to invoice (`INV`, `INVOICE`), order (`ORD`, `ORDER`), ticket (`TICK`, `TICKET`, `TKT`), SKU (`SKU`), container (`CONT`, `CONTAINER`), and incident (`INC`, `INCIDENT`), but can be extended or overridden via `QueryBuilderConfig`.
4. In `RetrievalQuery`, `to_dict()` and `from_dict()` ensure lossless persistence for job records and replay debugging (R12.6).

---

## Plan

### Step 1: Extend `RetrievalQuery` with Serialization
- **Files:** `packages/retrieval/models.py`
- **Changes:**
  - Add `to_dict()` method returning JSON-serializable dictionary.
  - Add `@classmethod from_dict(cls, data: dict[str, Any]) -> RetrievalQuery`.
- **Verify:** `uv run python -c "from packages.retrieval.models import RetrievalQuery; q = RetrievalQuery(semantic_text='test'); assert RetrievalQuery.from_dict(q.to_dict()).semantic_text == 'test'"`

### Step 2: Implement `RetrievalQueryBuilder` & Regex Extractor
- **Files:** `packages/retrieval/query_builder.py`, `packages/retrieval/__init__.py`
- **Changes:**
  - Define `DEFAULT_IDENTIFIER_PATTERNS` dictionary covering `invoice`, `order`, `ticket`, `sku`, `container`, `incident`.
  - Define `QueryBuilderConfig` (custom regex patterns, stopwords set, category filter mapping, max keyword count).
  - Implement `RetrievalQueryBuilder`:
    - `extract_identifiers(text: str) -> list[str]`
    - `extract_lexical_keywords(text: str) -> list[str]`
    - `compose_semantic_text(subject: str, body: str, intent: str | None, thread_summary: str | None) -> str`
    - `build(...) -> RetrievalQuery` accepting either `(message, classification, thread_summary=None)` or primitive arguments (`subject`, `body_text`, `category`, `intent`, `organization_id`).
  - Export new symbols in `packages/retrieval/__init__.py`.
- **Verify:** `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/query_builder.py`

### Step 3: Implement Comprehensive Unit Tests & Retrieval Verification
- **Files:** `tests/unit/test_query_builder.py`
- **Changes:**
  - Test identifier extraction across all patterns (`INV-2026-01829`, `ORD-82915`, `TICKET-4401`, `SKU-992-A`, `CONT-1002`, `INC-9912`).
  - Test lexical keyword extraction with stopword removal and deduplication.
  - Test semantic text composition with and without `thread_summary` (Phase 3 degraded vs Phase 4).
  - Test filter synthesis (`organization_id`, `category`, `status='active'`).
  - Test `to_dict()` / `from_dict()` serialization fidelity (R12.6).
  - Test zero-LLM assertion (pure synchronous/deterministic, sub-millisecond execution).
  - **Explicit Retrieval Test:** Construct an email containing `INV-2026-01829`. Query `FakeSearchBackend` configured such that vector branch prefers a generic procedure doc, but lexical branch with the extracted identifier retrieves the exact invoice record. Assert that hybrid RRF fusion selects the exact invoice chunk as #1 candidate, proving identifier superiority.
- **Verify:** `uv run pytest tests/unit/test_query_builder.py --cov=packages.retrieval.query_builder --cov-report=term-missing -v` (100% coverage target).

### Step 4: Architectural Integrity & Full Regression Validation
- **Files:** N/A
- **Changes:**
  - Run architectural boundary checks: `uv run pytest tests/unit/test_dependency_rules.py -v`.
  - Run full regression suite: `uv run pytest -m "not slow" -q`.
  - Run static analysis: `uv run ruff check packages/ tests/ && uv run mypy packages/retrieval/ tests/unit/test_query_builder.py`.
- **Verify:** All tests pass and zero linter/type issues.

### Step 5: Update Task Queue & Git Commit
- **Files:** `specs/tasks.md`
- **Changes:**
  - Mark Task 3.13 `[x]` in `specs/tasks.md`.
  - Commit: `feat(retrieval): retrieval query builder and identifier extraction [task 3.13] [R12.1, R12.2, R12.3, R12.4, R12.5, R12.6]`.
- **Verify:** `git log -1 --stat`

---

## Risks & Mitigations
- **Risk:** Overly aggressive regexes matching false positive strings.
  - *Mitigation:* Bounded word boundaries (`\b`), specific standard prefix tags (`INV`, `ORD`, `TICKET`, `SKU`, `CONT`, `INC`), and configurable pattern dictionaries allow tenant-specific overrides without code changes.
- **Risk:** Missing thread context in Phase 3.
  - *Mitigation:* Explicitly designed with `thread_summary: str | None = None` parameter so Phase 3 degrades gracefully without thread state, while Phase 4 can plug into the exact same signature.

---

## Rollback Plan
If issues arise, `packages/retrieval/query_builder.py` and `tests/unit/test_query_builder.py` can be removed and `packages/retrieval/models.py` reverted via `git checkout`.

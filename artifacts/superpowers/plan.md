# Implementation Plan: Task 3.12 Context Packing

## Goal
Implement **Task 3.12: Context packing** (`packages/retrieval/packing.py`), fulfilling requirements **R11.3** and **R11.4**:
- Pass a configurable `top-K` to generation, defaulting to 4–6 chunks (default 5).
- Enforce a maximum retrieved-context token budget (hard ceiling).
- Truncate strictly at chunk boundaries — never mid-chunk.
- Provide clean citation IDs and structured formatting for prompt assembly.

---

## Architecture & Data Flow

```
Fused / Reranked Candidates
             │
             ▼
┌─────────────────────────┐
│ PackingPolicy / Config  │  ◄── [top_k (default 5), token_budget (default 2048)]
└─────────────────────────┘
             │
             ▼
┌─────────────────────────┐
│ ContextPacker           │  ◄── TokenCounter (tiktoken BPE / fallback)
│ - Candidate slice [:top_k]
│ - Sequential token check│
│ - Strict chunk boundary │
│   truncation (never mid)│
└─────────────────────────┘
             │
             ▼
┌─────────────────────────┐
│ PackedContext           │
│ - chunks: [PackedChunk] │
│ - total_tokens          │
│ - truncated_by_budget   │
│ - format_knowledge_sec()│
└─────────────────────────┘
```

---

## Assumptions
1. Context packing operates on `Sequence[Candidate]` produced by `SearchBackend` / `HybridRetriever` / `RerankService`.
2. Token counting uses `TokenCounter` (`packages/knowledge/token_counter.py` backed by `tiktoken` BPE `cl100k_base`) with support for a `TokenCounterProtocol` to allow mocking or custom tokenizers.
3. If a chunk's addition would breach the `token_budget`, packing terminates at that chunk boundary. No partial or sliced text is ever included (R11.4).
4. Default `top_k` is 5 (satisfying the 4–6 requirement in R11.3 and design.md §5.5). Default `token_budget` is 2048 tokens.
5. All operations are pure, in-memory, deterministic, and hermetic without network access.

---

## Plan

### Step 1: Define Context Packing Models & Protocols
- **Files:** `packages/retrieval/packing.py`, `packages/retrieval/__init__.py`
- **Changes:**
  - Create `PackedChunk` dataclass: `chunk_id`, `document_id`, `content`, `token_count`, `citation_id`, `metadata`, `fused_score`, `rerank_score`.
  - Create `PackedContext` dataclass: `chunks`, `total_tokens`, `token_budget`, `top_k`, `truncated_by_budget`, `candidate_count_initial`, `candidate_count_selected`, and `format_knowledge_section()`.
  - Create `TokenCounterProtocol` (Protocol for `count_tokens(text: str) -> int`).
  - Create `PackingConfig` dataclass supporting `default_top_k` (default 5), `default_token_budget` (default 2048), and per-tenant / per-category override maps.
- **Verify:** `uv run python -c "from packages.retrieval.packing import PackedChunk, PackedContext; print('PackedChunk loaded')"`

### Step 2: Implement `ContextPacker` Logic
- **Files:** `packages/retrieval/packing.py`
- **Changes:**
  - Implement `ContextPacker`:
    - `__init__(token_counter: TokenCounterProtocol | None = None, config: PackingConfig | None = None)`
    - `pack(candidates: Sequence[Candidate], *, top_k: int | None = None, token_budget: int | None = None, organization_id: str | None = None, category: str | None = None, citation_prefix: str = "") -> PackedContext`
    - Resolves effective `top_k` and `token_budget`.
    - Iterates over candidates up to `top_k`.
    - Computes token count for each chunk.
    - If `accumulated_tokens + chunk_tokens > token_budget`: marks `truncated_by_budget = True` and breaks without adding the chunk (chunk-boundary truncation).
    - Assembles `PackedChunk` instances with sequential citations (e.g., `[1]`, `[2]`, ...).
  - Export all new classes and functions in `packages/retrieval/__init__.py`.
- **Verify:** `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/packing.py`

### Step 3: Implement Comprehensive Unit Test Suite
- **Files:** `tests/unit/test_packing.py`
- **Changes:**
  - Test default top-K (5 chunks returned when budget permits).
  - Test custom top-K overriding defaults.
  - Test hard token budget truncation at chunk boundaries:
    - Candidate 1 (200 tokens) + Candidate 2 (300 tokens) fit in 600 budget.
    - Candidate 3 (200 tokens) would breach 600 budget -> excluded, `truncated_by_budget=True`.
    - Assert candidate 3 content does NOT appear in result or partial form.
  - Test edge cases:
    - First candidate exceeds total budget -> 0 chunks returned, strictly no mid-chunk text.
    - Empty candidate list -> empty context with 0 tokens.
    - Budget exactly equals sum of tokens -> all fit, `truncated_by_budget=False`.
  - Test citation generation and formatting (`format_knowledge_section()`).
  - Test tenant and category policy overrides.
  - Test integration with `TokenCounter` and custom mock counters.
- **Verify:** `uv run pytest tests/unit/test_packing.py --cov=packages.retrieval.packing --cov-report=term-missing -v` (100% coverage target).

### Step 4: Architectural Integrity & Full Regression Validation
- **Files:** N/A
- **Changes:**
  - Run architectural boundary suite: `uv run pytest tests/unit/test_dependency_rules.py -v`.
  - Run full non-slow regression suite: `uv run pytest -m "not slow" -q`.
  - Run static analysis: `uv run ruff check packages/ tests/ && uv run mypy packages/retrieval/ tests/unit/test_packing.py`.
- **Verify:** All tests pass and zero linter/type errors.

### Step 5: Update Task Queue & Git Commit
- **Files:** `specs/tasks.md`
- **Changes:**
  - Mark Task 3.12 `[x]` in `specs/tasks.md`.
  - Git commit: `feat(retrieval): context packing with hard token budget [task 3.12] [R11.3, R11.4]`.
- **Verify:** `git log -1 --stat`

---

## Risks & Mitigations
- **Risk:** Token counting divergence between tiktoken and fallback if tiktoken model files are not locally cached in offline environments.
  - *Mitigation:* `TokenCounter` in `packages/knowledge/token_counter.py` already includes a resilient whitespace-heuristic fallback (`max(1, int(len(words) * 1.33))`), and `ContextPacker` accepts any injected `TokenCounterProtocol` for deterministic testing.
- **Risk:** Mid-chunk slicing if an implementer tries to "fit as much as possible".
  - *Mitigation:* Strict invariant: either the entire chunk fits, or it is rejected entirely (R11.4). Verified explicitly with unit tests.

---

## Rollback Plan
If issues arise, `packages/retrieval/packing.py` and `tests/unit/test_packing.py` can be removed and `packages/retrieval/__init__.py` reverted via `git checkout`.

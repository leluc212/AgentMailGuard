# Implementation Plan — Phase 3, Task 3.7: SearchBackend Interface

### Goal
Implement `SearchBackend` Protocol, `Candidate` and `RetrievalQuery` data models, `FakeSearchBackend` in-memory test implementation, and the shared `SearchBackendContractSuite` contract test harness per `specs/tasks.md Task 3.7` and `specs/requirements.md R10.7, R10.8`.

### Assumptions
- `packages/retrieval` is the designated home for search abstractions and models per `design.md §4`.
- `SearchBackend` hides retrieval mechanics from pipeline code (`lexical` and `vector` methods), enabling drop-in backends (`PostgresSearchBackend`, `OpenSearchBackend`).
- `Candidate` carries both ranks and both scores, plus chunk/doc identifiers and source metadata per R10.8.

### Plan
1. Implement Data Models (`RetrievalQuery`, `Candidate`)
   - Files: `packages/retrieval/models.py`
   - Change: Define `RetrievalQuery` with semantic text, lexical terms, identifiers, filters, and optional query vector. Define `Candidate` with chunk/doc IDs, content, metadata, lexical_rank/score, vector_rank/score, fused_score, rerank_score (R10.8).
   - Verify: `uv run pytest tests/unit/test_retrieval_models.py -v`

2. Implement SearchBackend Protocol
   - Files: `packages/retrieval/protocol.py`, `packages/retrieval/__init__.py`
   - Change: Define runtime-checkable `SearchBackend` protocol declaring `lexical(q, top_n)` and `vector(q, top_n)` (R10.7). Re-export models and protocol in package `__init__.py`.
   - Verify: `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/`

3. Implement FakeSearchBackend & Contract Test Suite
   - Files: `packages/retrieval/fake.py`, `packages/retrieval/testing.py`
   - Change: Implement `FakeSearchBackend` supporting multi-tenant isolation, status/category filtering, keyword/identifier matching, and vector cosine similarity. Implement abstract `SearchBackendContractSuite(ABC)` with comprehensive test assertions.
   - Verify: `uv run ruff check packages/retrieval/ && uv run mypy packages/retrieval/`

4. Unit Tests for Contract Suite and Models
   - Files: `tests/unit/test_retrieval_models.py`, `tests/unit/test_search_backend_contract.py`
   - Change: Write tests for models and inherit `SearchBackendContractSuite` using `FakeSearchBackend`.
   - Verify: `uv run pytest tests/unit/test_retrieval_models.py tests/unit/test_search_backend_contract.py -v`

5. Verification and Full Suite Regression Run
   - Files: `specs/tasks.md`
   - Change: Run dependency rules test, linter, and full test suite.
   - Verify: `uv run pytest tests/unit/test_dependency_rules.py -v && uv run pytest -m "not slow" -q`

### Risks & mitigations
- **Risk:** Filtered queries returning chunks from other tenants.
  - **Mitigation:** Mandatory `organization_id` filter check in `FakeSearchBackend` and contract suite test cases explicitly verifying tenant isolation across multiple organizations.
- **Risk:** Misalignment between `Candidate` fields and downstream RRF / reranking expectations.
  - **Mitigation:** Exact adherence to `specs/design.md §5.5` definition of `Candidate`.

### Rollback plan
Revert newly created files in `packages/retrieval/` and `tests/unit/test_search_backend_contract.py`, `test_retrieval_models.py`.

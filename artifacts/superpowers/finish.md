# Phase 2 Task 2.4: Small-LLM Fallback (Triage Stage 3) — Finish Summary

## 1. Summary of Changes

- **LLM Abstractions & Protocol (`packages/llm/protocol.py`)**:
  - Defined `ModelTier` enum (`FAST`, `ROUTINE`, `STRONG`, `HIGH_CAPABILITY`, `FALLBACK`) per `R15` and `design.md §5.7`.
  - Defined `ChatMessage` dataclass (`role: str`, `content: str`).
  - Defined `LLMResult` dataclass (`content: dict`, `model: str`, `tier: ModelTier`, `input_tokens: int`, `output_tokens: int`, `latency_ms: int`, `raw_finish_reason: str`).
  - Defined `LLMProvider` runtime-checkable protocol with `async def generate(self, *, messages, schema, tier, max_tokens, temperature) -> LLMResult`.
  - Defined LLM exception hierarchy: `LLMError`, `LLMTimeoutError`, `LLMResponseError`, `LLMSchemaValidationError`.
- **Deterministic FakeLLMProvider (`packages/llm/fake.py`)**:
  - Implemented `FakeLLMProvider` complying with `LLMProvider` protocol for offline, hermetic, credential-free CI and testing per `GEMINI.md §8`.
  - Supports configurable default responses, FIFO queued canned responses, dynamic responder callbacks, failure injection, latency simulation, and call inspection (`recorded_calls`).
- **HTTP LLM Client (`packages/llm/client.py`)**:
  - Implemented `HttpLLMProvider` using `httpx.AsyncClient` supporting OpenAI / LiteLLM-compatible `/chat/completions` API.
  - Implemented tier-to-model resolution (`FAST` / `ROUTINE` -> `gpt-4o-mini`, `STRONG` / `HIGH_CAPABILITY` -> `gpt-4o`, `FALLBACK` -> `claude-3-haiku`).
  - Enforced structured JSON schema outputs (`response_format={"type": "json_schema", ...}`) and payload validation.
  - Strictly encapsulated all vendor SDK interactions within `packages/llm/` per `GEMINI.md §4`.
- **Stage 3 LLM Classifier (`services/triage_worker/llm_classifier.py`)**:
  - Defined Pydantic schema `LLMTriageOutput` enforcing the 9 canonical categories (`R6.4`), priority cues, reply_required, workflow_hint (`ai`, `template`, `none`), retrieval_required, and confidence bounds [0.0, 1.0].
  - Authored concise system prompt and `prepare_triage_prompt(ctx)` handling body truncation to 2000 chars and header extraction (`Auto-Submitted`, `List-Unsubscribe`).
  - Implemented `LLMTriageClassifier` with `classify(context)` and `classify_sync(context)` mapping outputs to domain `Classification` entity with `decided_by='llm'`.
  - Implemented `safe_default(error_message)` helper meeting `R6.11` safe fallback with review flag.
- **Automated Tests**:
  - Created `tests/unit/test_llm_provider.py` (11 tests) testing protocol conformance, FakeLLMProvider mock facilities, and HttpLLMProvider with `httpx.MockTransport` covering success, timeout, HTTP 429, and invalid JSON.
  - Created `tests/unit/test_triage_stage3.py` (12 tests) testing `LLMTriageOutput` validation, category synonym normalization, prompt formatting, end-to-end `LLMTriageClassifier` async and sync invocation, multi-type context coercion, exception handling, and safe default fallback.
- **Task Verification**: Marked Task 2.4 complete in `specs/tasks.md`.

---

## 2. Review Pass (Blocker / Major / Minor / Nit)

- **Blocker**: None.
- **Major**: None.
- **Minor**: None.
- **Nit**: None. All 160 source files pass `ruff check` and `mypy --strict`.

---

## 3. Verification Commands Run & Results

| Verification Target | Command | Result |
|---|---|---|
| Provider Protocol & Unit Tests | `uv run pytest tests/unit/test_llm_provider.py -v` | PASS (11/11 passed in 0.92s) |
| Stage 3 Classifier Unit Tests | `uv run pytest tests/unit/test_triage_stage3.py -v` | PASS (12/12 passed in 0.90s) |
| Architectural Boundaries Guard | `uv run pytest tests/unit/test_dependency_rules.py -v` | PASS (4/4 passed) |
| Full Test Suite | `uv run pytest tests/unit tests/integration -q` | PASS (436/436 passed in 18.2s) |
| Code Style & Strict Types | `uv run ruff check . && uv run mypy packages services tests` | PASS (0 errors, 160 files clean) |

---

## 4. Manual Validation Steps

To verify Stage 3 LLM classification locally using `FakeLLMProvider`:
```bash
uv run python -c "
from services.triage_worker.llm_classifier import LLMTriageClassifier
from packages.llm.fake import FakeLLMProvider
from packages.domain.rules import EmailContext

fake_llm = FakeLLMProvider(default_response={
    'category': 'administration',
    'intent': 'password_reset',
    'priority': 'normal',
    'reply_required': True,
    'workflow_hint': 'ai',
    'retrieval_required': True,
    'confidence': 0.95,
})

clf = LLMTriageClassifier(provider=fake_llm)
ctx = EmailContext(
    subject='Cannot log into customer portal',
    body_text='My team member is locked out of their account. Can you help reset their credentials?',
)
res = clf.classify_sync(ctx)
print(f'Category: {res.category}, Intent: {res.intent}, Priority: {res.priority}, DecidedBy: {res.decided_by}, Model: {res.model}')
"
```

---

## 5. Follow-Ups

- Next task in queue is **Phase 2 Task 2.5: Cascade orchestration & thresholds** (`R6.2, R6.7, R6.9, R6.11`) integrating Stage 1 (Rules), Stage 2 (ML), and Stage 3 (LLM) into a unified triage pipeline.

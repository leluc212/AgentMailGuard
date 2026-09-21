# Implementation Plan — Phase 2 Task 2.4: Small-LLM Fallback (Triage Stage 3)

Implement Stage 3 of the cascading triage engine: a structured-output classification call through `LLMProvider` using a concise prompt and strict schema, adhering to `R6.1, R6.3`, `design.md §5.3, §5.7`, and the non-negotiable rule that all model calls go through `LLMProvider` without SDK imports outside `packages/llm/`.

---

### Goal

1. Define `LLMProvider` protocol, `ModelTier`, `ChatMessage`, and `LLMResult` in `packages/llm/protocol.py` matching `design.md §5.7`.
2. Build `FakeLLMProvider` in `packages/llm/fake.py` for deterministic, offline, credential-free CI and testing per `GEMINI.md §8`.
3. Build `HttpLLMProvider` in `packages/llm/client.py` using `httpx.AsyncClient` supporting JSON schema structured outputs and tiered model resolution (`gpt-4o-mini` for routine fast triage per `LLMTiersSettings`).
4. Build `services/triage_worker/llm_classifier.py` (`LLMTriageClassifier`) with strict `LLMTriageOutput` schema, concise prompt, and structured `Classification` generation (`decided_by="llm"`).
5. Author automated test suites in `tests/unit/test_llm_provider.py` and `tests/unit/test_triage_stage3.py` verifying protocol conformance, structured classification, priority routing, early-exit flags, and malformed output handling.

---

### Assumptions

1. Stage 3 is invoked only when Stage 1 rules and Stage 2 ML confidence are below their configured thresholds (or for ambiguous queries in the cascade).
2. Per `LLMTiersSettings`, the fast model for Stage 3 triage is `gpt-4o-mini` (routine tier, ~200–300 ms, low cost).
3. Per `GEMINI.md §8`, no test requires live credentials or external network access; tests use `FakeLLMProvider` or mock HTTP transports.
4. Per `GEMINI.md §4`, all model calls go through `LLMProvider`; no SDK imports appear outside `packages/llm/`.

---

### Plan

1. **Define LLMProvider protocol and types**
   - Files: `packages/llm/protocol.py`, `packages/llm/__init__.py`
   - Change:
     - Define `ModelTier` enum (`FAST = "fast"`, `STRONG = "strong"`, `FALLBACK = "fallback"`).
     - Define `ChatMessage` (`role: str`, `content: str`).
     - Define `LLMResult` dataclass (`content: dict[str, Any]`, `model: str`, `tier: ModelTier`, `input_tokens: int`, `output_tokens: int`, `latency_ms: int`, `raw_finish_reason: str`).
     - Define `LLMProvider` protocol with `async def generate(self, *, messages, schema, tier, max_tokens, temperature) -> LLMResult`.
   - Verify:
     - Run `uv run mypy packages/llm/`.

2. **Implement FakeLLMProvider for offline testing**
   - Files: `packages/llm/fake.py`, `packages/llm/__init__.py`
   - Change:
     - Implement `FakeLLMProvider` implementing `LLMProvider`.
     - Supports configurable canned responses, dynamic schema-validating responders, latency simulation, and call recording (`recorded_calls`).
   - Verify:
     - Run test checking `isinstance(FakeLLMProvider(), LLMProvider)` and calling `generate()`.

3. **Implement HttpLLMProvider (OpenAI-compatible / LiteLLM)**
   - Files: `packages/llm/client.py`, `packages/llm/__init__.py`
   - Change:
     - Implement `HttpLLMProvider` using `httpx.AsyncClient`.
     - Supports JSON schema enforcement (`response_format={"type": "json_object"}` or OpenAI structured output), token usage extraction, latency measurement, and timeout/retry handling.
   - Verify:
     - Run unit tests with `httpx.MockTransport`.

4. **Implement Stage 3 LLMTriageClassifier**
   - Files: `services/triage_worker/llm_classifier.py`
   - Change:
     - Define Pydantic schema `LLMTriageOutput` for strict validation (category enum across all 9 R6.4 categories, intent, priority, reply_required, workflow_hint, retrieval_required, confidence, reasoning).
     - Implement short system prompt and message formatter (`prepare_triage_prompt(subject, body, sender, headers)`).
     - Implement `LLMTriageClassifier` with `async def classify(context) -> Classification` and `classify_sync(context) -> Classification`.
     - Populate domain `Classification` entity with `decided_by="llm"`, model name, latency, and full output snapshot.
   - Verify:
     - Run test invoking `LLMTriageClassifier` with `FakeLLMProvider`.

5. **Author comprehensive automated test suite**
   - Files: `tests/unit/test_llm_provider.py`, `tests/unit/test_triage_stage3.py`
   - Change:
     - Test `LLMProvider` protocol, `FakeLLMProvider`, and `HttpLLMProvider`.
     - Test `LLMTriageClassifier` across all categories, priorities, and workflow hints.
     - Test error handling when LLM returns invalid JSON or schema violation.
     - Verify architectural boundary rules (no SDK imports outside `packages/llm/`).
   - Verify:
     - Run `uv run pytest tests/unit/test_triage_stage3.py tests/unit/test_llm_provider.py -v`.
     - Run full test suite: `uv run pytest tests/unit tests/integration`.

6. **Update task checklist**
   - Files: `specs/tasks.md`
   - Change:
     - Mark task 2.4 `[x]` upon all passing tests and verified Definition of Done.
   - Verify:
     - Review `git diff specs/tasks.md`.

---

### Risks & mitigations

- **Risk:** LLM output schema violations or JSON formatting errors.
  - *Mitigation:* Strict Pydantic V2 schema validation (`LLMTriageOutput.model_validate`); if validation fails, error is logged and caught so Stage cascade can apply safe default review flag per R6.11.
- **Risk:** Latency spikes exceeding budget.
  - *Mitigation:* Uses `tier=ModelTier.FAST` (`gpt-4o-mini`) with `max_tokens=250` and concise prompt to keep inference under 200–300 ms.
- **Risk:** Coupling pipeline code to specific LLM vendor SDKs.
  - *Mitigation:* Protocol-first design (`LLMProvider`). All vendor interactions are encapsulated in `packages/llm/` using standard HTTP protocols.

---

### Rollback plan

If issues occur:
1. Revert changes to `packages/llm/` and `services/triage_worker/llm_classifier.py`.
2. Existing Stage 1 (Rules) and Stage 2 (ML) remain fully functional and unimpacted.

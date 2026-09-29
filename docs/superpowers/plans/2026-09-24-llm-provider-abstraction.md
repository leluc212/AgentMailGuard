# LLMProvider Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the model-agnostic `LLMProvider` abstraction, supporting at least two provider implementations (hosted API: OpenAI and Anthropic; local endpoint: OpenAI-compatible/Ollama/vLLM) plus a deterministic offline stub (`FakeLLMProvider` for CI), configurable model tiers, and a shared contract test suite (`LLMProviderContractSuite`).

**Architecture:** All model access throughout the enterprise email processing pipeline goes strictly through the runtime-checkable `LLMProvider` protocol without third-party vendor SDK dependencies outside `packages/llm` (per CLAUDE.md §4). Providers implement async `generate(messages, schema, tier, **params) -> LLMResult`, tracking latency, input/output tokens, and schema conformance. A provider factory instantiates the configured provider from validated Pydantic settings. A reusable `LLMProviderContractSuite` enforces identical behavioral contracts across hosted, local, and stub implementations.

**Architecture Diagram:**

```mermaid
graph TD
    subgraph "Application Layer"
        Triage[Triage Worker]
        Summary[Summarizer]
        Gen[Context & Generation]
    end

    subgraph "packages/llm Abstraction"
        Factory[create_llm_provider Factory]
        Protocol[LLMProvider Protocol]
        Result[LLMResult: content, model, tier, tokens, latency]
    end

    subgraph "Implementations"
        Fake[FakeLLMProvider - Offline CI Stub]
        OpenAI[OpenAILLMProvider - Hosted OpenAI API]
        Anthropic[AnthropicLLMProvider - Hosted Claude API]
        Local[LocalLLMProvider - OpenAI-Compatible Local Endpoint]
    end

    subgraph "Testing & Verification"
        Suite[LLMProviderContractSuite]
    end

    Triage --> Factory
    Summary --> Factory
    Gen --> Factory
    Factory --> Protocol

    Protocol <|.. Fake
    Protocol <|.. OpenAI
    Protocol <|.. Anthropic
    Protocol <|.. Local

    Fake --> Result
    OpenAI --> Result
    Anthropic --> Result
    Local --> Result

    Suite -.->|validates contract| Fake
    Suite -.->|validates contract| OpenAI
    Suite -.->|validates contract| Anthropic
    Suite -.->|validates contract| Local
```

**Tech Stack:** Python 3.12+, `httpx` (async HTTP client with `MockTransport`), `pydantic` & `pydantic-settings` V2, `pytest` & `pytest-asyncio`.

**Spec:** `specs/tasks.md` Task 4.5, citing `specs/requirements.md` criteria `R14.5`, `R14.7`, `R24.5`, and `specs/design.md` §5.7 & §1083.

---

## Global Constraints

- Provider names and vendor SDKs appear ONLY inside `packages/llm/` (CLAUDE.md §4).
- No test requires live credentials or external network access. All CI external calls must be stubbed or use `httpx.MockTransport` (CLAUDE.md §8, R24.5).
- Exactly one `LLMResult` returned per generation with mandatory fields: `content` (dict), `model` (str), `tier` (ModelTier), `input_tokens` (int), `output_tokens` (int), `latency_ms` (int), `raw_finish_reason` (str) (R14.5, design.md §5.7).
- Configuration keys must be documented in `.env.example` and `docs/configuration.md` (CLAUDE.md §3).

---

## Task 1: Protocol Enhancements & Protocol Types

**Files:**
- Modify: `packages/llm/protocol.py`
- Modify: `packages/llm/__init__.py`
- Test: `tests/unit/test_llm_provider.py`

**Interfaces:**
- Consumes: `ModelTier` (`FAST`, `ROUTINE`, `STRONG`, `HIGH_CAPABILITY`, `FALLBACK`), `ChatMessage`
- Produces: `LLMProvider.generate(messages, schema, tier, max_tokens, temperature, **params) -> LLMResult` accepting arbitrary runtime kwargs (`**params: Any`) per R14.5

- [ ] **Step 1: Write failing test for `**params` in `LLMProvider` Protocol & `FakeLLMProvider`**
  - Add test asserting `await provider.generate(..., custom_param="test_val")` forwards kwargs and records them in `recorded_calls`.
- [ ] **Step 2: Run test to verify failure**
  - Run `uv run pytest tests/unit/test_llm_provider.py -k test_extra_params` to confirm signature failure.
- [ ] **Step 3: Update `packages/llm/protocol.py` and `packages/llm/fake.py`**
  - Add `**params: Any` to `LLMProvider.generate()` protocol method.
  - Update `FakeLLMProvider.generate()` to accept `**params: Any` and record in `recorded_calls`.
- [ ] **Step 4: Run test to verify pass**
  - Run `uv run pytest tests/unit/test_llm_provider.py` and verify all tests pass.
- [ ] **Step 5: Commit changes**
  - `git commit -m "feat(llm): add arbitrary params support to LLMProvider protocol [task 4.5] [R14.5]"`

---

## Task 2: Hosted API & Local Endpoint Provider Implementations

**Files:**
- Modify: `packages/llm/client.py` (refactor into `OpenAILLMProvider` and `LocalLLMProvider`)
- Create: `packages/llm/anthropic.py` (`AnthropicLLMProvider`)
- Modify: `packages/llm/__init__.py` (re-exports)
- Test: `tests/unit/test_llm_provider.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`, `ChatMessage`, `ModelTier`, `LLMResult`
- Produces:
  - `OpenAILLMProvider(base_url="https://api.openai.com/v1", api_key=..., model_map=..., timeout_s=...)`
  - `LocalLLMProvider(base_url="http://localhost:11434/v1", api_key=None, model_map=..., timeout_s=...)`
  - `AnthropicLLMProvider(base_url="https://api.anthropic.com/v1", api_key=..., model_map=..., timeout_s=...)`

- [ ] **Step 1: Write failing unit tests for `OpenAILLMProvider`, `LocalLLMProvider`, and `AnthropicLLMProvider`**
  - Test `OpenAILLMProvider`: bearer auth, OpenAI `/v1/chat/completions` payload format, JSON schema response format.
  - Test `LocalLLMProvider`: local URL default (`http://localhost:11434/v1`), optional/empty API key, OpenAI-compatible completions format.
  - Test `AnthropicLLMProvider`: `x-api-key` header, `anthropic-version: 2023-06-01`, `/v1/messages` payload format, structured tool/json parsing, token usage extraction (`input_tokens`, `output_tokens`).
- [ ] **Step 2: Run tests to verify failure**
  - Run `uv run pytest tests/unit/test_llm_provider.py -k "test_anthropic or test_local"` to confirm failure before implementation.
- [ ] **Step 3: Implement `OpenAILLMProvider` and `LocalLLMProvider` in `packages/llm/client.py`**
  - Keep `HttpLLMProvider` as base or alias for backward compatibility.
  - Specialize `OpenAILLMProvider` with OpenAI defaults.
  - Specialize `LocalLLMProvider` with local defaults (e.g. Ollama/vLLM endpoints, no mandatory API key).
- [ ] **Step 4: Implement `AnthropicLLMProvider` in `packages/llm/anthropic.py`**
  - Implement Anthropic Messages API (`POST /v1/messages`).
  - Translate system messages to top-level `system` parameter and user/assistant messages to `messages` array.
  - Implement tool calling or JSON mode schema enforcement.
  - Map errors (`401`, `429`, `500`, timeout) to standard `LLMResponseError`, `LLMTimeoutError`.
- [ ] **Step 5: Run tests and verify clean pass**
  - Run `uv run pytest tests/unit/test_llm_provider.py -v`.
- [ ] **Step 6: Commit changes**
  - `git commit -m "feat(llm): implement OpenAILLMProvider, LocalLLMProvider, and AnthropicLLMProvider [task 4.5] [R14.5, R14.7]"`

---

## Task 3: Shared Contract Test Suite

**Files:**
- Create: `packages/llm/testing.py` (`LLMProviderContractSuite`)
- Create: `tests/unit/test_llm_contract.py`
- Modify: `packages/llm/__init__.py`

**Interfaces:**
- Consumes: `LLMProvider`, `ChatMessage`, `ModelTier`, `LLMResult`
- Produces: `LLMProviderContractSuite(ABC)` with `@abstractmethod def create_provider(self) -> LLMProvider`

- [ ] **Step 1: Write `LLMProviderContractSuite` in `packages/llm/testing.py`**
  - Standardized test cases every conforming provider must satisfy:
    1. `test_conforms_to_protocol`: asserts `isinstance(provider, LLMProvider)`.
    2. `test_generate_returns_valid_llm_result`: asserts `LLMResult` structure, types, non-empty `model`, valid `tier`, non-negative tokens and latency.
    3. `test_generate_structured_json_schema`: validates JSON schema parsing and dictionary content return.
    4. `test_generate_tier_model_resolution`: verifies `ModelTier.ROUTINE` vs `ModelTier.HIGH_CAPABILITY` resolves to appropriate model names.
    5. `test_generate_timeout_error`: asserts `LLMTimeoutError` raised on timeout.
    6. `test_generate_api_error`: asserts `LLMResponseError` raised on HTTP error status.
    7. `test_generate_schema_validation_error`: asserts `LLMSchemaValidationError` raised on unparseable JSON when schema requested.
- [ ] **Step 2: Implement test runner subclasses in `tests/unit/test_llm_contract.py`**
  - `TestFakeLLMProviderContract(LLMProviderContractSuite)`: validates `FakeLLMProvider`.
  - `TestOpenAILLMProviderContract(LLMProviderContractSuite)`: validates `OpenAILLMProvider` using `httpx.MockTransport`.
  - `TestAnthropicLLMProviderContract(LLMProviderContractSuite)`: validates `AnthropicLLMProvider` using `httpx.MockTransport`.
  - `TestLocalLLMProviderContract(LLMProviderContractSuite)`: validates `LocalLLMProvider` using `httpx.MockTransport`.
- [ ] **Step 3: Run contract test suite**
  - Run `uv run pytest tests/unit/test_llm_contract.py -v`.
- [ ] **Step 4: Commit changes**
  - `git commit -m "feat(llm): add shared LLMProviderContractSuite and contract tests [task 4.5] [R14.5, R14.7, R24.5]"`

---

## Task 4: Configuration, Factory & Quality Gate

**Files:**
- Modify: `packages/core/settings.py` (`LLMTiersSettings` / `LLMProviderSettings`)
- Create: `packages/llm/factory.py` (`create_llm_provider`)
- Modify: `packages/llm/__init__.py`
- Modify: `.env.example`
- Modify: `docs/configuration.md`
- Modify: `specs/tasks.md`
- Test: `tests/unit/test_llm_factory.py`

**Interfaces:**
- Consumes: `LLMTiersSettings`
- Produces: `create_llm_provider(settings: LLMTiersSettings | None = None) -> LLMProvider`

- [ ] **Step 1: Write failing tests for `create_llm_provider` factory in `tests/unit/test_llm_factory.py`**
  - Test selecting `"fake"` returns `FakeLLMProvider`.
  - Test selecting `"openai"` returns `OpenAILLMProvider` with configured base URL and key.
  - Test selecting `"anthropic"` returns `AnthropicLLMProvider` with configured base URL and key.
  - Test selecting `"local"` returns `LocalLLMProvider` with configured local base URL.
  - Test unknown provider raises `ValueError`.
- [ ] **Step 2: Run test to verify failure**
  - Run `uv run pytest tests/unit/test_llm_factory.py` to confirm failure.
- [ ] **Step 3: Update `packages/core/settings.py`**
  - Add `provider: str = Field(default="fake", description="LLM provider: fake | openai | anthropic | local")` to `LLMTiersSettings`.
  - Add `openai_api_key`, `openai_base_url`, `anthropic_api_key`, `anthropic_base_url`, `local_base_url`, `timeout_s`.
- [ ] **Step 4: Implement `create_llm_provider` in `packages/llm/factory.py`**
  - Instantiate and return corresponding provider class.
  - Re-export `create_llm_provider` in `packages/llm/__init__.py`.
- [ ] **Step 5: Run factory unit tests**
  - Run `uv run pytest tests/unit/test_llm_factory.py -v`.
- [ ] **Step 6: Update `.env.example` and `docs/configuration.md`**
  - Document all new `LLM__*` environment variables per DoD §3.4.
- [ ] **Step 7: Full test suite, lint, and type check validation**
  - Run `uv run pytest tests/unit/test_llm_*.py -v`.
  - Run `uv run ruff check packages/llm packages/core tests/unit/test_llm_*.py`.
  - Run `uv run mypy packages/llm packages/core`.
  - Run full test suite regression: `uv run pytest tests/unit/ -q`.
- [ ] **Step 8: Update `specs/tasks.md` and commit**
  - Mark Task 4.5 `[x]` in `specs/tasks.md`.
  - Commit: `feat(llm): complete LLMProvider abstraction and factory [task 4.5] [R14.5, R14.7, R24.5]`.

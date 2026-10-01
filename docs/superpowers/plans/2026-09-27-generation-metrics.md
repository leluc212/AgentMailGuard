# Generation Metrics (task 4.12) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every model inference records its final context size, token counts and estimated cost, and every persisted AI draft counts toward `emails_generated_total` and a per-category cost counter. All labels stay low-cardinality, and one structured log line per request makes context length correlatable with latency, cost and draft quality.

**Architecture:** One helper, `record_inference`, owns per-request telemetry. It observes the `llm_context_tokens` histogram, feeds token and cost counters through the existing `record_ai_cost`, and writes one `llm_inference` JSON log line. It runs at the two places model calls pass through: `BudgetedLLMProvider`, used by generation and repair, and a new `InstrumentedLLMProvider` decorator for fixed-kind callers (triage today, summarization in 4.13). The generator stops counting tokens itself, so nothing is counted twice. `DraftingService` counts the email and its cost only when a draft is actually created.

**Tech Stack:** Python 3.12, uv, prometheus_client, pytest 9 + pytest-asyncio (auto mode), mypy strict, ruff.

**Spec:** Source of truth is `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md`. Line 948 says "Context length should be recorded for every inference request so its relationship with response quality, latency, and cost can be measured". The Prometheus metric list at lines 1700-1725 includes `emails_generated_total`, `generation_latency_ms`, `input_tokens_total`, `output_tokens_total` and `estimated_ai_cost`. Requirements: `specs/requirements.md` R11.7, R21.4, R21.5, R21.6 and NFR8 (LLM generation 1–5 s), plus R21.3 (structured logs). Design: `specs/design.md` §10, which covers the metric list, "Cost is a monotonic counter, not a gauge", and "Label sets stay low-cardinality: `{organization, category, priority, model_tier, decided_by}`. Never label with `message_id`". Task: `specs/tasks.md` 4.12.

## Global Constraints

- Python 3.12; run everything through `uv run`; ruff line length 100 (rules E, F, I, N, W, UP, B, C4, SIM); `uv run mypy packages services` stays clean (strict).
- pytest `addopts` already contains `-q`; never add another. pytest-asyncio runs in auto mode.
- Integration tests run only via `tests/integration/conftest.py` (isolated to `rag_email_test`). The live dev stack is off-limits.
- **Label rule (design §10):** labels come only from `{organization, category, priority, model_tier, decided_by, kind, tier, model}`. `kind`, `tier` and `model` are bounded by `CallKind`, `ModelTier` and the configured price table. Never label with a message, job, thread or draft id.
- **Cost is a counter, never a gauge** (design §10). An unknown price is an unknown cost: tokens are still counted, cost is not incremented, and the log line carries `estimated_cost_usd: null` (the 4.11 rule).
- Telemetry never fails or masks a model call. Every emission is guarded, and an exception from the provider propagates unchanged.
- Never log prompt or completion text. Log sizes, ids from the correlation context, and outcomes only (requirements §0.5, "never log secrets").
- Commit format `type(scope): summary [task 4.12] [R…]`. **Standing user instructions:** no commits until Phase 4 is complete, so each Commit step becomes a controller snapshot. No `[x]` in `specs/tasks.md` while `make ci` is red (RA.14), so 4.12 closes `[~]`.

## Review Focus

1. **The provider raises** (timeout, HTTP error, unparseable output). Expected: the request's context size is still recorded with `outcome` set to the exception name, no tokens or cost are invented, and the original exception reaches the caller unchanged. Pinned by Task 2 `test_failed_call_records_context_but_no_tokens`.
2. **Token double counting** after moving recording out of the generator. Expected: `input_tokens_total` rises by exactly the tokens the provider reported, including the repair call. Pinned by Task 2 `test_generation_tokens_are_counted_once_per_request`.
3. **A model missing from the price table.** Expected: tokens counted, cost counters untouched, the log line has `estimated_cost_usd: null`, and a warning is emitted. Pinned by Task 1 `test_unpriced_model_counts_tokens_but_not_cost` and Task 4 `test_unpriced_draft_counts_email_but_not_cost`.
4. **A broken or partial metrics object** (older registry, mocked metrics). Expected: the model call and the draft still succeed. Pinned by Task 1 `test_broken_metrics_never_raise` and Task 4 `test_broken_metrics_do_not_block_the_draft`.
5. **A redelivered job** that is already drafted. Expected: `emails_generated_total` and the cost counters do not move a second time. Pinned by Task 4 `test_redelivery_does_not_count_the_email_twice`.

---

## Design decisions

- **D1. One per-request helper.** `record_inference` lives in `packages/llm/inference_metrics.py`. It needs `LLMResult` and `ChatMessage`, and `packages.observability` must not import `packages.llm`.
- **D2. Context size is counted from the messages actually sent, before the call.** It uses `TokenCounter` (tiktoken, heuristic fallback) and covers every request, including failed ones. Provider-reported input tokens stay in `input_tokens_total`.
- **D3. Two instrumentation points.** `BudgetedLLMProvider` knows each call's kind (`generate` or `repair`), so it records there. `InstrumentedLLMProvider(provider, kind=…)` wraps fixed-kind callers. The generator's own token increments are removed, which prevents double counting. Its latency histogram stays, because `generation_latency_ms` is per generation job.
- **D4. Price table is injected.** `SinglePassGenerator(price_table=…)` and `InstrumentedLLMProvider(price_table=…)` default to `None`, which means tokens are counted but no cost. Composition roots pass `settings.llm.price_table`: triage in Task 3, ai-worker in 4.13.
- **D5. Per-email counters are emitted only on `created=True`.** They are `emails_generated_total{organization, category, model_tier}` (gains `category`) and the new `generated_draft_cost_total{category, model_tier}`. Together these answer R21.6 per category and per day. Per email, the `generated_draft.cost_estimate` column from 4.11 answers it.
- **D6. Structured fields in logs.** `StructuredJSONFormatter` gains a `fields` slot: `logger.info("llm_inference", extra={"fields": {...}})` is emitted as `"fields": {...}`. Today the formatter drops every `extra=` key silently.

## Not in this plan

- Wrapping the thread summarizer's provider and passing `metrics` and `price_table` to `SinglePassGenerator` and `DraftingService` in the ai-worker composition: task 4.13 (added to its bullets by Task 5).
- Grafana panels and alert rules: tasks 7.x. PromQL for them is documented in Task 5.
- The 4.10 `draft_citation_mismatch` log call still uses bare `extra=` keys, which the formatter drops. D6 makes a fix trivial (`extra={"fields": …}`), but it is left out to keep scope.
- `end_to_end_latency_ms` and queue metrics are outside 4.12's requirement list.

## File structure

| File | Responsibility |
|---|---|
| `packages/observability/metrics.py` (modify) | `llm_context_tokens` histogram, `generated_draft_cost_total` counter, `category` label on `emails_generated_total` |
| `packages/observability/logging.py` (modify) | `fields` slot in `StructuredJSONFormatter` |
| `packages/llm/inference_metrics.py` (new) | `count_context_tokens`, `record_inference` |
| `packages/llm/budget.py` (modify) | `BudgetedLLMProvider` records every request |
| `packages/llm/instrumented.py` (new) | `InstrumentedLLMProvider` decorator for fixed-kind callers |
| `packages/llm/generator.py` (modify) | `price_table` parameter; drop its own token increments |
| `services/triage_worker/main.py` (modify) | wrap the triage provider |
| `services/ai_worker/drafting.py` (modify) | per-email counters on `created=True` |
| `docs/observability.md`, `specs/design.md`, `specs/tasks.md` (modify) | metric reference, PromQL, status |

---

### Task 1: Per-inference telemetry helper and instruments

**Files:**
- Modify: `packages/observability/metrics.py` (dataclass fields ~lines 62-110; factory ~lines 111-360; buckets ~line 28)
- Modify: `packages/observability/logging.py` (`StructuredJSONFormatter.format`)
- Modify: `tests/integration/test_funnel_metrics_integration.py:257`, `tests/unit/test_funnel_metrics.py:69,314`
- Create: `packages/llm/inference_metrics.py`
- Test: `tests/unit/test_inference_metrics.py`

**Interfaces:**
- Consumes: `record_ai_cost(model, tier, input_tokens, output_tokens, price_table, metrics=None) -> float`, `estimate_inference_cost(...) -> float | None` (4.11), `TokenCounter.count_tokens(text) -> int`, `LLMResult(content, model, tier, input_tokens, output_tokens, latency_ms, …)`, `ChatMessage(role, content)`.
- Produces:
  - `PipelineMetrics.llm_context_tokens: Histogram` with labels `kind`, `tier`; `CONTEXT_TOKEN_BUCKETS`.
  - `PipelineMetrics.generated_draft_cost_total: Counter` with labels `category`, `model_tier`.
  - `emails_generated_total` labels become `organization`, `category`, `model_tier`.
  - `packages.llm.inference_metrics.count_context_tokens(messages: Sequence[ChatMessage], token_counter: TokenCounter | None = None) -> int`.
  - `record_inference(metrics: Any | None, *, kind: str, tier: str, context_tokens: int, latency_ms: int, result: LLMResult | None, error: BaseException | None, price_table: Mapping[str, ModelPricing] | None) -> None` (never raises).
  - `INFERENCE_LOG_EVENT = "llm_inference"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_inference_metrics.py`:

```python
"""Per-inference telemetry: context size, tokens, cost and one log line (R11.7, R21.4, R21.6)."""

from __future__ import annotations

import json
import logging

import pytest

from packages.core.settings import ModelPricing
from packages.llm.inference_metrics import (
    INFERENCE_LOG_EVENT,
    count_context_tokens,
    record_inference,
)
from packages.llm.protocol import ChatMessage, LLMResult, ModelTier
from packages.observability.logging import StructuredJSONFormatter
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics

PRICES = {"model-a": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
MESSAGES = [
    ChatMessage(role="system", content="You are a support assistant."),
    ChatMessage(role="user", content="How do I reset my password?"),
]


def _result(model: str = "model-a") -> LLMResult:
    return LLMResult(
        content={}, model=model, tier=ModelTier.ROUTINE, input_tokens=1_200, output_tokens=300
    )


def _sample(m: PipelineMetrics, name: str, labels: dict[str, str]) -> float | None:
    return m.registry.get_sample_value(name, labels)


def _inference_fields(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    records = [r for r in caplog.records if r.getMessage() == INFERENCE_LOG_EVENT]
    assert len(records) == 1
    fields = records[0].__dict__["fields"]
    assert isinstance(fields, dict)
    return fields


def test_context_tokens_cover_every_message() -> None:
    one = count_context_tokens(MESSAGES[:1])
    both = count_context_tokens(MESSAGES)
    assert 0 < one < both
    assert count_context_tokens([]) == 0


def test_successful_request_records_context_tokens_and_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    m = create_pipeline_metrics()
    caplog.set_level(logging.INFO)

    record_inference(
        m,
        kind="generate",
        tier="routine",
        context_tokens=812,
        latency_ms=900,
        result=_result(),
        error=None,
        price_table=PRICES,
    )

    labels = {"kind": "generate", "tier": "routine"}
    assert _sample(m, "llm_context_tokens_count", labels) == 1
    assert _sample(m, "llm_context_tokens_sum", labels) == 812
    tok = {"model": "model-a", "tier": "routine"}
    assert _sample(m, "input_tokens_total", tok) == 1_200
    assert _sample(m, "output_tokens_total", tok) == 300
    assert _sample(m, "estimated_ai_cost_total", tok) == pytest.approx(0.00036)
    fields = _inference_fields(caplog)
    assert fields["kind"] == "generate"
    assert fields["context_tokens"] == 812
    assert fields["outcome"] == "ok"
    assert fields["estimated_cost_usd"] == pytest.approx(0.00036)
    assert "content" not in fields


def test_unpriced_model_counts_tokens_but_not_cost(caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 3."""
    m = create_pipeline_metrics()
    caplog.set_level(logging.INFO)

    record_inference(
        m,
        kind="generate",
        tier="routine",
        context_tokens=10,
        latency_ms=5,
        result=_result("model-unpriced"),
        error=None,
        price_table=PRICES,
    )

    tok = {"model": "model-unpriced", "tier": "routine"}
    assert _sample(m, "input_tokens_total", tok) == 1_200
    assert _sample(m, "estimated_ai_cost_total", tok) is None
    assert _inference_fields(caplog)["estimated_cost_usd"] is None


def test_no_price_table_counts_tokens_only() -> None:
    m = create_pipeline_metrics()
    record_inference(
        m,
        kind="triage",
        tier="fast",
        context_tokens=10,
        latency_ms=5,
        result=_result(),
        error=None,
        price_table=None,
    )
    tok = {"model": "model-a", "tier": "fast"}
    assert _sample(m, "input_tokens_total", tok) == 1_200
    assert _sample(m, "estimated_ai_cost_total", tok) is None


def test_failed_request_records_context_and_outcome_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    m = create_pipeline_metrics()
    caplog.set_level(logging.INFO)

    record_inference(
        m,
        kind="generate",
        tier="routine",
        context_tokens=640,
        latency_ms=30_000,
        result=None,
        error=TimeoutError("upstream"),
        price_table=PRICES,
    )

    assert _sample(m, "llm_context_tokens_count", {"kind": "generate", "tier": "routine"}) == 1
    assert _sample(m, "input_tokens_total", {"model": "model-a", "tier": "routine"}) is None
    fields = _inference_fields(caplog)
    assert fields["outcome"] == "TimeoutError"
    assert fields["input_tokens"] is None


def test_broken_metrics_never_raise() -> None:
    """Review Focus 4: telemetry must never fail the call it measures."""
    record_inference(
        object(),
        kind="generate",
        tier="routine",
        context_tokens=1,
        latency_ms=1,
        result=_result(),
        error=None,
        price_table=PRICES,
    )
    record_inference(
        None,
        kind="generate",
        tier="routine",
        context_tokens=1,
        latency_ms=1,
        result=_result(),
        error=None,
        price_table=PRICES,
    )


def test_new_and_changed_collectors_use_low_cardinality_labels() -> None:
    m = create_pipeline_metrics()
    allowed = {
        "organization",
        "category",
        "priority",
        "model_tier",
        "decided_by",
        "kind",
        "tier",
        "model",
    }
    for collector in (m.llm_context_tokens, m.generated_draft_cost_total, m.emails_generated_total):
        assert set(collector._labelnames) <= allowed
    assert set(m.emails_generated_total._labelnames) == {"organization", "category", "model_tier"}


def test_json_formatter_emits_structured_fields() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, INFERENCE_LOG_EVENT, None, None)
    record.fields = {"kind": "generate", "context_tokens": 812}
    payload = json.loads(StructuredJSONFormatter().format(record))
    assert payload["fields"] == {"kind": "generate", "context_tokens": 812}
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/unit/test_inference_metrics.py`
Expected: collection error `ModuleNotFoundError: No module named 'packages.llm.inference_metrics'`.

- [ ] **Step 3: Add the instruments**

In `packages/observability/metrics.py`:

1. After `CALLS_PER_JOB_BUCKETS = …`, add:

```python
CONTEXT_TOKEN_BUCKETS = (
    256.0,
    512.0,
    1024.0,
    2048.0,
    4096.0,
    8192.0,
    16384.0,
    32768.0,
    65536.0,
    131072.0,
)
```

2. In the `PipelineMetrics` dataclass, add `generated_draft_cost_total: Counter` directly after `estimated_ai_cost_total: Counter`, and `llm_context_tokens: Histogram` directly after `llm_calls_per_job: Histogram`.

3. In `create_pipeline_metrics`, change the `emails_generated_total` label list from `["organization", "model_tier"]` to `["organization", "category", "model_tier"]`. After the `estimated_ai_cost_total=Counter(...)` entry, add:

```text
        generated_draft_cost_total=Counter(
            "generated_draft_cost_total",
            "Estimated USD cost of persisted AI drafts, by category (R21.6)",
            ["category", "model_tier"],
            registry=reg,
        ),
```

After the `llm_calls_per_job=Histogram(...)` entry, add:

```text
        llm_context_tokens=Histogram(
            "llm_context_tokens",
            "Final assembled context size per inference request, in tokens (R11.7)",
            ["kind", "tier"],
            buckets=CONTEXT_TOKEN_BUCKETS,
            registry=reg,
        ),
```

In `tests/integration/test_funnel_metrics_integration.py`, line 257, change `.labels(organization=str(org_id), model_tier="fast")` to `.labels(organization=str(org_id), category="support", model_tier="fast")`. In `tests/unit/test_funnel_metrics.py`, line 69, change `emails_generated_total.labels(organization="org-1", model_tier="fast")` to `emails_generated_total.labels(organization="org-1", category="marketing", model_tier="fast")`; line 314, change `emails_generated_total.labels(organization=org, model_tier="strong")` to `emails_generated_total.labels(organization=org, category="newsletter", model_tier="strong")`.

In `packages/observability/metrics.py`, `record_ai_cost`, widen `price_table: dict[str, "ModelPricing"]` to `price_table: Mapping[str, "ModelPricing"]` and add `from collections.abc import Mapping` to the imports (mypy strict: `record_inference` passes a `Mapping`, and `dict` is invariant).

- [ ] **Step 4: Let the JSON formatter carry structured fields**

In `packages/observability/logging.py`, `StructuredJSONFormatter.format`, insert directly before the `# Caller location` comment:

```python
        # Structured event fields: logger.info("event", extra={"fields": {...}}) (R21.3)
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            payload["fields"] = fields
```

- [ ] **Step 5: Implement the helper**

Create `packages/llm/inference_metrics.py`:

```python
"""Per-inference telemetry: context size, tokens, cost and one structured log line.

Requirements: R11.7 (record the final context token count on every inference request),
R21.4 (input/output token and cost counters), R21.6 (cost from the price table),
R21.3 (structured logs). Proposal line 948: context length is recorded on every request so it
can be correlated with quality, latency and cost.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing
from packages.knowledge.token_counter import TokenCounter
from packages.llm.protocol import ChatMessage, LLMResult
from packages.observability.metrics import record_ai_cost

logger = logging.getLogger(__name__)

INFERENCE_LOG_EVENT = "llm_inference"
"""Message of the one structured log line written per inference request."""

_default_counter: TokenCounter | None = None


def _counter() -> TokenCounter:
    global _default_counter
    if _default_counter is None:
        _default_counter = TokenCounter()
    return _default_counter


def count_context_tokens(
    messages: Sequence[ChatMessage], token_counter: TokenCounter | None = None
) -> int:
    """Token size of the messages exactly as they are sent to the model."""
    counter = token_counter or _counter()
    return sum(counter.count_tokens(f"{m.role}\n{m.content}") for m in messages)


def record_inference(
    metrics: Any | None,
    *,
    kind: str,
    tier: str,
    context_tokens: int,
    latency_ms: int,
    result: LLMResult | None,
    error: BaseException | None,
    price_table: Mapping[str, ModelPricing] | None,
) -> None:
    """Record one inference request. Never raises.

    Always observes ``llm_context_tokens``. Tokens and cost are recorded only when the
    provider returned a result; a failed request has no billed usage to report. Cost is
    recorded only when ``price_table`` prices the model.
    """
    cost: float | None = None
    if result is not None and price_table is not None:
        try:
            cost = estimate_inference_cost(
                result.model, result.input_tokens, result.output_tokens, price_table
            )
        except ValueError:
            cost = None
    if metrics is not None:
        try:
            metrics.llm_context_tokens.labels(kind=kind, tier=tier).observe(context_tokens)
            if result is not None:
                if price_table is not None:
                    record_ai_cost(
                        result.model,
                        tier,
                        result.input_tokens,
                        result.output_tokens,
                        price_table,
                        metrics=metrics,
                    )
                else:
                    metrics.input_tokens_total.labels(model=result.model, tier=tier).inc(
                        result.input_tokens
                    )
                    metrics.output_tokens_total.labels(model=result.model, tier=tier).inc(
                        result.output_tokens
                    )
        except Exception:
            logger.warning("Inference metrics emission failed", exc_info=True)
    with contextlib.suppress(Exception):  # logging must never fail the call
        logger.info(
            INFERENCE_LOG_EVENT,
            extra={
                "fields": {
                    "kind": kind,
                    "tier": tier,
                    "model": result.model if result is not None else None,
                    "context_tokens": context_tokens,
                    "input_tokens": result.input_tokens if result is not None else None,
                    "output_tokens": result.output_tokens if result is not None else None,
                    "latency_ms": latency_ms,
                    "outcome": "ok" if error is None else type(error).__name__,
                    "estimated_cost_usd": cost,
                }
            },
        )
```

`record_ai_cost` already logs a warning for an unpriced model and returns without incrementing cost, so Review Focus 3's warning comes from there.

- [ ] **Step 6: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_inference_metrics.py tests/unit/test_observability_metrics.py tests/unit/test_funnel_metrics.py tests/unit/test_inference_cost.py`
Expected: all pass (8 in the new file).

- [ ] **Step 7: Lint, type-check, commit**

Run: `uv run ruff check packages/observability packages/llm/inference_metrics.py tests/unit/test_inference_metrics.py tests/unit/test_funnel_metrics.py tests/integration/test_funnel_metrics_integration.py && uv run ruff format --check packages/llm/inference_metrics.py tests/unit/test_inference_metrics.py && uv run mypy packages services tests/unit/test_inference_metrics.py`
Expected: clean. (Repo-wide, `ruff format --check .` stays at its RA.14 baseline.) If `ruff format --check` flags the test file's compact call style, run `uv run ruff format tests/unit/test_inference_metrics.py` and keep the result. The file is new, so it is not on the RA.14 baseline.

```bash
git add packages/observability/metrics.py packages/observability/logging.py packages/llm/inference_metrics.py \
  tests/unit/test_inference_metrics.py tests/unit/test_funnel_metrics.py \
  tests/integration/test_funnel_metrics_integration.py
git commit -m "feat(observability): per-inference context tokens, tokens and cost in one helper [task 4.12] [R11.7, R21.4, R21.6]"
```

---

### Task 2: Instrument every model request

**Files:**
- Modify: `packages/llm/budget.py` (`BudgetedLLMProvider.__init__` and `generate`, ~lines 209-270)
- Create: `packages/llm/instrumented.py`
- Modify: `packages/llm/generator.py` (`SinglePassGenerator.__init__` ~line 78; `BudgetedLLMProvider(...)` constructions ~lines 150-166; `_emit_generation_metrics` ~lines 380-400)
- Modify: `packages/llm/__init__.py` (export `InstrumentedLLMProvider`)
- Test: `tests/unit/test_llm_request_instrumentation.py`

**Interfaces:**
- Consumes: `count_context_tokens`, `record_inference` (Task 1); `CallKind` (`triage`, `summarize`, `generate`, `repair`).
- Produces:
  - `BudgetedLLMProvider(provider, tracker=None, default_kind=CallKind.GENERATE, metrics=None, price_table=None)` records every request.
  - `packages.llm.instrumented.InstrumentedLLMProvider(provider: LLMProvider, *, kind: CallKind, metrics: Any | None = None, price_table: Mapping[str, ModelPricing] | None = None)` with `.provider`, `.kind`, `.metrics`, `generate(...)` and `aclose()`.
  - `SinglePassGenerator(llm_provider, profile_registry, metrics=None, price_table=None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_llm_request_instrumentation.py`:

```python
"""Every model request records context size, tokens and cost exactly once (R11.7, R21.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage
from packages.llm import (
    AgentProfileRegistry,
    FakeLLMProvider,
    InstrumentedLLMProvider,
    SinglePassGenerator,
)
from packages.llm.budget import CallKind
from packages.llm.protocol import ChatMessage, ModelTier
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics

PRICES = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
REPLY = {
    "action": "reply",
    "draft": "Hello Alice, open Settings and choose Reset Password.",
    "confidence": 0.95,
    "knowledge_chunks": ["DOC-125-08"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


def _context() -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="prov-1",
        sender=EmailAddress(email="alice@example.com", name="Alice"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=[
            Candidate(
                chunk_id="c1",
                document_id="d1",
                content="Reset via Settings.",
                external_id="DOC-125-08",
            )
        ],
    )


def _count(m: PipelineMetrics, kind: str, tier: str) -> float:
    return (
        m.registry.get_sample_value("llm_context_tokens_count", {"kind": kind, "tier": tier}) or 0
    )


def _tokens(m: PipelineMetrics, name: str, model: str, tier: str) -> float:
    return m.registry.get_sample_value(name, {"model": model, "tier": tier}) or 0


def _generator(provider: FakeLLMProvider, m: PipelineMetrics) -> SinglePassGenerator:
    return SinglePassGenerator(
        llm_provider=provider,
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        metrics=m,
        price_table=PRICES,
    )


async def test_generation_tokens_are_counted_once_per_request() -> None:
    """Review Focus 2: the generator no longer counts tokens itself."""
    m = create_pipeline_metrics()
    result = await _generator(FakeLLMProvider(default_response=REPLY), m).generate_draft(
        _context(), category="technical_support"
    )

    tier = str(result.tier)
    assert _count(m, "generate", tier) == 1
    assert _tokens(m, "input_tokens_total", result.model, tier) == result.input_tokens
    assert _tokens(m, "output_tokens_total", result.model, tier) == result.output_tokens
    assert m.registry.get_sample_value(
        "estimated_ai_cost_total", {"model": result.model, "tier": tier}
    ) == pytest.approx((result.input_tokens * 0.15 + result.output_tokens * 0.60) / 1e6, abs=1e-6)


async def test_repair_request_is_recorded_under_its_own_kind() -> None:
    m = create_pipeline_metrics()
    provider = FakeLLMProvider(canned_responses=[{"action": "reply"}, dict(REPLY)])
    result = await _generator(provider, m).generate_draft(_context(), category="technical_support")

    tier = str(result.tier)
    assert _count(m, "generate", tier) == 1
    assert _count(m, "repair", tier) == 1
    assert _tokens(m, "input_tokens_total", result.model, tier) == result.input_tokens


async def test_failed_call_records_context_but_no_tokens() -> None:
    """Review Focus 1: the request is recorded and the provider's error reaches the caller."""
    m = create_pipeline_metrics()
    provider = FakeLLMProvider(error_to_raise=TimeoutError("upstream timeout"))

    with pytest.raises(TimeoutError, match="upstream timeout"):
        await _generator(provider, m).generate_draft(_context(), category="technical_support")

    total = sum(
        s.value
        for metric in m.llm_context_tokens.collect()
        for s in metric.samples
        if s.name == "llm_context_tokens_count" and s.labels["kind"] == "generate"
    )
    assert total == 1
    assert not [
        s for metric in m.input_tokens_total.collect() for s in metric.samples if s.value > 0
    ]


async def test_instrumented_provider_records_fixed_kind_requests() -> None:
    m = create_pipeline_metrics()
    inner = FakeLLMProvider()
    provider = InstrumentedLLMProvider(inner, kind=CallKind.TRIAGE, metrics=m, price_table=PRICES)

    result = await provider.generate(
        messages=[ChatMessage(role="user", content="classify me")], tier=ModelTier.FAST
    )

    assert provider.provider is inner
    assert _count(m, "triage", "fast") == 1
    assert _tokens(m, "input_tokens_total", result.model, "fast") == result.input_tokens


async def test_instrumented_provider_forwards_aclose() -> None:
    closed: list[bool] = []

    class _Closable(FakeLLMProvider):
        async def aclose(self) -> None:
            closed.append(True)

    await InstrumentedLLMProvider(_Closable(), kind=CallKind.TRIAGE).aclose()
    assert closed == [True]


async def test_instrumented_provider_propagates_errors_unchanged() -> None:
    boom = RuntimeError("provider down")
    provider = InstrumentedLLMProvider(
        FakeLLMProvider(error_to_raise=boom), kind=CallKind.SUMMARIZE, metrics=object()
    )
    with pytest.raises(RuntimeError) as excinfo:
        await provider.generate(messages=[ChatMessage(role="user", content="x")])
    assert excinfo.value is boom
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_llm_request_instrumentation.py`
Expected: collection error `ImportError: cannot import name 'InstrumentedLLMProvider' from 'packages.llm'`.

- [ ] **Step 3: Record every request in `BudgetedLLMProvider`**

In `packages/llm/budget.py`, add the imports:

```python
import time
from collections.abc import Mapping

from packages.core.settings import ModelPricing
from packages.llm.inference_metrics import count_context_tokens, record_inference
```

If `packages.llm.inference_metrics` creates an import cycle through `packages.llm.__init__`, import both names inside `generate` instead and name that change under Deviations.

Add `price_table: Mapping[str, ModelPricing] | None = None` as the last `__init__` parameter and store `self.price_table = price_table`. Replace the body of `generate` from `self.tracker.check_can_call(kind)` to the end with:

```text
        self.tracker.check_can_call(kind)
        context_tokens = count_context_tokens(messages)
        started = time.perf_counter()
        result: LLMResult | None = None
        error: BaseException | None = None
        try:
            result = await self.provider.generate(
                messages=messages,
                schema=schema,
                tier=tier,
                max_tokens=max_tokens,
                temperature=temperature,
                **params,
            )
        except BaseException as exc:
            error = exc
            raise
        finally:
            record_inference(
                self.metrics,
                kind=kind.value,
                tier=str(result.tier) if result is not None else str(tier),
                context_tokens=context_tokens,
                latency_ms=(
                    result.latency_ms
                    if result is not None
                    else int((time.perf_counter() - started) * 1000)
                ),
                result=result,
                error=error,
                price_table=self.price_table,
            )
        self.tracker.record_call(
            kind,
            model=result.model,
            tier=str(result.tier),
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
        )
        if self.metrics is not None and hasattr(self.metrics, "llm_calls_total"):
            self.metrics.llm_calls_total.labels(kind=kind.value, model=result.model).inc()
        return result
```

- [ ] **Step 4: Add the fixed-kind decorator**

Create `packages/llm/instrumented.py`:

```python
"""LLMProvider decorator that records every request of one fixed call kind (R11.7, R21.4).

For callers outside the per-job generation budget: the triage classifier (kind ``triage``)
and the thread summarizer (kind ``summarize``). Generation and repair are recorded by
``BudgetedLLMProvider``, which knows each call's kind.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from typing import Any

from packages.core.settings import ModelPricing
from packages.llm.budget import CallKind
from packages.llm.inference_metrics import count_context_tokens, record_inference
from packages.llm.protocol import ChatMessage, LLMProvider, LLMResult, ModelTier


class InstrumentedLLMProvider(LLMProvider):
    """Wraps a provider; records context size, tokens, cost and a log line per request."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        kind: CallKind,
        metrics: Any | None = None,
        price_table: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self.provider = provider
        self.kind = kind
        self.metrics = metrics
        self.price_table = price_table

    async def aclose(self) -> None:
        """Close the wrapped provider's resources when it supports ``aclose``."""
        aclose_fn = getattr(self.provider, "aclose", None)
        if callable(aclose_fn):
            res = aclose_fn()
            if inspect.isawaitable(res):
                await res

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
        **params: Any,
    ) -> LLMResult:
        context_tokens = count_context_tokens(messages)
        started = time.perf_counter()
        result: LLMResult | None = None
        error: BaseException | None = None
        try:
            result = await self.provider.generate(
                messages=messages,
                schema=schema,
                tier=tier,
                max_tokens=max_tokens,
                temperature=temperature,
                **params,
            )
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            record_inference(
                self.metrics,
                kind=self.kind.value,
                tier=str(result.tier) if result is not None else str(tier),
                context_tokens=context_tokens,
                latency_ms=(
                    result.latency_ms
                    if result is not None
                    else int((time.perf_counter() - started) * 1000)
                ),
                result=result,
                error=error,
                price_table=self.price_table,
            )
```

In `packages/llm/__init__.py`, add `from packages.llm.instrumented import InstrumentedLLMProvider` in alphabetical position, between the `packages.llm.generator` and `packages.llm.profile` import blocks (ruff I001), and add `"InstrumentedLLMProvider"` to `__all__`.

- [ ] **Step 5: Thread the price table through the generator and stop double counting**

In `packages/llm/generator.py`:

1. `SinglePassGenerator.__init__` gains `price_table: Mapping[str, ModelPricing] | None = None` as its last parameter, stored as `self.price_table = price_table`. Add `from collections.abc import Mapping` and `from packages.core.settings import ModelPricing` to the imports if they are not there.
2. Both `BudgetedLLMProvider(...)` constructions (~lines 156-166) gain `price_table=self.price_table`.
3. In `_emit_generation_metrics`, delete the two blocks that begin `if hasattr(self.metrics, "input_tokens_total"):` and `if hasattr(self.metrics, "output_tokens_total"):`, including their `self._guarded(...)` bodies. Keep the latency observation and the budget export. The now-lonely nested `if attempts:` / `if hasattr(self.metrics, "generation_latency_ms"):` pair must collapse into `if attempts and hasattr(self.metrics, "generation_latency_ms"):` (ruff SIM102). Replace the comment above it with:

```python
        # Only observe latency for attempts that actually returned; a call the provider
        # aborted has unknown timings. Tokens and cost are recorded per request by
        # BudgetedLLMProvider (packages/llm/inference_metrics.py), never here.
```

- [ ] **Step 6: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_llm_request_instrumentation.py tests/unit/test_single_pass_generator.py tests/unit/test_draft_repair_orchestration.py tests/unit/test_citation_verification_generation.py tests/unit/test_drafting_service.py`
Expected: all pass (6 in the new file). An existing test that asserted the generator's own token increment may now see the same value from `BudgetedLLMProvider`, which is expected. If one fails because it built a generator with `metrics=` but the provider's tokens moved to per-request labels, read the assertion. Keep it if it checks totals. Name any change under Deviations.

- [ ] **Step 7: Lint, type-check, commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_llm_request_instrumentation.py && uv run ruff format --check packages/llm/budget.py packages/llm/instrumented.py packages/llm/generator.py tests/unit/test_llm_request_instrumentation.py`
Expected: all pass, clean. If `ruff format --check` flags the new test file, run `uv run ruff format tests/unit/test_llm_request_instrumentation.py` and keep the result (new file, not on the RA.14 baseline). `packages/llm/generator.py` or `budget.py` may already be on the RA.14 format baseline. If so, check them against the pre-task snapshot, and do not reformat whole files.

```bash
git add packages/llm/budget.py packages/llm/instrumented.py packages/llm/generator.py packages/llm/__init__.py \
  tests/unit/test_llm_request_instrumentation.py
git commit -m "feat(llm): record context size, tokens and cost on every model request [task 4.12] [R11.7, R21.4, R21.6]"
```

---

### Task 3: Instrument the triage classifier's requests

**Files:**
- Modify: `services/triage_worker/main.py` (`build_triage_consumer` ~line 94, the `LLMTriageClassifier(...)` line ~119, `build_components` ~line 149)
- Modify: `tests/unit/test_triage_worker_main.py` (`test_build_wires_components_from_settings`, and one new test)

**Interfaces:**
- Consumes: `InstrumentedLLMProvider` (Task 2); `WorkerResources.metrics: PipelineMetrics`; `settings.llm.price_table`.
- Produces: `build_triage_consumer(settings, *, publisher, job_store, message_store, draft_store, connection=None, shutdown_coordinator=None, metrics: PipelineMetrics | None = None) -> TriageConsumer`, whose `consumer.cascade.llm_classifier.provider` is an `InstrumentedLLMProvider` of kind `triage`.

- [ ] **Step 1: Write the failing test and adjust the wiring assertion**

In `tests/unit/test_triage_worker_main.py`, `test_build_wires_components_from_settings`, replace:

```python
    assert isinstance(consumer.cascade.llm_classifier.provider, FakeLLMProvider)
```

with:

```python
    provider = consumer.cascade.llm_classifier.provider
    assert isinstance(provider, InstrumentedLLMProvider)
    assert provider.kind == CallKind.TRIAGE
    assert isinstance(provider.provider, FakeLLMProvider)
```

Append this test to the file:

```python
async def test_triage_llm_requests_are_instrumented(repo_cwd: Path) -> None:
    """R11.7: the stage-3 triage call records its context size like every other request."""
    metrics = create_pipeline_metrics()
    settings = _settings()
    consumer = build_triage_consumer(
        settings,
        publisher=MagicMock(),
        job_store=InMemoryJobStore(),
        message_store=InMemoryMessageStore(),
        draft_store=InMemoryDraftStore(),
        metrics=metrics,
    )

    await consumer.cascade.llm_classifier.classify(
        EmailContext(subject="Question", body_text="Can you help?", sender_email="a@x.com")
    )

    samples = [
        s
        for metric in metrics.llm_context_tokens.collect()
        for s in metric.samples
        if s.name == "llm_context_tokens_count" and s.labels["kind"] == "triage"
    ]
    assert sum(s.value for s in samples) == 1
    provider = consumer.cascade.llm_classifier.provider
    assert isinstance(provider, InstrumentedLLMProvider)
    assert provider.price_table == settings.llm.price_table
```

Add the imports the file lacks. Read its existing import block and `_build` helper first, and reuse whatever it already imports:

```python
from unittest.mock import MagicMock

from packages.db.message import InMemoryMessageStore
from packages.domain.rules import EmailContext
from packages.llm import InstrumentedLLMProvider
from packages.llm.budget import CallKind
from packages.observability.metrics import create_pipeline_metrics
from services.triage_worker.main import build_triage_consumer
```

The file already has a `_build(settings, **stores)` helper (line ~52). Leave it unchanged: the new test calls `build_triage_consumer` directly. If the file already defines `_publisher()`, use `publisher=_publisher()` instead of `MagicMock()` and drop the `MagicMock` import.

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/unit/test_triage_worker_main.py`
Expected: `test_build_wires_components_from_settings` fails its `isinstance(provider, InstrumentedLLMProvider)` assertion. `test_triage_llm_requests_are_instrumented` fails with `TypeError: build_triage_consumer() got an unexpected keyword argument 'metrics'`.

- [ ] **Step 3: Wire the decorator**

In `services/triage_worker/main.py`:

1. Add the imports `from packages.llm import InstrumentedLLMProvider`, `from packages.llm.budget import CallKind`, and, under `TYPE_CHECKING` or directly, `from packages.observability.metrics import PipelineMetrics`.
2. Add `metrics: PipelineMetrics | None = None,` as the last keyword parameter of `build_triage_consumer`.
3. Replace `llm_classifier = LLMTriageClassifier(provider=create_llm_provider(settings.llm))` with:

```python
    llm_classifier = LLMTriageClassifier(
        provider=InstrumentedLLMProvider(
            create_llm_provider(settings.llm),
            kind=CallKind.TRIAGE,
            metrics=metrics,
            price_table=settings.llm.price_table,
        )
    )
```

4. In `build_components`, add `metrics=res.metrics,` to the `build_triage_consumer(...)` call. The existing `aclose` lookup on `consumer.cascade.llm_classifier.provider` keeps working, because `InstrumentedLLMProvider.aclose` forwards.

- [ ] **Step 4: Run and watch it pass**

Run: `uv run pytest tests/unit/test_triage_worker_main.py tests/unit/test_production_imports.py tests/unit/test_dependency_rules.py`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check services/triage_worker/main.py tests/unit/test_triage_worker_main.py && uv run mypy packages services tests/unit/test_triage_worker_main.py`
Expected: clean.

```bash
git add services/triage_worker/main.py tests/unit/test_triage_worker_main.py
git commit -m "feat(triage): record context size, tokens and cost for stage-3 LLM requests [task 4.12] [R11.7, R21.4]"
```

---

### Task 4: Count generated emails and their cost

**Files:**
- Modify: `services/ai_worker/drafting.py` (`DraftingService.__init__` and `draft`)
- Test: `tests/unit/test_drafting_service_metrics.py`

**Interfaces:**
- Consumes: `emails_generated_total{organization, category, model_tier}` and `generated_draft_cost_total{category, model_tier}` (Task 1); `DraftingOutcome.created` (4.11).
- Produces: `DraftingService(*, generator, job_store, persistence, price_table, metrics: PipelineMetrics | None = None)`. `UNKNOWN_CATEGORY = "unknown"`, the label used when `draft()` gets no category.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_drafting_service_metrics.py`:

```python
"""A persisted AI draft counts once toward emails_generated_total and its cost (R21.4, R21.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from services.ai_worker.drafting import UNKNOWN_CATEGORY, DraftingService

REPLY = {
    "action": "reply",
    "draft": "Hello Alice, open Settings and choose Reset Password.",
    "confidence": 0.95,
    "knowledge_chunks": ["DOC-125-08"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
PRICED = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}


def _context(org_id: object) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=org_id,  # type: ignore[arg-type]
        provider="mock",
        provider_message_id="prov-1",
        sender=EmailAddress(email="alice@example.com", name="Alice"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    return ContextPackage(
        agent_instructions="a",
        category_instructions="c",
        current_message=message,
        retrieved_chunks=[
            Candidate(chunk_id="c1", document_id="d1", content="Reset.", external_id="DOC-125-08")
        ],
    )


async def _service(metrics: object, prices: dict[str, ModelPricing]) -> tuple[DraftingService, Job]:
    jobs = InMemoryJobStore()
    job, _ = await jobs.create_job(
        Job(
            organization_id=uuid4(),
            state=JobState.CONTEXT_READY.value,
            idempotency_key=f"k-{uuid4()}",
        )
    )
    service = DraftingService(
        generator=SinglePassGenerator(
            llm_provider=FakeLLMProvider(default_response=REPLY),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=jobs,
        persistence=InMemoryDraftPersistence(jobs, InMemoryDraftStore()),
        price_table=prices,
        metrics=metrics,  # type: ignore[arg-type]
    )
    return service, job


def _emails(m: PipelineMetrics, org: object, category: str, tier: str) -> float | None:
    return m.registry.get_sample_value(
        "emails_generated_total",
        {"organization": str(org), "category": category, "model_tier": tier},
    )


async def test_created_draft_counts_the_email_and_its_cost() -> None:
    m = create_pipeline_metrics()
    service, job = await _service(m, PRICED)

    outcome = await service.draft(job, _context(job.organization_id), category="support")

    tier = outcome.draft.model_tier or ""
    assert _emails(m, job.organization_id, "support", tier) == 1
    assert m.registry.get_sample_value(
        "generated_draft_cost_total", {"category": "support", "model_tier": tier}
    ) == pytest.approx(outcome.draft.cost_estimate)


async def test_redelivery_does_not_count_the_email_twice() -> None:
    """Review Focus 5."""
    m = create_pipeline_metrics()
    service, job = await _service(m, PRICED)
    context = _context(job.organization_id)
    first = await service.draft(job, context, category="support")

    second = await service.draft(job, context, category="support")

    assert second.created is False
    assert _emails(m, job.organization_id, "support", first.draft.model_tier or "") == 1


async def test_unpriced_draft_counts_email_but_not_cost() -> None:
    """Review Focus 3."""
    m = create_pipeline_metrics()
    service, job = await _service(m, {})

    outcome = await service.draft(job, _context(job.organization_id), category="billing")

    tier = outcome.draft.model_tier or ""
    assert outcome.draft.cost_estimate is None
    assert _emails(m, job.organization_id, "billing", tier) == 1
    assert (
        m.registry.get_sample_value(
            "generated_draft_cost_total", {"category": "billing", "model_tier": tier}
        )
        is None
    )


async def test_missing_category_uses_the_unknown_label() -> None:
    m = create_pipeline_metrics()
    service, job = await _service(m, PRICED)

    outcome = await service.draft(job, _context(job.organization_id))

    assert _emails(m, job.organization_id, UNKNOWN_CATEGORY, outcome.draft.model_tier or "") == 1


async def test_broken_metrics_do_not_block_the_draft() -> None:
    """Review Focus 4."""
    service, job = await _service(object(), PRICED)

    outcome = await service.draft(job, _context(job.organization_id), category="support")

    assert outcome.created is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_drafting_service_metrics.py`
Expected: collection error `ImportError: cannot import name 'UNKNOWN_CATEGORY' from 'services.ai_worker.drafting'`.

- [ ] **Step 3: Emit the per-email counters**

In `services/ai_worker/drafting.py`:

1. Add under the imports:

```python
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

UNKNOWN_CATEGORY = "unknown"
"""Metric label for drafts generated without a classification category."""
```

2. Add `metrics: PipelineMetrics | None = None,` as the last keyword parameter of `DraftingService.__init__`, stored as `self.metrics = metrics`.

3. In `draft`, replace the final two lines:

```python
        outcome = await self.persistence.persist_drafted(draft)
        return DraftingOutcome(draft=outcome.draft, job=outcome.job, created=outcome.created)
```

with:

```python
        outcome = await self.persistence.persist_drafted(draft)
        if outcome.created:
            self._count_generated(outcome.draft, category)
        return DraftingOutcome(draft=outcome.draft, job=outcome.job, created=outcome.created)

    def _count_generated(self, draft: GeneratedDraft, category: str | None) -> None:
        """Count one generated email and its cost (R21.4, R21.6). Never raises."""
        metrics: Any = self.metrics
        if metrics is None:
            return
        label = category or UNKNOWN_CATEGORY
        tier = draft.model_tier or "unknown"
        try:
            metrics.emails_generated_total.labels(
                organization=str(draft.organization_id), category=label, model_tier=tier
            ).inc()
            if draft.cost_estimate is not None:
                metrics.generated_draft_cost_total.labels(category=label, model_tier=tier).inc(
                    draft.cost_estimate
                )
        except Exception:
            logger.warning("Generated-email metrics emission failed", exc_info=True)
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/unit/test_drafting_service_metrics.py tests/unit/test_drafting_service.py`
Expected: all pass (5 new).

- [ ] **Step 5: Full suites, lint, types, commit**

Run: `uv run pytest tests/unit && uv run pytest tests/integration && uv run ruff check . && uv run mypy packages services tests/unit/test_drafting_service_metrics.py && uv run ruff format --check services/ai_worker/drafting.py tests/unit/test_drafting_service_metrics.py`
Expected: all pass, clean (run `uv run ruff format` on the new test file if flagged). Also check `uv run mypy packages services tests evaluation 2>&1 | tail -1`, which should stay at `Found 14 errors in 6 files`.

```bash
git add services/ai_worker/drafting.py tests/unit/test_drafting_service_metrics.py
git commit -m "feat(ai-worker): count generated emails and their cost by category [task 4.12] [R21.4, R21.6]"
```

---

### Task 5: Metric reference, queries and status

**Files:**
- Modify: `docs/observability.md` (section "2. Prometheus Metric Instruments")
- Modify: `specs/design.md` (§10 metrics paragraph)
- Modify: `specs/tasks.md` (tasks 4.12 and 4.13)

- [ ] **Step 1: Document the instruments and queries**

In `docs/observability.md`, section 2, append these rows to its metric table, matching the table's column format:

```markdown
| `llm_context_tokens` | Histogram | `kind`, `tier` | Final assembled context size per inference request, counted before the call (R11.7). |
| `emails_generated_total` | Counter | `organization`, `category`, `model_tier` | Persisted AI drafts; counted once per job, never on redelivery (R21.4). |
| `generated_draft_cost_total` | Counter | `category`, `model_tier` | Estimated USD cost of persisted AI drafts; unpriced models add nothing (R21.6). |
```

Then add this subsection at the end of section 2:

````markdown
#### Generation cost, context size and latency (R11.7, R21.4–R21.6, NFR8)

Every model request (triage, summarize, generate, repair) passes through one helper,
`packages/llm/inference_metrics.py::record_inference`. It observes `llm_context_tokens`,
adds provider-reported tokens to `input_tokens_total` / `output_tokens_total`, adds priced
cost to `estimated_ai_cost_total`, and writes one JSON log line `llm_inference` whose
`fields` carry `kind, tier, model, context_tokens, input_tokens, output_tokens, latency_ms,
outcome, estimated_cost_usd`. The line also carries the correlation ids (`job_id`, `trace_id`),
which is how context length is joined to a draft's feedback (quality) per request.

```promql
# Cost per generated email over a day (SC9): all AI cost / generated emails
sum(increase(estimated_ai_cost_total[1d])) / sum(increase(emails_generated_total[1d]))

# Draft cost per category per day (R21.6)
sum by (category) (increase(generated_draft_cost_total[1d]))

# p95 context size by call kind (R11.7)
histogram_quantile(0.95, sum by (le, kind) (rate(llm_context_tokens_bucket[5m])))

# p95 generation latency against the NFR8 target of 1-5 s
histogram_quantile(0.95, sum by (le) (rate(generation_latency_ms_bucket[5m]))) > 5000
```

Per-email cost is the `generated_draft.cost_estimate` column; `NULL` means the model had no
price in `LLM__PRICE_TABLE`, and such drafts are excluded from cost sums rather than counted as free.
````

- [ ] **Step 2: Update the design metric list**

In `specs/design.md` §10, in the **Metrics (R21.4)** paragraph, add `generated_draft_cost_total` after `estimated_ai_cost_total`, and add `llm_context_tokens` after `llm_calls_per_job`. After the sentence `Label sets stay low-cardinality: …`, add:

```markdown
`llm_context_tokens{kind, tier}` is observed on every inference request, failed ones included, from the messages actually sent (R11.7); the per-request `llm_inference` log line carries the same size with the job's correlation ids so it can be joined to draft quality.
```

- [ ] **Step 3: Update the task entries**

In `specs/tasks.md`:

1. Change `- [ ] **4.12 Generation metrics**` to `- [~] **4.12 Generation metrics**`. Above its `_Requirements:_` line, add:

```markdown
  - Done: `record_inference` (`packages/llm/inference_metrics.py`) records `llm_context_tokens{kind, tier}` (R11.7), input/output tokens and priced cost on every request through `BudgetedLLMProvider` (generate, repair) and `InstrumentedLLMProvider` (triage wired in `services/triage_worker/main.py`), plus one `llm_inference` JSON log line. `DraftingService` counts `emails_generated_total{organization, category, model_tier}` and `generated_draft_cost_total{category, model_tier}` once per created draft. The generator no longer counts tokens itself. PromQL in `docs/observability.md`.
  - Left: held at `[~]` until RA.14 turns `make ci` green. Summarizer instrumentation and passing `metrics`/`price_table` into the generator and `DraftingService` happen in the ai-worker composition (4.13). Dashboards: 7.x.
```

2. In task 4.13, directly under its "Per job, in process" bullet, add:

```markdown
  - Compose with telemetry (4.12): wrap the summarizer's provider in `InstrumentedLLMProvider(kind=CallKind.SUMMARIZE)`, and pass `metrics` and `settings.llm.price_table` to `SinglePassGenerator` and `DraftingService`, so every request in the worker is measured.
```

- [ ] **Step 4: Verify and commit**

Run: `grep -n "4.12 Generation metrics" specs/tasks.md && grep -c "llm_context_tokens" docs/observability.md specs/design.md && grep -c "InstrumentedLLMProvider(kind=CallKind.SUMMARIZE)" specs/tasks.md`
Expected: `[~]` on the 4.12 line; each doc count ≥ 1; the 4.13 count is `1`.

```bash
git add docs/observability.md specs/design.md specs/tasks.md
git commit -m "docs(observability): generation metric reference, SC9 and context-size queries [task 4.12] [R21.4, R21.6, R11.7]"
```

---

## Dry-run verification (2026-09-27)

All five tasks were applied verbatim in a scratch copy. Eleven defects were found and corrected above:
- a missing update to `tests/unit/test_funnel_metrics.py`;
- `record_ai_cost`'s `dict` parameter widened to `Mapping` for mypy strict;
- SIM105 and SIM102 lint fixes;
- import order in `packages/llm/__init__.py`;
- `isinstance` narrowing in the triage test;
- the Task 3 helper guidance;
- `ruff format` checks added to Tasks 2 and 4, and every code block in this file normalised with `ruff format`.

Fragment blocks meant to be pasted into existing code are fenced as `text`, so ruff leaves them alone. All five Review Focus tests went RED, then GREEN for the stated reason. `FakeLLMProvider(canned_responses=[invalid, valid])` makes exactly one repair call. Measured after Task 4: unit 1255 passed, integration 149 passed, `ruff check .` clean, mypy baseline unchanged at 14 errors in 6 files.

## Self-review record

1. **Spec coverage.**
   - R11.7 and proposal line 948, context size on every request: Tasks 1–3. Summarization is wired in 4.13 and recorded in Task 5.
   - R21.4, the metric names: `generation_latency_ms` and `input/output_tokens_total` already existed and are now fed per request. `emails_generated_total` is now emitted (Task 4). `estimated_ai_cost` is the monotonic `estimated_ai_cost_total` (design §10).
   - R21.5, latency histograms: unchanged, and `llm_context_tokens` is a histogram too.
   - R21.6, cost per inference: Task 1. Per category and day: Task 4 plus the PromQL. Per email: the 4.11 column.
   - NFR8: PromQL alert expression in Task 5.
   - Low-cardinality labels: Task 1 test.
2. **Placeholder scan.** No TBD. Each code step has code. The fallback notes (import cycle in Task 2 Step 3, test-helper reuse in Task 3 Step 1, formatting in Task 1 Step 7) name the exact action.
3. **Type consistency.**
   - `record_inference(metrics, *, kind, tier, context_tokens, latency_ms, result, error, price_table)` has identical call sites in `budget.py` and `instrumented.py`.
   - `InstrumentedLLMProvider(provider, *, kind, metrics, price_table)` has the same signature in Tasks 2 and 3.
   - `UNKNOWN_CATEGORY` is defined in Task 4 and imported by its test.
4. **Review Focus.** Each of the five lines names its pinning test and owning task.

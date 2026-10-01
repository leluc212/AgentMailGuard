# Draft Persistence (task 4.11) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every schema-valid, citation-verified generation result becomes exactly one `generated_draft` row carrying body, verified citations, model, tier, escalation reason, prompt version, token counts and estimated cost, committed in the same transaction as the job's `GENERATING → DRAFTED` transition.

**Architecture:** A pure pricing function in `packages/core` turns token counts into a cost from the configured price table. A pure builder in `packages/llm` maps a `GenerationResult` plus its `ContextPackage` onto a `GeneratedDraft`, refusing anything unvalidated or unverified. A persistence unit of work in `packages/db` inserts the draft and transitions the job on one connection in one transaction, with a partial unique index as the one-draft-per-job backstop. A `DraftingService` in `services/ai_worker` ties it together: `CONTEXT_READY → GENERATING`, one generation call, build, persist, and an idempotent short-circuit when a redelivered job is already `DRAFTED`.

**Tech Stack:** Python 3.12, uv, asyncpg, PostgreSQL 16 + pgvector, pydantic-settings, pytest 9 + pytest-asyncio (auto mode), mypy strict, ruff.

**Spec:** Source of truth is `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md` (§24 structured output, §25 state machine, latency table "Draft persistence < 50 ms", Phase 4 deliverable "Email + Thread + RAG → generated draft"). Requirements: `specs/requirements.md` R16.4, R18.1, R21.6, plus R15.4, R16.2, R16.5, R18.4, R18.5, R19.4, R19.7. Design: `specs/design.md` §6.1 (`generated_draft` DDL), §9 (idempotency, worker-kill test), §10 (cost accounting). Task: `specs/tasks.md` 4.11, with the carried notes in 4.9 and 4.10.

## Global Constraints

- Python 3.12; run everything through `uv run`; ruff line length 100; `uv run mypy packages services` must stay clean (strict).
- pytest `addopts` already contains `-q`; never add another `-q`. pytest-asyncio runs in auto mode, so `@pytest.mark.asyncio` is optional; existing files use it and new files may too.
- Integration tests run only through `tests/integration/conftest.py`, which isolates them into vhost and database `rag_email_test`. Never connect a test to vhost `/` or database `rag_email`.
- Security design (auth, encryption, DLP, prompt-injection defence) is out of scope (requirements §0.5). Never log secrets or credentialed URLs.
- Persist drafts only through the new unit of work; never persist an unvalidated draft (R16.2, R16.3).
- Persist `CitationVerdict.citations`, never `content["knowledge_chunks"]` (tasks.md 4.10 note, R16.5).
- Record `"none"` as the escalation reason when no escalation happened (R15.4).
- An unknown model price is an unknown cost: store `NULL`, never `0` (R21.6, SC9).
- Commit format: `type(scope): summary [task 4.11] [R…]`, ending with the `Co-Authored-By` line from the session. **Standing user instruction: no commits until Phase 4 is complete.** Each Commit step becomes a controller snapshot.
- **Standing user instruction (RA.14):** no task goes to `[x]` in `specs/tasks.md` while `make ci` is red. 4.11 closes as `[~]` with done-notes.

## Review Focus

1. **A redelivered job that is already `DRAFTED`** (worker died after commit, before ack). Expected: no second generation call is billed and no second draft appears. Pinned by Task 4 `test_redelivered_drafted_job_skips_generation`.
2. **Two workers racing on the same job.** Expected: exactly one `generated_draft` row, and both calls return that row. Pinned by Task 3 `test_concurrent_persist_yields_one_draft` (Postgres).
3. **A model missing from the price table** (new model, typo in config). Expected: `cost_estimate` is `NULL`, not `0.0`, so cost reports never silently under-count. Pinned by Task 1 `test_unknown_model_has_unknown_cost` and Task 3 `test_unknown_cost_round_trips_as_null`.
4. **The model cites a chunk that was never supplied.** Expected: `citation_mismatch = true` and the bogus id is absent from the stored citations. Pinned by Task 2 `test_citations_come_from_verdict_not_model_output`.
5. **The job left `GENERATING` before persistence** (lease reaper moved it to `RETRY_PENDING`, or an operator acted). Expected: the transition fails and no orphan draft row survives. Pinned by Task 3 `test_illegal_state_rolls_back_draft_insert` (Postgres).

---

## Design decisions

- **D1. Pricing lives in `packages/core/pricing.py`.** `packages/observability/metrics.py` and `packages/llm` both need it; putting it in `packages/llm` would create an import cycle through `packages.llm.__init__`. `record_ai_cost` delegates to it, so metrics and persisted cost can never disagree.
- **D2. Unknown price ⇒ `None`.** `GeneratedDraft.cost_estimate` becomes `float | None` and `_row_to_draft` keeps `NULL` as `None`. The column is already nullable `NUMERIC(10,6)`, so values are rounded to 6 places. `record_ai_cost` keeps its public contract (returns `0.0` and warns) because a Prometheus counter cannot take `None`.
- **D3. The builder refuses rather than guesses.** No validated payload, no citation verdict, or no thread id raises `UnpersistableDraftError`. The generator always sets the first two, so this guards future callers.
- **D4. Subject is `Re: <original>`.** The model schema has no subject, and the template path already stores one. An existing `Re:` prefix is kept, and an empty subject stores `NULL`.
- **D5. One transaction, caller-owned connection.** `PostgresJobStore.transition_job_state` is split: the body moves to `transition_job_state_on(conn, …)`, and the old method wraps it in `acquire()` + `transaction()`. The persistence unit locks the job row `FOR UPDATE`, inserts the draft, then transitions on the same connection. An illegal transition rolls the insert back (R18.5).
- **D6. Idempotency is two-layered (design §9).** Fast path: a job already `DRAFTED` with a draft returns that draft (`created=False`, no event). Truth layer: migration `0004` adds `UNIQUE (job_id) WHERE job_id IS NOT NULL` on `generated_draft`. The row lock serialises racers, so the index is a backstop, not the normal path. The index is created without `CONCURRENTLY`, because the migrator runs each file in a transaction and the table is small. The live dev DB was checked on 2026-09-27: 3 drafts, no duplicate `job_id`.
- **D7. `DraftingService` owns `CONTEXT_READY → GENERATING`.** It accepts a job already in `GENERATING` (a redelivery after `RETRY_PENDING → GENERATING` recovery in `packages/broker/retry.py`). Any other state raises `IllegalStateTransitionError` before a billed call. Generation errors propagate unchanged, leaving the job in `GENERATING` for the retry ladder.

## Not in this plan

- The `ai-worker` RabbitMQ consumer that calls `DraftingService`, and the `FatalError` vs. retry-ladder decision for `UnvalidatedDraftError` / `DraftSchemaContractError`: task 4.13 in `specs/tasks.md`.
- Generation metrics (`emails_generated_total`, `generation_latency_ms`, cost counters per draft): task 4.12.
- Making the template path (`services/triage_worker/gate.py`) atomic. It inserts the draft and transitions in two transactions today, a pre-existing R18.5 gap. The new unit of work can serve it later.
- The `draft.persist` OpenTelemetry span (R21.2), the drafts API (R16.6) and feedback rows (R16.7).

## File structure

| File | Responsibility |
|---|---|
| `packages/core/pricing.py` (new) | `estimate_inference_cost`: tokens × price table → rounded USD or `None` |
| `packages/observability/metrics.py` (modify) | `record_ai_cost` delegates to `estimate_inference_cost` |
| `packages/domain/entities.py` (modify) | `GeneratedDraft.cost_estimate: float \| None` |
| `packages/llm/drafts.py` (new) | `build_generated_draft`, `reply_subject`, `UnpersistableDraftError` |
| `migrations/0004_generated_draft_one_per_job.{up,down}.sql` (new) | partial unique index on `generated_draft(job_id)` |
| `packages/db/job.py` (modify) | `PostgresJobStore.transition_job_state_on(conn, …)` |
| `packages/db/draft.py` (modify) | `insert_draft(conn, draft)`, `fetch_draft_for_job(conn, …)`; keep `NULL` cost |
| `packages/db/draft_persistence.py` (new) | `DraftPersistence` protocol, `DraftPersistOutcome`, Postgres and in-memory units of work |
| `services/ai_worker/drafting.py` (new) | `DraftingService`, `DraftingOutcome` |
| `specs/design.md`, `specs/tasks.md` (modify) | cost-`NULL` rule, 4.11 status |

---

### Task 1: Inference cost from the price table

**Files:**
- Create: `packages/core/pricing.py`
- Modify: `packages/observability/metrics.py` (`record_ai_cost`, around lines 370-420)
- Test: `tests/unit/test_inference_cost.py`

**Interfaces:**
- Consumes: `packages.core.settings.ModelPricing(input_per_m: float, output_per_m: float)`.
- Produces: `packages.core.pricing.estimate_inference_cost(model: str, input_tokens: int, output_tokens: int, price_table: Mapping[str, ModelPricing]) -> float | None` and `COST_DECIMAL_PLACES = 6`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_inference_cost.py`:

```python
"""Per-inference cost from the configured price table (R21.6, design.md §10)."""

from __future__ import annotations

import pytest

from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing
from packages.observability.metrics import create_pipeline_metrics, record_ai_cost

PRICES = {"model-a": ModelPricing(input_per_m=0.15, output_per_m=0.60)}


def test_cost_combines_input_and_output_prices() -> None:
    # 1,200 in * 0.15/M + 300 out * 0.60/M = 0.00018 + 0.00018
    assert estimate_inference_cost("model-a", 1_200, 300, PRICES) == pytest.approx(0.00036)


def test_cost_is_rounded_to_the_column_scale() -> None:
    # NUMERIC(10,6): 1 input token costs 0.00000015, which rounds to 0.0
    assert estimate_inference_cost("model-a", 1, 0, PRICES) == 0.0
    assert estimate_inference_cost("model-a", 7, 0, PRICES) == 0.000001


def test_zero_tokens_cost_nothing() -> None:
    assert estimate_inference_cost("model-a", 0, 0, PRICES) == 0.0


def test_unknown_model_has_unknown_cost() -> None:
    """A missing price is not a free call: None, never 0.0 (Review Focus 3)."""
    assert estimate_inference_cost("model-missing", 1_000, 1_000, PRICES) is None


def test_negative_token_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="token counts"):
        estimate_inference_cost("model-a", -1, 0, PRICES)


def test_metrics_cost_matches_estimate() -> None:
    metrics = create_pipeline_metrics()
    cost = record_ai_cost("model-a", "routine", 1_200, 300, PRICES, metrics=metrics)
    assert cost == estimate_inference_cost("model-a", 1_200, 300, PRICES)


def test_metrics_unknown_model_still_returns_zero() -> None:
    """record_ai_cost feeds a counter, which cannot take None; its contract is unchanged."""
    metrics = create_pipeline_metrics()
    assert record_ai_cost("model-missing", "routine", 10, 10, PRICES, metrics=metrics) == 0.0
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/unit/test_inference_cost.py`
Expected: collection error `ModuleNotFoundError: No module named 'packages.core.pricing'`.

- [ ] **Step 3: Implement the pricing function**

Create `packages/core/pricing.py`:

```python
"""Per-inference cost estimation from the configured price table (R21.6, design.md §10).

Lives in ``packages.core`` so persistence (``packages.llm.drafts``) and metrics
(``packages.observability.metrics``) share one formula without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping

from packages.core.settings import ModelPricing

COST_DECIMAL_PLACES = 6
"""Scale of ``generated_draft.cost_estimate`` (``NUMERIC(10,6)``)."""

_TOKENS_PER_PRICE_UNIT = 1_000_000


def estimate_inference_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_table: Mapping[str, ModelPricing],
) -> float | None:
    """Return the USD cost of one inference, or ``None`` when the model has no price.

    Args:
        model: Concrete model identifier reported by the provider.
        input_tokens: Prompt tokens billed.
        output_tokens: Completion tokens billed.
        price_table: Configured ``{model: ModelPricing}`` table (``LLMTiersSettings.price_table``).

    Returns:
        Cost rounded to ``COST_DECIMAL_PLACES``, or ``None`` if ``model`` is not priced.
        An unknown price is an unknown cost, never a free call.

    Raises:
        ValueError: If either token count is negative.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(
            f"token counts must be non-negative, got input={input_tokens} output={output_tokens}"
        )
    pricing = price_table.get(model)
    if pricing is None:
        return None
    cost = (
        input_tokens * pricing.input_per_m + output_tokens * pricing.output_per_m
    ) / _TOKENS_PER_PRICE_UNIT
    return round(cost, COST_DECIMAL_PLACES)
```

- [ ] **Step 4: Make `record_ai_cost` delegate**

In `packages/observability/metrics.py`, add `from packages.core.pricing import estimate_inference_cost` to the imports. In `record_ai_cost`, replace everything from `pricing = price_table.get(model)` to the end of the function with:

```python
    cost = estimate_inference_cost(model, input_tokens, output_tokens, price_table)
    if cost is None:
        logger.warning("No pricing configured for model '%s'; recorded tokens at 0 cost", model)
        return 0.0

    m.estimated_ai_cost_total.labels(model=model, tier=tier).inc(cost)
    return cost
```

If importing `packages.core.pricing` at module level raises a circular-import error, move it inside the function. `packages.core.settings` does not import observability, so this is not expected.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_inference_cost.py`
Expected: 7 passed.

Run: `uv run pytest tests/unit -k "cost or metrics"`
Expected: all pass. Any existing `record_ai_cost` test that compared an unrounded float must still pass, because the rounding error is below 1e-6. If one asserts exact equality on an unrounded value, change it to `pytest.approx` and name it under Deviations.

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff check packages/core/pricing.py packages/observability/metrics.py tests/unit/test_inference_cost.py && uv run ruff format --check packages/core/pricing.py tests/unit/test_inference_cost.py && uv run mypy packages/core/pricing.py packages/observability/metrics.py tests/unit/test_inference_cost.py`
Expected: clean.

```bash
git add packages/core/pricing.py packages/observability/metrics.py tests/unit/test_inference_cost.py
git commit -m "feat(cost): estimate inference cost from the price table, unknown price is NULL [task 4.11] [R21.6]"
```

---

### Task 2: Build the draft record from a generation result

**Files:**
- Modify: `packages/domain/entities.py` (`GeneratedDraft.cost_estimate`, line ~368)
- Modify: `packages/db/draft.py` (`_row_to_draft`, the `cost_estimate=` line)
- Create: `packages/llm/drafts.py`
- Test: `tests/unit/test_draft_builder.py`

**Interfaces:**
- Consumes: `estimate_inference_cost` (Task 1); `GenerationResult` (`packages.llm.generator`) with `validated_payload: DraftReplyPayload | None`, `citation_verdict: CitationVerdict | None`, `model`, `tier`, `escalation_reason`, `prompt_version`, `input_tokens`, `output_tokens`; `ContextPackage.current_message: NormalizedMessage` with `message_id`, `thread_id`, `organization_id`, `subject`.
- Produces: `packages.llm.drafts.build_generated_draft(result: GenerationResult, context: ContextPackage, *, job_id: UUID | str, price_table: Mapping[str, ModelPricing], draft_id: UUID | None = None) -> GeneratedDraft`; `reply_subject(subject: str) -> str | None`; `UnpersistableDraftError(ValueError)`; `NO_ESCALATION = "none"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_draft_builder.py`:

```python
"""Mapping a validated generation result onto the persisted draft (R16.4, R16.5, R15.4, R21.6)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.domain.entities import ContextPackage, EmailAddress, NormalizedMessage
from packages.llm.citations import CitationVerdict
from packages.llm.drafts import (
    NO_ESCALATION,
    UnpersistableDraftError,
    build_generated_draft,
    reply_subject,
)
from packages.llm.generator import GenerationResult
from packages.llm.profile import AgentProfile
from packages.llm.protocol import ModelTier
from packages.llm.validation import DraftReplyPayload

PRICES = {"model-routine": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
SUPPLIED = {
    "citation_id": "DOC-125-08",
    "chunk_id": "chunk-1",
    "document_id": "doc-1",
    "external_id": "DOC-125-08",
}


def _context(subject: str = "Password reset", thread_id: object = None) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4() if thread_id is None else thread_id,  # type: ignore[arg-type]
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="prov-1",
        sender=EmailAddress(email="alice@example.com"),
        received_at=datetime.now(UTC),
        subject=subject,
    )
    return ContextPackage(
        agent_instructions="a", category_instructions="c", current_message=message
    )


def _result(**overrides: object) -> GenerationResult:
    payload = DraftReplyPayload(
        action="reply",
        draft="Hello Alice, open Settings and choose Reset Password.",
        confidence=0.91,
        knowledge_chunks=["DOC-125-08", "DOC-999-99"],
        thread_summary_updated=False,
        model_tier="routine",
    )
    profile = AgentProfile(
        profile="technical_support",
        knowledge_domain="support",
        response_style="professional",
        prompt_template="prompts/technical_support.v1.j2",
        output_schema="schemas/reply.v1.json",
        prompt_version="technical_support.v1",
    )
    result = GenerationResult(
        content=payload.model_dump(),
        profile=profile,
        prompt_version="technical_support.v1",
        model="model-routine",
        tier=ModelTier.ROUTINE,
        input_tokens=1_200,
        output_tokens=300,
        validated_payload=payload,
        citation_verdict=CitationVerdict(
            citations=[dict(SUPPLIED)],
            mismatched=["DOC-999-99"],
            supplied_count=1,
            cited_count=2,
        ),
    )
    return replace(result, **overrides)  # type: ignore[arg-type]


def test_every_required_field_is_mapped() -> None:
    context = _context()
    job_id = uuid4()
    draft = build_generated_draft(_result(), context, job_id=job_id, price_table=PRICES)

    message = context.current_message
    assert draft.job_id == job_id
    assert draft.organization_id == message.organization_id
    assert draft.message_id == message.message_id
    assert draft.thread_id == message.thread_id
    assert draft.action == "reply"
    assert draft.body.startswith("Hello Alice")
    assert draft.confidence == 0.91
    assert draft.model_name == "model-routine"
    assert draft.model_tier == "routine"
    assert draft.prompt_version == "technical_support.v1"
    assert (draft.input_tokens, draft.output_tokens) == (1_200, 300)
    assert draft.cost_estimate == pytest.approx(0.00036)
    assert draft.status == "draft"


def test_citations_come_from_verdict_not_model_output() -> None:
    """The model cited DOC-999-99, which was never supplied (Review Focus 4)."""
    draft = build_generated_draft(_result(), _context(), job_id=uuid4(), price_table=PRICES)
    assert draft.citations == [SUPPLIED]
    assert draft.citation_mismatch is True


def test_no_escalation_is_recorded_as_none() -> None:
    draft = build_generated_draft(_result(), _context(), job_id=uuid4(), price_table=PRICES)
    assert draft.escalation_reason == NO_ESCALATION == "none"


def test_escalation_reason_and_tier_are_kept() -> None:
    result = _result(tier=ModelTier.HIGH_CAPABILITY, escalation_reason="low_confidence")
    draft = build_generated_draft(result, _context(), job_id=uuid4(), price_table=PRICES)
    assert draft.model_tier == "high_capability"
    assert draft.escalation_reason == "low_confidence"


def test_unpriced_model_stores_unknown_cost() -> None:
    result = _result(model="model-unpriced")
    draft = build_generated_draft(result, _context(), job_id=uuid4(), price_table=PRICES)
    assert draft.cost_estimate is None


def test_unvalidated_result_is_refused() -> None:
    with pytest.raises(UnpersistableDraftError, match="validated"):
        build_generated_draft(
            _result(validated_payload=None), _context(), job_id=uuid4(), price_table=PRICES
        )


def test_unverified_citations_are_refused() -> None:
    with pytest.raises(UnpersistableDraftError, match="citations"):
        build_generated_draft(
            _result(citation_verdict=None), _context(), job_id=uuid4(), price_table=PRICES
        )


@pytest.mark.parametrize("thread_id", ["", "   "])
def test_draft_without_thread_is_refused(thread_id: str) -> None:
    with pytest.raises(UnpersistableDraftError, match="thread"):
        build_generated_draft(
            _result(), _context(thread_id=thread_id), job_id=uuid4(), price_table=PRICES
        )


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Password reset", "Re: Password reset"),
        ("  Password reset  ", "Re: Password reset"),
        ("Re: Password reset", "Re: Password reset"),
        ("RE: Password reset", "RE: Password reset"),
        ("", None),
        ("   ", None),
    ],
)
def test_reply_subject(subject: str, expected: str | None) -> None:
    assert reply_subject(subject) == expected
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/unit/test_draft_builder.py`
Expected: collection error `ModuleNotFoundError: No module named 'packages.llm.drafts'`.

- [ ] **Step 3: Let the entity and store carry an unknown cost**

In `packages/domain/entities.py`, class `GeneratedDraft`, change:

```python
    cost_estimate: float = 0.0
```

to:

```python
    cost_estimate: float | None = 0.0  # None: model missing from the price table (R21.6)
```

In `packages/db/draft.py`, `_row_to_draft`, change:

```text
        cost_estimate=float(row["cost_estimate"] or 0.0),
```

to:

```text
        cost_estimate=(float(row["cost_estimate"]) if row["cost_estimate"] is not None else None),
```

- [ ] **Step 4: Implement the builder**

Create `packages/llm/drafts.py`:

```python
"""Map a validated generation result onto the persisted draft record.

Requirements: R16.4 (persist citations, model, tier, prompt version, tokens, cost),
R16.2 (never persist an unvalidated draft), R16.5 (persist only verified citations),
R15.4 (escalation reason or ``none``), R21.6 (cost from the price table).
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing
from packages.domain.entities import ContextPackage, GeneratedDraft
from packages.llm.generator import GenerationResult

NO_ESCALATION = "none"
"""Escalation reason recorded when the job stayed on its default tier (R15.4)."""

_REPLY_PREFIX = "Re: "


class UnpersistableDraftError(ValueError):
    """A generation result lacks something every persisted draft must carry."""


def reply_subject(subject: str) -> str | None:
    """Return the reply subject for ``subject``: ``Re: <subject>``, unprefixed twice.

    An empty subject yields ``None`` (stored as NULL) rather than a bare ``Re:``.
    """
    clean = subject.strip()
    if not clean:
        return None
    if clean.lower().startswith("re:"):
        return clean
    return f"{_REPLY_PREFIX}{clean}"


def build_generated_draft(
    result: GenerationResult,
    context: ContextPackage,
    *,
    job_id: UUID | str,
    price_table: Mapping[str, ModelPricing],
    draft_id: UUID | None = None,
) -> GeneratedDraft:
    """Build the ``generated_draft`` record for one validated generation.

    Args:
        result: Output of ``SinglePassGenerator.generate_draft``.
        context: The context package the draft was generated from.
        job_id: Processing job that owns the draft.
        price_table: Configured ``{model: ModelPricing}`` table.
        draft_id: Optional explicit id; a new UUID otherwise.

    Returns:
        A ``GeneratedDraft`` in status ``draft``, not yet persisted.

    Raises:
        UnpersistableDraftError: If the result was never validated, its citations were
            never verified, or the current message has no thread.
    """
    payload = result.validated_payload
    if payload is None:
        raise UnpersistableDraftError(
            "Generation result was never schema-validated; refusing to persist (R16.2)"
        )
    verdict = result.citation_verdict
    if verdict is None:
        raise UnpersistableDraftError(
            "Generation result carries no verified citations; refusing to persist (R16.5)"
        )
    message = context.current_message
    if not str(message.thread_id or "").strip():
        raise UnpersistableDraftError(
            f"Message {message.message_id} has no thread; generated_draft.thread_id is required"
        )

    return GeneratedDraft(
        id=draft_id or uuid4(),
        organization_id=message.organization_id,
        job_id=job_id,
        message_id=message.message_id,
        thread_id=message.thread_id,
        action=payload.action,
        subject=reply_subject(message.subject),
        body=payload.draft,
        confidence=payload.confidence,
        citations=[dict(citation) for citation in verdict.citations],
        citation_mismatch=verdict.mismatch,
        model_name=result.model,
        model_tier=str(result.tier),
        escalation_reason=(
            str(result.escalation_reason) if result.escalation_reason else NO_ESCALATION
        ),
        prompt_version=result.prompt_version,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_estimate=estimate_inference_cost(
            result.model, result.input_tokens, result.output_tokens, price_table
        ),
        status="draft",
    )
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_draft_builder.py tests/unit/test_draft_store.py tests/unit/test_template_gate.py`
Expected: all pass (15 in the new file).

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff check packages/llm/drafts.py packages/domain/entities.py packages/db/draft.py tests/unit/test_draft_builder.py && uv run ruff format --check packages/llm/drafts.py tests/unit/test_draft_builder.py && uv run mypy packages services tests/unit/test_draft_builder.py`
Expected: clean. If mypy flags a consumer of `cost_estimate` that assumed `float`, fix that consumer to handle `None`, and list it.

```bash
git add packages/llm/drafts.py packages/domain/entities.py packages/db/draft.py tests/unit/test_draft_builder.py
git commit -m "feat(llm): build generated_draft from a validated, citation-verified result [task 4.11] [R16.4, R16.5, R15.4, R21.6]"
```

---

### Task 3: Atomic, idempotent draft persistence with `GENERATING → DRAFTED`

**Files:**
- Create: `migrations/0004_generated_draft_one_per_job.up.sql`, `migrations/0004_generated_draft_one_per_job.down.sql`
- Modify: `packages/db/job.py` (`PostgresJobStore.transition_job_state`, lines ~324-435)
- Modify: `packages/db/draft.py` (extract `insert_draft`, add `fetch_draft_for_job`)
- Create: `packages/db/draft_persistence.py`
- Test: `tests/unit/test_draft_persistence.py`, `tests/integration/test_draft_persistence_postgres.py`

**Interfaces:**
- Consumes: `GeneratedDraft` (Task 2 shape); `transition_job(job, target_state, payload, trace_id)` state machine; `InMemoryJobStore`, `InMemoryDraftStore`.
- Produces:
  - `PostgresJobStore.transition_job_state_on(conn, organization_id, job_id, target_state, payload=None, result_ref=None, error_message=None, message_id=None, thread_id=None) -> tuple[Job, ProcessingEvent]`
  - `packages.db.draft.insert_draft(conn, draft: GeneratedDraft) -> GeneratedDraft` and `fetch_draft_for_job(conn, job_id: UUID, organization_id: UUID) -> GeneratedDraft | None`
  - `packages.db.draft_persistence.DraftPersistOutcome(draft: GeneratedDraft, job: Job, event: ProcessingEvent | None, created: bool)`
  - `DraftPersistence` protocol with `persist_drafted(draft: GeneratedDraft) -> DraftPersistOutcome` and `find_draft_for_job(organization_id, job_id) -> GeneratedDraft | None`
  - `PostgresDraftPersistence(pool)`, `InMemoryDraftPersistence(job_store: InMemoryJobStore, draft_store: InMemoryDraftStore)`
  - `drafted_event_payload(draft: GeneratedDraft) -> dict[str, Any]`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_draft_persistence.py`:

```python
"""Draft persistence unit of work: draft + GENERATING -> DRAFTED together (R16.4, R18.1, R18.5)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState


async def _setup(state: JobState) -> tuple[InMemoryDraftPersistence, InMemoryJobStore, Job]:
    jobs = InMemoryJobStore()
    job, _ = await jobs.create_job(
        Job(organization_id=uuid4(), state=state.value, idempotency_key=f"k-{uuid4()}")
    )
    return InMemoryDraftPersistence(jobs, InMemoryDraftStore()), jobs, job


def _draft(job: Job) -> GeneratedDraft:
    return GeneratedDraft(
        organization_id=job.organization_id,
        job_id=job.id,
        message_id=uuid4(),
        thread_id=uuid4(),
        body="Hello",
        model_name="model-routine",
        model_tier="routine",
        escalation_reason="none",
        prompt_version="p.v1",
        input_tokens=10,
        output_tokens=5,
        cost_estimate=0.000001,
    )


async def test_persist_moves_generating_job_to_drafted() -> None:
    persistence, jobs, job = await _setup(JobState.GENERATING)
    draft = _draft(job)

    outcome = await persistence.persist_drafted(draft)

    assert outcome.created is True
    assert outcome.draft.id == draft.id
    assert outcome.job.state == JobState.DRAFTED.value
    assert outcome.job.result_ref == {"draft_id": str(draft.id)}
    assert outcome.event is not None
    assert (outcome.event.state_from, outcome.event.state_to) == ("GENERATING", "DRAFTED")
    assert outcome.event.payload["draft_id"] == str(draft.id)
    assert outcome.event.payload["cost_estimate"] == 0.000001
    stored = await persistence.find_draft_for_job(job.organization_id, job.id)
    assert stored is not None and stored.id == draft.id


async def test_job_not_generating_is_refused_without_a_draft() -> None:
    persistence, _, job = await _setup(JobState.CONTEXT_READY)

    with pytest.raises(IllegalStateTransitionError):
        await persistence.persist_drafted(_draft(job))

    assert await persistence.find_draft_for_job(job.organization_id, job.id) is None


async def test_already_drafted_job_returns_the_existing_draft() -> None:
    persistence, jobs, job = await _setup(JobState.GENERATING)
    first = await persistence.persist_drafted(_draft(job))
    events_before = len(await jobs.list_events_for_job(job.organization_id, job.id))

    second = await persistence.persist_drafted(_draft(job))

    assert second.created is False
    assert second.event is None
    assert second.draft.id == first.draft.id
    assert len(await jobs.list_events_for_job(job.organization_id, job.id)) == events_before


async def test_draft_for_another_tenant_is_refused() -> None:
    persistence, _, job = await _setup(JobState.GENERATING)
    foreign = _draft(job)
    foreign.organization_id = uuid4()

    with pytest.raises(KeyError):
        await persistence.persist_drafted(foreign)


async def test_draft_without_job_is_refused() -> None:
    persistence, _, job = await _setup(JobState.GENERATING)
    orphan = _draft(job)
    orphan.job_id = None

    with pytest.raises(ValueError, match="job_id"):
        await persistence.persist_drafted(orphan)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_draft_persistence.py`
Expected: collection error `ModuleNotFoundError: No module named 'packages.db.draft_persistence'`.

- [ ] **Step 3: Add the one-draft-per-job migration**

Create `migrations/0004_generated_draft_one_per_job.up.sql`:

```sql
-- Migration: 0004_generated_draft_one_per_job.up.sql
-- One draft per processing job: the truth layer behind redelivery idempotency
-- (R19.4, R19.7, design.md §9 "Assert COUNT(generated_draft) == 1").
-- Not CONCURRENTLY: the migrator runs each file inside a transaction.
CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_draft_job
    ON generated_draft (job_id)
    WHERE job_id IS NOT NULL;
```

Create `migrations/0004_generated_draft_one_per_job.down.sql`:

```sql
-- Migration: 0004_generated_draft_one_per_job.down.sql
DROP INDEX IF EXISTS uq_generated_draft_job;
```

- [ ] **Step 4: Split the Postgres job transition so a caller can own the transaction**

In `packages/db/job.py`, class `PostgresJobStore`, keep the `transition_job_state` signature and docstring. Replace its body with:

```python
        async with self.pool.acquire() as conn, conn.transaction():
            return await self.transition_job_state_on(
                conn,
                organization_id=organization_id,
                job_id=job_id,
                target_state=target_state,
                payload=payload,
                result_ref=result_ref,
                error_message=error_message,
                message_id=message_id,
                thread_id=thread_id,
            )

    async def transition_job_state_on(
        self,
        conn: Any,
        organization_id: UUID | str,
        job_id: UUID | str,
        target_state: JobState | str,
        payload: dict[str, Any] | None = None,
        result_ref: dict[str, Any] | None = None,
        error_message: str | None = None,
        message_id: UUID | str | None = None,
        thread_id: UUID | str | None = None,
    ) -> tuple[Job, ProcessingEvent]:
        """Transition on a caller-owned connection inside the caller's transaction (R18.5).

        Lets a side effect (for example a draft insert) commit or roll back together
        with the state change and its ``processing_event`` row. The caller must have
        opened ``conn.transaction()``.
        """
```

Then move the old body into `transition_job_state_on` verbatim: everything from `org_u = _to_uuid(organization_id)` through `return final_job, persisted_event`. Delete only the line `async with self.pool.acquire() as conn, conn.transaction():` and dedent the block it opened by one level. The SQL strings, `FOR UPDATE`, and the logging stay unchanged. `conn` is typed `Any`, because `pool.acquire()` yields a `PoolConnectionProxy`, not an `asyncpg.Connection`. `Any` is already imported in `job.py`.

Run: `uv run pytest tests/unit -k "job" && uv run mypy packages/db/job.py`
Expected: all pass, clean. This is behaviour-preserving; the Postgres job tests in Step 9 cover it live.

- [ ] **Step 5: Extract the draft insert and add the per-job lookup**

In `packages/db/draft.py`, add these module-level functions after `_row_to_draft`. Move the `INSERT … RETURNING *` SQL string out of `PostgresDraftStore.create_draft` into `_INSERT_DRAFT_SQL`, verbatim.

```python
_INSERT_DRAFT_SQL = """
    INSERT INTO generated_draft (
        id, organization_id, job_id, message_id, thread_id,
        action, subject, body, confidence, citations,
        citation_mismatch, model_name, model_tier, escalation_reason,
        prompt_version, input_tokens, output_tokens, cost_estimate,
        status, provider_ref, created_at
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8, $9, $10::jsonb,
        $11, $12, $13, $14,
        $15, $16, $17, $18,
        $19, $20, $21
    )
    RETURNING *;
"""


async def insert_draft(conn: Any, draft: GeneratedDraft) -> GeneratedDraft:
    """Insert ``draft`` on ``conn`` (a connection or pool proxy) and return the stored row."""
    draft_id = _to_uuid(draft.id)
    row = await conn.fetchrow(
        _INSERT_DRAFT_SQL,
        draft_id,
        _to_uuid(draft.organization_id),
        _opt_uuid(draft.job_id),
        _to_uuid(draft.message_id),
        _to_uuid(draft.thread_id),
        draft.action,
        draft.subject,
        draft.body,
        draft.confidence,
        json.dumps(draft.citations or []),
        draft.citation_mismatch,
        draft.model_name,
        draft.model_tier,
        draft.escalation_reason,
        draft.prompt_version,
        draft.input_tokens,
        draft.output_tokens,
        draft.cost_estimate,
        draft.status,
        draft.provider_ref,
        draft.created_at or datetime.now(UTC),
    )
    if row is None:
        raise RuntimeError(f"Failed to insert generated_draft {draft_id}")
    return _row_to_draft(row)


async def fetch_draft_for_job(
    conn: Any, job_id: UUID, organization_id: UUID
) -> GeneratedDraft | None:
    """Return the job's draft within the tenant, newest first, or ``None``."""
    row = await conn.fetchrow(
        """
        SELECT * FROM generated_draft
        WHERE job_id = $1 AND organization_id = $2
        ORDER BY created_at DESC
        LIMIT 1;
        """,
        job_id,
        organization_id,
    )
    return _row_to_draft(row) if row is not None else None
```

Add `from typing import Any` to the imports. Replace the body of `PostgresDraftStore.create_draft` with:

```python
        async with self._pool.acquire() as conn:
            return await insert_draft(conn, draft)
```

Run: `uv run pytest tests/unit/test_draft_store.py && uv run mypy packages/db/draft.py`
Expected: pass, clean.

- [ ] **Step 6: Implement the persistence unit of work**

Create `packages/db/draft_persistence.py`:

```python
"""Persist a generated draft together with the job's GENERATING -> DRAFTED transition.

Requirements: R16.4 (persist every draft), R18.1 (DRAFTED state), R18.4 (event row),
R18.5 (state change in the same transaction as its side effect), R19.4 / R19.7
(one draft per job across redeliveries; design.md §9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.db.draft import (
    InMemoryDraftStore,
    fetch_draft_for_job,
    insert_draft,
)
from packages.db.job import InMemoryJobStore, PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job, ProcessingEvent
from packages.domain.state_machine import JobState


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class DraftPersistOutcome:
    """Result of persisting a draft.

    ``created`` is False (and ``event`` None) when an earlier delivery of the same job
    already persisted its draft; ``draft`` is then that earlier draft.
    """

    draft: GeneratedDraft
    job: Job
    event: ProcessingEvent | None
    created: bool


def drafted_event_payload(draft: GeneratedDraft) -> dict[str, Any]:
    """Audit payload for the GENERATING -> DRAFTED ``processing_event`` (R18.4)."""
    return {
        "draft_id": str(draft.id),
        "model": draft.model_name,
        "model_tier": draft.model_tier,
        "escalation_reason": draft.escalation_reason,
        "prompt_version": draft.prompt_version,
        "input_tokens": draft.input_tokens,
        "output_tokens": draft.output_tokens,
        "cost_estimate": draft.cost_estimate,
        "citation_mismatch": draft.citation_mismatch,
    }


def _require_job_id(draft: GeneratedDraft) -> UUID:
    if draft.job_id is None or str(draft.job_id) == "":
        raise ValueError(f"Draft {draft.id} has no job_id; generated drafts belong to a job")
    return _to_uuid(draft.job_id)


@runtime_checkable
class DraftPersistence(Protocol):
    """Unit of work persisting a draft and transitioning its job to DRAFTED."""

    async def persist_drafted(self, draft: GeneratedDraft) -> DraftPersistOutcome:
        """Insert ``draft`` and move its job GENERATING -> DRAFTED atomically.

        Raises:
            ValueError: If the draft has no ``job_id``.
            KeyError: If the job does not exist in the draft's organization.
            IllegalStateTransitionError: If the job is not in GENERATING (and not
                already DRAFTED with a draft). Nothing is persisted.
        """
        ...

    async def find_draft_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GeneratedDraft | None:
        """Return the job's persisted draft, or ``None``."""
        ...


class PostgresDraftPersistence:
    """PostgreSQL unit of work: row lock, draft insert and transition on one connection."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._jobs = PostgresJobStore(pool)

    async def persist_drafted(self, draft: GeneratedDraft) -> DraftPersistOutcome:
        job_id = _require_job_id(draft)
        org_id = _to_uuid(draft.organization_id)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, organization_id, message_id, thread_id, job_type, state,
                       attempt, max_attempts, idempotency_key, result_ref, queue_name,
                       priority, lease_expires_at, last_error, next_retry_at, trace_id,
                       created_at, updated_at
                FROM processing_job
                WHERE id = $1 AND organization_id = $2
                FOR UPDATE;
                """,
                job_id,
                org_id,
            )
            if row is None:
                raise KeyError(f"Job {job_id} not found for organization {org_id}")
            if row["state"] == JobState.DRAFTED.value:
                existing = await fetch_draft_for_job(conn, job_id, org_id)
                if existing is not None:
                    return DraftPersistOutcome(
                        draft=existing,
                        job=PostgresJobStore._row_to_job(row),
                        event=None,
                        created=False,
                    )
            stored = await insert_draft(conn, draft)
            job, event = await self._jobs.transition_job_state_on(
                conn,
                organization_id=org_id,
                job_id=job_id,
                target_state=JobState.DRAFTED,
                payload=drafted_event_payload(stored),
                result_ref={"draft_id": str(stored.id)},
                message_id=stored.message_id,
                thread_id=stored.thread_id,
            )
            return DraftPersistOutcome(draft=stored, job=job, event=event, created=True)

    async def find_draft_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GeneratedDraft | None:
        async with self._pool.acquire() as conn:
            return await fetch_draft_for_job(conn, _to_uuid(job_id), _to_uuid(organization_id))


class InMemoryDraftPersistence:
    """In-memory unit of work for unit tests; validates the transition before storing."""

    def __init__(self, job_store: InMemoryJobStore, draft_store: InMemoryDraftStore) -> None:
        self._jobs = job_store
        self._drafts = draft_store

    async def persist_drafted(self, draft: GeneratedDraft) -> DraftPersistOutcome:
        job_id = _require_job_id(draft)
        org_id = _to_uuid(draft.organization_id)
        job = await self._jobs.get_job(org_id, job_id)
        if job is None:
            raise KeyError(f"Job {job_id} not found for organization {org_id}")
        if job.state == JobState.DRAFTED.value:
            existing = await self.find_draft_for_job(org_id, job_id)
            if existing is not None:
                return DraftPersistOutcome(draft=existing, job=job, event=None, created=False)
        # Transition first: it raises on an illegal state before anything is stored.
        job, event = await self._jobs.transition_job_state(
            organization_id=org_id,
            job_id=job_id,
            target_state=JobState.DRAFTED,
            payload=drafted_event_payload(draft),
            result_ref={"draft_id": str(draft.id)},
            message_id=draft.message_id,
            thread_id=draft.thread_id,
        )
        stored = await self._drafts.create_draft(draft)
        return DraftPersistOutcome(draft=stored, job=job, event=event, created=True)

    async def find_draft_for_job(
        self, organization_id: UUID | str, job_id: UUID | str
    ) -> GeneratedDraft | None:
        drafts = await self._drafts.list_drafts_for_job(job_id, organization_id)
        return drafts[0] if drafts else None
```

`PostgresJobStore._row_to_job` is a `@staticmethod` (`job.py:822`). If ruff flags the private access (`SLF001`), add `# noqa: SLF001`. Do not duplicate the mapper. `InMemoryJobStore.get_job` takes `(organization_id, job_id)`.

- [ ] **Step 7: Run the unit tests and watch them pass**

Run: `uv run pytest tests/unit/test_draft_persistence.py`
Expected: 5 passed.

- [ ] **Step 8: Write the Postgres integration tests**

Create `tests/integration/test_draft_persistence_postgres.py`:

```python
"""Draft persistence against live PostgreSQL in rag_email_test (R16.4, R18.5, R19.4, R19.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed(pool: asyncpg.Pool, state: JobState) -> tuple[Job, uuid.UUID, uuid.UUID]:
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Box', 'active')",
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'c@example.com', 'C', '[]', 'Hi', 'Hi', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:6]}",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=state.value,
            idempotency_key=f"draft-persist-{uuid.uuid4()}",
        )
    )
    return job, msg_id, thread_id


def _draft(job: Job, msg_id: uuid.UUID, thread_id: uuid.UUID, cost: float | None) -> GeneratedDraft:
    return GeneratedDraft(
        organization_id=job.organization_id,
        job_id=job.id,
        message_id=msg_id,
        thread_id=thread_id,
        subject="Re: Hi",
        body="Hello from the model",
        confidence=0.9,
        citations=[{"citation_id": "DOC-1", "chunk_id": "c1"}],
        citation_mismatch=False,
        model_name="model-routine",
        model_tier="routine",
        escalation_reason="none",
        prompt_version="p.v1",
        input_tokens=1_200,
        output_tokens=300,
        cost_estimate=cost,
    )


async def _draft_count(pool: asyncpg.Pool, job_id: object) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval("SELECT count(*) FROM generated_draft WHERE job_id = $1", job_id)
        )


async def test_draft_and_transition_commit_together(db_pool: asyncpg.Pool) -> None:
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    outcome = await PostgresDraftPersistence(db_pool).persist_drafted(
        _draft(job, msg_id, thread_id, 0.00036)
    )

    assert outcome.created is True
    assert outcome.job.state == JobState.DRAFTED.value
    assert outcome.job.result_ref == {"draft_id": str(outcome.draft.id)}
    assert outcome.draft.cost_estimate == pytest.approx(0.00036)
    assert outcome.draft.citations == [{"citation_id": "DOC-1", "chunk_id": "c1"}]
    events = await PostgresJobStore(db_pool).list_events_for_job(job.organization_id, job.id)
    assert [(e.state_from, e.state_to) for e in events][-1] == ("GENERATING", "DRAFTED")
    assert events[-1].payload["draft_id"] == str(outcome.draft.id)


async def test_illegal_state_rolls_back_draft_insert(db_pool: asyncpg.Pool) -> None:
    """Review Focus 5: the job left GENERATING; no orphan draft may survive."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.CONTEXT_READY)

    with pytest.raises(IllegalStateTransitionError):
        await PostgresDraftPersistence(db_pool).persist_drafted(
            _draft(job, msg_id, thread_id, 0.0001)
        )

    assert await _draft_count(db_pool, job.id) == 0
    stored = await PostgresJobStore(db_pool).get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.CONTEXT_READY.value


async def test_unknown_cost_round_trips_as_null(db_pool: asyncpg.Pool) -> None:
    """Review Focus 3: an unpriced model stores NULL, and reads back as None."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    persistence = PostgresDraftPersistence(db_pool)
    await persistence.persist_drafted(_draft(job, msg_id, thread_id, None))

    stored = await persistence.find_draft_for_job(job.organization_id, job.id)
    assert stored is not None and stored.cost_estimate is None


async def test_redelivery_returns_existing_draft(db_pool: asyncpg.Pool) -> None:
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    persistence = PostgresDraftPersistence(db_pool)
    first = await persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001))

    second = await persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001))

    assert second.created is False and second.event is None
    assert second.draft.id == first.draft.id
    assert await _draft_count(db_pool, job.id) == 1


async def test_concurrent_persist_yields_one_draft(db_pool: asyncpg.Pool) -> None:
    """Review Focus 2: two workers racing on one job leave exactly one draft."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    persistence = PostgresDraftPersistence(db_pool)

    outcomes = await asyncio.gather(
        persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001)),
        persistence.persist_drafted(_draft(job, msg_id, thread_id, 0.0001)),
    )

    assert sorted(o.created for o in outcomes) == [False, True]
    assert outcomes[0].draft.id == outcomes[1].draft.id
    assert await _draft_count(db_pool, job.id) == 1


async def test_unique_index_rejects_a_second_draft_for_a_job(db_pool: asyncpg.Pool) -> None:
    """The truth layer holds even for a writer that bypasses the unit of work."""
    job, msg_id, thread_id = await _seed(db_pool, JobState.GENERATING)
    await PostgresDraftPersistence(db_pool).persist_drafted(_draft(job, msg_id, thread_id, 0.0))

    async with db_pool.acquire() as conn:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO generated_draft (id, organization_id, job_id, message_id, thread_id,"
                " action, body) VALUES ($1, $2, $3, $4, $5, 'reply', 'dup')",
                uuid.uuid4(),
                job.organization_id,
                job.id,
                msg_id,
                thread_id,
            )
```

- [ ] **Step 9: Run the integration tests**

Run: `uv run pytest tests/integration/test_draft_persistence_postgres.py tests/integration/test_template_gate_postgres.py tests/integration/test_database_schema.py`
Expected: all pass. The isolation fixture migrates `rag_email_test`, so `0004` is applied there, and `test_migration_reversibility` rolls it back and re-applies it. If `test_unique_index_rejects_a_second_draft_for_a_job` fails with no error raised, the migration did not run. Check `discover_migrations()` picks up `0004`.

- [ ] **Step 10: Full unit suite, lint, types, commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_draft_persistence.py tests/integration/test_draft_persistence_postgres.py`
Expected: all pass, clean.

Run: `uv run ruff format --check packages/db/draft_persistence.py packages/db/draft.py packages/db/job.py tests/unit/test_draft_persistence.py tests/integration/test_draft_persistence_postgres.py`
Expected: formatted. `packages/db/job.py` may already have been on the RA.14 baseline list; if so, `ruff format --check` on it was failing before this task. Record that; do not reformat the whole file.

```bash
git add migrations/0004_generated_draft_one_per_job.up.sql migrations/0004_generated_draft_one_per_job.down.sql \
  packages/db/job.py packages/db/draft.py packages/db/draft_persistence.py \
  tests/unit/test_draft_persistence.py tests/integration/test_draft_persistence_postgres.py
git commit -m "feat(db): persist a draft and GENERATING->DRAFTED in one transaction, one draft per job [task 4.11] [R16.4, R18.1, R18.5, R19.7]"
```

---

### Task 4: Drafting service — generate once, persist once

**Files:**
- Create: `services/ai_worker/drafting.py`
- Test: `tests/unit/test_drafting_service.py`, `tests/integration/test_drafting_service_postgres.py`

**Interfaces:**
- Consumes: `SinglePassGenerator.generate_draft(context, *, category=None, profile=None, budget_tracker=None, escalated_tier=None, escalation_reason=None, temperature=0.0, max_tokens=1000) -> GenerationResult`; `build_generated_draft` (Task 2); `DraftPersistence` (Task 3); `JobStore` protocol (`get_job(organization_id, job_id)`, `transition_job_state(...)`).
- Produces: `services.ai_worker.drafting.DraftingService(*, generator: SinglePassGenerator, job_store: JobStore, persistence: DraftPersistence, price_table: Mapping[str, ModelPricing])` with `async draft(job: Job, context: ContextPackage, *, category: str | None = None, budget_tracker: CallBudgetTracker | None = None, escalated_tier: ModelTier | str | None = None, escalation_reason: str | None = None) -> DraftingOutcome`; `DraftingOutcome(draft: GeneratedDraft, job: Job, created: bool)`.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_drafting_service.py`:

```python
"""DraftingService: CONTEXT_READY -> GENERATING -> DRAFTED with one generation (R18.1, R16.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.core.settings import ModelPricing
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm import (
    AgentProfileRegistry,
    FakeLLMProvider,
    SinglePassGenerator,
    UnvalidatedDraftError,
)
from services.ai_worker.drafting import DraftingService

PRICES = {"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
REPLY = {
    "action": "reply",
    "draft": "Hello Alice, open Settings and choose Reset Password.",
    "confidence": 0.95,
    "knowledge_chunks": ["DOC-125-08"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


class _CountingResponder:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.calls = 0
        self._responses = list(responses) or [REPLY]

    def __call__(self, messages: Any, schema: Any, tier: Any) -> dict[str, Any]:
        self.calls += 1
        return dict(self._responses[min(self.calls, len(self._responses)) - 1])


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
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=[
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Open Settings and choose Reset Password.",
                external_id="DOC-125-08",
            )
        ],
    )


async def _service(
    state: JobState, responder: _CountingResponder
) -> tuple[DraftingService, InMemoryJobStore, Job]:
    jobs = InMemoryJobStore()
    job, _ = await jobs.create_job(
        Job(organization_id=uuid4(), state=state.value, idempotency_key=f"k-{uuid4()}")
    )
    service = DraftingService(
        generator=SinglePassGenerator(
            llm_provider=FakeLLMProvider(responder=responder),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=jobs,
        persistence=InMemoryDraftPersistence(jobs, InMemoryDraftStore()),
        price_table=PRICES,
    )
    return service, jobs, job


async def test_context_ready_job_is_drafted_with_one_generation() -> None:
    responder = _CountingResponder()
    service, jobs, job = await _service(JobState.CONTEXT_READY, responder)

    outcome = await service.draft(job, _context(job.organization_id), category="technical_support")

    assert responder.calls == 1
    assert outcome.created is True
    assert outcome.job.state == JobState.DRAFTED.value
    assert outcome.draft.body == REPLY["draft"]
    assert outcome.draft.citation_mismatch is False
    assert outcome.draft.cost_estimate is not None
    transitions = [
        (e.state_from, e.state_to)
        for e in await jobs.list_events_for_job(job.organization_id, job.id)
    ]
    assert transitions[-2:] == [("CONTEXT_READY", "GENERATING"), ("GENERATING", "DRAFTED")]


async def test_generating_job_from_a_retry_is_drafted() -> None:
    """A redelivery after RETRY_PENDING -> GENERATING recovery continues where it left off."""
    responder = _CountingResponder()
    service, _, job = await _service(JobState.GENERATING, responder)

    outcome = await service.draft(job, _context(job.organization_id))

    assert responder.calls == 1
    assert outcome.job.state == JobState.DRAFTED.value


async def test_redelivered_drafted_job_skips_generation() -> None:
    """Review Focus 1: no second billed call and no second draft after redelivery."""
    responder = _CountingResponder()
    service, _, job = await _service(JobState.CONTEXT_READY, responder)
    context = _context(job.organization_id)
    first = await service.draft(job, context)

    second = await service.draft(job, context)

    assert responder.calls == 1
    assert second.created is False
    assert second.draft.id == first.draft.id


async def test_job_in_the_wrong_state_is_refused_before_any_call() -> None:
    responder = _CountingResponder()
    service, _, job = await _service(JobState.QUEUED, responder)

    with pytest.raises(IllegalStateTransitionError):
        await service.draft(job, _context(job.organization_id))

    assert responder.calls == 0


async def test_invalid_output_persists_nothing_and_leaves_job_generating() -> None:
    """R16.3: a payload that fails validation twice never becomes a draft."""
    bad = {"action": "reply"}
    responder = _CountingResponder(bad, bad)
    service, jobs, job = await _service(JobState.CONTEXT_READY, responder)

    with pytest.raises(UnvalidatedDraftError):
        await service.draft(job, _context(job.organization_id))

    stored = await jobs.get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.GENERATING.value
    persistence = service.persistence
    assert await persistence.find_draft_for_job(job.organization_id, job.id) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_drafting_service.py`
Expected: collection error `ModuleNotFoundError: No module named 'services.ai_worker.drafting'`.

- [ ] **Step 3: Implement the service**

Create `services/ai_worker/drafting.py`:

```python
"""Drafting service: generate one reply for a context-ready job and persist it once.

Requirements: R18.1 (CONTEXT_READY -> GENERATING -> DRAFTED), R16.4 (persist the
draft), R14.9 (exactly one generation per job), R19.3 / R19.7 (a redelivered job that
is already drafted is not generated again), R16.3 (generation errors propagate to the
retry/DLQ path and persist nothing).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from packages.core.settings import ModelPricing
from packages.db.draft_persistence import DraftPersistence
from packages.db.job import JobStore
from packages.domain.entities import ContextPackage, GeneratedDraft, Job
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.budget import CallBudgetTracker
from packages.llm.drafts import NO_ESCALATION, build_generated_draft
from packages.llm.generator import SinglePassGenerator
from packages.llm.protocol import ModelTier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftingOutcome:
    """The job's draft; ``created`` is False when an earlier delivery already drafted it."""

    draft: GeneratedDraft
    job: Job
    created: bool


class DraftingService:
    """Moves a context-ready job through generation to a persisted draft."""

    def __init__(
        self,
        *,
        generator: SinglePassGenerator,
        job_store: JobStore,
        persistence: DraftPersistence,
        price_table: Mapping[str, ModelPricing],
    ) -> None:
        self.generator = generator
        self.job_store = job_store
        self.persistence = persistence
        self.price_table = price_table

    async def draft(
        self,
        job: Job,
        context: ContextPackage,
        *,
        category: str | None = None,
        budget_tracker: CallBudgetTracker | None = None,
        escalated_tier: ModelTier | str | None = None,
        escalation_reason: str | None = None,
    ) -> DraftingOutcome:
        """Generate and persist the job's draft, or return the one already persisted.

        Raises:
            KeyError: If the job does not exist in its organization.
            IllegalStateTransitionError: If the job is neither CONTEXT_READY, GENERATING
                nor already DRAFTED. Raised before any model call is billed.
            UnpersistableDraftError: If the generation result cannot be persisted.
            LLMError: Any generation failure, including ``UnvalidatedDraftError``; the
                job stays GENERATING for the retry ladder and nothing is persisted.
        """
        current = await self.job_store.get_job(job.organization_id, job.id)
        if current is None:
            raise KeyError(f"Job {job.id} not found for organization {job.organization_id}")

        if current.state == JobState.DRAFTED.value:
            existing = await self.persistence.find_draft_for_job(
                current.organization_id, current.id
            )
            if existing is not None:
                logger.info(
                    "Job %s already drafted as %s; skipping generation", job.id, existing.id
                )
                return DraftingOutcome(draft=existing, job=current, created=False)

        if current.state == JobState.CONTEXT_READY.value:
            message = context.current_message
            current, _ = await self.job_store.transition_job_state(
                organization_id=current.organization_id,
                job_id=current.id,
                target_state=JobState.GENERATING,
                payload={
                    "category": category,
                    "escalated_tier": str(escalated_tier) if escalated_tier else None,
                    "escalation_reason": escalation_reason or NO_ESCALATION,
                },
                message_id=message.message_id,
                thread_id=message.thread_id,
            )
        elif current.state != JobState.GENERATING.value:
            raise IllegalStateTransitionError(current.state, JobState.GENERATING)

        result = await self.generator.generate_draft(
            context,
            category=category,
            budget_tracker=budget_tracker,
            escalated_tier=escalated_tier,
            escalation_reason=escalation_reason,
        )
        draft = build_generated_draft(
            result, context, job_id=current.id, price_table=self.price_table
        )
        outcome = await self.persistence.persist_drafted(draft)
        return DraftingOutcome(draft=outcome.draft, job=outcome.job, created=outcome.created)
```

`CallBudgetTracker` lives in `packages/llm/budget.py`. If `generate_draft` rejects `escalated_tier=None` alongside `escalation_reason=None`, pass them only when set. Read `generate_draft` lines 88-130 first; its signature defaults both to `None`.

- [ ] **Step 4: Run the unit tests and watch them pass**

Run: `uv run pytest tests/unit/test_drafting_service.py`
Expected: 5 passed.

If `test_invalid_output_persists_nothing_and_leaves_job_generating` fails because the generator raised a different subclass, read `packages/llm/generator.py` `_validate_with_repair`. Assert the class it actually raises for a payload missing required fields twice. It is `UnvalidatedDraftError` per the 4.9 notes.

- [ ] **Step 5: Write the Postgres end-to-end test**

Create `tests/integration/test_drafting_service_postgres.py`:

```python
"""DraftingService end to end on live PostgreSQL in rag_email_test (R18.1, R16.4, R19.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import asyncpg
import pytest

from packages.core.settings import AppSettings, ModelPricing
from packages.db.connection import create_pool_from_settings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from services.ai_worker.drafting import DraftingService

REPLY = {
    "action": "reply",
    "draft": "Hello, open Settings and choose Reset Password.",
    "confidence": 0.93,
    "knowledge_chunks": ["DOC-125-08", "DOC-404-00"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_context_ready(pool: asyncpg.Pool) -> tuple[Job, ContextPackage]:
    org_id, mbx_id, thread_id, msg_id = (uuid.uuid4() for _ in range(4))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Box', 'active')",
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'c@example.com', 'C', '[]',
                      'Password reset', 'I forgot my password.', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:6]}",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.CONTEXT_READY.value,
            idempotency_key=f"drafting-{uuid.uuid4()}",
        )
    )
    message = NormalizedMessage(
        message_id=msg_id,
        thread_id=thread_id,
        mailbox_id=mbx_id,
        organization_id=org_id,
        provider="gmail",
        provider_message_id=f"prov-{msg_id.hex[:6]}",
        sender=EmailAddress(email="c@example.com", name="C"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    context = ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=[
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Open Settings and choose Reset Password.",
                external_id="DOC-125-08",
            )
        ],
    )
    return job, context


def _service(pool: asyncpg.Pool) -> DraftingService:
    return DraftingService(
        generator=SinglePassGenerator(
            llm_provider=FakeLLMProvider(default_response=REPLY),
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=PostgresJobStore(pool),
        persistence=PostgresDraftPersistence(pool),
        price_table={"fake-fast-model": ModelPricing(input_per_m=0.15, output_per_m=0.60)},
    )


async def test_context_ready_job_ends_drafted_with_full_record(db_pool: asyncpg.Pool) -> None:
    job, context = await _seed_context_ready(db_pool)

    outcome = await _service(db_pool).draft(job, context, category="technical_support")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM generated_draft WHERE job_id = $1", job.id)
        state = await conn.fetchval("SELECT state FROM processing_job WHERE id = $1", job.id)
    assert state == JobState.DRAFTED.value
    assert row is not None
    assert row["id"] == outcome.draft.id
    assert row["body"] == REPLY["draft"]
    assert row["subject"] == "Re: Password reset"
    assert row["model_name"] == "fake-fast-model"
    assert row["model_tier"] == "routine"
    assert row["escalation_reason"] == "none"
    assert row["prompt_version"]
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0
    assert row["cost_estimate"] is not None and row["cost_estimate"] > 0
    assert row["citation_mismatch"] is True  # DOC-404-00 was never supplied
    assert '"DOC-404-00"' not in row["citations"]


async def test_concurrent_deliveries_leave_one_draft(db_pool: asyncpg.Pool) -> None:
    """R19.7 / design §9: two deliveries of one job never produce two drafts."""
    job, context = await _seed_context_ready(db_pool)
    service = _service(db_pool)
    await service.job_store.transition_job_state(
        organization_id=job.organization_id, job_id=job.id, target_state=JobState.GENERATING
    )

    outcomes = await asyncio.gather(service.draft(job, context), service.draft(job, context))

    assert sorted(o.created for o in outcomes) == [False, True]
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM generated_draft WHERE job_id = $1", job.id
        )
    assert count == 1
```

- [ ] **Step 6: Run the integration tests**

Run: `uv run pytest tests/integration/test_drafting_service_postgres.py`
Expected: 2 passed. `row["citations"]` is returned by asyncpg as a JSON string, so the `not in` check is a string check. If it comes back as a list, assert on the `citation_id` values instead and note it.

- [ ] **Step 7: Full suites, lint, types, commit**

Run: `uv run pytest tests/unit && uv run pytest tests/integration && uv run ruff check . && uv run mypy packages services tests/unit/test_drafting_service.py tests/integration/test_drafting_service_postgres.py`
Expected: all pass, clean. The mypy baseline `uv run mypy packages services tests evaluation 2>&1 | tail -1` stays at `Found 14 errors in 6 files`.

```bash
git add services/ai_worker/drafting.py tests/unit/test_drafting_service.py tests/integration/test_drafting_service_postgres.py
git commit -m "feat(ai-worker): drafting service generates once and persists once per job [task 4.11] [R18.1, R16.4, R14.9, R19.7]"
```

---

### Task 5: Record the rules and close the task entry

**Files:**
- Modify: `specs/design.md` (§10 "Cost accounting (R21.6)" paragraph)
- Modify: `specs/tasks.md` (task 4.11)

- [ ] **Step 1: Document the cost-`NULL` rule and the draft unit of work**

In `specs/design.md` §10, after the sentence ending `This is what makes SC9 answerable.`, add:

```markdown
A model missing from the price table yields `cost_estimate = NULL`, never `0`: an unknown cost is not a free call, and SQL aggregates then exclude it visibly instead of under-counting. The draft row and the job's `GENERATING → DRAFTED` transition commit in one transaction (`packages/db/draft_persistence.py`), and a partial unique index on `generated_draft(job_id)` keeps one draft per job across redeliveries (§9).
```

- [ ] **Step 2: Update task 4.11**

In `specs/tasks.md`, change `- [ ] **4.11 Draft persistence**` to `- [~] **4.11 Draft persistence**`. Under its existing bullets and above its `_Requirements:_` line, add:

```markdown
  - Done: `DraftingService` (`services/ai_worker/drafting.py`) moves `CONTEXT_READY → GENERATING`, makes the one generation call, builds the record (`packages/llm/drafts.py`: verified citations from `CitationVerdict`, escalation reason or `none`, `Re:` subject, cost from `packages/core/pricing.py`) and persists it with `GENERATING → DRAFTED` in one transaction (`packages/db/draft_persistence.py`). Migration `0004` enforces one draft per job; a redelivered `DRAFTED` job returns its draft without a second generation. An unpriced model stores `cost_estimate = NULL`.
  - Left: held at `[~]` until RA.14 turns `make ci` green (DoD #6). The `ai-worker` broker consumer that calls `DraftingService`, and so the retry/DLQ hop from 4.9, is task 4.13; the Phase 4 gate needs it. Per-draft cost *aggregation* per email / category / day (R21.6) is a query over `generated_draft.cost_estimate` and belongs with the cost dashboard (7.4).
```

- [ ] **Step 3: Verify and commit**

Run: `grep -n "4.11 Draft persistence" specs/tasks.md && grep -c "cost_estimate = NULL" specs/design.md`
Expected: the line shows `[~]`; count `1`.

```bash
git add specs/design.md specs/tasks.md
git commit -m "docs(spec): record draft persistence rules and 4.11 status [task 4.11] [R16.4, R21.6]"
```

---

## Dry-run verification (2026-09-27)

All five tasks were applied verbatim in a scratch copy. Four defects were found and corrected above: a test count, one formatter collapse in `_row_to_draft`, and three E501 lines in Task 4. All five Review Focus tests went RED, then GREEN for the stated reason; the concurrency test was stable across 5 runs. Measured after Task 4: unit 1234 passed, integration 148 passed, `ruff check .` clean, mypy baseline unchanged at 14 errors in 6 files.

## Self-review record

1. **Spec coverage.**
   - R16.4, "persist every draft with citations, model, tier, prompt version, token counts, estimated cost": Task 2 maps each field and Task 4's Postgres test asserts each column.
   - R18.1, `GENERATING → DRAFTED`: Tasks 3 and 4. `CONTEXT_READY → GENERATING` is Task 4.
   - R21.6, cost per inference from the price table: Task 1. Aggregation per email, category and day is a query over the persisted column, recorded as "Left" in Task 5.
   - Related requirements are covered too: R15.4 escalation `none` (Task 2), R16.2 and R16.3 no unvalidated draft (Tasks 2 and 4), R16.5 verified citations (Task 2), R18.4 and R18.5 event in the same transaction (Task 3), R19.7 one draft per job (Tasks 3 and 4).
2. **Placeholder scan.** No TBD or "similar to" text. Every code step has code. Two steps carry explicit fallbacks (Task 1 Step 4 import cycle, Task 4 Step 6 JSON type), each with the exact action to take.
3. **Type consistency.**
   - `estimate_inference_cost` has the same signature in Tasks 1 and 2.
   - `GeneratedDraft.cost_estimate: float | None` is set in Task 2 and consumed in Task 3.
   - `DraftPersistOutcome.created` and `DraftingOutcome.created` agree.
   - `find_draft_for_job(organization_id, job_id)` has the same argument order in the protocol, both implementations and Task 4.
   - `transition_job_state_on(conn, organization_id=…, …)` uses keywords everywhere.
4. **Review Focus.** Each of the five lines names its pinning test and owning task.

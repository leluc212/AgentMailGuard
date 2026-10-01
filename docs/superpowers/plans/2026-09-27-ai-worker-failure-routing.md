# AI-Worker Consumer Core and Generation Failure Routing (task 4.13a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A real `AIWorkerConsumer` turns a routed generation job into a draft, and routes every generation failure by an explicit policy. Temporary failures go to the retry ladder, permanent ones go straight to the dead-letter queue with the reason, and redeliveries of drafted jobs are acknowledged and dropped. All three paths are proven on a real broker, which closes task 4.9's open item.

**Architecture:** `services/ai_worker/failure_policy.py` maps an exception, plus the job's current state, to one of three dispositions: `RETRY`, `DEAD_LETTER` or `ACK_DROP`. `services/ai_worker/consumer.py` subclasses the existing `BaseConsumer`. Per job it loads the job and message, builds the context, routes the tier and calls `DraftingService`. It translates any exception through the policy: `ACK_DROP` returns, `DEAD_LETTER` raises `FatalError(reason)`, and `RETRY` re-raises. `BaseConsumer` keeps owning state transitions, ack/nack, and retry and DLQ publishing. `UnvalidatedDraftError` gains the provider's `finish_reason`, so a truncated response is named in the DLQ reason.

**Tech Stack:** Python 3.12, uv, aio-pika 10 / RabbitMQ 3.13 (classic queues), asyncpg / PostgreSQL 16, pytest 9 + pytest-asyncio (auto), mypy strict, ruff.

**Spec:** `docs/superpowers/specs/2026-09-27-ai-worker-failure-routing-design.md`, approved 2026-09-27: option 1, the design, and the 4.13a/4.13b split. The source of truth remains `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md`. Requirements: R3.3, R3.5, R7.3, R16.3, R18.1, R19.3, R19.5, R19.6, R19.7. Design: `specs/design.md` §5.7, §7, §9. Tasks: `specs/tasks.md` 4.9 and 4.13.

## Global Constraints

- Python 3.12; `uv run` only; ruff line length 100 (E, F, I, N, W, UP, B, C4, SIM); `make ci` must stay green: `ruff format --check .`, `ruff check .`, and `mypy packages services tests evaluation` strict with 0 errors.
- `packages.llm` must not import `packages.broker`. The failure policy lives in `services/ai_worker`.
- Unknown exceptions default to `RETRY`. A failing-safe retry still reaches the DLQ at `max_retries`.
- Integration tests use `tests/integration/conftest.py` isolation (`rag_email_test`) and a **scratch vhost** (`tests/integration/isolation.py::scratch_vhost`). Retry queues with 1/2/3 s TTLs would otherwise collide with the shared test vhost's default TTLs (RabbitMQ 406). The live dev stack is never touched.
- pytest `addopts` already has `-q`. No `# type: ignore` except the `organization_id=org_id  # type: ignore[arg-type]` pattern already used in existing tests.
- Never log prompt or draft text. Reasons carry exception names and messages only.
- Commit format `type(scope): summary [task 4.13a] [R…]`. **Standing user instructions:** no commits until Phase 4 is complete, so Commit steps become snapshots. No `[x]` until `make ci` is green **and** a completion audit passes. 4.9 and 4.13a flip only after the audit that follows this plan.

## Review Focus

1. **A malformed or unknown job id in the envelope.** Expected: `FatalError` goes straight to the DLQ, because a bad id never becomes valid. It must not climb the ladder as an unknown `ValueError`. Pinned by Task 2 `test_malformed_job_id_is_fatal`.
2. **A job that is already `DRAFTED` arrives again.** Expected: acked with no context build and no provider call. Pinned by Task 2 `test_already_drafted_job_is_acked_without_work` and Task 3 `test_redelivered_drafted_job_is_acked_and_dropped`.
3. **A draft truncated at `max_tokens`.** Expected: the DLQ reason says so, so an operator raises `max_tokens` before replaying. Pinned by Task 1 `test_truncated_draft_reason_names_max_tokens` and Task 2 `test_truncated_invalid_output_reason_names_max_tokens`.
4. **A temporary provider failure.** Expected: the retry ladder brings the job back and it drafts exactly once, with no extra draft and no DLQ entry. Pinned by Task 3 `test_transient_failure_retries_then_drafts_once`.
5. **An `IllegalStateTransitionError` for a job that is *not* past `DRAFTED`,** for example still `CLASSIFIED`. Expected: dead-lettered with the reason, not silently dropped. Pinned by Task 1 `test_illegal_transition_for_undrafted_job_is_dead_lettered`.

---

## Design decisions

- **D1. Policy by type, plus state for one case.** `IllegalStateTransitionError` is only a "not a failure" when the job is already `DRAFTED`, `DISPATCHED` or `COMPLETED`. So the consumer re-reads the job state for that exception alone.
- **D2. `DEAD_LETTER` reuses `FatalError`.** `BaseConsumer` already routes `FatalError` to `handle_job_terminal_failure`: job `FAILED → DEAD_LETTER`, a `dlx.email` publish with `x-failure-reason`, and a metric. No broker code changes.
- **D3. Early exit for drafted jobs.** It runs before the context build, so a redelivery costs one DB read. `DraftingService`'s own check stays as a second layer.
- **D4. `finish_reason` travels on the exception.** It is set where the generator raises after a failed repair, from the repair response. Truncation is recognised by `length` (OpenAI-compatible), `max_tokens` (Anthropic) or `max_output_tokens` (OpenAI Responses).

## Not in this plan (4.13b)

- The `services/ai_worker/main.py` entrypoint and compose `command`.
- Summarizer `InstrumentedLLMProvider` wiring, `start_token_counter_warmup()`, and `metrics`/`price_table` composition.
- The composed-worker telemetry test.
- The worker-kill test.
- The `make smoke` extension.

## File structure

| File | Responsibility |
|---|---|
| `packages/llm/validation.py` (modify) | `UnvalidatedDraftError(message, *, finish_reason=None)` |
| `packages/llm/generator.py` (modify) | pass the repair response's `raw_finish_reason` when raising |
| `services/ai_worker/failure_policy.py` (new) | `Disposition`, `FailureDecision`, `classify_generation_failure` |
| `services/ai_worker/consumer.py` (new) | `AIWorkerConsumer`, `classification_from_snapshot` |
| `tests/unit/test_generation_failure_policy.py` (new) | every policy row and the truncation reason |
| `tests/unit/test_ai_worker_consumer.py` (new) | consumer behaviour on in-memory stores |
| `tests/integration/test_ai_worker_failure_routing_integration.py` (new) | the three hops on a scratch vhost |
| `specs/tasks.md`, `specs/design.md` (modify) | split 4.13, record the policy |

---

### Task 1: Failure policy and truncation-aware `UnvalidatedDraftError`

**Files:**
- Modify: `packages/llm/validation.py` (`UnvalidatedDraftError`, ~line 57)
- Modify: `packages/llm/generator.py` (the `raise UnvalidatedDraftError(` after `except DraftValidationError as repair_error:`, ~line 342)
- Create: `services/ai_worker/failure_policy.py`
- Test: `tests/unit/test_generation_failure_policy.py`

**Interfaces:**
- Produces:
  - `UnvalidatedDraftError(message: str, *, finish_reason: str | None = None)`, with a `.finish_reason` attribute.
  - `services.ai_worker.failure_policy.Disposition`, a `StrEnum` with values `RETRY="retry"`, `DEAD_LETTER="dead_letter"` and `ACK_DROP="ack_drop"`.
  - `FailureDecision(disposition: Disposition, reason: str)`.
  - `DRAFTED_OR_LATER: frozenset[str]` and `TRUNCATION_FINISH_REASONS: frozenset[str]`.
  - `classify_generation_failure(exc: BaseException, *, job_state: str | None) -> FailureDecision`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_generation_failure_policy.py`:

```python
"""Generation failure routing: retry, dead-letter or drop (4.13a; closes 4.9)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from packages.broker.consumer import FatalError
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.drafts import UnpersistableDraftError
from packages.llm.protocol import LLMResponseError, LLMResult, LLMTimeoutError
from packages.llm.validation import DraftSchemaContractError, UnvalidatedDraftError
from services.ai_worker.failure_policy import (
    DRAFTED_OR_LATER,
    Disposition,
    classify_generation_failure,
)


@pytest.mark.parametrize("state", sorted(DRAFTED_OR_LATER))
def test_illegal_transition_for_drafted_job_is_dropped(state: str) -> None:
    exc = IllegalStateTransitionError(state, JobState.GENERATING)
    decision = classify_generation_failure(exc, job_state=state)
    assert decision.disposition is Disposition.ACK_DROP
    assert state in decision.reason


def test_illegal_transition_for_undrafted_job_is_dead_lettered() -> None:
    """Review Focus 5: a state-machine violation that is not a redelivery is not dropped."""
    exc = IllegalStateTransitionError(JobState.CLASSIFIED, JobState.GENERATING)
    decision = classify_generation_failure(exc, job_state=JobState.CLASSIFIED.value)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert "IllegalStateTransitionError" in decision.reason


@pytest.mark.parametrize(
    "exc",
    [
        UnvalidatedDraftError("draft failed schema validation after one repair retry"),
        DraftSchemaContractError("profile schema narrows the action enum"),
        UnpersistableDraftError("message has no thread"),
        FatalError("job not found"),
    ],
)
def test_permanent_failures_are_dead_lettered(exc: Exception) -> None:
    decision = classify_generation_failure(exc, job_state=None)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert type(exc).__name__ in decision.reason


@pytest.mark.parametrize(
    "exc",
    [
        LLMTimeoutError("upstream timeout"),
        LLMResponseError("HTTP 529 overloaded"),
        ConnectionError("reset by peer"),
        RuntimeError("something unexpected"),
    ],
)
def test_transient_and_unknown_failures_are_retried(exc: Exception) -> None:
    decision = classify_generation_failure(exc, job_state=None)
    assert decision.disposition is Disposition.RETRY


def test_truncated_draft_reason_names_max_tokens() -> None:
    """Review Focus 3."""
    exc = UnvalidatedDraftError("invalid after repair", finish_reason="length")
    decision = classify_generation_failure(exc, job_state=None)
    assert decision.disposition is Disposition.DEAD_LETTER
    assert "truncated at max_tokens" in decision.reason
    assert "finish_reason=length" in decision.reason


def test_untruncated_draft_reason_does_not_mention_max_tokens() -> None:
    exc = UnvalidatedDraftError("invalid after repair", finish_reason="stop")
    assert "max_tokens" not in classify_generation_failure(exc, job_state=None).reason


class _TruncatingProvider(FakeLLMProvider):
    """Fake provider whose every response reports a length stop."""

    async def generate(self, **kwargs: Any) -> LLMResult:
        result = await super().generate(**kwargs)
        return replace(result, raw_finish_reason="length")


async def test_generator_attaches_finish_reason_to_unvalidated_draft() -> None:
    invalid = {"action": "reply"}
    generator = SinglePassGenerator(
        llm_provider=_TruncatingProvider(canned_responses=[invalid, dict(invalid)]),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    from datetime import UTC, datetime
    from uuid import uuid4

    from packages.domain.entities import ContextPackage, EmailAddress, NormalizedMessage

    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="p-1",
        sender=EmailAddress(email="a@example.com"),
        received_at=datetime.now(UTC),
        subject="Hello",
        body_text="Question",
        body_text_clean="Question",
    )
    context = ContextPackage(agent_instructions="a", category_instructions="c", current_message=message)

    with pytest.raises(UnvalidatedDraftError) as excinfo:
        await generator.generate_draft(context, category="support")

    assert excinfo.value.finish_reason == "length"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_generation_failure_policy.py`
Expected: collection error `ModuleNotFoundError: No module named 'services.ai_worker.failure_policy'`.

- [ ] **Step 3: Carry the finish reason on `UnvalidatedDraftError`**

In `packages/llm/validation.py`, replace the `UnvalidatedDraftError` class body with:

```text
class UnvalidatedDraftError(LLMSchemaValidationError):
    """Raised when a draft remains unvalidated after repair retry (R16.3). Never persist.

    ``finish_reason`` is the provider's stop reason for the last response, when known; a
    length stop means the output was truncated at ``max_tokens``.
    """

    def __init__(self, message: str, *, finish_reason: str | None = None) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason
```

In `packages/llm/generator.py`, at the `raise UnvalidatedDraftError(` directly after `except DraftValidationError as repair_error:`, add the keyword argument so the call reads:

```text
            raise UnvalidatedDraftError(
                "Draft failed schema validation after one repair retry; "
                f"failing job into the retry/DLQ path without persisting: {repair_error}",
                finish_reason=repair_result.raw_finish_reason,
            ) from repair_error
```

`repair_result` is the repair response already in scope there; it is appended to `attempts` just above. Leave the other two `raise UnvalidatedDraftError(` sites unchanged, because they have no parsed response.

- [ ] **Step 4: Implement the policy**

Create `services/ai_worker/failure_policy.py`:

```python
"""Generation failure routing for the AI worker (task 4.13a; closes 4.9's retry/DLQ item).

Retry only transient faults; send non-transient ones straight to the dead-letter queue with
their reason; acknowledge and drop redeliveries of jobs that are already drafted. Sources:
Microsoft Retry pattern ("cancel" non-transient faults; decide retries where the full context
is understood), NServiceBus recoverability (unrecoverable exceptions skip retries), OpenAI
Structured Outputs (non-matching output comes from refusals or max_tokens truncation).
Lives in the worker so ``packages.llm`` never depends on broker types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from packages.broker.consumer import FatalError
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from packages.llm.drafts import UnpersistableDraftError
from packages.llm.validation import DraftSchemaContractError, UnvalidatedDraftError


class Disposition(StrEnum):
    """What the consumer does with a delivery whose generation failed."""

    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    ACK_DROP = "ack_drop"


@dataclass(frozen=True)
class FailureDecision:
    """A disposition and the human-readable reason recorded with it."""

    disposition: Disposition
    reason: str


DRAFTED_OR_LATER: frozenset[str] = frozenset(
    {JobState.DRAFTED.value, JobState.DISPATCHED.value, JobState.COMPLETED.value}
)
"""Job states in which a new delivery is a redelivery, not a failure."""

TRUNCATION_FINISH_REASONS: frozenset[str] = frozenset({"length", "max_tokens", "max_output_tokens"})
"""Provider stop reasons meaning the output was cut off at the token limit."""

_PERMANENT: tuple[type[BaseException], ...] = (
    DraftSchemaContractError,
    UnpersistableDraftError,
    FatalError,
)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def classify_generation_failure(
    exc: BaseException, *, job_state: str | None
) -> FailureDecision:
    """Map a generation failure (and, for state errors, the job's state) to a disposition.

    Unknown exceptions are retried: failing safe still reaches the DLQ at ``max_retries``.
    """
    if isinstance(exc, IllegalStateTransitionError):
        if job_state in DRAFTED_OR_LATER:
            return FailureDecision(
                Disposition.ACK_DROP, f"job already {job_state}; redelivery dropped"
            )
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    if isinstance(exc, UnvalidatedDraftError):
        reason = _describe(exc)
        if exc.finish_reason in TRUNCATION_FINISH_REASONS:
            reason = (
                f"{reason} [truncated at max_tokens (finish_reason={exc.finish_reason}); "
                "raise max_tokens before replaying]"
            )
        return FailureDecision(Disposition.DEAD_LETTER, reason)
    if isinstance(exc, _PERMANENT):
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    return FailureDecision(Disposition.RETRY, _describe(exc))
```

If `LLMSchemaValidationError.__init__` does not accept a single message argument, read `packages/llm/protocol.py` and match its signature. It is a plain `Exception` subclass today.

- [ ] **Step 5: Run and watch them pass**

Run: `uv run pytest tests/unit/test_generation_failure_policy.py tests/unit/test_draft_repair_orchestration.py tests/unit/test_draft_validation.py`
Expected: all pass (15 in the new file).

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff check services/ai_worker packages/llm tests/unit/test_generation_failure_policy.py && uv run ruff format --check services/ai_worker/failure_policy.py packages/llm/validation.py packages/llm/generator.py tests/unit/test_generation_failure_policy.py && uv run mypy packages services tests/unit/test_generation_failure_policy.py`
Expected: clean. If `ruff format --check` flags any listed file, for example `failure_policy.py`'s multi-line signature collapsing to one line, run `uv run ruff format` on the flagged files and keep the result.

```bash
git add packages/llm/validation.py packages/llm/generator.py services/ai_worker/failure_policy.py \
  tests/unit/test_generation_failure_policy.py
git commit -m "feat(ai-worker): route generation failures to retry, dead-letter or drop [task 4.13a] [R16.3, R19.5, R19.6]"
```

---

### Task 2: `AIWorkerConsumer` core

**Files:**
- Create: `services/ai_worker/consumer.py`
- Test: `tests/unit/test_ai_worker_consumer.py`

**Interfaces:**
- Consumes:
  - `classify_generation_failure`, `Disposition` and `DRAFTED_OR_LATER` from Task 1.
  - `BaseConsumer(queue_name, broker_settings=None, retry_settings=None, prefetch_count=None, connection=None, shutdown_coordinator=None, job_store=None, lease_timeout_s=300, metrics=None)`.
  - `ContextBuilder.build_context(job, message, classification=None, thread_messages=None, thread_state=None) -> ContextPackage`.
  - `ComplexityRouter.route(context, classification=None, escalations_performed=0) -> RoutingDecision`, with `.tier`, `.is_escalated` and `.escalation_reason`.
  - `DraftingService.draft(job, context, *, category=None, budget_tracker=None, escalated_tier=None, escalation_reason=None) -> DraftingOutcome`.
  - `MessageStore.get_message(organization_id, message_id) -> NormalizedMessage | None`; `JobStore.get_job(organization_id, job_id) -> Job | None`.
- Produces:
  - `services.ai_worker.consumer.AIWorkerConsumer(queue_name: str, *, job_store: JobStore, message_store: MessageStore, context_builder: ContextBuilder, router: ComplexityRouter, drafting: DraftingService, broker_settings: BrokerSettings | None = None, retry_settings: RetryLadderSettings | None = None, prefetch_count: int | None = None, connection: AbstractRobustConnection | None = None, shutdown_coordinator: GracefulShutdownCoordinator | None = None, metrics: PipelineMetrics | None = None)`.
  - `classification_from_snapshot(snapshot: dict[str, Any]) -> Classification`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ai_worker_consumer.py`:

```python
"""AIWorkerConsumer: one routed job -> context -> tier -> draft, with failure routing (4.13a)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from packages.broker.consumer import FatalError
from packages.broker.envelope import JobEnvelope
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.core.settings import SummarizationSettings
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.db.thread_state import InMemoryThreadStateStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.protocol import LLMResult, LLMTimeoutError
from packages.llm.router import ComplexityRouter
from services.ai_worker.consumer import AIWorkerConsumer, classification_from_snapshot
from services.ai_worker.drafting import DraftingService

REPLY = {
    "action": "reply",
    "draft": "Hello, open Settings and choose Reset Password.",
    "confidence": 0.93,
    "knowledge_chunks": [],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
INVALID = {"action": "reply"}
SNAPSHOT = {
    "category": "support",
    "intent": "technical_troubleshooting",
    "priority": "normal",
    "retrieval_required": False,
    "confidence": 0.9,
    "unknown_extra_key": "ignored",
}


class _ScriptedProvider(FakeLLMProvider):
    """Plays a script of responses or exceptions and counts calls."""

    def __init__(self, *script: Any, finish_reason: str = "stop") -> None:
        super().__init__()
        self.script = list(script)
        self.calls = 0
        self.finish_reason = finish_reason

    async def generate(self, **kwargs: Any) -> LLMResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        self._default_response = dict(step)
        result = await super().generate(**kwargs)
        return replace(result, raw_finish_reason=self.finish_reason)


async def _setup(
    provider: _ScriptedProvider, state: JobState = JobState.QUEUED
) -> tuple[AIWorkerConsumer, InMemoryJobStore, InMemoryDraftStore, Job, JobEnvelope]:
    jobs, messages, drafts = InMemoryJobStore(), InMemoryMessageStore(), InMemoryDraftStore()
    org_id = uuid4()
    message = NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=org_id,
        provider="mock",
        provider_message_id=f"p-{uuid4().hex[:6]}",
        sender=EmailAddress(email="alice@example.com", name="Alice"),
        received_at=datetime.now(UTC),
        subject="Password reset",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
    )
    await messages.insert_message(message)
    job, _ = await jobs.create_job(
        Job(
            organization_id=org_id,
            message_id=message.message_id,
            thread_id=message.thread_id,
            state=state.value,
            idempotency_key=f"k-{uuid4()}",
        )
    )
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(
            settings=SummarizationSettings(),
            thread_state_store=InMemoryThreadStateStore(),
            message_store=messages,
        ),
        job_store=jobs,
    )
    drafting = DraftingService(
        generator=SinglePassGenerator(
            llm_provider=provider,
            profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        ),
        job_store=jobs,
        persistence=InMemoryDraftPersistence(jobs, drafts),
        price_table={},
    )
    consumer = AIWorkerConsumer(
        "email.support.normal",
        job_store=jobs,
        message_store=messages,
        context_builder=builder,
        router=ComplexityRouter(),
        drafting=drafting,
    )
    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(org_id),
        message_id=message.provider_message_id,
        classification=dict(SNAPSHOT),
    )
    return consumer, jobs, drafts, job, envelope


async def test_queued_job_is_drafted() -> None:
    provider = _ScriptedProvider(REPLY)
    consumer, jobs, drafts, job, envelope = await _setup(provider)

    await consumer.process_job(envelope, MagicMock())

    stored = await jobs.get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.DRAFTED.value
    assert len(await drafts.list_drafts_for_job(job.id, job.organization_id)) == 1
    assert provider.calls == 1


async def test_invalid_output_twice_is_fatal_with_reason() -> None:
    provider = _ScriptedProvider(INVALID, INVALID)
    consumer, _, drafts, job, envelope = await _setup(provider)

    with pytest.raises(FatalError, match="UnvalidatedDraftError"):
        await consumer.process_job(envelope, MagicMock())

    assert provider.calls == 2
    assert await drafts.list_drafts_for_job(job.id, job.organization_id) == []


async def test_truncated_invalid_output_reason_names_max_tokens() -> None:
    """Review Focus 3."""
    provider = _ScriptedProvider(INVALID, INVALID, finish_reason="length")
    consumer, _, _, _, envelope = await _setup(provider)

    with pytest.raises(FatalError, match="truncated at max_tokens"):
        await consumer.process_job(envelope, MagicMock())


async def test_transient_failure_is_reraised_unchanged() -> None:
    timeout = LLMTimeoutError("upstream timeout")
    provider = _ScriptedProvider(timeout)
    consumer, _, _, _, envelope = await _setup(provider)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await consumer.process_job(envelope, MagicMock())

    assert excinfo.value is timeout


async def test_already_drafted_job_is_acked_without_work() -> None:
    """Review Focus 2."""
    provider = _ScriptedProvider(REPLY)
    consumer, _, _, _, envelope = await _setup(provider, state=JobState.DRAFTED)

    await consumer.process_job(envelope, MagicMock())
    assert provider.calls == 0


async def test_malformed_job_id_is_fatal() -> None:
    """Review Focus 1."""
    consumer, _, _, _, envelope = await _setup(_ScriptedProvider(REPLY))
    bad = envelope.model_copy(update={"job_id": "not-a-uuid"})

    with pytest.raises(FatalError, match="job_id"):
        await consumer.process_job(bad, MagicMock())


async def test_unknown_job_is_fatal() -> None:
    consumer, _, _, _, envelope = await _setup(_ScriptedProvider(REPLY))
    missing = envelope.model_copy(update={"job_id": str(uuid4())})

    with pytest.raises(FatalError, match="not found"):
        await consumer.process_job(missing, MagicMock())


def test_classification_from_snapshot_keeps_known_fields_only() -> None:
    cls = classification_from_snapshot(dict(SNAPSHOT))
    assert cls.category == "support"
    assert cls.retrieval_required is False
    assert classification_from_snapshot({}).category == "general_inquiry"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_ai_worker_consumer.py`
Expected: collection error `ModuleNotFoundError: No module named 'services.ai_worker.consumer'`.

- [ ] **Step 3: Implement the consumer**

Create `services/ai_worker/consumer.py`:

```python
"""AI-worker consumer core (task 4.13a): lane queue -> context -> tier -> draft.

Per job: load job (early exit if already drafted, R19.3) -> load message -> classification
from the envelope snapshot (R7.3) -> ContextBuilder (QUEUED -> CONTEXT_READY) ->
ComplexityRouter -> DraftingService (CONTEXT_READY -> GENERATING -> DRAFTED). Failures are
routed by ``failure_policy``; BaseConsumer owns ack/nack, the retry ladder and the DLQ.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError
from packages.broker.envelope import JobEnvelope
from packages.context.builder import ContextBuilder
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import JobStore
from packages.db.message import MessageStore
from packages.domain.entities import Classification
from packages.domain.state_machine import IllegalStateTransitionError
from packages.llm.router import ComplexityRouter
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.drafting import DraftingService
from services.ai_worker.failure_policy import (
    DRAFTED_OR_LATER,
    Disposition,
    classify_generation_failure,
)

logger = logging.getLogger(__name__)

DEFAULT_CATEGORY = "general_inquiry"
_CLASSIFICATION_FIELDS = frozenset(f.name for f in dataclasses.fields(Classification))


def classification_from_snapshot(snapshot: dict[str, Any]) -> Classification:
    """Rebuild the triage classification carried in the job envelope (R7.3)."""
    values = {k: v for k, v in snapshot.items() if k in _CLASSIFICATION_FIELDS}
    values.setdefault("category", DEFAULT_CATEGORY)
    return Classification(**values)


class AIWorkerConsumer(BaseConsumer):
    """Consumes one lane queue ``email.<category>.<priority>`` and drafts each job."""

    def __init__(
        self,
        queue_name: str,
        *,
        job_store: JobStore,
        message_store: MessageStore,
        context_builder: ContextBuilder,
        router: ComplexityRouter,
        drafting: DraftingService,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        super().__init__(
            queue_name=queue_name,
            broker_settings=broker_settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=job_store,
            metrics=metrics,
        )
        self.jobs = job_store
        self.messages = message_store
        self.context_builder = context_builder
        self.router = router
        self.drafting = drafting

    async def process_job(self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage) -> None:
        """Draft one job; route any failure to retry, dead-letter or drop."""
        try:
            await self._generate(envelope)
        except Exception as exc:
            job_state = (
                await self._current_state(envelope)
                if isinstance(exc, IllegalStateTransitionError)
                else None
            )
            decision = classify_generation_failure(exc, job_state=job_state)
            if decision.disposition is Disposition.ACK_DROP:
                logger.info("Delivery for job %s dropped: %s", envelope.job_id, decision.reason)
                return
            if decision.disposition is Disposition.DEAD_LETTER:
                if isinstance(exc, FatalError):
                    raise
                raise FatalError(decision.reason) from exc
            raise

    async def _generate(self, envelope: JobEnvelope) -> None:
        org_id = envelope.organization_id
        try:
            job_id = UUID(str(envelope.job_id))
        except ValueError as err:
            raise FatalError(f"Envelope job_id {envelope.job_id!r} is not a UUID") from err
        job = await self.jobs.get_job(org_id, job_id)
        if job is None:
            raise FatalError(f"Job {job_id} not found for organization {org_id}")
        if job.state in DRAFTED_OR_LATER:
            logger.info("Job %s already %s; acknowledging without work", job_id, job.state)
            return
        if job.message_id is None:
            raise FatalError(f"Job {job_id} has no message_id")
        message = await self.messages.get_message(org_id, job.message_id)
        if message is None:
            raise FatalError(f"Message {job.message_id} not found for job {job_id}")

        classification = classification_from_snapshot(envelope.classification)
        context = await self.context_builder.build_context(job, message, classification)
        decision = self.router.route(context, classification)
        await self.drafting.draft(
            job,
            context,
            category=classification.category,
            escalated_tier=decision.tier if decision.is_escalated else None,
            escalation_reason=str(decision.escalation_reason) if decision.is_escalated else None,
        )

    async def _current_state(self, envelope: JobEnvelope) -> str | None:
        try:
            job = await self.jobs.get_job(envelope.organization_id, UUID(str(envelope.job_id)))
        except Exception:
            return None
        return job.state if job is not None else None
```

If the `process_job` signature line exceeds 100 characters, let `uv run ruff format` wrap it.

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/unit/test_ai_worker_consumer.py`
Expected: 8 passed. If `ThreadContextAssembler` rejects a keyword, or `InMemoryMessageStore` lacks a thread-listing method the assembler calls, read `packages/context/assembly.py` (`__init__` ~line 155, `assemble` ~line 169) and match the real names. Change only the test's setup, never the assertions.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check services/ai_worker tests/unit/test_ai_worker_consumer.py && uv run ruff format --check services/ai_worker/consumer.py tests/unit/test_ai_worker_consumer.py && uv run mypy packages services tests/unit/test_ai_worker_consumer.py`
Expected: clean (run `uv run ruff format` on the two new files if flagged).

```bash
git add services/ai_worker/consumer.py tests/unit/test_ai_worker_consumer.py
git commit -m "feat(ai-worker): consumer core drafts routed jobs and routes failures [task 4.13a] [R7.3, R18.1, R19.3]"
```

---

### Task 3: Prove the three hops on a real broker

**Files:**
- Test: `tests/integration/test_ai_worker_failure_routing_integration.py`

**Interfaces:**
- Consumes: `AIWorkerConsumer` (Task 2); `setup_topology`, `MessagePublisher`, `scratch_vhost`; `PostgresJobStore`, `PostgresMessageStore`, `PostgresThreadStateStore`, `PostgresDraftPersistence`.

- [ ] **Step 1: Write the integration tests**

Create `tests/integration/test_ai_worker_failure_routing_integration.py`:

```python
"""Generation failure routing on a real broker and Postgres (4.13a; closes 4.9).

Scratch vhost: retry queues are declared with 1/2/3 s TTLs, which would 406 against the
shared test vhost's default-TTL queues.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings, SummarizationSettings
from packages.db.connection import create_pool_from_settings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.protocol import LLMResult, LLMTimeoutError
from packages.llm.router import ComplexityRouter
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.drafting import DraftingService
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3)
LANE = "email.support.normal"
REPLY = {
    "action": "reply",
    "draft": "Hello, open Settings and choose Reset Password.",
    "confidence": 0.93,
    "knowledge_chunks": [],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
INVALID = {"action": "reply"}


class _ScriptedProvider(FakeLLMProvider):
    def __init__(self, *script: Any) -> None:
        super().__init__()
        self.script = list(script)
        self.calls = 0

    async def generate(self, **kwargs: Any) -> LLMResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        self._default_response = dict(step)
        return replace(await super().generate(**kwargs), raw_finish_reason="stop")


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "aiworker") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


async def _seed_queued_job(pool: asyncpg.Pool) -> Job:
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
            state=JobState.QUEUED.value,
            idempotency_key=f"aiw-{uuid.uuid4()}",
        )
    )
    return job


def _consumer(broker: BrokerSettings, pool: asyncpg.Pool, provider: _ScriptedProvider) -> AIWorkerConsumer:
    jobs, messages = PostgresJobStore(pool), PostgresMessageStore(pool)
    return AIWorkerConsumer(
        LANE,
        job_store=jobs,
        message_store=messages,
        context_builder=ContextBuilder(
            thread_assembler=ThreadContextAssembler(
                settings=SummarizationSettings(),
                thread_state_store=PostgresThreadStateStore(pool),
                message_store=messages,
            ),
            job_store=jobs,
        ),
        router=ComplexityRouter(),
        drafting=DraftingService(
            generator=SinglePassGenerator(
                llm_provider=provider,
                profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
            ),
            job_store=jobs,
            persistence=PostgresDraftPersistence(pool),
            price_table={},
        ),
        broker_settings=broker,
        retry_settings=FAST_RETRY,
        prefetch_count=1,
    )


def _envelope(job: Job) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(job.organization_id),
        message_id=str(job.message_id),
        classification={"category": "support", "priority": "normal", "retrieval_required": False},
    )


async def _publish(broker: BrokerSettings, envelope: JobEnvelope) -> None:
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(broker.exchange_email_route, LANE, envelope)
    finally:
        await publisher.close()


async def _wait_for_state(pool: asyncpg.Pool, job: Job, state: JobState, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    current = None
    while asyncio.get_running_loop().time() < deadline:
        stored = await PostgresJobStore(pool).get_job(job.organization_id, job.id)
        current = stored.state if stored else None
        if current == state.value:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job.id} ended {current}, expected {state.value}")


async def _draft_count(pool: asyncpg.Pool, job: Job) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM generated_draft WHERE job_id = $1", job.id))


async def _dlq_count(channel: AbstractChannel, broker: BrokerSettings) -> int:
    queue = await channel.declare_queue(broker.queue_dead_letter, passive=True)
    return int(queue.declaration_result.message_count or 0)


async def test_transient_failure_retries_then_drafts_once(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Review Focus 4: timeout -> retry ladder -> redelivery -> exactly one draft, no DLQ."""
    job = await _seed_queued_job(pool)
    provider = _ScriptedProvider(LLMTimeoutError("upstream timeout"), REPLY)
    consumer = _consumer(broker, pool, provider)
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await consumer.stop()

    assert provider.calls == 2
    assert await _draft_count(pool, job) == 1
    assert await _dlq_count(channel, broker) == 0


async def test_invalid_output_twice_goes_straight_to_dead_letter(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Option 1: no retry ladder; DLQ with the reason; job DEAD_LETTER; no draft."""
    job = await _seed_queued_job(pool)
    provider = _ScriptedProvider(INVALID, INVALID)
    consumer = _consumer(broker, pool, provider)
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DEAD_LETTER, timeout_s=15)
    finally:
        await consumer.stop()

    assert provider.calls == 2
    assert await _draft_count(pool, job) == 0
    dlq = await channel.declare_queue(broker.queue_dead_letter, passive=True)
    message = await dlq.get(no_ack=True, fail=True, timeout=5)
    reason = str((message.headers or {}).get("x-failure-reason"))
    assert "UnvalidatedDraftError" in reason


async def test_redelivered_drafted_job_is_acked_and_dropped(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Review Focus 2: a redelivery after DRAFTED costs no model call and never dead-letters."""
    job = await _seed_queued_job(pool)
    provider = _ScriptedProvider(REPLY)
    consumer = _consumer(broker, pool, provider)
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=15)
        await _publish(broker, _envelope(job))
        lane = await channel.declare_queue(LANE, passive=True)
        for _ in range(50):
            lane = await channel.declare_queue(LANE, passive=True)
            if (lane.declaration_result.message_count or 0) == 0:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
    finally:
        await consumer.stop()

    assert provider.calls == 1
    assert await _draft_count(pool, job) == 1
    assert await _dlq_count(channel, broker) == 0
```

- [ ] **Step 2: Run the integration tests**

Run: `uv run pytest tests/integration/test_ai_worker_failure_routing_integration.py`
Expected: 3 passed. These tests exercise code finished in Tasks 1–2, so they are expected to pass on first run. Show they are not vacuous by temporarily changing the `DEAD_LETTER` branch in `services/ai_worker/consumer.py` to `raise` (re-raise the original). `test_invalid_output_twice_goes_straight_to_dead_letter` must then fail, because the job climbs the ladder and the provider is called more than twice. Revert and re-run: 3 passed. Record both runs.

If `setup_topology` or the passive declare of `LANE` fails because `support` has no `normal` lane in `config/categories.yaml`, read that file and use a declared lane (`email.<category>.normal`) in `LANE` and the snapshot's `category`.

- [ ] **Step 3: Full suites, lint, types, commit**

Run: `uv run pytest tests/unit && uv run pytest tests/integration && uv run ruff check . && uv run ruff format --check . && uv run mypy packages services tests evaluation`
Expected: all pass; ruff clean; `Success: no issues found`. If `ruff check .` reports E501 in the new integration test (the `FAST_RETRY = …` line is 101 characters as written), or `ruff format --check` flags it, run `uv run ruff format tests/integration/test_ai_worker_failure_routing_integration.py` first and re-run.

```bash
git add tests/integration/test_ai_worker_failure_routing_integration.py
git commit -m "test(ai-worker): prove retry, dead-letter and drop hops on a real broker [task 4.13a] [R3.5, R19.5, R19.6, R19.7]"
```

---

### Task 4: Record the split and the policy

**Files:**
- Modify: `specs/tasks.md` (the `4.13 AI-worker consumer (generation path end to end)` block; the 4.9 `Left:` bullet)
- Modify: `specs/design.md` (§5.7, after the paragraph beginning `Validate → repair once → fail to retry/DLQ.`)

- [ ] **Step 1: Split 4.13**

In `specs/tasks.md`, replace the whole block from `- [ ] **4.13 AI-worker consumer (generation path end to end)**` through its `_Requirements:` line with:

```markdown
- [~] **4.13a AI-worker consumer core & generation failure routing**
  - Discovered missing work (CLAUDE.md §7): `services/ai_worker/` hosted no consumer, so no actionable job moved past `QUEUED`, and 4.9's retry/DLQ hop could not be observed.
  - `AIWorkerConsumer` (`services/ai_worker/consumer.py`) consumes one lane queue `email.<category>.<priority>`. Per job, in process (`design.md` §3.2): Context Builder (`QUEUED → CONTEXT_READY`, 4.4) → Complexity Router (4.8) → `DraftingService` (`CONTEXT_READY → GENERATING → DRAFTED`, 4.11). The classification comes from the envelope snapshot, never a re-classification (`design.md` §7.3).
  - Failure policy (`services/ai_worker/failure_policy.py`, decided 2026-09-27, option 1):
    - `UnvalidatedDraftError`, `DraftSchemaContractError` and `UnpersistableDraftError` → DLQ at once, with the reason. A `max_tokens` truncation is named.
    - A delivery for a job already `DRAFTED` / `DISPATCHED` / `COMPLETED` is acked and dropped.
    - Transient and unknown errors → retry ladder.
  - Proven on a real broker (scratch vhost) and Postgres: a timeout retries then drafts once; invalid output twice → DLQ with `x-failure-reason`, job `DEAD_LETTER`, no draft, 2 model calls; a drafted-job redelivery is acked with no call.
  - _Requirements: R3.3, R3.5, R7.3, R16.3, R18.1, R19.3, R19.5, R19.6, R19.7_

- [ ] **4.13b AI-worker service wiring & live gate**
  - `services/ai_worker/main.py` on the shared `WorkerRuntime` (`/healthz`, `/readyz`, graceful drain), one `AIWorkerConsumer` per lane queue in `routing.configured_consumers` with a bounded, configurable `prefetch`. Replace the `ai-worker` placeholder in `docker-compose.yml`.
  - Compose with telemetry (4.12): wrap the summarizer's provider in `InstrumentedLLMProvider(kind=CallKind.SUMMARIZE)`, and pass `metrics` and `settings.llm.price_table` to `SinglePassGenerator` and `DraftingService`, so every request in the worker is measured.
  - Prove the wiring with a composed-worker test: one actionable job through the built components moves `llm_context_tokens{kind="generate"}` and `emails_generated_total`, and `llm_context_tokens{kind="summarize"}` when the thread crosses the summarization threshold (R11.7).
  - Call `start_token_counter_warmup()` in the worker's `build_components` (as the triage worker does), so the BPE encoding never loads on the event loop. Pass the plain provider to `SinglePassGenerator`, not a pre-built `BudgetedLLMProvider`: a reused wrapper keeps its own `metrics`/`price_table`, so the generator's would be ignored.
  - Worker-kill test (`design.md` §9): kill the worker mid-generation → redelivery → exactly one `generated_draft` row.
  - Extend `make smoke` so an actionable email reaches `DRAFTED` through the live stack.
  - _Requirements: R3.4, R11.7, R19.7, R20.1, R20.7, R20.8, R22.8, R24.7_
```

In the **Phase 4 gate** paragraph, change `through the \`ai-worker\` consumer (4.13)` to `through the \`ai-worker\` consumer (4.13a, 4.13b)`, and change `reaches the path 4.13 chose (retry ladder or DLQ)` to `reaches the DLQ with its reason (4.13a policy)`.

In the "Requirement coverage index" table, replace every `4.13` entry with `4.13a` or `4.13b`, following the two `_Requirements:` lines above. If a row's requirement appears in both lines, list both.

- [ ] **Step 2: Update 4.9's open item**

In task 4.9's `Left:` bullet, append:

```markdown
**Resolved 2026-09-27 by 4.13a:** the deterministic failures (`UnvalidatedDraftError`, `DraftSchemaContractError`) are dead-lettered at once rather than climbing the ladder (option 1), and the hop is proven on a real broker. 4.9 flips to `[x]` after the completion audit.
```

- [ ] **Step 3: Record the policy in the design**

In `specs/design.md` §5.7, after the paragraph that begins `Validate → repair once → fail to retry/DLQ.`, add:

```markdown
**Generation failure routing (4.13a).** The AI worker classifies every generation failure before `BaseConsumer` acts:

| Failure | Route |
|---|---|
| Timeouts, 429/5xx, connection errors, unknown errors | retry ladder, then DLQ at `max_retries` |
| `UnvalidatedDraftError` (invalid after the one repair), `DraftSchemaContractError`, `UnpersistableDraftError` | DLQ at once with `x-failure-reason`; a `max_tokens` truncation is named in the reason |
| Redelivery of a job already `DRAFTED`/`DISPATCHED`/`COMPLETED` | ack and drop, no model call |

Retrying a failure that does not go away only multiplies cost (each redelivery re-runs generate + repair). A human replays dead-lettered jobs through the replay endpoint after fixing the cause.
```

- [ ] **Step 4: Verify and commit**

Run: `grep -n "4.13a AI-worker\|4.13b AI-worker\|Resolved 2026-09-27 by 4.13a" specs/tasks.md && grep -c "Generation failure routing (4.13a)" specs/design.md && grep -c "(4.13)" specs/tasks.md && make ci > /tmp/ai413a_ci.log 2>&1; echo "make ci exit $?"`
Expected: three matching lines in `specs/tasks.md`; `1` for the design count; `0` for the `(4.13)` count; `make ci exit 0`.

```bash
git add specs/tasks.md specs/design.md
git commit -m "docs(spec): split 4.13, record generation failure routing [task 4.13a] [R16.3, R19.5, R19.6]"
```

---

## Dry-run verification (2026-09-27)

All four tasks were applied verbatim in a scratch copy. Four defects were corrected above: a test count, two formatter fallbacks, and a strict-mypy `func-returns-value` on an `is None` assertion.
- Every Interfaces signature matched the real code.
- The scripted-provider technique really controls `FakeLLMProvider`'s output.
- The transient path (timeout → ladder → one draft) and the terminal path (`FAILED → DEAD_LETTER`, DLQ `x-failure-reason` naming `UnvalidatedDraftError`) both ran on a scratch vhost.
- The non-vacuity break made the dead-letter test fail, with 8 provider calls instead of 2.

Measured: unit 1285 passed, integration 152 passed, `mypy` clean on 345 files, `make ci` exit 0.

## Self-review record

1. **Spec coverage.** Each spec section maps to a task:
   - Failure policy table: Task 1.
   - Truncation reason: Tasks 1 and 2.
   - Consumer steps 1–7: Task 2.
   - Proof items 1–3: Task 3.
   - Split and doc updates: Task 4.
   - "Closing 4.9" (audit, then `[x]`): done by the controller after this plan, as the Global Constraints state.
2. **Placeholder scan.** No TBD. Fallback notes (Task 2 Step 4, Task 3 Step 2) name the exact file and action.
3. **Type consistency.** `classify_generation_failure(exc, *, job_state)`, `Disposition` and `DRAFTED_OR_LATER` are the same across Tasks 1–2. `AIWorkerConsumer(queue_name, *, job_store, message_store, context_builder, router, drafting, …)` is the same across Tasks 2–3. `UnvalidatedDraftError(message, *, finish_reason=None)` is the same across Tasks 1–2.
4. **Review Focus.** Each of the five items names its pinning test.

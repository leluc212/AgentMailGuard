# AI-Worker Service Wiring and Live Gate (task 4.13b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `ai-worker` compose service runs real `AIWorkerConsumer`s on every configured lane queue, with threshold-triggered thread summarization, hybrid retrieval, per-request telemetry, graceful drain and readiness checks. A worker crash mid-generation still yields exactly one draft, and `make smoke` shows an actionable email reaching `DRAFTED` on the live stack.

**Architecture:** `services/ai_worker/main.py` follows the other workers: `WorkerRuntime` owns the process lifecycle, and `build_components(res)` composes one shared generation pipeline plus one `AIWorkerConsumer` per lane. The pipeline is `ThreadSummarizer` (instrumented as `summarize`) → `ContextBuilder` (with `HybridRetriever` over `PostgresSearchBackend`) → `ComplexityRouter` → `DraftingService` (plain provider, metrics, price table). The consumer gains an optional summarizer step before the context build. The offline `fake` provider learns to answer the draft and summary schemas, so the default compose stack can draft end to end. The lease reaper stops reclaiming `DRAFTED` jobs.

**Tech Stack:** Python 3.12, uv, aio-pika 10 / RabbitMQ 3.13, asyncpg / PostgreSQL 16 + pgvector, prometheus_client, Docker Compose, pytest 9, mypy strict, ruff.

**Spec:** Source of truth is `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md`. Requirements: `specs/tasks.md` 4.13b and its `_Requirements:_` R3.4, R11.7, R19.7, R20.1, R20.7, R20.8, R22.8, R24.7, plus R8.2, R8.3, R10.1, R14.3 and R19.8. Design: `specs/design.md` §3.2 ("Context Builder, Hybrid RAG, and Reply Agent co-locate in `ai-worker`"), §5.4, §7.1, §9 (worker-kill test), §10. 4.13a spec: `docs/superpowers/specs/2026-09-27-ai-worker-failure-routing-design.md`.

## Global Constraints

- Python 3.12; `uv run` only; ruff line length 100; `make ci` stays green: `ruff format --check .`, `ruff check .`, and `mypy packages services tests evaluation` strict with 0 errors.
- Follow the existing worker pattern (`services/email_worker/main.py`, `services/triage_worker/main.py`): `SERVICE_NAME`, `DEFAULT_PORT`, `build_components(res) -> list[StartFn]`, and `main()` on `WorkerRuntime`.
- The model provider reaches `SinglePassGenerator` **plain**, never a pre-built `BudgetedLLMProvider` (4.12 rule). The summarizer's provider is wrapped in `InstrumentedLLMProvider(kind=CallKind.SUMMARIZE)`. `start_token_counter_warmup()` runs in `build_components`. The shared `TokenCounter` is built off the event loop with `asyncio.to_thread`.
- Integration tests use `tests/integration/conftest.py` isolation (`rag_email_test`). Broker tests use `scratch_vhost`. **The live dev stack is touched only in Task 6's live-gate steps, which the user runs** (`make up`, `make smoke`). The agent's auto-mode classifier blocks them, so the executor hands them over.
- pytest `addopts` already has `-q`. Never log prompt, email or draft text.
- Commit format `type(scope): summary [task 4.13b] [R…]`. **Standing user instructions:** no commits until Phase 4 is complete, so Commit steps become snapshots. `[x]` only after `make ci` is green, the live gate passes, and a completion audit passes.

## Review Focus

1. **A worker killed mid-generation.** Expected: the unacked delivery returns, another worker drafts, and exactly one `generated_draft` row exists even after the dead worker's stale call finishes later. Pinned by Task 5 `test_worker_killed_mid_generation_yields_one_draft`.
2. **A lane glob in `ROUTING__CONFIGURED_CONSUMERS`** (e.g. `email.*.priority`). Expected: only declared queues matching it get a consumer, and a pattern matching nothing starts no consumer instead of crashing on a passive declare. Pinned by Task 3 `test_lane_resolution_expands_globs_against_declared_queues`.
3. **A short thread.** Expected: no summarization call (R8.2) in the running pipeline, not just in the policy unit. Pinned by Task 2 `test_short_thread_is_not_summarized`.
4. **The default compose stack (`LLM__PROVIDER=fake`).** Expected: a draft is produced, not a dead-letter from a triage-shaped fake answer. Pinned by Task 1 `test_fake_answers_the_reply_schema_with_a_valid_draft` and the Task 6 live smoke.
5. **A `DRAFTED` job whose lease expired** while it waits for human review. Expected: the reaper leaves it alone. Pinned by Task 4 `test_reaper_skips_drafted_jobs` (in-memory and Postgres).

---

## Design decisions

- **D1. Summarization in the consumer, before the context build.** `ThreadSummarizer.summarize_thread` runs its policy and calls the model only above the threshold (R8.2, R8.3). The consumer then passes the thread messages and resulting state into `build_context`, so the assembler neither re-reads nor re-summarizes. `ContextBuilder` is unchanged.
- **D2. One pipeline, many consumers.** Stores, provider, summarizer, builder, router and drafting are built once and shared. Each lane gets its own `AIWorkerConsumer` with the lane's prefetch: `ai_worker_priority_prefetch` for `*.priority`, otherwise `ai_worker_normal_prefetch` (R3.4, R7.2).
- **D3. Lanes = declared category queues filtered by `configured_consumers`.** Globs are expanded with the existing `is_queue_consumed`. A configured name that is not declared is ignored, because the passive declare would fail.
- **D4. Retrieval is composed now; the query embedding is a recorded gap.** `HybridRetriever(PostgresSearchBackend)` runs the lexical branch. Nothing in the production path fills `RetrievalQuery.query_vector`, so the vector branch returns nothing (`packages/retrieval/postgres.py:192`). That falls short of R10.1 and is recorded as discovered task 3.16 (Task 6). It is not built here.
- **D5. The fake provider is schema-aware** only when no default response was given explicitly. A schema requiring `draft` gets a valid reply, and one requiring `summary` gets a valid summary. Everything else keeps today's triage-shaped default. This is the configured offline provider (R24.5), not a test hook.
- **D6. The reaper excludes `DRAFTED`.** A drafted job waits for a human, with no worker holding it. `DISPATCHED` stays reapable.
- **D7. The worker-kill test is in-process.** It closes the worker's broker connection while a generation call is blocked, which is exactly what a `SIGKILL` looks like to RabbitMQ: the unacked delivery is requeued. A live `docker kill` stays optional (Task 6 note).

## Not in this plan

- Building the query embedding for vector retrieval (new task 3.16).
- Sharing one `CallBudgetTracker` across summarization and generation per job (R14.9). The generator enforces its own kinds, and summarization is bounded by its policy.
- Reranker composition (`RetrievalSettings.rerank_enabled`), Grafana dashboards (7.x), and business-data providers (Phase 5).

## File structure

| File | Responsibility |
|---|---|
| `packages/llm/fake.py` (modify) | schema-aware offline answers for reply and summary schemas |
| `services/ai_worker/consumer.py` (modify) | optional `summarizer`; thread messages fetched once and passed through |
| `services/ai_worker/main.py` (new) | lane resolution, pipeline composition, `build_components`, `main` |
| `packages/db/job.py` (modify) | reaper skips `DRAFTED` (Postgres and in-memory) |
| `tests/unit/test_fake_llm_schema_answers.py`, `tests/unit/test_ai_worker_summarization.py`, `tests/unit/test_ai_worker_main.py`, `tests/unit/test_lease_reaper.py` (new/modify) | unit coverage |
| `tests/integration/test_ai_worker_service_integration.py` (new) | composed-worker telemetry and worker-kill proof |
| `tests/integration/test_lease_reaper_integration.py` (modify) | Postgres reaper skips `DRAFTED` |
| `docker-compose.yml`, `scripts/image_smoke.py`, `scripts/stack_smoke.py` (modify) | live wiring and gate |
| `specs/tasks.md` (modify) | 4.13b status, discovered task 3.16 |

---

### Task 1: The offline provider answers the draft and summary schemas

**Files:**
- Modify: `packages/llm/fake.py` (`FakeLLMProvider.__init__` ~line 27 and `generate` ~line 95)
- Modify: `tests/unit/test_ai_worker_consumer.py` and `tests/integration/test_ai_worker_failure_routing_integration.py` (their `_ScriptedProvider.generate`)
- Test: `tests/unit/test_fake_llm_schema_answers.py`

**Interfaces:**
- Produces: `packages.llm.fake.FAKE_REPLY` and `FAKE_THREAD_SUMMARY` (dicts). `FakeLLMProvider` returns them for a `schema` whose `required` contains `draft`, respectively `summary`, when no `default_response`, `canned_responses` or `responder` was given.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_fake_llm_schema_answers.py`:

```python
"""The offline provider answers the draft and summary schemas (R24.5; 4.13b D5)."""

from __future__ import annotations

import json
from pathlib import Path

from packages.context.policy import THREAD_SUMMARY_SCHEMA
from packages.llm.fake import FAKE_REPLY, FAKE_THREAD_SUMMARY, FakeLLMProvider
from packages.llm.protocol import ChatMessage
from packages.llm.validation import validate_draft_payload

REPLY_SCHEMA = json.loads(Path("schemas/reply.v1.json").read_text())
MESSAGES = [ChatMessage(role="user", content="Please help")]


async def test_fake_answers_the_reply_schema_with_a_valid_draft() -> None:
    result = await FakeLLMProvider().generate(messages=MESSAGES, schema=REPLY_SCHEMA)
    assert result.content == FAKE_REPLY
    validate_draft_payload(result.content, REPLY_SCHEMA)


async def test_fake_answers_the_summary_schema() -> None:
    result = await FakeLLMProvider().generate(messages=MESSAGES, schema=THREAD_SUMMARY_SCHEMA)
    assert result.content == FAKE_THREAD_SUMMARY
    assert set(THREAD_SUMMARY_SCHEMA.get("required", [])) <= set(result.content)


async def test_fake_keeps_the_triage_default_for_other_schemas() -> None:
    result = await FakeLLMProvider().generate(messages=MESSAGES, schema={"type": "object"})
    assert result.content["category"] == "support"


async def test_explicit_default_response_still_wins() -> None:
    explicit = {"action": "reply"}
    result = await FakeLLMProvider(default_response=explicit).generate(
        messages=MESSAGES, schema=REPLY_SCHEMA
    )
    assert result.content == explicit
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_fake_llm_schema_answers.py`
Expected: collection error `ImportError: cannot import name 'FAKE_REPLY'`.

- [ ] **Step 3: Implement**

In `packages/llm/fake.py`, add at module level, below the imports:

```python
FAKE_REPLY: dict[str, Any] = {
    "action": "reply",
    "draft": (
        "Thank you for your message. A member of our team will review it and follow up "
        "shortly."
    ),
    "confidence": 0.5,
    "knowledge_chunks": [],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
"""Offline answer to the draft schema (schemas/reply.v1.json), so the fake stack drafts."""

FAKE_THREAD_SUMMARY: dict[str, Any] = {
    "topic": "Customer conversation",
    "current_intent": "Awaiting a reply",
    "summary": "Offline summary placeholder; configure a real provider for real summaries.",
    "open_questions": [],
    "resolved_items": [],
}
"""Offline answer to THREAD_SUMMARY_SCHEMA."""


def _schema_default(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pick the offline answer that satisfies a known schema, or None."""
    required = set((schema or {}).get("required", []))
    if "draft" in required:
        return dict(FAKE_REPLY)
    if "summary" in required:
        return dict(FAKE_THREAD_SUMMARY)
    return None
```

In `__init__`, before `self._default_response = default_response or {`, add:

```python
        self._explicit_default = default_response is not None
```

In `generate`, change the final `else` branch of the response selection, which currently reads `content = dict(self._default_response)`, to:

```python
        else:
            schema_answer = None if self._explicit_default else _schema_default(schema)
            content = schema_answer if schema_answer is not None else dict(self._default_response)
```

Read `generate` first. The selection is the `if self._responder … elif self._canned_responses … else` block around line 96. Keep the other two branches unchanged. If `schema` has another local name there, use it.

- [ ] **Step 4: Run and watch them pass, then the suites that use the fake**

Two existing test doubles set the fake's response by assigning `self._default_response = dict(step)` directly, bypassing `__init__`. That leaves `_explicit_default` False, so the new schema answer would silently replace their deliberately invalid `INVALID` payload, and the dead-letter tests would stop raising. In both `tests/unit/test_ai_worker_consumer.py` and `tests/integration/test_ai_worker_failure_routing_integration.py`, add this line directly after `self._default_response = dict(step)` in `_ScriptedProvider.generate`:

```text
        self._explicit_default = True
```

Then sweep for any other such pattern: `grep -rn "_default_response\s*=" tests`. Every hit outside `FakeLLMProvider.__init__` needs the same line.

Run: `uv run pytest tests/unit/test_fake_llm_schema_answers.py && uv run pytest tests/unit && uv run pytest tests/integration/test_ai_worker_failure_routing_integration.py`
Expected: 4 passed; all unit tests pass; 3 passed. List any further test you had to adjust under Deviations.

- [ ] **Step 5: Lint, types, commit**

Run: `uv run ruff check packages/llm/fake.py tests/unit/test_fake_llm_schema_answers.py && uv run ruff format --check packages/llm/fake.py tests/unit/test_fake_llm_schema_answers.py && uv run mypy packages services tests/unit/test_fake_llm_schema_answers.py`
Expected: clean (run `uv run ruff format` on flagged files).

```bash
git add packages/llm/fake.py tests/unit/test_fake_llm_schema_answers.py \
  tests/unit/test_ai_worker_consumer.py tests/integration/test_ai_worker_failure_routing_integration.py
git commit -m "feat(llm): offline provider answers the draft and summary schemas [task 4.13b] [R24.5]"
```

---

### Task 2: Threshold-triggered summarization in the consumer

**Files:**
- Modify: `services/ai_worker/consumer.py` (`AIWorkerConsumer.__init__` and `_generate`)
- Test: `tests/unit/test_ai_worker_summarization.py`

**Interfaces:**
- Consumes: `ThreadSummarizer.summarize_thread(organization_id, thread_id, messages, current_state=None) -> SummarizationResult` (`.thread_state`); `MessageStore.get_messages_by_thread(organization_id, thread_id) -> list[NormalizedMessage]`; `ContextBuilder.build_context(job, message, classification, thread_messages=None, thread_state=None)`.
- Produces: `AIWorkerConsumer(..., summarizer: ThreadSummarizer | None = None)`, a keyword argument after `drafting`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ai_worker_summarization.py`:

```python
"""The consumer summarizes long threads before building context, never short ones (R8.2-R8.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from packages.broker.envelope import JobEnvelope
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.context.summarizer import ThreadSummarizer
from packages.core.settings import SummarizationSettings
from packages.db.draft import InMemoryDraftStore
from packages.db.draft_persistence import InMemoryDraftPersistence
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.db.thread_state import InMemoryThreadStateStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator
from packages.llm.fake import FAKE_REPLY
from packages.llm.protocol import LLMResult
from packages.llm.router import ComplexityRouter
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.drafting import DraftingService

SETTINGS = SummarizationSettings(min_messages_threshold=4)


class _CountingFake(FakeLLMProvider):
    """Schema-aware fake that records which schemas it was asked to answer."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []

    async def generate(self, **kwargs: Any) -> LLMResult:
        required = set((kwargs.get("schema") or {}).get("required", []))
        self.asked.append("draft" if "draft" in required else "summary")
        return replace(await super().generate(**kwargs), raw_finish_reason="stop")


async def _run(message_count: int) -> tuple[_CountingFake, InMemoryThreadStateStore, Job, Any]:
    jobs, messages, drafts = InMemoryJobStore(), InMemoryMessageStore(), InMemoryDraftStore()
    states = InMemoryThreadStateStore()
    org_id, mbx_id, thread_id = uuid4(), uuid4(), uuid4()
    start = datetime.now(UTC) - timedelta(hours=message_count)
    last: NormalizedMessage | None = None
    for i in range(message_count):
        last = NormalizedMessage(
            message_id=uuid4(),
            thread_id=thread_id,
            mailbox_id=mbx_id,
            organization_id=org_id,
            provider="mock",
            provider_message_id=f"p-{uuid4().hex[:8]}",
            sender=EmailAddress(email="alice@example.com"),
            received_at=start + timedelta(hours=i),
            subject="Setup help",
            body_text=f"Message {i} about the setup.",
            body_text_clean=f"Message {i} about the setup.",
        )
        await messages.insert_message(last)
    assert last is not None
    job, _ = await jobs.create_job(
        Job(
            organization_id=org_id,
            message_id=last.message_id,
            thread_id=thread_id,
            state=JobState.QUEUED.value,
            idempotency_key=f"k-{uuid4()}",
        )
    )
    provider = _CountingFake()
    consumer = AIWorkerConsumer(
        "email.support.normal",
        job_store=jobs,
        message_store=messages,
        context_builder=ContextBuilder(
            thread_assembler=ThreadContextAssembler(
                settings=SETTINGS, thread_state_store=states, message_store=messages
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
            persistence=InMemoryDraftPersistence(jobs, drafts),
            price_table={},
        ),
        summarizer=ThreadSummarizer(llm=provider, store=states, settings=SETTINGS),
    )
    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(org_id),
        message_id=last.provider_message_id,
        classification={"category": "support", "retrieval_required": False},
    )
    await consumer.process_job(envelope, MagicMock())
    stored = await jobs.get_job(org_id, job.id)
    assert stored is not None
    return provider, states, stored, (org_id, thread_id)


async def test_long_thread_is_summarized_before_drafting() -> None:
    provider, states, job, (org_id, thread_id) = await _run(6)
    assert job is not None and job.state == JobState.DRAFTED.value
    assert provider.asked == ["summary", "draft"]
    state = await states.get(org_id, thread_id)
    assert state is not None and state.summary


async def test_short_thread_is_not_summarized() -> None:
    """Review Focus 3 (R8.2): no summarization call below the threshold."""
    provider, states, job, (org_id, thread_id) = await _run(2)
    assert job is not None and job.state == JobState.DRAFTED.value
    assert provider.asked == ["draft"]
    assert FAKE_REPLY["action"] == "reply"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_ai_worker_summarization.py`
Expected: `TypeError: AIWorkerConsumer.__init__() got an unexpected keyword argument 'summarizer'`.

- [ ] **Step 3: Implement**

In `services/ai_worker/consumer.py`:

1. Add `from packages.context.summarizer import ThreadSummarizer` to the imports.
2. Add `summarizer: ThreadSummarizer | None = None,` as a keyword parameter of `__init__`, directly after `drafting: DraftingService,`, and store it as `self.summarizer = summarizer`.
3. In `_generate`, replace:

```text
        classification = classification_from_snapshot(envelope.classification)
        context = await self.context_builder.build_context(job, message, classification)
```

with:

```text
        classification = classification_from_snapshot(envelope.classification)
        thread_messages = await self.messages.get_messages_by_thread(org_id, message.thread_id)
        thread_state = None
        if self.summarizer is not None:
            # Threshold-triggered (R8.2, R8.3): below the threshold this makes no model call.
            summary = await self.summarizer.summarize_thread(
                org_id, message.thread_id, thread_messages
            )
            thread_state = summary.thread_state
        context = await self.context_builder.build_context(
            job,
            message,
            classification,
            thread_messages=thread_messages,
            thread_state=thread_state,
        )
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/unit/test_ai_worker_summarization.py tests/unit/test_ai_worker_consumer.py`
Expected: all pass (2 new). If `summarize_thread` below the threshold returns `thread_state=None` while a state exists in the store, the assembler reads it itself, because `thread_state=None` means "fetch".

- [ ] **Step 5: Lint, types, commit**

Run: `uv run ruff check services/ai_worker tests/unit/test_ai_worker_summarization.py && uv run ruff format --check services/ai_worker/consumer.py tests/unit/test_ai_worker_summarization.py && uv run mypy packages services tests/unit/test_ai_worker_summarization.py`
Expected: clean (run `uv run ruff format` on flagged files).

```bash
git add services/ai_worker/consumer.py tests/unit/test_ai_worker_summarization.py
git commit -m "feat(ai-worker): summarize long threads before building context [task 4.13b] [R8.2, R8.3]"
```

---

### Task 3: `services/ai_worker/main.py` — lanes and composition

**Files:**
- Create: `services/ai_worker/main.py`
- Test: `tests/unit/test_ai_worker_main.py`

**Interfaces:**
- Consumes: `WorkerResources` (`settings, db_pool, connection, publisher, health, shutdown, metrics`); `StartFn`; `WorkerRuntime`; `AIWorkerSettings`; `fake_worker_resources(settings)` (tests); Tasks 1–2.
- Produces:
  - `SERVICE_NAME = "ai_worker"`, `DEFAULT_PORT = 8004`.
  - `resolve_lane_queues(settings: AppSettings) -> list[str]` and `lane_prefetch(settings: AppSettings, queue_name: str) -> int`.
  - `build_consumers(res: WorkerResources, *, llm_provider: LLMProvider | None = None, token_counter: TokenCounter | None = None) -> list[AIWorkerConsumer]`.
  - `async build_components(res: WorkerResources) -> list[StartFn]`.
  - `main() -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ai_worker_main.py`:

```python
"""AI-worker composition root: lanes, prefetch, and the telemetry wiring rules (4.13b)."""

from __future__ import annotations

import pytest

from packages.core.settings import AIWorkerSettings, AppSettings, CategoryRoutingSettings
from packages.knowledge.token_counter import TokenCounter
from packages.llm import InstrumentedLLMProvider
from packages.llm.budget import BudgetedLLMProvider, CallKind
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.main import (
    DEFAULT_PORT,
    SERVICE_NAME,
    build_components,
    build_consumers,
    lane_prefetch,
    resolve_lane_queues,
)
from tests.stubs.worker_resources import fake_worker_resources


def test_service_identity() -> None:
    assert (SERVICE_NAME, DEFAULT_PORT) == ("ai_worker", 8004)


def test_default_lanes_are_the_configured_declared_queues() -> None:
    settings = AppSettings()
    lanes = resolve_lane_queues(settings)
    assert lanes
    assert set(lanes) <= set(settings.routing.configured_consumers)
    assert "email.support.normal" in lanes and "email.support.priority" in lanes


def test_lane_resolution_expands_globs_against_declared_queues() -> None:
    """Review Focus 2."""
    settings = AppSettings(
        routing=CategoryRoutingSettings(configured_consumers=["email.*.priority", "email.nope.x"])
    )
    lanes = resolve_lane_queues(settings)
    assert lanes and all(q.endswith(".priority") for q in lanes)
    assert "email.nope.x" not in lanes
    nothing = AppSettings(routing=CategoryRoutingSettings(configured_consumers=["email.nope.*"]))
    assert resolve_lane_queues(nothing) == []


def test_prefetch_follows_the_lane() -> None:
    settings = AppSettings()
    c = settings.concurrency
    assert lane_prefetch(settings, "email.billing.priority") == c.ai_worker_priority_prefetch
    assert lane_prefetch(settings, "email.billing.normal") == c.ai_worker_normal_prefetch


def test_one_consumer_per_lane_sharing_one_instrumented_pipeline() -> None:
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)
    consumers = build_consumers(res, token_counter=TokenCounter())

    assert [c.queue_name for c in consumers] == resolve_lane_queues(settings)
    first = consumers[0]
    assert all(c.drafting is first.drafting for c in consumers)
    assert {c.prefetch_count for c in consumers} <= {
        settings.concurrency.ai_worker_normal_prefetch,
        settings.concurrency.ai_worker_priority_prefetch,
    }
    generator = first.drafting.generator
    assert not isinstance(generator.llm_provider, BudgetedLLMProvider)
    assert generator.metrics is res.metrics
    assert generator.price_table == settings.llm.price_table
    assert first.drafting.metrics is res.metrics
    assert first.drafting.price_table == settings.llm.price_table
    assert first.summarizer is not None
    summarize_provider = first.summarizer.llm
    assert isinstance(summarize_provider, InstrumentedLLMProvider)
    assert summarize_provider.kind == CallKind.SUMMARIZE
    assert summarize_provider.metrics is res.metrics
    assert isinstance(first.context_builder.retriever, HybridRetriever)


async def test_build_components_warms_the_counter_and_starts_every_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.ai_worker.main as ai_main

    started: list[bool] = []
    monkeypatch.setattr(ai_main, "start_token_counter_warmup", lambda: started.append(True))
    settings = AIWorkerSettings()

    start_fns = await build_components(fake_worker_resources(settings))

    assert started == [True]
    assert len(start_fns) == len(resolve_lane_queues(settings))
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_ai_worker_main.py`
Expected: collection error `ModuleNotFoundError: No module named 'services.ai_worker.main'`.

- [ ] **Step 3: Implement**

Create `services/ai_worker/main.py`:

```python
"""AI generation worker entrypoint (task 4.13b; design.md §3.2, §5.4, §5.7).

Hosts Context Builder + Hybrid RAG + Reply Agent in one process: one shared pipeline
(summarizer -> context builder -> complexity router -> drafting service) and one
AIWorkerConsumer per configured lane queue ``email.<category>.<priority>``. Process lifecycle
(topology, /healthz, /readyz, graceful drain) comes from WorkerRuntime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.routing import is_queue_consumed, load_categories_from_yaml
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.context.summarizer import ThreadSummarizer
from packages.core.settings import AIWorkerSettings, AppSettings
from packages.db.draft_persistence import PostgresDraftPersistence
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.taxonomy import get_default_registry
from packages.knowledge.token_counter import TokenCounter
from packages.llm import AgentProfileRegistry, InstrumentedLLMProvider, SinglePassGenerator
from packages.llm.budget import CallKind
from packages.llm.factory import create_llm_provider
from packages.llm.inference_metrics import start_token_counter_warmup
from packages.llm.protocol import LLMProvider
from packages.llm.router import ComplexityRouter
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.drafting import DraftingService

SERVICE_NAME = "ai_worker"
DEFAULT_PORT = 8004
PRIORITY_LANE_SUFFIX = ".priority"


def resolve_lane_queues(settings: AppSettings) -> list[str]:
    """Declared category lane queues that ``routing.configured_consumers`` claims (R7.6).

    Globs such as ``email.*.priority`` are expanded; configured names that are not declared
    are ignored, because a passive declare of them would fail at start.
    """
    routing = settings.routing
    if routing.categories_config_path:
        load_categories_from_yaml(routing.categories_config_path)
    lanes = routing.priority_lanes or ["normal", "priority"]
    declared = [
        f"email.{category}.{lane}"
        for category in get_default_registry().all_categories()
        for lane in lanes
    ]
    return [q for q in declared if is_queue_consumed(q, routing.configured_consumers)]


def lane_prefetch(settings: AppSettings, queue_name: str) -> int:
    """Bounded prefetch per lane (R3.4, R7.2)."""
    concurrency = settings.concurrency
    if queue_name.endswith(PRIORITY_LANE_SUFFIX):
        return concurrency.ai_worker_priority_prefetch
    return concurrency.ai_worker_normal_prefetch


def build_consumers(
    res: WorkerResources,
    *,
    llm_provider: LLMProvider | None = None,
    token_counter: TokenCounter | None = None,
) -> list[AIWorkerConsumer]:
    """Compose one shared generation pipeline and one consumer per lane."""
    settings = res.settings
    counter = token_counter or TokenCounter()
    provider = llm_provider or create_llm_provider(settings.llm)
    jobs = PostgresJobStore(res.db_pool)
    messages = PostgresMessageStore(res.db_pool)
    states = PostgresThreadStateStore(res.db_pool)

    summarizer = ThreadSummarizer(
        llm=InstrumentedLLMProvider(
            provider,
            kind=CallKind.SUMMARIZE,
            metrics=res.metrics,
            price_table=settings.llm.price_table,
        ),
        store=states,
        settings=settings.summarization,
        token_counter=counter,
    )
    context_builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(
            settings=settings.summarization,
            token_counter=counter,
            metrics=res.metrics,
            thread_state_store=states,
            message_store=messages,
        ),
        retriever=HybridRetriever(
            PostgresSearchBackend(pool=res.db_pool, metrics=res.metrics),
            default_top_n=settings.retrieval.top_n,
            rrf_k=settings.retrieval.rrf_k,
            metrics=res.metrics,
        ),
        job_store=jobs,
        top_k=settings.retrieval.top_k,
    )
    drafting = DraftingService(
        # The plain provider: SinglePassGenerator wraps it per job with its own budget and
        # telemetry; a pre-built BudgetedLLMProvider would keep its own metrics/price table.
        generator=SinglePassGenerator(
            llm_provider=provider,
            profile_registry=AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path),
            metrics=res.metrics,
            price_table=settings.llm.price_table,
        ),
        job_store=jobs,
        persistence=PostgresDraftPersistence(res.db_pool),
        price_table=settings.llm.price_table,
        metrics=res.metrics,
    )
    router = ComplexityRouter(token_counter=counter, metrics=res.metrics)

    return [
        AIWorkerConsumer(
            queue,
            job_store=jobs,
            message_store=messages,
            context_builder=context_builder,
            router=router,
            drafting=drafting,
            summarizer=summarizer,
            broker_settings=settings.broker,
            retry_settings=settings.retry,
            prefetch_count=lane_prefetch(settings, queue),
            connection=res.connection,
            shutdown_coordinator=res.shutdown,
            metrics=res.metrics,
        )
        for queue in resolve_lane_queues(settings)
    ]


async def build_components(res: WorkerResources) -> list[StartFn]:
    """Warm the tokenizers off the event loop, then compose and start every lane consumer."""
    start_token_counter_warmup()
    counter = await asyncio.to_thread(TokenCounter)
    consumers = build_consumers(res, token_counter=counter)
    provider = consumers[0].drafting.generator.llm_provider if consumers else None
    aclose = getattr(provider, "aclose", None)
    if aclose is not None:
        # After every consumer's close(): the HTTP client closes once all lanes have drained.
        res.shutdown.register_cleanup_callback(aclose)
    return [consumer.start for consumer in consumers]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=AIWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
```

Before running, check three names against the real code and change the plan code only if they differ, listing each under Deviations:

- **`settings.agent_profiles.config_path`:** grep `class AgentProfileSettings` in `packages/core/settings.py`. It holds the YAML path used by `scripts/image_smoke.py`.
- **`HybridRetriever`'s first parameter:** the positional `backend`.
- **`ComplexityRouter(token_counter=…, metrics=…)`:** its keyword names.

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/unit/test_ai_worker_main.py tests/unit/test_production_imports.py tests/unit/test_dependency_rules.py`
Expected: all pass (6 new).

- [ ] **Step 5: Lint, types, commit**

Run: `uv run ruff check services/ai_worker tests/unit/test_ai_worker_main.py && uv run ruff format --check services/ai_worker/main.py tests/unit/test_ai_worker_main.py && uv run mypy packages services tests/unit/test_ai_worker_main.py`
Expected: clean (run `uv run ruff format` on flagged files).

```bash
git add services/ai_worker/main.py tests/unit/test_ai_worker_main.py
git commit -m "feat(ai-worker): service entrypoint composes the instrumented pipeline per lane [task 4.13b] [R3.4, R11.7, R20.7, R20.8]"
```

---

### Task 4: The lease reaper leaves `DRAFTED` jobs alone

**Files:**
- Modify: `packages/db/job.py` (Postgres reaper `WHERE state NOT IN ('COMPLETED', 'DEAD_LETTER')` ~line 567; in-memory reaper `if j.state in ("COMPLETED", "DEAD_LETTER"):` ~line 1085)
- Test: `tests/unit/test_lease_reaper.py`, `tests/integration/test_lease_reaper_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_lease_reaper.py`:

```python
@pytest.mark.asyncio
async def test_reaper_skips_drafted_jobs() -> None:
    """Review Focus 5: a drafted job waits for a human; its old lease must not reclaim it."""
    store = InMemoryJobStore()
    past_time = datetime.now(UTC) - timedelta(seconds=600)
    job, _ = await store.create_job(
        Job(
            organization_id=uuid4(),
            state=JobState.DRAFTED.value,
            lease_expires_at=past_time,
            idempotency_key=str(uuid4()),
        )
    )
    job.lease_expires_at = past_time

    reaped = await store.reap_expired_jobs(batch_size=10)

    assert reaped == []
    stored = await store.get_job(job.organization_id, job.id)
    assert stored is not None and stored.state == JobState.DRAFTED.value
```

Append to `tests/integration/test_lease_reaper_integration.py` the same scenario on Postgres. Reuse that file's existing pool fixture and seeding helper. Read the file first and match its fixture names. The test creates a `DRAFTED` job for a fresh organization, sets `lease_expires_at = now() - interval '10 minutes'` with SQL, calls `reap_expired_jobs(batch_size=10, organization_id=org_id)`, and asserts an empty result and an unchanged state. Name it `test_reaper_skips_drafted_jobs_postgres`.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_lease_reaper.py -k drafted && uv run pytest tests/integration/test_lease_reaper_integration.py -k drafted`
Expected: both fail. The reaper returns the drafted job, as a `reclaimed` or `dead_lettered` action.

- [ ] **Step 3: Implement**

In `packages/db/job.py`, change the Postgres reaper's `WHERE state NOT IN ('COMPLETED', 'DEAD_LETTER')` to `WHERE state NOT IN ('COMPLETED', 'DEAD_LETTER', 'DRAFTED')`. Change the in-memory reaper's `if j.state in ("COMPLETED", "DEAD_LETTER"):` to `if j.state in ("COMPLETED", "DEAD_LETTER", "DRAFTED"):`. Leave `acquire_lease`'s `if job.state in (...)` unchanged. Add one comment above each: `# DRAFTED waits for a human reviewer; no worker holds it (4.13b).`

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/unit/test_lease_reaper.py && uv run pytest tests/integration/test_lease_reaper_integration.py`
Expected: all pass.

- [ ] **Step 5: Lint, types, commit**

Run: `uv run ruff check packages/db/job.py tests/unit/test_lease_reaper.py tests/integration/test_lease_reaper_integration.py && uv run ruff format --check packages/db/job.py tests/unit/test_lease_reaper.py tests/integration/test_lease_reaper_integration.py && uv run mypy packages services tests/unit/test_lease_reaper.py tests/integration/test_lease_reaper_integration.py`
Expected: clean.

```bash
git add packages/db/job.py tests/unit/test_lease_reaper.py tests/integration/test_lease_reaper_integration.py
git commit -m "fix(db): lease reaper never reclaims a drafted job [task 4.13b] [R19.8]"
```

---

### Task 5: Composed-worker telemetry and worker-kill proof

**Files:**
- Test: `tests/integration/test_ai_worker_service_integration.py`

**Interfaces:**
- Consumes: `build_consumers(res, *, llm_provider=None, token_counter=None)` (Task 3); `WorkerResources`; `scratch_vhost`; `setup_topology`.

- [ ] **Step 1: Write the integration tests**

Create `tests/integration/test_ai_worker_service_integration.py`:

```python
"""The composed AI worker on a real broker and Postgres (4.13b; R11.7, R19.7, R22.8)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.settings import (
    AIWorkerSettings,
    AppSettings,
    BrokerSettings,
    RetryLadderSettings,
    SummarizationSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.llm.protocol import LLMResult
from packages.observability.health import HealthRegistry
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.consumer import AIWorkerConsumer
from services.ai_worker.main import build_consumers
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
LANE = "email.support.normal"


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "aisvc") as fast:
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


async def _seed_thread(pool: asyncpg.Pool, message_count: int) -> Job:
    org_id, mbx_id, thread_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    start = datetime.now(UTC) - timedelta(hours=message_count)
    last_msg = uuid.uuid4()
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
        for i in range(message_count):
            msg_id = last_msg if i == message_count - 1 else uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO email_message (
                    id, organization_id, mailbox_id, thread_id, provider_message_id,
                    direction, sender_email, sender_name, recipients, subject, body_text,
                    received_at
                ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'c@example.com', 'C', '[]',
                          'Setup help', $6, $7)
                """,
                msg_id,
                org_id,
                mbx_id,
                thread_id,
                f"prov-{msg_id.hex[:8]}",
                f"Message {i} about the setup.",
                start + timedelta(hours=i),
            )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=last_msg,
            thread_id=thread_id,
            state=JobState.QUEUED.value,
            idempotency_key=f"aisvc-{uuid.uuid4()}",
        )
    )
    return job


def _envelope(job: Job) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(job.organization_id),
        message_id=str(job.message_id),
        classification={"category": "support", "priority": "normal", "retrieval_required": True},
    )


async def _resources(
    broker: BrokerSettings, pool: asyncpg.Pool, metrics: PipelineMetrics
) -> WorkerResources:
    settings = AIWorkerSettings(
        broker=broker, retry=FAST_RETRY, summarization=SummarizationSettings(min_messages_threshold=4)
    )
    connection = await aio_pika.connect_robust(broker.url)
    publisher = MessagePublisher(
        broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
    )
    return WorkerResources(
        settings=settings,
        db_pool=pool,
        connection=connection,
        publisher=publisher,
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=metrics,
    )


def _lane_consumer(consumers: list[AIWorkerConsumer]) -> AIWorkerConsumer:
    return next(c for c in consumers if c.queue_name == LANE)


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


def _count(metrics: PipelineMetrics, name: str, **labels: str) -> float:
    total = 0.0
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
    return total


async def test_composed_worker_measures_every_request(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """A long-thread job through the built components moves every telemetry series."""
    metrics = create_pipeline_metrics()
    res = await _resources(broker, pool, metrics)
    job = await _seed_thread(pool, message_count=6)
    consumer = _lane_consumer(build_consumers(res, token_counter=TokenCounter()))
    await consumer.start()
    try:
        await _publish(broker, _envelope(job))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await consumer.stop()
        await res.connection.close()

    assert _count(metrics, "llm_context_tokens_count", kind="summarize") == 1
    assert _count(metrics, "llm_context_tokens_count", kind="generate") == 1
    assert (
        _count(
            metrics,
            "emails_generated_total",
            organization=str(job.organization_id),
            category="support",
        )
        == 1
    )
    assert await _draft_count(pool, job) == 1


class _BlockingFake(FakeLLMProvider):
    """Schema-aware fake whose draft call blocks until released (a worker stuck mid-generation)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, **kwargs: Any) -> LLMResult:
        if "draft" in set((kwargs.get("schema") or {}).get("required", [])):
            self.entered.set()
            await self.release.wait()
        return await super().generate(**kwargs)


async def test_worker_killed_mid_generation_yields_one_draft(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Review Focus 1 (design §9, R19.7, R22.8): kill -> redelivery -> exactly one draft."""
    job = await _seed_thread(pool, message_count=1)
    stuck = _BlockingFake()
    res_a = await _resources(broker, pool, create_pipeline_metrics())
    worker_a = _lane_consumer(build_consumers(res_a, llm_provider=stuck, token_counter=TokenCounter()))
    await worker_a.start()
    await _publish(broker, _envelope(job))
    await asyncio.wait_for(stuck.entered.wait(), timeout=15)

    # The kill: the broker sees the connection drop with the delivery unacked and requeues it.
    await res_a.connection.close()

    res_b = await _resources(broker, pool, create_pipeline_metrics())
    worker_b = _lane_consumer(build_consumers(res_b, token_counter=TokenCounter()))
    await worker_b.start()
    try:
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
        # The dead worker's stale call now finishes; it must not add a second draft.
        stuck.release.set()
        await asyncio.sleep(1.0)
    finally:
        await worker_b.stop()
        await res_b.connection.close()

    assert await _draft_count(pool, job) == 1
```

- [ ] **Step 2: Run the integration tests**

Run: `uv run pytest tests/integration/test_ai_worker_service_integration.py`
Expected: 2 passed. These exercise code finished in Tasks 1–4, so they pass on first run. Show they are not vacuous by making two temporary breaks, reverting after each:

- **Summarizer wrapper.** In `services/ai_worker/main.py`, construct the summarizer with the plain `provider` instead of the `InstrumentedLLMProvider`. The telemetry test must fail on `kind="summarize"`.
- **Redelivery.** In `test_worker_killed_mid_generation_yields_one_draft`, comment out `await res_a.connection.close()`, so there is no kill. The delivery stays with the blocked worker A, worker B never receives it, and `_wait_for_state(... DRAFTED ...)` must fail with a timeout. This shows the test depends on the kill-and-redeliver path.

Do not use "disable `persist_drafted`'s `DRAFTED` early return" as a break. The duplicate insert still rolls back, because the same transaction's `DRAFTED→DRAFTED` state transition is illegal and raises `IllegalStateTransitionError`. That break proves nothing. The one-draft guarantee has three layers: the early return, the transactional state check, and the unique index from migration `0004`.

Revert both and record all runs. If `_count(..., kind="summarize")` is 0 because the fake's summary call happened on the `fast` tier under a different label, read the sample labels and assert on `kind` only; the helper already ignores `tier`.

- [ ] **Step 3: Full suites, lint, types, commit**

Run: `uv run pytest tests/unit && uv run pytest tests/integration && uv run ruff check . && uv run ruff format --check . && uv run mypy packages services tests evaluation`
Expected: all pass, clean. Run `uv run ruff format` on the new test file if flagged, because some call lines exceed 100 characters as written. `ruff format` never rewraps docstrings, so shorten any docstring still over 100 characters by hand.

```bash
git add tests/integration/test_ai_worker_service_integration.py
git commit -m "test(ai-worker): composed telemetry and worker-kill proof on a real broker [task 4.13b] [R11.7, R19.7, R22.8]"
```

---

### Task 6: Compose wiring, image and smoke checks, docs, live gate

**Files:**
- Modify: `docker-compose.yml` (the `ai-worker:` service)
- Modify: `scripts/image_smoke.py` (entrypoint list)
- Modify: `scripts/stack_smoke.py` (billing step)
- Modify: `specs/tasks.md` (4.13b status; new task 3.16)

- [ ] **Step 1: Wire the service**

In `docker-compose.yml`, replace the `ai-worker:` service block with the following, matching `triage-worker`'s shape:

```yaml
  ai-worker:
    build: *app-build
    restart: unless-stopped
    command: ["python", "-m", "services.ai_worker.main"]
    stop_grace_period: 45s
    expose:
      - "8004"
    environment:
      <<: *app-env
      SERVICE_NAME: ai_worker
      PORT: "8004"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8004/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 30s
```

`stop_grace_period: 45s` covers the drain window. Generation takes 1–5 s (NFR8), plus a repair, across up to `ai_worker_priority_prefetch` in-flight jobs.

In `scripts/image_smoke.py`, add `"services.ai_worker.main",` to the module tuple, after `"services.triage_worker.main",`.

- [ ] **Step 2: Static checks**

Run: `docker compose config -q && docker compose config --format json | python3 -c "
import json, sys
svc = json.load(sys.stdin)['services']['ai-worker']
assert svc['command'] == ['python', '-m', 'services.ai_worker.main'], svc['command']
assert svc['depends_on']['init']['condition'] == 'service_completed_successfully'
assert svc['environment']['DATABASE__HOST'] == 'postgres' and svc['environment']['BROKER__HOST'] == 'rabbitmq'
assert 'readyz' in ' '.join(svc['healthcheck']['test'])
print('ai-worker wiring OK')
"`
Expected: `ai-worker wiring OK`. `docker compose config` is read-only.

Run: `make image-smoke`
Expected: the last line is `image smoke OK`. This builds the separately tagged `rag-email-runtime:smoke` and never touches the running stack.

- [ ] **Step 3: Extend the smoke check**

In `scripts/stack_smoke.py`, step 2 (billing), replace:

```text
        await wait_for_state(pool, org_id, billing_job, {JobState.QUEUED.value})
        print(f"ok   billing email -> {routed.routing_key}, job QUEUED")
```

with:

```text
        await wait_for_state(pool, org_id, billing_job, {JobState.DRAFTED.value})
        async with pool.acquire() as conn:
            drafts = await conn.fetchval(
                "SELECT count(*) FROM generated_draft WHERE job_id = $1", billing_job
            )
        if drafts != 1:
            raise SmokeFailure(f"billing job {billing_job} has {drafts} drafts, expected 1")
        print(f"ok   billing email -> {routed.routing_key} -> ai-worker, job DRAFTED (1 draft)")
```

Also update the module docstring's billing line from `job QUEUED` to `job DRAFTED` if it says so. `wait_for_state(pool, org_id, job_id, expected)` takes no timeout argument; it uses the module constant `TIMEOUT_S = 45.0`, which already covers a context build and one generation.

Run: `uv run ruff check scripts && uv run ruff format --check scripts/stack_smoke.py scripts/image_smoke.py && uv run python -m py_compile scripts/stack_smoke.py`
Expected: clean.

- [ ] **Step 4: Record status and the discovered gap**

In `specs/tasks.md`:

1. Change `- [ ] **4.13b AI-worker service wiring & live gate**` to `- [~] **4.13b AI-worker service wiring & live gate**`. Above its `_Requirements:_` line, add:

```markdown
  - Implemented: `services/ai_worker/main.py` composes one pipeline for all lanes:
    - `ThreadSummarizer`, instrumented as `summarize` and threshold-triggered;
    - `ContextBuilder` with `HybridRetriever` over Postgres;
    - `ComplexityRouter`;
    - `DraftingService` with a plain provider, metrics and price table.

    One `AIWorkerConsumer` runs per configured lane, with lane prefetch. The compose `ai-worker` runs it with a `/readyz` healthcheck and a 45 s drain grace. The fake provider answers the draft and summary schemas, so the default stack drafts. The lease reaper skips `DRAFTED`. The composed-worker telemetry and worker-kill proofs pass on a real broker.
  - Left: the live gate. The user runs `make up` and `make smoke` (the billing email must reach `DRAFTED` with one draft), then a completion audit runs.
```

2. After the last Phase 3 task (`3.15`), add:

```markdown
- [ ] **3.16 Dense query embedding in the production retrieval path** *(discovered 2026-09-27, GEMINI.md §7)*
  - Nothing in the production path fills `RetrievalQuery.query_vector`, so the pgvector branch returns no candidates (`packages/retrieval/postgres.py:192`) and hybrid retrieval runs lexical-only. Evidence: `grep -rn "query_vector" packages services` finds no producer.
  - Embed the query's semantic text with the configured embedder (`packages/knowledge/embedder.py`, the same model and dimension as the corpus, R5.10) in `RetrievalQueryBuilder` or the `ContextBuilder`, guarded by a timeout so a slow embedder degrades to lexical-only (R10.9). Count embedding tokens (`embedding_tokens_total`).
  - _Requirements: R10.1, R10.9, R9.11_
```

In the "Requirement coverage index", add `3.16` to the R10 row and the R9 row.

Run: `grep -n "4.13b AI-worker\|3.16 Dense query" specs/tasks.md && make ci > /tmp/ai413b_ci.log 2>&1; echo "make ci exit $?"`
Expected: two matching lines; `make ci exit 0`.

- [ ] **Step 5: Live gate (user-run)**

This restarts the running dev stack. The executor must not run it; the auto-mode classifier blocks it. Hand the user these commands and wait:

```bash
make up
docker compose ps ai-worker
make smoke
```

Expected:

- `ai-worker` shows `(healthy)`.
- `make smoke` prints `ok   billing email -> email.billing.<lane> -> ai-worker, job DRAFTED (1 draft)` and ends with `SMOKE OK`.

Before `make up`, run the read-only duplicate-draft check from the 4.11 note. Migration `0004`'s unique index fails on duplicates:

```bash
docker exec rag-email-postgres psql -U postgres -d rag_email -tAc "SELECT job_id, count(*) FROM generated_draft WHERE job_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1"
```

It must print nothing.

After the smoke run, the executor verifies read-only:

```bash
docker exec rag-email-rabbitmq rabbitmqctl list_queues name consumers | grep -E "^email\.support\.(normal|priority)\s"
docker compose exec ai-worker curl -fs localhost:8004/readyz
```

Each lane should show `1` consumer, and readyz should report `"status":"ok"`.

Optional live fault injection (R22.8): during a smoke run, `docker kill dazzling-bose-ai-worker-1` while the billing job is `GENERATING`. The container restarts (`restart: unless-stopped`), the delivery is redelivered, and the job still ends `DRAFTED` with one draft. The in-process test in Task 5 is the required proof.

- [ ] **Step 6: Commit (after the live gate)**

```bash
git add docker-compose.yml scripts/image_smoke.py scripts/stack_smoke.py specs/tasks.md
git commit -m "feat(compose): run the ai-worker and gate on an email reaching DRAFTED [task 4.13b] [R20.1, R24.7]"
```

---

## Dry-run verification (2026-09-27)

Tasks 1–5 and Task 6 Steps 1–4 were applied in a scratch copy. Seven defects were corrected above:
- the two `_ScriptedProvider` doubles that bypass `__init__`, one of them only caught by the integration suite;
- a long docstring;
- a `Job | None` return for strict mypy;
- a vacuous Task 5 break, replaced by the no-kill break;
- a non-existent `wait_for_state` timeout argument;
- docstring wrapping.

All Task 3 name checks matched the real code. Threshold semantics hold: 6 messages summarize and 2 do not. Measured: unit 1310 passed, integration 155 passed, `make ci` exit 0, `make image-smoke` printed `image smoke OK`, and the compose check printed `ai-worker wiring OK`. The live gate (Task 6 Step 5) was not run.

## Self-review record

1. **Spec coverage (4.13b bullets):**
   - The entrypoint on `WorkerRuntime`, one consumer per lane with prefetch, and the compose placeholder replaced: Tasks 3 and 6.
   - Summarizer instrumentation, and metrics plus price table into the generator and `DraftingService`: Task 3, with the summarizer call itself in Task 2.
   - The composed-worker telemetry test: Task 5.
   - Warm-up and the plain provider: Task 3.
   - The worker-kill test: Task 5.
   - `make smoke` reaching `DRAFTED`: Task 6.
   - The reaper and `DRAFTED`: Task 4.
   - Design §3.2 co-locating Hybrid RAG: Task 3. Its vector gap is recorded as 3.16.
2. **Placeholder scan.** No TBD. Each fallback note names the file and the exact check (Task 1 Step 4, Task 3 Step 3, Task 4 Step 1, Task 5 Step 2, Task 6 Step 3).
3. **Type consistency.**
   - `AIWorkerConsumer(..., summarizer=…)`: Tasks 2, 3 and 5.
   - `build_consumers(res, *, llm_provider=None, token_counter=None)`: Tasks 3 and 5.
   - `resolve_lane_queues(settings)` and `lane_prefetch(settings, queue)`: Task 3.
   - `FAKE_REPLY` and `FAKE_THREAD_SUMMARY`: Tasks 1 and 2.
4. **Review Focus.** Each of the five items names its pinning test.

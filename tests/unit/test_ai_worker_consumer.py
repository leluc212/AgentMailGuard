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
        self._explicit_default = True
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


async def test_skipped_recovery_is_retried_not_dead_lettered() -> None:
    """I2: recovery's DB write failed, so the job is still RETRY_PENDING on redelivery."""
    provider = _ScriptedProvider(REPLY)
    consumer, _, _, _, envelope = await _setup(provider, state=JobState.RETRY_PENDING)

    with pytest.raises(Exception) as excinfo:
        await consumer.process_job(envelope, MagicMock())

    assert not isinstance(excinfo.value, FatalError)
    assert provider.calls == 0

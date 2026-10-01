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

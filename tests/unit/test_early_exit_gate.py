"""Unit tests for the Early-Exit Gate and selective AI routing (R6.5, R6.6).

Verifies:
- reply_required == false transitions job directly to COMPLETED (R6.5).
- No embedding, retrieval, reranking, or generation call is made on early-exit (R6.5).
- retrieval_required == false skips hybrid RAG retrieval and reranking downstream (R6.6).
- Actionable retrieval-required emails proceed with full RAG pipeline.
- Integration with CascadingTriageEngine.triage_and_gate.
- State machine transition safety and telemetry events.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from packages.db.job import InMemoryJobStore
from packages.domain.entities import Classification, Job
from packages.domain.rules import EmailContext, Rule
from packages.domain.state_machine import IllegalStateTransitionError, JobState
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.gate import (
    EarlyExitGate,
    GateAction,
    GatedPipelineRunner,
)
from services.triage_worker.rules import HotReloadableRuleEngine


class MockPipelineHooks:
    """Mock implementation of DownstreamPipelineHooks for call verification."""

    def __init__(self) -> None:
        self.embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
        self.retrieve_knowledge = AsyncMock(return_value=[{"chunk_id": "c1", "score": 0.9}])
        self.rerank_candidates = AsyncMock(return_value=[{"chunk_id": "c1", "score": 0.95}])
        self.generate_reply = AsyncMock(return_value="Generated draft reply.")


@pytest.fixture
def mock_hooks() -> MockPipelineHooks:
    return MockPipelineHooks()


@pytest.fixture
def gate() -> EarlyExitGate:
    return EarlyExitGate()


@pytest.fixture
def sample_job() -> Job:
    return Job(
        id=uuid4(),
        organization_id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        state=JobState.CLASSIFIED,
    )


@pytest.fixture
def normalized_job() -> Job:
    return Job(
        id=uuid4(),
        organization_id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        state=JobState.NORMALIZED,
    )


class TestEarlyExitGateZeroAIExecution:
    """Validate R6.5: reply_required == false terminates at COMPLETED with zero AI calls."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "category",
        ["automated_notification", "acknowledgement", "no_response"],
    )
    async def test_early_exit_no_reply_categories(
        self,
        gate: EarlyExitGate,
        sample_job: Job,
        mock_hooks: MockPipelineHooks,
        category: str,
    ) -> None:
        """Every no-reply category transitions directly to COMPLETED without AI invocations."""
        classification = Classification(
            category=category,
            intent="system_alert",
            reply_required=False,
            retrieval_required=False,
            workflow_hint="none",
            confidence=0.99,
            decided_by="rule",
        )

        decision = gate.evaluate_decision(sample_job, classification)

        # 1. State machine assertion
        assert decision.action == GateAction.EARLY_EXIT
        assert decision.job.state == JobState.COMPLETED
        assert decision.should_embed is False
        assert decision.should_retrieve is False
        assert decision.should_rerank is False
        assert decision.should_generate is False

        # 2. Event assertion
        assert decision.event.state_to == JobState.COMPLETED
        assert decision.event.payload["early_exit"] is True
        assert decision.event.payload["reason"] == "no_reply_required"

        # 3. Downstream execution barrier assertion (R6.5 zero-call guarantee)
        runner = GatedPipelineRunner(mock_hooks)
        result = await runner.run_pipeline(decision, query="How do I reset password?")

        assert result["status"] == "completed_early_exit"
        assert result["reply"] is None
        assert mock_hooks.embed_query.call_count == 0
        assert mock_hooks.retrieve_knowledge.call_count == 0
        assert mock_hooks.rerank_candidates.call_count == 0
        assert mock_hooks.generate_reply.call_count == 0

    @pytest.mark.asyncio
    async def test_early_exit_from_normalized_state(
        self,
        gate: EarlyExitGate,
        normalized_job: Job,
        mock_hooks: MockPipelineHooks,
    ) -> None:
        """A job at NORMALIZED progresses legally through CLASSIFIED to COMPLETED."""
        classification = Classification(
            category="no_response",
            intent="out_of_office",
            reply_required=False,
            retrieval_required=False,
            workflow_hint="none",
            confidence=0.98,
            decided_by="rule",
        )

        decision = gate.evaluate_decision(normalized_job, classification)

        assert decision.action == GateAction.EARLY_EXIT
        assert decision.job.state == JobState.COMPLETED

        runner = GatedPipelineRunner(mock_hooks)
        await runner.run_pipeline(decision)

        assert mock_hooks.embed_query.call_count == 0
        assert mock_hooks.retrieve_knowledge.call_count == 0
        assert mock_hooks.rerank_candidates.call_count == 0
        assert mock_hooks.generate_reply.call_count == 0


class TestSelectiveRetrievalBypass:
    """Validate R6.6: retrieval_required == false skips hybrid RAG call."""

    @pytest.mark.asyncio
    async def test_retrieval_bypassed_when_retrieval_required_false(
        self,
        gate: EarlyExitGate,
        sample_job: Job,
        mock_hooks: MockPipelineHooks,
    ) -> None:
        """retrieval_required=false transitions to QUEUED, calls generator, but skips RAG."""
        classification = Classification(
            category="scheduling",
            intent="meeting_request",
            reply_required=True,
            retrieval_required=False,
            workflow_hint="ai",
            confidence=0.95,
            decided_by="ml",
        )

        decision = gate.evaluate_decision(sample_job, classification)

        assert decision.action == GateAction.PROCEED_NO_RAG
        assert decision.job.state == JobState.QUEUED
        assert decision.should_embed is False
        assert decision.should_retrieve is False
        assert decision.should_rerank is False
        assert decision.should_generate is True

        runner = GatedPipelineRunner(mock_hooks)
        result = await runner.run_pipeline(
            decision,
            query="Meeting tomorrow?",
            context={"thread": []},
        )

        assert result["status"] == "processed"
        assert result["retrieval_performed"] is False
        assert result["generation_performed"] is True
        assert result["reply"] == "Generated draft reply."

        # Verify zero calls to RAG components
        assert mock_hooks.embed_query.call_count == 0
        assert mock_hooks.retrieve_knowledge.call_count == 0
        assert mock_hooks.rerank_candidates.call_count == 0
        # Generator was called exactly once
        assert mock_hooks.generate_reply.call_count == 1

    @pytest.mark.asyncio
    async def test_full_rag_invoked_when_retrieval_required_true(
        self,
        gate: EarlyExitGate,
        sample_job: Job,
        mock_hooks: MockPipelineHooks,
    ) -> None:
        """retrieval_required=true executes embed, retrieve, rerank, and generate."""
        classification = Classification(
            category="support",
            intent="bug_report",
            reply_required=True,
            retrieval_required=True,
            workflow_hint="ai",
            confidence=0.92,
            decided_by="llm",
        )

        decision = gate.evaluate_decision(sample_job, classification)

        assert decision.action == GateAction.PROCEED_RAG
        assert decision.job.state == JobState.QUEUED
        assert decision.should_embed is True
        assert decision.should_retrieve is True
        assert decision.should_rerank is True
        assert decision.should_generate is True

        runner = GatedPipelineRunner(mock_hooks)
        result = await runner.run_pipeline(decision, query="App crashes on boot")

        assert result["status"] == "processed"
        assert result["retrieval_performed"] is True
        assert result["generation_performed"] is True
        assert result["chunks_count"] == 1

        # All pipeline stages executed
        assert mock_hooks.embed_query.call_count == 1
        assert mock_hooks.retrieve_knowledge.call_count == 1
        assert mock_hooks.rerank_candidates.call_count == 1
        assert mock_hooks.generate_reply.call_count == 1


class TestStateTransitionSafety:
    """Validate state machine transition invariants and illegal state handling."""

    def test_illegal_state_transition_raises(
        self,
        gate: EarlyExitGate,
    ) -> None:
        """Attempting to evaluate a job in an illegal state raises IllegalStateTransitionError."""
        completed_job = Job(
            id=uuid4(),
            organization_id=uuid4(),
            state=JobState.COMPLETED,
        )
        classification = Classification(
            category="support",
            reply_required=True,
            retrieval_required=True,
        )

        with pytest.raises(IllegalStateTransitionError):
            gate.evaluate_decision(completed_job, classification)

    @pytest.mark.asyncio
    async def test_evaluate_and_persist_with_in_memory_store(
        self,
        gate: EarlyExitGate,
        sample_job: Job,
    ) -> None:
        """evaluate_and_persist persists state and event to JobStore."""
        store = InMemoryJobStore()
        # Seed initial job
        await store.create_job(sample_job)

        classification = Classification(
            category="automated_notification",
            reply_required=False,
            retrieval_required=False,
            confidence=0.99,
            decided_by="rule",
        )

        decision = await gate.evaluate_and_persist(sample_job, classification, job_store=store)

        assert decision.action == GateAction.EARLY_EXIT
        assert decision.job.state == JobState.COMPLETED

        # Verify in store
        persisted = await store.get_job(sample_job.organization_id, sample_job.id)
        assert persisted is not None
        assert persisted.state == JobState.COMPLETED

        # Verify event logged
        events = await store.list_events_for_job(sample_job.organization_id, sample_job.id)
        assert any(e.state_to == JobState.COMPLETED for e in events)


class TestCascadeEngineGateIntegration:
    """Validate end-to-end integration between CascadingTriageEngine and EarlyExitGate."""

    @pytest.mark.asyncio
    async def test_cascade_triage_and_gate_early_exit(self) -> None:
        """Rule matching noreply leads directly to early exit in cascade."""
        rule = Rule.from_dict(
            {
                "id": "rule_noreply",
                "when": {"sender": {"contains": "alerts.acme.com"}},
                "then": {
                    "category": "automated_notification",
                    "priority": "low",
                    "reply_required": False,
                    "retrieval_required": False,
                    "workflow_hint": "none",
                    "confidence": 1.0,
                },
            }
        )
        engine = CascadingTriageEngine(rule_engine=HotReloadableRuleEngine(initial_rules=[rule]))
        job = Job(
            id=uuid4(),
            organization_id=uuid4(),
            state=JobState.NORMALIZED,
        )
        ctx = EmailContext(
            subject="CI/CD Build Successful",
            body_text="All tests passed.",
            sender_email="bot@alerts.acme.com",
        )

        cascade_res, gate_decision = await engine.triage_and_gate(job, ctx)

        assert cascade_res.classification.reply_required is False
        assert gate_decision.action == GateAction.EARLY_EXIT
        assert gate_decision.job.state == JobState.COMPLETED
        assert gate_decision.should_embed is False
        assert gate_decision.should_generate is False

    def test_cascade_triage_and_gate_sync(self) -> None:
        """Sync wrapper triage_and_gate_sync functions correctly."""
        rule = Rule.from_dict(
            {
                "id": "rule_sales",
                "when": {"sender": {"contains": "prospect.com"}},
                "then": {
                    "category": "sales",
                    "priority": "urgent",
                    "reply_required": True,
                    "retrieval_required": True,
                    "workflow_hint": "ai",
                    "confidence": 1.0,
                },
            }
        )
        engine = CascadingTriageEngine(rule_engine=HotReloadableRuleEngine(initial_rules=[rule]))
        job = Job(
            id=uuid4(),
            organization_id=uuid4(),
            state=JobState.CLASSIFIED,
        )
        ctx = EmailContext(
            subject="Enterprise Contract",
            body_text="We need pricing for 1000 seats.",
            sender_email="vp@prospect.com",
        )

        cascade_res, gate_decision = engine.triage_and_gate_sync(job, ctx)

        assert cascade_res.classification.category == "sales"
        assert gate_decision.action == GateAction.PROCEED_RAG
        assert gate_decision.job.state == JobState.QUEUED
        assert gate_decision.should_generate is True
        assert gate_decision.should_retrieve is True

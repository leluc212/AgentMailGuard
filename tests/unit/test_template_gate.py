"""Unit tests for EarlyExitGate template reply path (R6.12, R6.13, R6.14, R6.15).

Verifies:
1. Deterministic template replies transition directly to DRAFTED with zero retrieval,
   zero rerank, and zero generation calls.
2. Missing template in registry falls back to workflow_hint='ai', transitioning to QUEUED.
3. Funnel accounting: early exit, template reply, and AI generation are mutually exclusive
   and exhaustive over all actionable and non-actionable email.
4. Correct persistence in JobStore and DraftStore.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from packages.db.draft import InMemoryDraftStore
from packages.db.job import InMemoryJobStore
from packages.domain.entities import (
    Classification,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import JobState
from packages.domain.templates import (
    TemplateDefinition,
    TemplateRegistry,
)
from services.triage_worker.gate import (
    DownstreamPipelineHooks,
    EarlyExitGate,
    GateAction,
    GatedPipelineRunner,
)


@pytest.fixture
def sample_registry() -> TemplateRegistry:
    registry = TemplateRegistry()
    registry.register(
        TemplateDefinition(
            id="ack-receipt-v1",
            version="v1",
            category="acknowledgement",
            intent="receipt_confirmation",
            subject="Re: {{ subject }}",
            body=(
                "Hello {{ sender_name }},\n\nWe received message {{ message_id }}.\n"
                "Ref: {{ order_id }}"
            ),
        )
    )
    registry.register(
        TemplateDefinition(
            id="scheduling-ack-v1",
            version="v1",
            category="scheduling",
            intent="meeting_accepted",
            subject="Confirmed: {{ subject }}",
            body="Hello {{ sender_name }},\n\nMeeting accepted for order {{ order_id }}.",
        )
    )
    return registry


@pytest.fixture
def sample_message() -> NormalizedMessage:
    from datetime import UTC, datetime

    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="fake",
        provider_message_id="prov-101",
        rfc822_message_id="<msg-101@example.com>",
        sender=EmailAddress(name="Alice Smith", email="alice@example.com"),
        received_at=datetime.now(UTC),
        recipients=[EmailAddress(name="Support", email="support@company.com")],
        cc=[],
        subject="Status update inquiry",
        subject_normalized="Status update inquiry",
        body_text="Can you confirm receipt of my request?",
        body_text_clean="Can you confirm receipt of my request?",
        direction="inbound",
    )


@pytest.mark.asyncio
async def test_template_reply_transitions_to_drafted_with_zero_ai_calls(
    sample_registry: TemplateRegistry,
    sample_message: NormalizedMessage,
) -> None:
    """R6.13: workflow_hint='template' transitions to DRAFTED with zero AI calls."""
    gate = EarlyExitGate(template_registry=sample_registry)
    job = Job(
        state=JobState.CLASSIFIED,
        organization_id=sample_message.organization_id,
        message_id=sample_message.message_id,
        thread_id=sample_message.thread_id,
    )
    classification = Classification(
        category="acknowledgement",
        intent="receipt_confirmation",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
        confidence=0.98,
        decided_by="rule",
    )
    biz_data = {"order_id": "ORD-9999"}

    decision = gate.evaluate_decision(
        job=job,
        classification=classification,
        message=sample_message,
        business_data=biz_data,
    )

    # 1. Verify gate decision
    assert decision.action == GateAction.TEMPLATE_REPLY
    assert decision.job.state == JobState.DRAFTED
    assert decision.reply_required is True
    assert decision.workflow_hint == "template"
    assert decision.should_embed is False
    assert decision.should_retrieve is False
    assert decision.should_rerank is False
    assert decision.should_generate is False

    # 2. Verify rendered draft
    draft = decision.rendered_draft
    assert draft is not None
    assert draft.subject == "Re: Status update inquiry"
    assert "Hello Alice Smith" in draft.body
    assert "Ref: ORD-9999" in draft.body
    assert draft.status == "draft"
    assert draft.model_name == "template"
    assert draft.model_tier == "template"
    assert draft.cost_estimate == 0.0
    assert draft.input_tokens == 0
    assert draft.output_tokens == 0
    assert draft.citations == []

    # 3. Verify downstream pipeline execution asserts ZERO AI calls
    mock_hooks = AsyncMock(spec=DownstreamPipelineHooks)
    runner = GatedPipelineRunner(mock_hooks)
    result = await runner.run_pipeline(decision)

    assert result["status"] == "drafted_template_reply"
    assert result["embedding_performed"] is False
    assert result["retrieval_performed"] is False
    assert result["rerank_performed"] is False
    assert result["generation_performed"] is False
    assert result["reply"] == draft.body

    mock_hooks.embed_query.assert_not_called()
    mock_hooks.retrieve_knowledge.assert_not_called()
    mock_hooks.rerank_candidates.assert_not_called()
    mock_hooks.generate_reply.assert_not_called()


@pytest.mark.asyncio
async def test_missing_template_falls_back_to_ai_generation(
    sample_registry: TemplateRegistry,
    sample_message: NormalizedMessage,
) -> None:
    """R6.14: Missing template in registry falls back to 'ai' and transitions to QUEUED."""
    gate = EarlyExitGate(template_registry=sample_registry)
    job = Job(
        state=JobState.CLASSIFIED,
        organization_id=sample_message.organization_id,
        message_id=sample_message.message_id,
        thread_id=sample_message.thread_id,
    )
    # Category has no matching template in sample_registry
    classification = Classification(
        category="billing",
        intent="dispute_charge",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=True,
        confidence=0.85,
        decided_by="ml",
    )

    decision = gate.evaluate_decision(
        job=job,
        classification=classification,
        message=sample_message,
    )

    # Must fall back to AI generation, transitioning to QUEUED (never blocking a reply)
    assert decision.action == GateAction.PROCEED_RAG
    assert decision.job.state == JobState.QUEUED
    assert decision.workflow_hint == "ai"
    assert decision.should_retrieve is True
    assert decision.should_embed is True
    assert decision.should_rerank is True
    assert decision.should_generate is True
    assert decision.rendered_draft is None


@pytest.mark.asyncio
async def test_three_outcomes_are_mutually_exclusive_and_exhaustive(
    sample_registry: TemplateRegistry,
    sample_message: NormalizedMessage,
) -> None:
    """R6.15: Verify early exit, template reply, and AI are mutually exclusive and exhaustive."""
    gate = EarlyExitGate(template_registry=sample_registry)

    cases = [
        # Case 1: Automated notification -> Early Exit (Outcome 1)
        (
            Classification(
                category="automated_notification",
                reply_required=False,
                workflow_hint="none",
                retrieval_required=False,
            ),
            GateAction.EARLY_EXIT,
            JobState.COMPLETED,
        ),
        # Case 2: Acknowledgement with template -> Template Reply (Outcome 2)
        (
            Classification(
                category="acknowledgement",
                intent="receipt_confirmation",
                reply_required=True,
                workflow_hint="template",
                retrieval_required=False,
            ),
            GateAction.TEMPLATE_REPLY,
            JobState.DRAFTED,
        ),
        # Case 3: Scheduling with template -> Template Reply (Outcome 2)
        (
            Classification(
                category="scheduling",
                intent="meeting_accepted",
                reply_required=True,
                workflow_hint="template",
                retrieval_required=False,
            ),
            GateAction.TEMPLATE_REPLY,
            JobState.DRAFTED,
        ),
        # Case 4: Template requested but no template found -> Fallback to AI (Outcome 3)
        (
            Classification(
                category="general_inquiry",
                intent="unmatched_intent",
                reply_required=True,
                workflow_hint="template",
                retrieval_required=True,
            ),
            GateAction.PROCEED_RAG,
            JobState.QUEUED,
        ),
        # Case 5: AI with retrieval -> Actionable AI Generation (Outcome 3)
        (
            Classification(
                category="support",
                intent="troubleshoot",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=True,
            ),
            GateAction.PROCEED_RAG,
            JobState.QUEUED,
        ),
        # Case 6: AI without retrieval -> Actionable AI Generation (Outcome 3)
        (
            Classification(
                category="sales",
                intent="followup",
                reply_required=True,
                workflow_hint="ai",
                retrieval_required=False,
            ),
            GateAction.PROCEED_NO_RAG,
            JobState.QUEUED,
        ),
    ]

    for cls_input, expected_action, expected_state in cases:
        fresh_job = Job(
            state=JobState.CLASSIFIED,
            organization_id=sample_message.organization_id,
            message_id=sample_message.message_id,
            thread_id=sample_message.thread_id,
        )
        decision = gate.evaluate_decision(
            job=fresh_job, classification=cls_input, message=sample_message
        )
        assert decision.action == expected_action
        assert decision.job.state == expected_state

        # Check mutual exclusivity
        is_early_exit = decision.action == GateAction.EARLY_EXIT
        is_template = decision.action == GateAction.TEMPLATE_REPLY
        is_ai = decision.action in (GateAction.PROCEED_RAG, GateAction.PROCEED_NO_RAG)

        assert (int(is_early_exit) + int(is_template) + int(is_ai)) == 1, (
            "Must land in exactly ONE outcome"
        )


@pytest.mark.asyncio
async def test_evaluate_and_persist_template_reply(
    sample_registry: TemplateRegistry,
    sample_message: NormalizedMessage,
) -> None:
    """Verify evaluate_and_persist transitions JobStore to DRAFTED and stores draft."""
    job_store = InMemoryJobStore()
    draft_store = InMemoryDraftStore()
    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=sample_registry,
        draft_store=draft_store,
    )

    org_id = sample_message.organization_id
    job = Job(
        organization_id=org_id,
        message_id=sample_message.message_id,
        thread_id=sample_message.thread_id,
        state=JobState.NORMALIZED,
    )
    job, _ = await job_store.create_job(job)

    cls = Classification(
        category="acknowledgement",
        intent="receipt_confirmation",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
    )

    decision = await gate.evaluate_and_persist(
        job=job,
        classification=cls,
        message=sample_message,
        business_data={"order_id": "ORD-123"},
    )

    assert decision.action == GateAction.TEMPLATE_REPLY
    assert decision.job.state == JobState.DRAFTED

    # Verify job in JobStore
    stored_job = await job_store.get_job(org_id, job.id)
    assert stored_job is not None
    assert stored_job.state == JobState.DRAFTED

    # Verify draft in DraftStore
    assert decision.rendered_draft is not None
    stored_draft = await draft_store.get_draft(decision.rendered_draft.id, org_id)
    assert stored_draft is not None
    assert stored_draft.subject == "Re: Status update inquiry"
    assert "Alice Smith" in stored_draft.body
    assert "Ref: ORD-123" in stored_draft.body

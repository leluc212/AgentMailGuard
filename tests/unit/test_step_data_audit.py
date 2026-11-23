"""Task 9.1 (R25.7, R23.5): each pipeline step's event carries what the demo console shows."""

from __future__ import annotations

from uuid import uuid4

from packages.db.draft_persistence import drafted_event_payload
from packages.domain.entities import Classification, GeneratedDraft, Job
from packages.domain.state_machine import JobState
from services.triage_worker.gate import EarlyExitGate, GateAction, triage_event_fields


def _job() -> Job:
    return Job(
        id=uuid4(),
        organization_id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        state=JobState.NORMALIZED.value,
    )


def test_triage_fields_carry_the_whole_classification() -> None:
    cls = Classification(
        category="billing",
        intent="invoice_question",
        priority="urgent",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=True,
        confidence=0.91,
        decided_by="ml",
        latency_ms=12,
        model="clf-v1",
    )

    fields = triage_event_fields(cls)

    assert fields == {
        "category": "billing",
        "intent": "invoice_question",
        "priority": "urgent",
        "reply_required": True,
        "workflow_hint": "ai",
        "retrieval_required": True,
        "confidence": 0.91,
        "decided_by": "ml",
        "triage_latency_ms": 12,
        "triage_model": "clf-v1",
    }


def test_every_gate_outcome_names_its_action_and_reason() -> None:
    gate = EarlyExitGate(category_retrieval_floor=False)
    cases = [
        (Classification(category="newsletter", reply_required=False), GateAction.EARLY_EXIT),
        (
            Classification(category="general_inquiry", retrieval_required=False),
            GateAction.PROCEED_NO_RAG,
        ),
        (Classification(category="support", retrieval_required=True), GateAction.PROCEED_RAG),
    ]
    for cls, expected in cases:
        decision = gate.evaluate_decision(_job(), cls)
        assert decision.event.payload["gate_action"] == expected.value
        assert decision.event.payload["reason"] == decision.reason


def test_drafted_event_lists_the_cited_chunks() -> None:
    draft = GeneratedDraft(
        id=uuid4(),
        organization_id=uuid4(),
        job_id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        action="reply",
        subject="s",
        body="b",
        confidence=0.8,
        citations=[
            {"citation_id": "K1", "chunk_id": "c-1", "document_id": "d", "external_id": None},
            {"citation_id": "K2", "chunk_id": "c-2", "document_id": "d", "external_id": None},
        ],
        citation_mismatch=False,
        model_name="fake",
        model_tier="standard",
        escalation_reason=None,
        prompt_version="v1",
        input_tokens=10,
        output_tokens=5,
        cost_estimate=0.0,
        status="draft",
    )

    payload = drafted_event_payload(draft)

    assert payload["citations_count"] == 2
    assert payload["cited_chunk_ids"] == ["c-1", "c-2"]

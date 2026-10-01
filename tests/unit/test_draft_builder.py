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

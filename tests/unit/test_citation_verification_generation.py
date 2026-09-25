"""Integration of citation verification into draft generation (R16.5, R21.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    NormalizedMessage,
)
from packages.llm import (
    AgentProfileRegistry,
    FakeLLMProvider,
    SinglePassGenerator,
)
from packages.observability.metrics import (
    create_pipeline_metrics,
    generate_metrics_payload,
)


def _context(*chunks: Candidate) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-cite-gen-001",
        sender=EmailAddress(email="customer@example.com", name="Alice Customer"),
        subject="How do I reset my password?",
        subject_normalized="How do I reset my password?",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
        received_at=datetime.now(UTC),
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=list(chunks),
    )


def _supplied_chunk() -> Candidate:
    return Candidate(
        chunk_id="chunk-1",
        document_id="doc-kb-01",
        content="To reset a password, open settings and choose Reset Password.",
        external_id="DOC-125-08",
    )


def _reply(*cited: str) -> dict[str, Any]:
    return {
        "action": "reply",
        "draft": "Hello Alice, open settings and choose Reset Password.",
        "confidence": 0.95,
        "knowledge_chunks": list(cited),
        "thread_summary_updated": False,
        "model_tier": "routine",
    }


@pytest.fixture
def profile_registry() -> AgentProfileRegistry:
    return AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")


def _metrics_payload(metrics: Any) -> str:
    payload, _ = generate_metrics_payload(metrics.registry)
    return str(payload.decode("utf-8"))


@pytest.mark.asyncio
async def test_grounded_draft_reports_no_citation_mismatch(
    profile_registry: AgentProfileRegistry,
) -> None:
    """A draft citing a supplied chunk is clean and counted in the denominator (R16.5)."""
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-125-08")),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(_context(_supplied_chunk()), category="support")

    assert result.citation_mismatch is False
    assert result.citation_verdict is not None
    assert result.citation_verdict.citations[0]["chunk_id"] == "chunk-1"

    payload = _metrics_payload(metrics)
    assert 'citations_verified_total{category="support"} 1.0' in payload
    assert 'citation_mismatches_total{category="support"}' not in payload


@pytest.mark.asyncio
async def test_hallucinated_citation_flags_the_draft_without_failing_the_job(
    profile_registry: AgentProfileRegistry,
) -> None:
    """An ungrounded citation is rejected and flagged, and the draft is still returned.

    R16.5 records a flag; failing the job is R16.3's behaviour for a schema failure. A
    reviewer needs to see the draft in order to judge it, so it must survive.
    """
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-125-08", "DOC-999-99")),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(_context(_supplied_chunk()), category="support")

    assert result.citation_mismatch is True
    assert result.citation_verdict is not None
    assert result.citation_verdict.mismatched == ["DOC-999-99"]
    # The accepted citation survives; only the invented one is rejected.
    assert [c["external_id"] for c in result.citation_verdict.citations] == ["DOC-125-08"]
    # The draft itself is intact and the model's own claim is preserved for debugging (D5).
    assert result.content["draft"].startswith("Hello Alice")
    assert result.content["knowledge_chunks"] == ["DOC-125-08", "DOC-999-99"]

    payload = _metrics_payload(metrics)
    assert 'citations_verified_total{category="support"} 1.0' in payload
    assert 'citation_mismatches_total{category="support"} 1.0' in payload


@pytest.mark.asyncio
async def test_citations_of_a_repaired_draft_are_verified(
    profile_registry: AgentProfileRegistry,
) -> None:
    """Verification runs on the payload that survived repair, not the rejected one.

    The repair retry replaces the draft, so a first attempt that cited nothing must not
    decide the verdict for a second attempt that cited something ungrounded.
    """
    metrics = create_pipeline_metrics()
    malformed = {"action": "reply", "draft": "x", "confidence": 0.9, "knowledge_chunks": []}
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(canned_responses=[malformed, _reply("DOC-999-99")]),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(_context(_supplied_chunk()), category="support")

    assert result.is_repaired is True
    assert result.citation_mismatch is True
    assert result.citation_verdict is not None
    assert result.citation_verdict.mismatched == ["DOC-999-99"]
    assert 'citation_mismatches_total{category="support"} 1.0' in _metrics_payload(metrics)


@pytest.mark.asyncio
async def test_draft_citing_nothing_is_counted_but_not_flagged(
    profile_registry: AgentProfileRegistry,
) -> None:
    """A draft that cites nothing is clean, and still counts toward the denominator."""
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply()),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(_context(_supplied_chunk()), category="support")

    assert result.citation_mismatch is False
    assert result.citation_verdict is not None
    assert result.citation_verdict.cited_count == 0
    assert result.citation_verdict.mismatch_ratio == 0.0

    payload = _metrics_payload(metrics)
    assert 'citations_verified_total{category="support"} 1.0' in payload
    assert 'citation_mismatches_total{category="support"}' not in payload


@pytest.mark.asyncio
async def test_citing_when_no_chunks_were_retrieved_is_wholly_ungrounded(
    profile_registry: AgentProfileRegistry,
) -> None:
    """With an empty retrieval context every citation is a hallucination."""
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-125-08")),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(_context(), category="support")

    assert result.citation_mismatch is True
    assert result.citation_verdict is not None
    assert result.citation_verdict.supplied_count == 0
    assert result.citation_verdict.mismatch_ratio == 1.0
    assert 'citation_mismatches_total{category="support"} 1.0' in _metrics_payload(metrics)


@pytest.mark.asyncio
async def test_verification_runs_without_metrics_configured(
    profile_registry: AgentProfileRegistry,
) -> None:
    """A generator built without metrics still produces a verdict, and does not crash."""
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-999-99")),
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(_context(_supplied_chunk()), category="support")

    assert result.citation_mismatch is True

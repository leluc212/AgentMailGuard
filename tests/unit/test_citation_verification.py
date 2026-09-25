"""Unit tests for citation grounding verification (R16.5, design.md §5.7)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    NormalizedMessage,
)
from packages.llm import CitationVerdict, build_citation_index, verify_citations


def _chunk(
    chunk_id: str,
    external_id: str | None = None,
    document_id: str = "doc-kb-01",
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        document_id=document_id,
        content="To reset a password, open settings and choose Reset Password.",
        external_id=external_id,
    )


def _context(*chunks: Candidate) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-cite-001",
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


def test_citation_naming_a_supplied_chunk_is_accepted() -> None:
    """A citation matching a supplied chunk's external_id resolves to that chunk."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-125-08"], context)

    assert isinstance(verdict, CitationVerdict)
    assert verdict.mismatch is False
    assert verdict.mismatched == []
    assert verdict.supplied_count == 1
    assert verdict.cited_count == 1
    assert verdict.citations == [
        {
            "citation_id": "DOC-125-08",
            "chunk_id": "chunk-1",
            "document_id": "doc-kb-01",
            "external_id": "DOC-125-08",
        }
    ]


def test_citation_naming_no_supplied_chunk_is_rejected() -> None:
    """An id absent from the context is rejected and sets the mismatch flag (R16.5)."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-999-99"], context)

    assert verdict.mismatch is True
    assert verdict.mismatched == ["DOC-999-99"]
    assert verdict.citations == []
    assert verdict.cited_count == 1
    assert verdict.mismatch_ratio == 1.0


def test_accepted_and_rejected_citations_are_partitioned() -> None:
    """A draft mixing a real and an invented citation keeps the real one and flags the draft."""
    context = _context(
        _chunk("chunk-1", external_id="DOC-125-08"),
        _chunk("chunk-2", external_id="DOC-772-02", document_id="doc-kb-02"),
    )

    verdict = verify_citations(["DOC-125-08", "DOC-000-00", "DOC-772-02"], context)

    assert verdict.mismatch is True
    assert verdict.mismatched == ["DOC-000-00"]
    assert [entry["external_id"] for entry in verdict.citations] == [
        "DOC-125-08",
        "DOC-772-02",
    ]
    assert verdict.cited_count == 3
    assert verdict.mismatch_ratio == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    "cited",
    ["doc-125-08", "DOC-125-08 ", "  Doc-125-08", "\tDOC-125-08\n"],
)
def test_case_and_whitespace_drift_still_matches(cited: str) -> None:
    """A model that lowercases or pads a citation has not hallucinated it.

    Treating these as mismatches would inflate the very rate the metric reports, so the
    comparison folds case and strips surrounding whitespace while the output keeps the id
    as the model wrote it, trimmed.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations([cited], context)

    assert verdict.mismatch is False
    assert verdict.citations[0]["chunk_id"] == "chunk-1"
    assert verdict.citations[0]["citation_id"] == cited.strip()


def test_duplicate_citations_count_once() -> None:
    """The same chunk cited repeatedly is one citation, so the denominator is not inflated."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-125-08", "doc-125-08", "DOC-125-08"], context)

    assert verdict.cited_count == 1
    assert len(verdict.citations) == 1
    assert verdict.mismatch is False


def test_duplicate_mismatches_count_once() -> None:
    """A repeated invented id is reported once, not once per repetition."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-999-99", "DOC-999-99"], context)

    assert verdict.mismatched == ["DOC-999-99"]
    assert verdict.cited_count == 1
    assert verdict.mismatch_ratio == 1.0


def test_chunk_without_external_id_is_cited_by_chunk_id() -> None:
    """The template shows chunk_id when external_id is unset, so that id must match.

    `prompts/*.j2` renders `[CITATION: {{ chunk.external_id or chunk.chunk_id }}]`, so for a
    chunk with no external_id the chunk_id IS the citation id the model was given.
    """
    context = _context(_chunk("chunk-77", external_id=None))

    verdict = verify_citations(["chunk-77"], context)

    assert verdict.mismatch is False
    assert verdict.citations == [
        {
            "citation_id": "chunk-77",
            "chunk_id": "chunk-77",
            "document_id": "doc-kb-01",
            "external_id": None,
        }
    ]


def test_chunk_id_is_accepted_even_when_external_id_is_set() -> None:
    """Either alias of a supplied chunk is accepted (decision D1).

    A chunk_id names a chunk that genuinely was in the context, so it is not a hallucinated
    citation. R16.5 tests correspondence to supplied chunks, not alias discipline.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["chunk-1"], context)

    assert verdict.mismatch is False
    assert verdict.citations[0]["external_id"] == "DOC-125-08"


def test_citations_with_no_supplied_chunks_are_all_mismatches() -> None:
    """Citing anything when nothing was retrieved is wholly ungrounded."""
    context = _context()

    verdict = verify_citations(["DOC-125-08"], context)

    assert verdict.supplied_count == 0
    assert verdict.mismatch is True
    assert verdict.mismatched == ["DOC-125-08"]
    assert verdict.mismatch_ratio == 1.0


def test_no_citations_is_not_a_mismatch() -> None:
    """A draft that cites nothing has made no false claim, whatever was supplied.

    Guards the zero denominator: mismatch_ratio must be 0.0, never a ZeroDivisionError.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations([], context)

    assert verdict.mismatch is False
    assert verdict.cited_count == 0
    assert verdict.mismatch_ratio == 0.0
    assert verdict.citations == []


def test_blank_citation_strings_are_ignored() -> None:
    """An empty or whitespace-only entry is noise, not a hallucinated citation."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["", "   ", "DOC-125-08"], context)

    assert verdict.mismatch is False
    assert verdict.cited_count == 1


def test_document_id_is_not_an_accepted_citation() -> None:
    """Citing a document is not evidence a specific chunk was supplied (decision D2).

    Several chunks share one document_id, so accepting it would let a model name a document
    it saw one line of and have every claim about it counted as grounded.
    """
    context = _context(
        _chunk("chunk-1", external_id="DOC-125-08", document_id="doc-kb-01")
    )

    verdict = verify_citations(["doc-kb-01"], context)

    assert verdict.mismatch is True
    assert verdict.mismatched == ["doc-kb-01"]


def test_citation_index_holds_only_chunk_level_aliases() -> None:
    """build_citation_index exposes exactly the ids the prompt renders, and no others."""
    chunks = [
        _chunk("chunk-1", external_id="DOC-125-08", document_id="doc-kb-01"),
        _chunk("chunk-2", external_id=None, document_id="doc-kb-02"),
    ]

    index = build_citation_index(chunks)

    assert set(index) == {"doc-125-08", "chunk-1", "chunk-2"}
    assert "doc-kb-01" not in index
    assert "doc-kb-02" not in index

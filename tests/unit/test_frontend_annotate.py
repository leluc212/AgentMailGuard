"""Pure draft annotation for the review screen (task 6.8; R23.4, R13.5, R16.5)."""

from __future__ import annotations

from services.frontend.annotate import (
    AnnotatedSentence,
    Segment,
    annotate_draft,
    highlight_facts,
    split_paragraphs,
)
from services.frontend.api_client import BusinessFactView, CitedChunk


def _chunk(citation_id: str, text: str | None = None, chunk_id: str | None = None) -> CitedChunk:
    return CitedChunk(
        citation_id=citation_id, chunk_id=chunk_id, title=f"Doc {citation_id}", text=text
    )


def _plain(sentence: AnnotatedSentence) -> str:
    return "".join(segment.text for segment in sentence.segments)


def _order_fact(reference: str = "ORD-82915") -> BusinessFactView:
    return BusinessFactView(entity="order", reference=reference, status="FOUND")


def test_paragraphs_and_sentences_are_preserved() -> None:
    assert split_paragraphs("Hello Alice.\n\nYour order shipped. It arrives soon.\n") == [
        ["Hello Alice."],
        ["Your order shipped.", "It arrives soon."],
    ]
    assert split_paragraphs("   ") == []


def test_inline_marker_places_the_citation_and_is_removed_from_the_text() -> None:
    draft = annotate_draft(
        "We refund within 14 days [CITATION: KB-7]. Thanks for waiting.", [_chunk("kb-7")], []
    )
    first, second = draft.paragraphs[0]
    assert _plain(first) == "We refund within 14 days."
    assert [c.citation_id for c in first.citations] == ["kb-7"]
    assert second.citations == []
    assert draft.unplaced == []


def test_marker_after_the_full_stop_stays_with_its_sentence() -> None:
    draft = annotate_draft(
        "Returns are free. [CITATION: kb-1] Contact us anytime.", [_chunk("kb-1")], []
    )
    first, second = draft.paragraphs[0]
    assert _plain(first) == "Returns are free."
    assert [c.citation_id for c in first.citations] == ["kb-1"]
    assert _plain(second) == "Contact us anytime."


def test_marker_may_name_the_chunk_id_alias() -> None:
    chunk = _chunk("kb-9", chunk_id="c-9")
    draft = annotate_draft("Refunds take a week [CITATION: c-9].", [chunk], [])
    assert [c.citation_id for c in draft.paragraphs[0][0].citations] == ["kb-9"]


def test_without_a_marker_the_citation_goes_to_the_sentence_sharing_most_terms() -> None:
    body = (
        "Your parcel left on Monday. "
        "Refunds are issued within fourteen days after the return reaches our warehouse."
    )
    chunk = _chunk(
        "kb-2",
        text=(
            "Refunds are issued within fourteen days after the returned item reaches the warehouse."
        ),
    )
    draft = annotate_draft(body, [chunk], [])
    first, second = draft.paragraphs[0]
    assert first.citations == []
    assert [c.citation_id for c in second.citations] == ["kb-2"]


def test_a_citation_with_no_support_is_listed_as_unplaced() -> None:
    weak = _chunk("kb-3", text="Patience is appreciated in queues.")  # shares one term only
    no_text = _chunk("kb-4")
    draft = annotate_draft("Thanks for your patience.", [weak, no_text], [])
    assert draft.paragraphs[0][0].citations == []
    assert [c.citation_id for c in draft.unplaced] == ["kb-3", "kb-4"]


def test_citation_numbers_follow_api_order_and_ignore_duplicates() -> None:
    draft = annotate_draft("Text.", [_chunk("b"), _chunk("a"), _chunk("b")], [])
    assert draft.numbers == {"b": 1, "a": 2}
    assert [c.citation_id for c in draft.unplaced] == ["b", "a"]


def test_business_references_are_highlighted_case_insensitively_on_whole_tokens() -> None:
    segments = highlight_facts("Order ord-82915 shipped; ORD-829150 did not.", [_order_fact()])
    assert segments == [
        Segment("Order "),
        Segment("ord-82915", fact_reference="ORD-82915"),
        Segment(" shipped; ORD-829150 did not."),
    ]


def test_no_facts_leaves_the_text_whole() -> None:
    assert highlight_facts("Plain sentence.", []) == [Segment("Plain sentence.")]
    fact_without_reference = BusinessFactView(entity="order", reference=None, status="NOT_FOUND")
    assert highlight_facts("Plain.", [fact_without_reference]) == [Segment("Plain.")]


def test_annotate_highlights_facts_inside_sentences() -> None:
    draft = annotate_draft("Your order ORD-82915 was dispatched.", [], [_order_fact()])
    assert draft.paragraphs[0][0].segments == [
        Segment("Your order "),
        Segment("ORD-82915", fact_reference="ORD-82915"),
        Segment(" was dispatched."),
    ]

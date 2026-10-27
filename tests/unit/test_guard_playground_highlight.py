"""Evidence_Highlight (tasks 3.5, 3.6).

Requirements 14.1-14.3, 14.5, 14.6, 14.8, 14.9, 8.9, 11.11.
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st
from markupsafe import escape

from services.guard_playground.core.highlight import (
    NOT_LOCATED,
    Needle,
    Ref,
    Span,
    highlight_email,
    render,
    spans,
)

_MARKER_RE = re.compile(r'<span aria-hidden="true">\u2691</span>')
_LINK_RE = re.compile(r"<a [^>]*>[^<]*</a>")
_MARK_RE = re.compile(r"<mark [^>]*>|</mark>")
_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)")


def strip_highlights(html: str) -> str:
    """Remove the inserted marker and link elements entirely, then the ``mark`` tags."""
    return _MARK_RE.sub("", _LINK_RE.sub("", _MARKER_RE.sub("", html)))


def n(text: str, ref: str = "1", label: str = "L1 · R12") -> Needle:
    return Needle(text, ref, label)


# --- examples ---------------------------------------------------------------------------


def test_every_occurrence_is_highlighted() -> None:
    text = "pay now. pay now!"
    result = spans(text, [n("pay now")])
    assert [(s.start, s.end) for s in result.spans] == [(0, 7), (9, 16)]
    assert result.hits[0].count == 2
    assert result.hits[0].first_span == 1
    html = render(text, result.spans, id_prefix="L")
    assert html.count('<mark class="ev"') == 2
    assert 'id="L-ev-1"' in html and 'id="L-ev-2"' in html
    assert '<a class="ev-ref" href="#f-1">L1 · R12</a>' in html


def test_case_sensitive_match() -> None:
    assert spans("Ignore ignore", [n("ignore")]).hits[0].count == 1


def test_overlapping_spans_merge_with_union_refs() -> None:
    text = "abcdef"
    result = spans(text, [n("abcd", "a", "L1 · A"), n("cdef", "b", "L2 · —")])
    assert result.spans == [Span(0, 6, (Ref("a", "L1 · A"), Ref("b", "L2 · —")))]
    assert [h.first_span for h in result.hits] == [1, 1]
    html = render(text, result.spans, id_prefix="x")
    assert html.count("<mark") == 1
    assert 'href="#f-a"' in html and 'href="#f-b"' in html


def test_overlapping_occurrences_of_one_needle_merge() -> None:
    result = spans("aaaa", [n("aa")])
    assert result.hits[0].count == 3
    assert result.spans == [Span(0, 4, (Ref("1", "L1 · R12"),))]


def test_adjacent_spans_stay_separate() -> None:
    result = spans("abcdef", [n("abc", "a"), n("def", "b")])
    assert [(s.start, s.end) for s in result.spans] == [(0, 3), (3, 6)]
    assert [h.first_span for h in result.hits] == [1, 2]


def test_html_in_excerpt_is_escaped() -> None:
    text = "Hi <script>alert(1)</script> bye"
    excerpt = "<script>alert(1)</script>"
    html = str(render(text, spans(text, [n(excerpt)]).spans, id_prefix="b"))
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;<a " in html
    assert strip_highlights(html) == str(escape(text))


def test_not_located_and_blank_needles_get_note() -> None:
    result = spans("hello world", [n("absent"), n(""), n("  \n\t"), n("world")])
    assert [h.count for h in result.hits] == [0, 0, 0, 1]
    assert [h.note for h in result.hits] == [NOT_LOCATED, NOT_LOCATED, NOT_LOCATED, None]
    assert [h.first_span for h in result.hits] == [None, None, None, 1]
    assert len(result.spans) == 1


def test_whitespace_and_newlines_preserved() -> None:
    text = "  line one\n\n\tline two  \r\n"
    html = render(text, spans(text, [n("one\n\n\tline")]).spans, id_prefix="p")
    assert strip_highlights(str(html)) == text


def test_highlight_email_first_occurrence_in_reading_order() -> None:
    needles = [n("evil", "f1"), n("only chunk", "f2"), n("nowhere", "f3")]
    out = highlight_email(
        "an evil body", ["only chunk here", "evil and evil"], needles, id_prefix="C3"
    )
    loc = out.locations
    assert [x.count for x in loc] == [3, 1, 0]
    assert [x.first_id for x in loc] == ["C3-body-ev-1", "C3-c0-ev-1", None]
    assert [x.note for x in loc] == [None, None, NOT_LOCATED]
    assert 'id="C3-c1-ev-2"' in out.chunks[1]


def test_highlight_email_first_occurrence_in_chunk_when_absent_from_body() -> None:
    out = highlight_email("clean", ["x", "a bad b"], [n("bad")], id_prefix="L")
    assert out.locations[0].first_id == "L-c1-ev-1"
    assert out.locations[0].count == 1


# --- properties -------------------------------------------------------------------------

_ALPHABET = st.sampled_from(list("ab<>&\"' \n\t"))
_texts = st.text(_ALPHABET, max_size=40)


@st.composite
def text_and_needles(draw: st.DrawFn) -> tuple[str, list[Needle]]:
    text = draw(_texts)
    needles: list[Needle] = []
    for i in range(draw(st.integers(0, 5))):
        if text and draw(st.booleans()):
            a = draw(st.integers(0, len(text) - 1))
            b = draw(st.integers(a, min(len(text), a + 8)))
            needle_text = text[a:b]
        else:
            needle_text = draw(st.text(_ALPHABET, max_size=4))
        label = draw(st.text(_ALPHABET, max_size=6))
        needles.append(Needle(needle_text, f"{i}<\"'&>", label))
    return text, needles


@settings(max_examples=150, deadline=None)
@given(text_and_needles())
def test_property_4_highlight_preserves_text(case: tuple[str, list[Needle]]) -> None:
    """Property 4: Highlight giữ nguyên văn bản.

    **Validates: Requirements 14.8, 8.9**
    """
    text, needles = case
    html = render(text, spans(text, needles).spans, id_prefix="p")
    assert strip_highlights(str(html)) == str(escape(text))


@settings(max_examples=150, deadline=None)
@given(text_and_needles())
def test_property_5_highlight_injects_no_tags(case: tuple[str, list[Needle]]) -> None:
    """Property 5: Highlight không chèn được thẻ.

    **Validates: Requirements 8.9, 11.11**
    """
    text, needles = case
    result = spans(text, needles)
    html = str(render(text, result.spans, id_prefix="p"))
    assert {m.group(1) for m in _TAG_RE.finditer(html)} <= {"mark", "span", "a"}
    # Exactly the tags code produced: one mark + one span + one link per ref, per highlight.
    assert html.count("<mark ") == len(result.spans)
    assert html.count("<span ") == len(result.spans)
    assert html.count("<a ") == sum(len(s.refs) for s in result.spans)
    assert html.count("<") == 2 * (2 * len(result.spans) + sum(len(s.refs) for s in result.spans))


@settings(max_examples=150, deadline=None)
@given(text_and_needles())
def test_property_6_merged_spans(case: tuple[str, list[Needle]]) -> None:
    """Property 6: Gộp khoảng tô sáng.

    **Validates: Requirements 14.1, 14.5**
    """
    text, needles = case
    merged = spans(text, needles).spans
    for left, right in zip(merged, merged[1:], strict=False):
        assert left.end <= right.start  # no shared character, sorted
    for needle in needles:
        if not needle.text.strip():
            continue
        start = text.find(needle.text)
        while start != -1:
            end = start + len(needle.text)
            containing = [s for s in merged if s.start <= start and end <= s.end]
            assert len(containing) == 1
            assert Ref(needle.ref_id, needle.label) in containing[0].refs
            start = text.find(needle.text, start + 1)

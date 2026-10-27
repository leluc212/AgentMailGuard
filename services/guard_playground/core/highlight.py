"""Evidence_Highlight (design.md "core/highlight.py"; Requirements 14.1-14.3, 14.5, 14.6, 14.8,
14.9, 8.9).

``spans`` finds every exact, case-sensitive occurrence of each needle (a finding excerpt or a
stripped segment) in the raw text and merges spans that share at least one character.
``render`` turns the text into ``Markup`` where every piece of text goes through
``markupsafe.escape``; the only tags it produces are ``mark``, ``span`` and ``a``. Removing the
inserted elements therefore gives back exactly ``escape(text)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from markupsafe import Markup, escape

NOT_LOCATED = "excerpt not located in the shown text"
MARKER = "\u2691"  # ⚑, visible marker so a highlight does not rely on color (Req 14.2)


@dataclass(frozen=True)
class Needle:
    """Text to highlight. ``ref_id`` is the finding anchor (``#f-{ref_id}``), ``label`` e.g.
    ``"L1 · R12"`` (layer and rule_id, or ``"—"`` when the rule has no id)."""

    text: str
    ref_id: str
    label: str


@dataclass(frozen=True)
class Ref:
    ref_id: str
    label: str


@dataclass(frozen=True)
class Span:
    """Half-open range ``[start, end)`` of the raw text, linked to ``refs`` (first-seen order)."""

    start: int
    end: int
    refs: tuple[Ref, ...]


@dataclass(frozen=True)
class NeedleHit:
    """Per-needle result: occurrence count and 1-based number of the span holding the first one."""

    needle: Needle
    count: int
    first_span: int | None

    @property
    def note(self) -> str | None:
        return NOT_LOCATED if self.count == 0 else None


@dataclass(frozen=True)
class SpanResult:
    spans: list[Span]
    hits: list[NeedleHit]  # same order as the input needles


def highlight_id(id_prefix: str, n: int) -> str:
    """Element id of the ``n``-th (1-based) highlight rendered with ``id_prefix``."""
    return f"{id_prefix}-ev-{n}"


def _is_blank(text: str) -> bool:
    return not text or text.isspace()


def occurrences(text: str, needle: str) -> list[int]:
    """Start offsets of every occurrence of ``needle`` in ``text`` (overlapping starts allowed)."""
    if _is_blank(needle):
        return []
    found: list[int] = []
    i = text.find(needle)
    while i != -1:
        found.append(i)
        i = text.find(needle, i + 1)
    return found


def spans(text: str, needles: Sequence[Needle]) -> SpanResult:
    """Locate and merge highlight spans; blank or absent needles get ``NOT_LOCATED``."""
    raw: list[tuple[int, int, int]] = []  # (start, needle index, end)
    starts_per_needle: list[list[int]] = []
    for idx, needle in enumerate(needles):
        starts = occurrences(text, needle.text)
        starts_per_needle.append(starts)
        raw.extend((s, idx, s + len(needle.text)) for s in starts)
    raw.sort()

    merged: list[tuple[int, int, list[Ref]]] = []
    for start, idx, end in raw:
        ref = Ref(needles[idx].ref_id, needles[idx].label)
        if merged and start < merged[-1][1]:  # shares at least one character
            m_start, m_end, refs = merged[-1]
            if ref not in refs:
                refs.append(ref)
            merged[-1] = (m_start, max(m_end, end), refs)
        else:  # disjoint or merely adjacent: a separate highlight
            merged.append((start, end, [ref]))
    result = [Span(s, e, tuple(refs)) for s, e, refs in merged]

    hits: list[NeedleHit] = []
    for needle, starts in zip(needles, starts_per_needle, strict=True):
        first = None
        if starts:
            first = next(n for n, sp in enumerate(result, 1) if sp.start <= starts[0] < sp.end)
        hits.append(NeedleHit(needle, len(starts), first))
    return SpanResult(result, hits)


def render(text: str, spans: Sequence[Span], *, id_prefix: str) -> Markup:
    """Escaped ``text`` with each span wrapped in a ``<mark>`` linking to its findings."""
    parts: list[str] = []
    pos = 0
    for n, span in enumerate(spans, 1):
        parts.append(str(escape(text[pos : span.start])))
        links = "".join(
            f'<a class="ev-ref" href="#f-{escape(ref.ref_id)}">{escape(ref.label)}</a>'
            for ref in span.refs
        )
        parts.append(
            f'<mark class="ev" id="{escape(highlight_id(id_prefix, n))}" tabindex="-1">'
            f'<span aria-hidden="true">{MARKER}</span>'
            f"{escape(text[span.start : span.end])}{links}</mark>"
        )
        pos = span.end
    parts.append(str(escape(text[pos:])))
    return Markup("".join(parts))


@dataclass(frozen=True)
class RefLocation:
    """Back link data for one needle across all shown texts (Req 14.3, 14.6, 14.9)."""

    needle: Needle
    count: int
    first_id: str | None

    @property
    def note(self) -> str | None:
        return NOT_LOCATED if self.count == 0 else None


@dataclass(frozen=True)
class HighlightedTexts:
    body: Markup
    chunks: list[Markup]
    locations: list[RefLocation]  # same order as the input needles


def highlight_email(
    body: str, chunks: Sequence[str], needles: Sequence[Needle], *, id_prefix: str
) -> HighlightedTexts:
    """Highlight the body and the KB chunks with one shared needle set.

    Counts are summed over all texts; ``first_id`` is the first occurrence in reading order
    (body first, then chunks in the given order). Ids are ``{id_prefix}-body-ev-{n}`` and
    ``{id_prefix}-c{i}-ev-{n}``.
    """
    texts = [(f"{id_prefix}-body", body)] + [
        (f"{id_prefix}-c{i}", chunk) for i, chunk in enumerate(chunks)
    ]
    counts = [0] * len(needles)
    first_ids: list[str | None] = [None] * len(needles)
    rendered: list[Markup] = []
    for prefix, text in texts:
        result = spans(text, needles)
        rendered.append(render(text, result.spans, id_prefix=prefix))
        for idx, hit in enumerate(result.hits):
            counts[idx] += hit.count
            if first_ids[idx] is None and hit.first_span is not None:
                first_ids[idx] = highlight_id(prefix, hit.first_span)
    locations = [
        RefLocation(needle, count, first)
        for needle, count, first in zip(needles, counts, first_ids, strict=True)
    ]
    return HighlightedTexts(rendered[0], rendered[1:], locations)

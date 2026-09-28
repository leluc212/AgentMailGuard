"""Pure draft annotation for the review screen (R23.4, R13.5, R16.5; design.md §5.8).

Splits a draft into paragraphs and sentences, places each cited chunk next to the sentence it
supports, and marks the [BUSINESS DATA] references the draft states. No I/O.

A citation is placed, in order of preference:
1. on the sentence carrying an inline ``[CITATION: <id>]`` marker, the marker the prompts show
   the model (prompts/*.j2, packages/llm/citations.py); the marker is removed from the text;
2. otherwise on the sentence sharing the most content terms (4+ letters or digits) with the
   chunk text, if it shares at least MIN_SHARED_TERMS;
3. otherwise it is listed as "not matched to a sentence" under the draft.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from services.frontend.api_client import BusinessFactView, CitedChunk

MIN_SHARED_TERMS = 2

_MARKER = re.compile(r"\s*\[CITATION:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_TRAILING_MARKERS = re.compile(r"([.!?])((?:\s*\[CITATION:[^\]]*\])+)", re.IGNORECASE)
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_TERM = re.compile(r"[^\W_]{4,}")


@dataclass(frozen=True)
class Segment:
    """A run of sentence text; ``fact_reference`` is set when it names a business record."""

    text: str
    fact_reference: str | None = None


@dataclass
class AnnotatedSentence:
    segments: list[Segment]
    citations: list[CitedChunk] = field(default_factory=list)


@dataclass
class AnnotatedDraft:
    paragraphs: list[list[AnnotatedSentence]]
    unplaced: list[CitedChunk]
    numbers: dict[str, int]  # citation_id -> 1-based display number, in API order
    sources: list[CitedChunk] = field(default_factory=list)  # each citation once, API order


def _key(value: str) -> str:
    return value.strip().casefold()


def _terms(text: str) -> set[str]:
    return {term.casefold() for term in _TERM.findall(text)}


def split_paragraphs(body: str) -> list[list[str]]:
    """Paragraphs (blank-line separated) of sentences (split after . ! ?)."""
    paragraphs: list[list[str]] = []
    for block in _PARAGRAPH_BREAK.split(body.strip()):
        sentences = [s for s in _SENTENCE_BREAK.split(block.strip()) if s.strip()]
        if sentences:
            paragraphs.append(sentences)
    return paragraphs


def highlight_facts(text: str, facts: Sequence[BusinessFactView]) -> list[Segment]:
    """Split text so every whole-token mention of a fact reference is its own segment."""
    references = sorted(
        {f.reference.strip() for f in facts if f.reference and f.reference.strip()},
        key=len,
        reverse=True,
    )
    if not text:
        return []
    if not references:
        return [Segment(text)]
    alternatives = "|".join(re.escape(r) for r in references)
    pattern = re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)
    canonical = {_key(r): r for r in references}
    segments: list[Segment] = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            segments.append(Segment(text[cursor : match.start()]))
        segments.append(Segment(match.group(0), fact_reference=canonical[_key(match.group(0))]))
        cursor = match.end()
    if cursor < len(text):
        segments.append(Segment(text[cursor:]))
    return segments


def annotate_draft(
    body: str, citations: Sequence[CitedChunk], facts: Sequence[BusinessFactView]
) -> AnnotatedDraft:
    """Place citations beside sentences and highlight business references (R23.4)."""
    unique: list[CitedChunk] = []
    numbers: dict[str, int] = {}
    for cited in citations:
        if cited.citation_id not in numbers:
            numbers[cited.citation_id] = len(numbers) + 1
            unique.append(cited)

    by_alias: dict[str, CitedChunk] = {}
    for cited in unique:
        for alias in (cited.citation_id, cited.chunk_id):
            if alias:
                by_alias.setdefault(_key(alias), cited)

    prepared = _TRAILING_MARKERS.sub(lambda m: m.group(2) + m.group(1), body)
    cleaned: list[list[str]] = []
    placed: list[list[list[CitedChunk]]] = []
    placed_ids: set[str] = set()
    for paragraph in split_paragraphs(prepared):
        cleaned_paragraph: list[str] = []
        placed_paragraph: list[list[CitedChunk]] = []
        for sentence in paragraph:
            found: list[CitedChunk] = []
            for match in _MARKER.finditer(sentence):
                marked = by_alias.get(_key(match.group(1)))
                if marked is not None and marked.citation_id not in placed_ids:
                    found.append(marked)
                    placed_ids.add(marked.citation_id)
            cleaned_paragraph.append(_MARKER.sub("", sentence).strip())
            placed_paragraph.append(found)
        cleaned.append(cleaned_paragraph)
        placed.append(placed_paragraph)

    unplaced: list[CitedChunk] = []
    for citation in unique:
        if citation.citation_id in placed_ids:
            continue
        chunk_terms = _terms(citation.text or "")
        best: tuple[int, int] | None = None
        best_score = 0
        for p_index, paragraph_text in enumerate(cleaned):
            for s_index, sentence_text in enumerate(paragraph_text):
                score = len(chunk_terms & _terms(sentence_text))
                if score > best_score:
                    best, best_score = (p_index, s_index), score
        if best is not None and best_score >= MIN_SHARED_TERMS:
            placed[best[0]][best[1]].append(citation)
            placed_ids.add(citation.citation_id)
        else:
            unplaced.append(citation)

    paragraphs = [
        [
            AnnotatedSentence(segments=highlight_facts(text, facts), citations=placed[p][s])
            for s, text in enumerate(paragraph_text)
        ]
        for p, paragraph_text in enumerate(cleaned)
    ]
    return AnnotatedDraft(paragraphs=paragraphs, unplaced=unplaced, numbers=numbers, sources=unique)

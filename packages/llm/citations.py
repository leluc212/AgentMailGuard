"""Citation grounding verification for generated drafts (R16.5, design.md §5.7).

The hallucinated-citation detector. Every id a draft cites must name a knowledge chunk that
was actually supplied in the ContextPackage; citations that do not are rejected and the draft
is flagged `citation_mismatch`. A mismatch is a quality signal, not a job failure — an
unparseable or schema-invalid draft is R16.3's concern, handled in `packages/llm/validation.py`.

The accepted identifier space follows what the prompt template actually shows the model:
`prompts/*.j2` renders `[CITATION: {{ chunk.external_id or chunk.chunk_id }}]`, so both
aliases of a supplied chunk are accepted. `document_id` is not: several chunks share one, so
citing a document is not evidence that any particular chunk was supplied.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from packages.domain.entities import Candidate, ContextPackage


@dataclass(frozen=True)
class CitationVerdict:
    """Outcome of checking a draft's citations against the context it was generated from."""

    citations: list[dict[str, Any]] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    supplied_count: int = 0
    cited_count: int = 0

    @property
    def mismatch(self) -> bool:
        """True when at least one citation named no supplied chunk (R16.5)."""
        return bool(self.mismatched)

    @property
    def mismatch_ratio(self) -> float:
        """Share of distinct citations that named no supplied chunk, 0.0 when none were made."""
        if self.cited_count == 0:
            return 0.0
        return len(self.mismatched) / self.cited_count


def _normalize(citation_id: str) -> str:
    """Fold a citation id for comparison, absorbing case drift and stray whitespace."""
    return citation_id.strip().casefold()


def build_citation_index(chunks: Sequence[Candidate]) -> dict[str, Candidate]:
    """Map every citation id the prompt could have shown the model to its chunk.

    The first chunk claiming an alias wins, so a duplicated id resolves deterministically.
    """
    index: dict[str, Candidate] = {}
    for chunk in chunks:
        for alias in (chunk.external_id, chunk.chunk_id):
            if not alias:
                continue
            index.setdefault(_normalize(alias), chunk)
    return index


def verify_citations(
    cited_ids: Sequence[str],
    context: ContextPackage,
) -> CitationVerdict:
    """Partition a draft's citations into ones grounded in the context and ones that are not.

    Args:
        cited_ids: The draft's `knowledge_chunks` field, as validated by DraftReplyPayload.
        context: The ContextPackage the draft was generated from.

    Returns:
        A CitationVerdict whose `citations` hold only accepted citations, resolved to their
        chunk, and whose `mismatched` hold the rejected ids as the model wrote them.
    """
    index = build_citation_index(context.retrieved_chunks)
    accepted: list[dict[str, Any]] = []
    mismatched: list[str] = []
    seen: set[str] = set()

    for raw in cited_ids:
        key = _normalize(raw)
        if not key or key in seen:
            continue
        seen.add(key)

        chunk = index.get(key)
        if chunk is None:
            mismatched.append(raw.strip())
            continue

        accepted.append(
            {
                "citation_id": raw.strip(),
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "external_id": chunk.external_id,
            }
        )

    return CitationVerdict(
        citations=accepted,
        mismatched=mismatched,
        supplied_count=len(context.retrieved_chunks),
        cited_count=len(seen),
    )

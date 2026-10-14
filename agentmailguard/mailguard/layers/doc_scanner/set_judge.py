"""L2 Doc Scanner set judge: one verdict over the retrieved set (ADR-0015 Q2=c).

Deterministic by default: the set verdict aggregates the chunk verdicts (the strictest chunk
sets severity and score). The LLM set judge (Task 10) is behind a flag and only adds
quarantines (decision 4).
"""

from __future__ import annotations

from collections.abc import Sequence

from mailguard.contracts.verdict import ChunkVerdict, DocumentVerdict


def deterministic_set_verdict(chunk_verdicts: Sequence[ChunkVerdict]) -> DocumentVerdict:
    """Aggregate the chunk verdicts; records the set judge and the bait clusters found."""
    verdict = DocumentVerdict.from_chunks(chunk_verdicts)
    clusters = sorted({c.cluster_id for c in chunk_verdicts if c.cluster_id})
    verdict.metadata.update({"set_judge": "deterministic", "clusters": clusters})
    return verdict


__all__ = ["deterministic_set_verdict"]

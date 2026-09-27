"""Reciprocal Rank Fusion (RRF) algorithm for multi-branch hybrid search.

Requirements:
- R10.3: Fuses ranked lists using RRF with configurable k (default 60).
- R10.8: Return for every candidate its lexical rank, vector rank, fused score,
  and source metadata.
- R10.6: Degradation support when only one branch survives.
- R24.3: Pure algorithmic component with zero external I/O.
- specs/design.md §5.5: RRF score(d) = Σ_r 1/(k + rank_r(d)).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from packages.retrieval.models import Candidate

DEFAULT_RRF_K: int = 60


def compute_rrf_score(
    ranks: Sequence[int],
    k: int = DEFAULT_RRF_K,
    weights: Sequence[float] | None = None,
) -> float:
    """Compute the reciprocal rank fusion score for a set of 1-indexed branch ranks.

    Formula: score(d) = Σ_r w_r * (1 / (k + rank_r(d)))

    Args:
        ranks: Sequence of 1-indexed ranks (e.g. [1, 3]).
        k: RRF smoothing constant (default 60). Higher values flatten the rank penalty.
        weights: Optional sequence of weights corresponding to each rank.

    Returns:
        Computed fused score as a positive float.

    Raises:
        ValueError: If k <= 0, any rank < 1, or weights length does not match ranks length.
    """
    if k <= 0:
        raise ValueError(f"k must be greater than 0, got {k}")

    if not ranks:
        return 0.0

    if weights is not None and len(weights) != len(ranks):
        raise ValueError(f"weights length ({len(weights)}) must match ranks length ({len(ranks)})")

    score = 0.0
    for i, rank in enumerate(ranks):
        if rank < 1:
            raise ValueError(f"Rank must be >= 1 (1-indexed), got {rank}")
        w = weights[i] if weights is not None else 1.0
        if w < 0:
            raise ValueError(f"Weight must be non-negative, got {w}")
        score += w * (1.0 / (k + rank))

    return score


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[Candidate]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: Sequence[float] | None = None,
    limit: int | None = None,
) -> list[Candidate]:
    """Fuse multiple ranked lists of Candidate objects using Reciprocal Rank Fusion.

    Fulfills R10.3 and R24.3.

    Args:
        ranked_lists: Sequence of Candidate lists from different search branches.
        k: Smoothing constant, defaults to 60 (R10.3).
        weights: Optional per-list multiplier weights (must match len(ranked_lists)).
        limit: Optional maximum number of fused candidates to return.

    Returns:
        Merged list of Candidate objects sorted strictly by fused_score descending,
        with ties broken deterministically by chunk_id ascending.

    Raises:
        ValueError: If k <= 0, limit <= 0, or weights length does not match ranked_lists length.
    """
    if k <= 0:
        raise ValueError(f"k must be greater than 0, got {k}")

    if limit is not None and limit <= 0:
        raise ValueError(f"limit must be greater than 0 if provided, got {limit}")

    if not ranked_lists:
        return []

    if weights is not None:
        if len(weights) != len(ranked_lists):
            raise ValueError(
                f"weights length ({len(weights)}) must match "
                f"ranked_lists length ({len(ranked_lists)})"
            )
        for w in weights:
            if w < 0:
                raise ValueError(f"Weight must be non-negative, got {w}")

    # Track unique candidates and per-branch ranks: chunk_id -> dict of data
    # Deduplicate within a single list by recording only the first (best) occurrence.
    candidates_by_id: dict[str, Candidate] = {}
    merged_metadata: dict[str, dict[str, Any]] = defaultdict(dict)
    # chunk_id -> {list_idx: rank_1_indexed}
    branch_ranks: dict[str, dict[int, int]] = defaultdict(dict)
    lexical_ranks: dict[str, int] = {}
    vector_ranks: dict[str, int] = {}
    lexical_scores: dict[str, float] = {}
    vector_scores: dict[str, float] = {}

    for list_idx, cand_list in enumerate(ranked_lists):
        seen_in_this_branch: set[str] = set()
        for idx, cand in enumerate(cand_list):
            cid = cand.chunk_id
            if cid in seen_in_this_branch:
                # Discard intra-branch duplicates; earliest/best rank wins
                continue
            seen_in_this_branch.add(cid)

            rank_1_indexed = idx + 1
            branch_ranks[cid][list_idx] = rank_1_indexed

            if cid not in candidates_by_id:
                candidates_by_id[cid] = cand
            merged_metadata[cid].update(cand.metadata)

            if cand.lexical_rank is not None:
                lexical_ranks[cid] = cand.lexical_rank
            if cand.vector_rank is not None:
                vector_ranks[cid] = cand.vector_rank
            if cand.lexical_score is not None:
                lexical_scores[cid] = cand.lexical_score
            if cand.vector_score is not None:
                vector_scores[cid] = cand.vector_score

    # Compute fused score for each unique candidate
    fused_candidates: list[Candidate] = []
    for cid, cand in candidates_by_id.items():
        ranks_dict = branch_ranks[cid]
        fused_score = 0.0

        for list_idx, rank in ranks_dict.items():
            w = weights[list_idx] if weights is not None else 1.0
            fused_score += w * (1.0 / (k + rank))

        fused_cand = replace(
            cand,
            metadata=dict(merged_metadata[cid]),
            lexical_rank=lexical_ranks.get(cid, cand.lexical_rank),
            vector_rank=vector_ranks.get(cid, cand.vector_rank),
            lexical_score=lexical_scores.get(cid, cand.lexical_score),
            vector_score=vector_scores.get(cid, cand.vector_score),
            fused_score=fused_score,
            rerank_score=None,
        )
        fused_candidates.append(fused_cand)

    # Deterministic sorting: descending fused_score, then ascending chunk_id
    fused_candidates.sort(key=lambda c: (-float(c.fused_score or 0.0), c.chunk_id))

    if limit is not None:
        return fused_candidates[:limit]
    return fused_candidates


def fuse_lexical_and_vector(
    lexical_candidates: Sequence[Candidate] | None = None,
    vector_candidates: Sequence[Candidate] | None = None,
    *,
    k: int = DEFAULT_RRF_K,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
    limit: int | None = None,
) -> list[Candidate]:
    """Fuse lexical and vector retrieval branch results using Reciprocal Rank Fusion.

    Fulfills R10.3, R10.8, and single-branch degradation R10.6.

    Preserves lexical_rank, vector_rank, lexical_score, vector_score, and computes
    fused_score according to:
        score(d) = COALESCE(w_lex / (k + lexical_rank), 0) + COALESCE(w_vec / (k + vector_rank), 0)

    Args:
        lexical_candidates: Ranked Candidate sequence from lexical / FTS branch (or None).
        vector_candidates: Ranked Candidate sequence from vector / ANN branch (or None).
        k: Smoothing constant, defaults to 60 (R10.3).
        lexical_weight: Weight applied to lexical branch scores (default 1.0).
        vector_weight: Weight applied to vector branch scores (default 1.0).
        limit: Optional maximum number of fused candidates to return.

    Returns:
        List of Candidate objects with populated lexical_rank, vector_rank,
        fused_score, and merged metadata, sorted by fused_score descending.
    """
    if k <= 0:
        raise ValueError(f"k must be greater than 0, got {k}")

    if limit is not None and limit <= 0:
        raise ValueError(f"limit must be greater than 0 if provided, got {limit}")

    if lexical_weight < 0:
        raise ValueError(f"lexical_weight must be non-negative, got {lexical_weight}")
    if vector_weight < 0:
        raise ValueError(f"vector_weight must be non-negative, got {vector_weight}")

    lex_list = list(lexical_candidates) if lexical_candidates else []
    vec_list = list(vector_candidates) if vector_candidates else []

    if not lex_list and not vec_list:
        return []

    candidates_by_id: dict[str, Candidate] = {}
    merged_metadata: dict[str, dict[str, Any]] = defaultdict(dict)
    lex_ranks: dict[str, int] = {}
    vec_ranks: dict[str, int] = {}
    lex_scores: dict[str, float] = {}
    vec_scores: dict[str, float] = {}

    # Process lexical branch
    seen_lex: set[str] = set()
    for idx, cand in enumerate(lex_list):
        cid = cand.chunk_id
        if cid in seen_lex:
            continue
        seen_lex.add(cid)
        rank = cand.lexical_rank if cand.lexical_rank is not None else (idx + 1)
        lex_ranks[cid] = rank
        if cand.lexical_score is not None:
            lex_scores[cid] = cand.lexical_score
        if cid not in candidates_by_id:
            candidates_by_id[cid] = cand
        merged_metadata[cid].update(cand.metadata)

    # Process vector branch
    seen_vec: set[str] = set()
    for idx, cand in enumerate(vec_list):
        cid = cand.chunk_id
        if cid in seen_vec:
            continue
        seen_vec.add(cid)
        rank = cand.vector_rank if cand.vector_rank is not None else (idx + 1)
        vec_ranks[cid] = rank
        if cand.vector_score is not None:
            vec_scores[cid] = cand.vector_score
        if cid not in candidates_by_id:
            candidates_by_id[cid] = cand
        merged_metadata[cid].update(cand.metadata)

    fused_candidates: list[Candidate] = []
    for cid, cand in candidates_by_id.items():
        l_rank = lex_ranks.get(cid)
        v_rank = vec_ranks.get(cid)

        score = 0.0
        if l_rank is not None:
            score += lexical_weight * (1.0 / (k + l_rank))
        if v_rank is not None:
            score += vector_weight * (1.0 / (k + v_rank))

        fused_cand = replace(
            cand,
            metadata=dict(merged_metadata[cid]),
            lexical_rank=l_rank,
            vector_rank=v_rank,
            lexical_score=lex_scores.get(cid, cand.lexical_score),
            vector_score=vec_scores.get(cid, cand.vector_score),
            fused_score=score,
            rerank_score=None,
        )
        fused_candidates.append(fused_cand)

    # Deterministic sorting: descending fused_score, ascending chunk_id
    fused_candidates.sort(key=lambda c: (-float(c.fused_score or 0.0), c.chunk_id))

    if limit is not None:
        return fused_candidates[:limit]
    return fused_candidates

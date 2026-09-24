"""Unit tests for Reciprocal Rank Fusion (RRF) algorithm.

Requirements:
- R10.3: Fuses ranked lists using RRF with configurable k (default 60).
- R10.8: Return for every candidate its lexical rank, vector rank, fused score,
  and source metadata.
- R10.6: Degradation support when only one branch survives.
- R24.3: Pure algorithmic component unit tests.
- specs/design.md §5.5: RRF score(d) = Σ_r 1/(k + rank_r(d)).
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.retrieval.models import Candidate
from packages.retrieval.rrf import (
    DEFAULT_RRF_K,
    compute_rrf_score,
    fuse_lexical_and_vector,
    reciprocal_rank_fusion,
)


def _make_candidate(
    chunk_id: str,
    doc_id: str = "doc-1",
    content: str = "test chunk content",
    metadata: dict[str, Any] | None = None,
    lexical_rank: int | None = None,
    vector_rank: int | None = None,
    lexical_score: float | None = None,
    vector_score: float | None = None,
) -> Candidate:
    """Helper to instantiate test candidates with default values."""
    return Candidate(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        metadata=metadata or {},
        lexical_rank=lexical_rank,
        vector_rank=vector_rank,
        lexical_score=lexical_score,
        vector_score=vector_score,
    )


class TestComputeRRFScore:
    """Tests for the pure mathematical score calculation helper."""

    def test_default_rrf_k_constant(self) -> None:
        """Verify DEFAULT_RRF_K is 60 per R10.3 and design.md §5.5."""
        assert DEFAULT_RRF_K == 60

    def test_single_rank(self) -> None:
        """Verify score for a single branch rank with default k=60."""
        score = compute_rrf_score([1], k=60)
        expected = 1.0 / (60 + 1)
        assert pytest.approx(expected, rel=1e-9) == score

    def test_multiple_ranks(self) -> None:
        """Verify score for multiple branch ranks."""
        score = compute_rrf_score([1, 3], k=60)
        expected = (1.0 / 61) + (1.0 / 63)
        assert pytest.approx(expected, rel=1e-9) == score

    def test_weighted_ranks(self) -> None:
        """Verify weights apply as linear multipliers per rank."""
        score = compute_rrf_score([1, 2], k=60, weights=[1.0, 0.5])
        expected = (1.0 / 61) + (0.5 / 62)
        assert pytest.approx(expected, rel=1e-9) == score

    def test_empty_ranks_returns_zero(self) -> None:
        """Verify empty sequence produces 0.0 score."""
        assert compute_rrf_score([], k=60) == 0.0

    def test_validation_errors(self) -> None:
        """Verify parameter validation raises ValueError on illegal inputs."""
        with pytest.raises(ValueError, match="k must be greater than 0"):
            compute_rrf_score([1], k=0)

        with pytest.raises(ValueError, match="k must be greater than 0"):
            compute_rrf_score([1], k=-10)

        with pytest.raises(ValueError, match="Rank must be >= 1"):
            compute_rrf_score([0], k=60)

        with pytest.raises(ValueError, match="weights length"):
            compute_rrf_score([1, 2], k=60, weights=[1.0])

        with pytest.raises(ValueError, match="Weight must be non-negative"):
            compute_rrf_score([1], k=60, weights=[-0.5])


class TestReciprocalRankFusion:
    """Tests for generic multi-list Reciprocal Rank Fusion."""

    def test_empty_inputs(self) -> None:
        """Verify empty inputs return empty list."""
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[], []]) == []

    def test_single_list_preserves_order(self) -> None:
        """Verify single branch list keeps original order and assigns correct scores."""
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")
        c3 = _make_candidate("c3")

        fused = reciprocal_rank_fusion([[c1, c2, c3]], k=60)

        assert len(fused) == 3
        assert [c.chunk_id for c in fused] == ["c1", "c2", "c3"]
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[0].fused_score
        assert pytest.approx(1.0 / 62, rel=1e-9) == fused[1].fused_score
        assert pytest.approx(1.0 / 63, rel=1e-9) == fused[2].fused_score

    def test_disjoint_lists(self) -> None:
        """Verify disjoint branch lists are merged, scored, and tie-broken."""
        lexical = [_make_candidate("c1"), _make_candidate("c2")]
        vector = [_make_candidate("c3"), _make_candidate("c4")]

        fused = reciprocal_rank_fusion([lexical, vector], k=60)

        assert len(fused) == 4
        # c1 (rank 1, score 1/61) and c3 (rank 1, score 1/61) tie;
        # tie broken by chunk_id ascending: c1 before c3.
        # c2 (rank 2, score 1/62) and c4 (rank 2, score 1/62) tie;
        # c2 before c4.
        assert [c.chunk_id for c in fused] == ["c1", "c3", "c2", "c4"]
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[0].fused_score
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[1].fused_score
        assert pytest.approx(1.0 / 62, rel=1e-9) == fused[2].fused_score
        assert pytest.approx(1.0 / 62, rel=1e-9) == fused[3].fused_score

    def test_identical_lists(self) -> None:
        """Verify identical rankings across branches double scores and preserve ordering."""
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")
        c3 = _make_candidate("c3")

        fused = reciprocal_rank_fusion([[c1, c2, c3], [c1, c2, c3]], k=60)

        assert len(fused) == 3
        assert [c.chunk_id for c in fused] == ["c1", "c2", "c3"]
        assert pytest.approx(2.0 / 61, rel=1e-9) == fused[0].fused_score
        assert pytest.approx(2.0 / 62, rel=1e-9) == fused[1].fused_score
        assert pytest.approx(2.0 / 63, rel=1e-9) == fused[2].fused_score

    def test_overlapping_candidates_boost(self) -> None:
        """Verify candidates appearing in both branches outrank single-branch candidates."""
        # Lexical has c1 (#1) and c2 (#2)
        # Vector has c2 (#1) and c3 (#2)
        lexical = [_make_candidate("c1"), _make_candidate("c2")]
        vector = [_make_candidate("c2"), _make_candidate("c3")]

        fused = reciprocal_rank_fusion([lexical, vector], k=60)

        assert len(fused) == 3
        # c2 score: 1/62 + 1/61 = 0.016129 + 0.016393 = 0.032522
        # c1 score: 1/61 = 0.016393
        # c3 score: 1/62 = 0.016129
        assert [c.chunk_id for c in fused] == ["c2", "c1", "c3"]
        s0 = fused[0].fused_score
        s1 = fused[1].fused_score
        s2 = fused[2].fused_score
        assert s0 is not None and s1 is not None and s2 is not None
        assert s0 > s1 > s2

    def test_deterministic_tie_breaking(self) -> None:
        """Verify tie-breaking is strictly deterministic regardless of branch ordering."""
        cand_b = _make_candidate("chunk_b")
        cand_a = _make_candidate("chunk_a")

        # cand_b in list 0 (rank 1), cand_a in list 1 (rank 1)
        fused = reciprocal_rank_fusion([[cand_b], [cand_a]], k=60)

        # Both have score 1/61; chunk_a must precede chunk_b alphabetically
        assert [c.chunk_id for c in fused] == ["chunk_a", "chunk_b"]

    def test_intra_branch_duplicate_handling(self) -> None:
        """Verify multiple occurrences of the same chunk in one list use only the earliest rank."""
        c1_first = _make_candidate("c1")
        c2 = _make_candidate("c2")
        c1_dup = _make_candidate("c1")

        fused = reciprocal_rank_fusion([[c1_first, c2, c1_dup]], k=60)

        assert len(fused) == 2
        assert [c.chunk_id for c in fused] == ["c1", "c2"]
        # c1 must retain rank 1 (1/61), not get double-scored
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[0].fused_score
        assert pytest.approx(1.0 / 62, rel=1e-9) == fused[1].fused_score

    def test_custom_k_parameter(self) -> None:
        """Verify configurable k alters scores as expected."""
        c1 = _make_candidate("c1")

        fused_k1 = reciprocal_rank_fusion([[c1]], k=1)
        assert pytest.approx(1.0 / (1 + 1), rel=1e-9) == fused_k1[0].fused_score

        fused_k100 = reciprocal_rank_fusion([[c1]], k=100)
        assert pytest.approx(1.0 / (100 + 1), rel=1e-9) == fused_k100[0].fused_score

    def test_weights_application(self) -> None:
        """Verify custom weights scale branch contributions."""
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")

        # List 0 has c1 (weight 2.0); List 1 has c2 (weight 1.0)
        fused = reciprocal_rank_fusion([[c1], [c2]], k=60, weights=[2.0, 1.0])

        assert [c.chunk_id for c in fused] == ["c1", "c2"]
        assert pytest.approx(2.0 / 61, rel=1e-9) == fused[0].fused_score
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[1].fused_score

    def test_limit_truncation(self) -> None:
        """Verify limit parameter properly caps output size."""
        candidates = [_make_candidate(f"c{i}") for i in range(10)]
        fused = reciprocal_rank_fusion([candidates], k=60, limit=3)
        assert len(fused) == 3
        assert [c.chunk_id for c in fused] == ["c0", "c1", "c2"]

    def test_invalid_parameters_raise_error(self) -> None:
        """Verify invalid k, limit, or weights raise ValueError."""
        c1 = _make_candidate("c1")

        with pytest.raises(ValueError, match="k must be greater than 0"):
            reciprocal_rank_fusion([[c1]], k=0)

        with pytest.raises(ValueError, match="limit must be greater than 0"):
            reciprocal_rank_fusion([[c1]], limit=0)

        with pytest.raises(ValueError, match="weights length"):
            reciprocal_rank_fusion([[c1]], weights=[1.0, 2.0])

        with pytest.raises(ValueError, match="Weight must be non-negative"):
            reciprocal_rank_fusion([[c1]], weights=[-1.0])


class TestFuseLexicalAndVector:
    """Tests for the specialized lexical + vector fusion function (R10.3, R10.8, R10.6)."""

    def test_both_branches_preserves_ranks_and_scores(self) -> None:
        """Verify R10.8: lexical_rank, vector_rank, lexical_score, vector_score, and metadata."""
        c1_lex = _make_candidate(
            "c1",
            content="invoice payment details",
            metadata={"domain": "billing", "tag": "lex"},
            lexical_rank=1,
            lexical_score=5.5,
        )
        c2_lex = _make_candidate(
            "c2",
            metadata={"tag": "lex2"},
            lexical_rank=2,
            lexical_score=3.2,
        )

        c1_vec = _make_candidate(
            "c1",
            content="invoice payment details",
            metadata={"source": "kb_v2"},
            vector_rank=2,
            vector_score=0.89,
        )
        c3_vec = _make_candidate(
            "c3",
            metadata={"source": "kb_v3"},
            vector_rank=1,
            vector_score=0.95,
        )

        fused = fuse_lexical_and_vector(
            lexical_candidates=[c1_lex, c2_lex],
            vector_candidates=[c3_vec, c1_vec],
            k=60,
        )

        assert len(fused) == 3
        c1_result = next(c for c in fused if c.chunk_id == "c1")
        assert c1_result.lexical_rank == 1
        assert c1_result.vector_rank == 2
        assert c1_result.lexical_score == 5.5
        assert c1_result.vector_score == 0.89
        expected_c1_score = (1.0 / 61) + (1.0 / 62)
        assert pytest.approx(expected_c1_score, rel=1e-9) == c1_result.fused_score

        # Verify metadata was merged
        assert c1_result.metadata == {
            "domain": "billing",
            "tag": "lex",
            "source": "kb_v2",
        }

    def test_single_branch_survival_lexical_only(self) -> None:
        """Verify degradation (R10.6): vector branch fails/empty, lexical branch survives."""
        c1 = _make_candidate("c1", lexical_rank=1, lexical_score=4.0)
        c2 = _make_candidate("c2", lexical_rank=2, lexical_score=2.0)

        fused = fuse_lexical_and_vector(
            lexical_candidates=[c1, c2],
            vector_candidates=None,
            k=60,
        )

        assert len(fused) == 2
        assert [c.chunk_id for c in fused] == ["c1", "c2"]
        assert fused[0].lexical_rank == 1
        assert fused[0].vector_rank is None
        assert fused[0].lexical_score == 4.0
        assert fused[0].vector_score is None
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[0].fused_score

    def test_single_branch_survival_vector_only(self) -> None:
        """Verify degradation (R10.6): lexical branch fails/empty, vector branch survives."""
        c1 = _make_candidate("c1", vector_rank=1, vector_score=0.92)

        fused = fuse_lexical_and_vector(
            lexical_candidates=[],
            vector_candidates=[c1],
            k=60,
        )

        assert len(fused) == 1
        assert fused[0].chunk_id == "c1"
        assert fused[0].lexical_rank is None
        assert fused[0].vector_rank == 1
        assert fused[0].vector_score == 0.92
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[0].fused_score

    def test_both_branches_empty_returns_empty(self) -> None:
        """Verify empty branches produce empty result."""
        assert fuse_lexical_and_vector(None, None) == []
        assert fuse_lexical_and_vector([], []) == []

    def test_parity_with_sql_cte_formula(self) -> None:
        """Verify Python RRF calculation matches exact PostgreSQL CTE formula.

        CTE Formula (specs/design.md §5.5):
        COALESCE(1.0/(:k + l.rnk), 0) + COALESCE(1.0/(:k + v.rnk), 0) AS fused_score
        """
        k_val = 60
        test_matrix = [
            (1, 1),
            (1, 5),
            (3, None),
            (None, 2),
            (20, 20),
        ]

        for lex_rank, vec_rank in test_matrix:
            lex_c = [_make_candidate("cand", lexical_rank=lex_rank)] if lex_rank else []
            vec_c = [_make_candidate("cand", vector_rank=vec_rank)] if vec_rank else []

            fused = fuse_lexical_and_vector(lex_c, vec_c, k=k_val)
            assert len(fused) == 1

            sql_score = 0.0
            if lex_rank is not None:
                sql_score += 1.0 / (k_val + lex_rank)
            if vec_rank is not None:
                sql_score += 1.0 / (k_val + vec_rank)

            assert pytest.approx(sql_score, rel=1e-12) == fused[0].fused_score

    def test_branch_weights_lexical_and_vector(self) -> None:
        """Verify branch weights adjust scoring balance."""
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")

        # c1 in lexical only (rank 1), c2 in vector only (rank 1)
        # Vector weight 2.0, lexical weight 1.0 -> c2 must outrank c1
        fused = fuse_lexical_and_vector(
            lexical_candidates=[c1],
            vector_candidates=[c2],
            k=60,
            lexical_weight=1.0,
            vector_weight=2.0,
        )

        assert [c.chunk_id for c in fused] == ["c2", "c1"]
        assert pytest.approx(2.0 / 61, rel=1e-9) == fused[0].fused_score
        assert pytest.approx(1.0 / 61, rel=1e-9) == fused[1].fused_score

    def test_validation_errors(self) -> None:
        """Verify parameter validation raises ValueError on illegal parameters."""
        c1 = _make_candidate("c1")

        with pytest.raises(ValueError, match="k must be greater than 0"):
            fuse_lexical_and_vector([c1], k=0)

        with pytest.raises(ValueError, match="limit must be greater than 0"):
            fuse_lexical_and_vector([c1], limit=-1)

        with pytest.raises(ValueError, match="lexical_weight must be non-negative"):
            fuse_lexical_and_vector([c1], lexical_weight=-0.1)

        with pytest.raises(ValueError, match="vector_weight must be non-negative"):
            fuse_lexical_and_vector([c1], vector_weight=-0.5)

    def test_intra_branch_duplicates_and_limit(self) -> None:
        """Verify intra-branch duplicates are skipped and limit caps output."""
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")
        c3 = _make_candidate("c3")

        # Duplicate c1 in lexical, duplicate c2 in vector
        fused = fuse_lexical_and_vector(
            lexical_candidates=[c1, c1, c3],
            vector_candidates=[c2, c2, c3],
            k=60,
            limit=2,
        )

        assert len(fused) == 2


class TestGenericRRFPreservation:
    """Tests for preserving pre-populated ranks and scores in generic RRF."""

    def test_preexisting_candidate_ranks_and_scores_preserved(self) -> None:
        """Verify reciprocal_rank_fusion preserves preexisting candidate rank/score fields."""
        c1 = _make_candidate(
            "c1",
            lexical_rank=1,
            vector_rank=2,
            lexical_score=4.5,
            vector_score=0.88,
        )
        fused = reciprocal_rank_fusion([[c1]], k=60)
        assert len(fused) == 1
        assert fused[0].lexical_rank == 1
        assert fused[0].vector_rank == 2
        assert fused[0].lexical_score == 4.5
        assert fused[0].vector_score == 0.88


"""Unit tests for context packing, Top-K selection, and hard token budget enforcement.

Requirements:
- R11.3: Pass configurable top-K to generation, defaulting to 4–6 chunks (default 5).
- R11.4: Enforce a maximum retrieved-context token budget and truncate at chunk boundaries.
- R11.7: Record final context token count.
- specs/design.md §5.4 & §5.5: Top 4–6 chunks with citation IDs.
"""

from __future__ import annotations

from typing import Any

from packages.retrieval.models import Candidate
from packages.retrieval.packing import (
    ContextPacker,
    PackedContext,
    PackingConfig,
    TokenCounterProtocol,
)


class MockTokenCounter:
    """Deterministic token counter double for tests."""

    def __init__(self, token_map: dict[str, int] | None = None) -> None:
        self.token_map = token_map or {}

    def count_tokens(self, text: str) -> int:
        if text in self.token_map:
            return self.token_map[text]
        # Default simple count: 1 token per word, minimum 1 if not empty
        words = text.split()
        return len(words) if words else 0


def _make_candidate(
    chunk_id: str,
    doc_id: str = "doc-1",
    content: str = "knowledge chunk text content",
    metadata: dict[str, Any] | None = None,
    fused_score: float | None = 0.05,
    rerank_score: float | None = 0.9,
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        metadata=metadata or {},
        fused_score=fused_score,
        rerank_score=rerank_score,
    )


class TestContextPackingDefaults:
    """Test default Top-K and token budget settings (R11.3, R11.4)."""

    def test_default_top_k_is_between_4_and_6(self) -> None:
        """Verify default top-K conforms to R11.3 (defaults to 4-6 chunks)."""
        config = PackingConfig()
        assert 4 <= config.default_top_k <= 6
        assert config.default_top_k == 5

    def test_default_packing_selects_5_chunks_in_order(self) -> None:
        """Verify default packing selects 5 chunks preserving input rank order."""
        candidates = [
            _make_candidate(f"c{i}", content=f"Chunk content number {i}") for i in range(10)
        ]
        counter = MockTokenCounter()
        packer = ContextPacker(token_counter=counter)

        result = packer.pack(candidates)

        assert isinstance(result, PackedContext)
        assert len(result.chunks) == 5
        assert result.candidate_count_initial == 10
        assert result.candidate_count_selected == 5
        assert result.top_k == 5
        assert result.truncated_by_budget is False

        # Preserves rank order
        assert [c.chunk_id for c in result.chunks] == ["c0", "c1", "c2", "c3", "c4"]

        # Default sequential citation IDs [1] through [5]
        assert result.citation_ids == ["[1]", "[2]", "[3]", "[4]", "[5]"]
        for idx, chunk in enumerate(result.chunks, start=1):
            assert chunk.citation_id == f"[{idx}]"
            assert chunk.fused_score == 0.05
            assert chunk.rerank_score == 0.9


class TestContextPackingCustomTopK:
    """Test configurable Top-K selection (R11.3)."""

    def test_explicit_top_k_override(self) -> None:
        candidates = [_make_candidate(f"c{i}") for i in range(8)]
        packer = ContextPacker(token_counter=MockTokenCounter())

        # Top 3
        res3 = packer.pack(candidates, top_k=3)
        assert len(res3.chunks) == 3
        assert res3.top_k == 3
        assert res3.chunk_ids == ["c0", "c1", "c2"]

        # Top 7
        res7 = packer.pack(candidates, top_k=7)
        assert len(res7.chunks) == 7
        assert res7.top_k == 7

    def test_top_k_bounds_clamping(self) -> None:
        """Verify top-K is clamped between min_top_k and max_top_k."""
        config = PackingConfig(min_top_k=2, max_top_k=6)
        packer = ContextPacker(token_counter=MockTokenCounter(), config=config)
        candidates = [_make_candidate(f"c{i}") for i in range(10)]

        # Clamps below min
        res_low = packer.pack(candidates, top_k=1)
        assert len(res_low.chunks) == 2

        # Clamps above max
        res_high = packer.pack(candidates, top_k=10)
        assert len(res_high.chunks) == 6


class TestHardTokenBudgetTruncation:
    """Test hard token budget ceiling and chunk boundary truncation (R11.4)."""

    def test_truncation_at_chunk_boundary(self) -> None:
        """Verify packing stops at chunk boundary when budget is breached.

        Must NEVER truncate mid-chunk or include partial sentences.
        """
        token_map = {
            "First content chunk": 200,
            "Second content chunk": 300,
            "Third content chunk": 250,
            "Fourth content chunk": 100,
        }
        candidates = [
            _make_candidate("c1", content="First content chunk"),
            _make_candidate("c2", content="Second content chunk"),
            _make_candidate("c3", content="Third content chunk"),
            _make_candidate("c4", content="Fourth content chunk"),
        ]
        counter = MockTokenCounter(token_map=token_map)
        packer = ContextPacker(token_counter=counter)

        # Budget of 600: c1 (200) + c2 (300) = 500 <= 600.
        # c3 (250) would reach 750 > 600 -> excluded at chunk boundary.
        result = packer.pack(candidates, token_budget=600)

        assert len(result.chunks) == 2
        assert result.chunk_ids == ["c1", "c2"]
        assert result.total_tokens == 500
        assert result.token_budget == 600
        assert result.truncated_by_budget is True

        # Verify chunk 2 content is fully intact, and chunk 3 is completely absent
        assert result.chunks[0].content == "First content chunk"
        assert result.chunks[1].content == "Second content chunk"
        assert "Third content chunk" not in result.format_knowledge_section()

    def test_first_chunk_exceeds_budget_returns_zero_chunks(self) -> None:
        """Verify that when the very first chunk breaches budget, zero chunks are returned."""
        token_map = {"Massive single chunk": 1500}
        candidates = [_make_candidate("c1", content="Massive single chunk")]
        counter = MockTokenCounter(token_map=token_map)
        packer = ContextPacker(token_counter=counter)

        result = packer.pack(candidates, token_budget=1000)

        assert len(result.chunks) == 0
        assert result.total_tokens == 0
        assert result.truncated_by_budget is True
        assert result.format_knowledge_section() == ""

    def test_exact_budget_match_fits_all(self) -> None:
        """Verify exact token match includes all chunks without marking truncation."""
        token_map = {"Chunk A": 100, "Chunk B": 200}
        candidates = [
            _make_candidate("c1", content="Chunk A"),
            _make_candidate("c2", content="Chunk B"),
        ]
        counter = MockTokenCounter(token_map=token_map)
        packer = ContextPacker(token_counter=counter)

        result = packer.pack(candidates, token_budget=300)

        assert len(result.chunks) == 2
        assert result.total_tokens == 300
        assert result.truncated_by_budget is False


class TestEdgeCasesAndFormatting:
    """Test boundary conditions, empty inputs, cached tokens, and formatting."""

    def test_empty_candidates_list(self) -> None:
        packer = ContextPacker(token_counter=MockTokenCounter())
        result = packer.pack([])

        assert result.chunks == []
        assert result.total_tokens == 0
        assert result.truncated_by_budget is False
        assert result.candidate_count_initial == 0
        assert result.candidate_count_selected == 0
        assert result.format_knowledge_section() == ""

    def test_negative_or_zero_budget(self) -> None:
        candidates = [_make_candidate("c1", content="Some text")]
        packer = ContextPacker(token_counter=MockTokenCounter())

        res_zero = packer.pack(candidates, token_budget=0)
        assert res_zero.chunks == []
        assert res_zero.truncated_by_budget is True

        res_neg = packer.pack(candidates, token_budget=-10)
        assert res_neg.chunks == []
        assert res_neg.token_budget == 0
        assert res_neg.truncated_by_budget is True

    def test_metadata_cached_token_count_used(self) -> None:
        """Verify precomputed token_count in candidate metadata avoids re-counting."""
        candidate = _make_candidate(
            "c1",
            content="Expensive text that would not need tokenizing",
            metadata={"token_count": 42},
        )
        # Mock counter that would return 999 if called
        counter = MockTokenCounter({"Expensive text that would not need tokenizing": 999})
        packer = ContextPacker(token_counter=counter)

        result = packer.pack([candidate])
        assert len(result.chunks) == 1
        assert result.chunks[0].token_count == 42
        assert result.total_tokens == 42

    def test_count_formatting_tokens_flag(self) -> None:
        """Verify counting formatting tokens when count_formatting_tokens=True."""
        config = PackingConfig(count_formatting_tokens=True)
        counter = MockTokenCounter()
        packer = ContextPacker(token_counter=counter, config=config)
        candidate = _make_candidate("c1", doc_id="kb-doc-12", content="policy refund instructions")

        result = packer.pack([candidate])
        assert len(result.chunks) == 1
        # Formatted text: "[1] (Document: kb-doc-12)\npolicy refund instructions" -> 6 words
        assert result.chunks[0].token_count == 6

    def test_custom_citation_prefix(self) -> None:
        candidates = [_make_candidate("c1"), _make_candidate("c2")]
        packer = ContextPacker(token_counter=MockTokenCounter())

        result = packer.pack(candidates, citation_prefix="ref-")
        assert result.citation_ids == ["[ref-1]", "[ref-2]"]

    def test_format_knowledge_section_output(self) -> None:
        candidates = [
            _make_candidate("c1", doc_id="doc-A", content="Refunds take 5 business days."),
            _make_candidate("c2", doc_id="doc-B", content="Contact support@example.com for help."),
        ]
        packer = ContextPacker(token_counter=MockTokenCounter())
        result = packer.pack(candidates)

        text = result.format_knowledge_section()
        expected = (
            "[1] (Document: doc-A)\nRefunds take 5 business days.\n\n"
            "[2] (Document: doc-B)\nContact support@example.com for help."
        )
        assert text == expected


class TestPolicyOverrides:
    """Test organization and category policy overrides."""

    def test_org_policy_overrides(self) -> None:
        config = PackingConfig(
            default_top_k=5,
            default_token_budget=2048,
            org_top_k={"org-vip": 8},
            org_token_budget={"org-vip": 4096},
        )
        packer = ContextPacker(token_counter=MockTokenCounter(), config=config)
        candidates = [_make_candidate(f"c{i}") for i in range(12)]

        # Regular org gets default
        res_default = packer.pack(candidates, organization_id="org-standard")
        assert res_default.top_k == 5
        assert res_default.token_budget == 2048

        # VIP org gets overridden config
        res_vip = packer.pack(candidates, organization_id="org-vip")
        assert res_vip.top_k == 8
        assert res_vip.token_budget == 4096
        assert len(res_vip.chunks) == 8

    def test_category_policy_overrides(self) -> None:
        config = PackingConfig(
            default_top_k=5,
            default_token_budget=2048,
            category_top_k={"legal": 3},
            category_token_budget={"legal": 1024},
        )
        packer = ContextPacker(token_counter=MockTokenCounter(), config=config)
        candidates = [_make_candidate(f"c{i}") for i in range(10)]

        res_legal = packer.pack(candidates, category="legal")
        assert res_legal.top_k == 3
        assert res_legal.token_budget == 1024
        assert len(res_legal.chunks) == 3

    def test_explicit_argument_takes_precedence_over_policy(self) -> None:
        config = PackingConfig(
            default_top_k=5,
            default_token_budget=2048,
            org_top_k={"org-vip": 8},
            org_token_budget={"org-vip": 4096},
        )
        packer = ContextPacker(token_counter=MockTokenCounter(), config=config)
        candidates = [_make_candidate(f"c{i}") for i in range(10)]

        res = packer.pack(candidates, top_k=2, token_budget=500, organization_id="org-vip")
        assert res.top_k == 2
        assert res.token_budget == 500
        assert len(res.chunks) == 2


class TestDefaultTokenCounterIntegration:
    """Test default TokenCounter integration with tiktoken / heuristic fallback."""

    def test_instantiation_without_explicit_counter(self) -> None:
        """Verify ContextPacker instantiates TokenCounter from packages.knowledge."""
        packer = ContextPacker()
        assert isinstance(packer.token_counter, TokenCounterProtocol)

        candidate = _make_candidate("c1", content="Hello world from the default token counter.")
        result = packer.pack([candidate])
        assert len(result.chunks) == 1
        assert result.chunks[0].token_count > 0
        assert result.total_tokens == result.chunks[0].token_count

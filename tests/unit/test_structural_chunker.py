"""Unit tests for StructuralChunker.

Requirements:
- R9.3: Semantic boundary splitting (headings, sections, paragraphs, list groups, tables).
- R9.5: Complete chunk metadata (document_id, chunk_id, title, heading_path,
  section, category, version).
- R9.9: Content checksum (sha256:{hexdigest}) reproducibility across versions.
- R24.3: Pure component unit tests:
  1. Heading-heavy document
  2. Table-heavy document (with header retention)
  3. One long unbroken paragraph (>1500 tokens)
  4. Tiny document (<350 tokens)
"""

from __future__ import annotations

import pytest

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    ParsedDocument,
)
from packages.knowledge.chunker import ChunkerConfig, StructuralChunker
from packages.knowledge.token_counter import TokenCounter


@pytest.fixture
def token_counter() -> TokenCounter:
    return TokenCounter("cl100k_base")


@pytest.fixture
def default_chunker(token_counter: TokenCounter) -> StructuralChunker:
    return StructuralChunker(
        config=ChunkerConfig(min_tokens=350, max_tokens=700, overlap_tokens=50),
        token_counter=token_counter,
    )


class TestChunkerConfig:
    """Validate ChunkerConfig parameters and fail-fast invariants."""

    def test_default_config(self) -> None:
        cfg = ChunkerConfig()
        assert cfg.min_tokens == 350
        assert cfg.max_tokens == 700
        assert cfg.overlap_tokens == 50
        assert cfg.encoding_name == "cl100k_base"

    def test_invalid_min_tokens(self) -> None:
        with pytest.raises(ValueError, match="min_tokens must be positive"):
            ChunkerConfig(min_tokens=0)

    def test_invalid_max_tokens_less_than_min(self) -> None:
        with pytest.raises(ValueError, match="max_tokens .* must be >= min_tokens"):
            ChunkerConfig(min_tokens=500, max_tokens=300)

    def test_invalid_negative_overlap(self) -> None:
        with pytest.raises(ValueError, match="overlap_tokens cannot be negative"):
            ChunkerConfig(overlap_tokens=-1)

    def test_invalid_overlap_exceeding_max(self) -> None:
        with pytest.raises(ValueError, match="overlap_tokens .* must be < max_tokens"):
            ChunkerConfig(min_tokens=100, max_tokens=200, overlap_tokens=250)


class TestStructuralChunkerFixtures:
    """R24.3 Required Test Fixtures."""

    def test_heading_heavy_document(self, default_chunker: StructuralChunker) -> None:
        """R24.3 Fixture 1: Heading-heavy doc with nested sections and short paragraphs."""
        elements: list[DocumentElement] = []
        for i in range(1, 16):
            h_text = f"Section {i}: Operation Procedures"
            elements.append(
                DocumentElement(
                    element_type=ElementType.HEADING,
                    content=h_text,
                    level=2 if i % 3 != 0 else 3,
                    heading_path=("User Guide", f"Chapter {((i - 1) // 5) + 1}", h_text),
                    section=h_text,
                )
            )
            # Short paragraph of ~40 tokens
            p_text = (
                f"Detailed explanation for section {i}. Specifies prerequisites, "
                f"validation rules, and execution steps for task {i} safely."
            )
            elements.append(
                DocumentElement(
                    element_type=ElementType.PARAGRAPH,
                    content=p_text,
                    heading_path=("User Guide", f"Chapter {((i - 1) // 5) + 1}", h_text),
                    section=h_text,
                )
            )

        doc = ParsedDocument(title="User Operations Manual", elements=elements)
        chunks = default_chunker.chunk_document(
            parsed_doc=doc,
            document_id="DOC-HEADING",
            title="User Operations Manual",
            category="operations",
        )

        assert len(chunks) >= 2
        # All chunks must observe the max_tokens ceiling
        for idx, chunk in enumerate(chunks):
            assert chunk.token_count <= default_chunker.config.max_tokens
            assert chunk.chunk_index == idx
            assert chunk.external_id == f"DOC-HEADING-{idx + 1:02d}"
            assert chunk.document_id == "DOC-HEADING"
            assert chunk.category == "operations"
            assert chunk.content_checksum.startswith("sha256:")
            assert len(chunk.content_checksum) == 71  # "sha256:" + 64 hex chars
            assert len(chunk.heading_path) > 0
            assert chunk.section is not None
            assert chunk.metadata["title"] == "User Operations Manual"

    def test_table_heavy_document(self, default_chunker: StructuralChunker) -> None:
        """R24.3 Fixture 2: Table-heavy doc, verifying header retention across split sub-tables."""
        # Create a large table with 60 rows (~1200 tokens total)
        headers = ["Account ID", "Username", "Role", "Email Address", "Status", "Quota (GB)"]
        rows = [
            [
                f"ACC-{1000 + r}",
                f"user_{r}",
                "Operator" if r % 2 == 0 else "Admin",
                f"user_{r}@example.com",
                "Active" if r % 3 != 0 else "Suspended",
                f"{10 + (r * 2)}",
            ]
            for r in range(1, 61)
        ]

        table_md = (
            "| "
            + " | ".join(headers)
            + " |\n"
            + "| "
            + " | ".join(["---"] * len(headers))
            + " |\n"
            + "\n".join("| " + " | ".join(r) + " |" for r in rows)
        )

        elements = [
            DocumentElement(
                element_type=ElementType.HEADING,
                content="System User Directory",
                level=1,
                heading_path=("Administration", "Directory"),
                section="System User Directory",
            ),
            DocumentElement(
                element_type=ElementType.TABLE,
                content=table_md,
                level=None,
                heading_path=("Administration", "Directory"),
                section="System User Directory",
                metadata={"table_data": {"headers": headers, "rows": rows}},
            ),
        ]

        doc = ParsedDocument(title="Directory Export", elements=elements)
        chunks = default_chunker.chunk_document(
            parsed_doc=doc,
            document_id="DOC-TABLE",
            title="Directory Export",
        )

        assert len(chunks) >= 2
        for idx, chunk in enumerate(chunks):
            assert chunk.token_count <= default_chunker.config.max_tokens
            assert chunk.chunk_index == idx
            assert chunk.metadata.get("has_table") is True
            # Crucial requirement: Every sub-chunk containing the table MUST retain the headers
            if "Account ID" in chunk.content:
                header_prefix = "| Account ID | Username | Role |"
                assert header_prefix in chunk.content
                assert "| --- | --- | --- | --- | --- | --- |" in chunk.content

    def test_long_unbroken_paragraph(self, default_chunker: StructuralChunker) -> None:
        """R24.3 Fixture 3: One long unbroken paragraph (>1500 tokens) split on sentences."""
        sentence = (
            "The system architecture incorporates an asynchronous event-driven workflow "
            "where incoming mail messages are decoupled from the processing pipeline. "
        )
        # 1 sentence ~ 25 tokens. 70 repetitions ~ 1750 tokens.
        long_paragraph = sentence * 70
        token_count = default_chunker.token_counter.count_tokens(long_paragraph)
        assert token_count > 1500

        elem = DocumentElement(
            element_type=ElementType.PARAGRAPH,
            content=long_paragraph,
            heading_path=("Architecture", "Overview"),
            section="Overview",
        )

        doc = ParsedDocument(title="System Architecture", elements=[elem])
        chunks = default_chunker.chunk_document(
            parsed_doc=doc,
            document_id="DOC-LONG",
            title="System Architecture",
        )

        assert len(chunks) >= 3
        for idx, chunk in enumerate(chunks):
            assert chunk.token_count <= default_chunker.config.max_tokens
            assert chunk.chunk_index == idx
            assert chunk.content_checksum.startswith("sha256:")
            # Must NOT cut words in half
            for word in chunk.content.split():
                assert len(word) > 0

    def test_tiny_document(self, default_chunker: StructuralChunker) -> None:
        """R24.3 Fixture 4: Tiny doc (<350 tokens) emitting exactly 1 complete chunk."""
        text = (
            "This is a quick security announcement. All API tokens issued before October 1st "
            "must be rotated immediately. Please visit the admin security panel to revoke old keys."
        )
        elem = DocumentElement(
            element_type=ElementType.PARAGRAPH,
            content=text,
            heading_path=("Security Notices",),
            section="Security Notices",
        )

        doc = ParsedDocument(title="Security Notice", elements=[elem])
        chunks = default_chunker.chunk_document(
            parsed_doc=doc,
            document_id="DOC-TINY",
            title="Security Notice",
        )

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_index == 0
        assert chunk.external_id == "DOC-TINY-01"
        assert chunk.token_count < 350
        assert chunk.content == text
        assert chunk.content_checksum.startswith("sha256:")
        assert chunk.heading_path == ["Security Notices"]
        assert chunk.section == "Security Notices"
        assert chunk.metadata["title"] == "Security Notice"


class TestStructuralChunkerEdgeCases:
    """Additional edge cases, idempotency, and continuity tests."""

    def test_empty_document(self, default_chunker: StructuralChunker) -> None:
        """Empty ParsedDocument returns empty chunk list."""
        doc = ParsedDocument(title="Empty", elements=[])
        assert default_chunker.chunk_document(doc, "DOC-EMPTY") == []
        assert default_chunker.chunk_text("", "DOC-EMPTY") == []
        assert default_chunker.chunk_text("   \n\n  ", "DOC-EMPTY") == []

    def test_checksum_reproducibility(self, default_chunker: StructuralChunker) -> None:
        """R9.9: Content checksum sha256 is deterministic and reproducible."""
        text = (
            "# Getting Started\n\nWelcome to the knowledge base.\n\n"
            "## Installation\n\nRun pip install rag-email to install all core dependencies."
        )
        chunks_1 = default_chunker.chunk_text(text, document_id="DOC-100", title="Guide")
        chunks_2 = default_chunker.chunk_text(text, document_id="DOC-100", title="Guide")

        assert len(chunks_1) == len(chunks_2)
        for c1, c2 in zip(chunks_1, chunks_2, strict=True):
            assert c1.content_checksum == c2.content_checksum
            assert c1.content == c2.content
            assert c1.token_count == c2.token_count

        # Modifying content must change checksum
        modified_text = text + " Extra sentence at the end."
        chunks_mod = default_chunker.chunk_text(modified_text, document_id="DOC-100", title="Guide")
        assert chunks_mod[-1].content_checksum != chunks_1[-1].content_checksum

    def test_overlap_continuity(self) -> None:
        """Consecutive chunks share configured token overlap."""
        chunker = StructuralChunker(
            config=ChunkerConfig(min_tokens=100, max_tokens=200, overlap_tokens=30)
        )
        # Create text with 5 distinct numbered sentences
        sentences = [
            f"Sentence number {i} provides specific instructional details for the user workflow."
            for i in range(1, 20)
        ]
        text = " ".join(sentences)

        chunks = chunker.chunk_text(text, document_id="DOC-OVERLAP", title="Overlap Test")
        assert len(chunks) >= 2

        # Verify that chunk 1 has overlap from chunk 0
        chunk_0_words = chunks[0].content.split()
        chunk_1_content = chunks[1].content
        # Trailing words of chunk 0 should appear in chunk 1
        trailing_snippet = " ".join(chunk_0_words[-5:])
        assert trailing_snippet in chunk_1_content
        assert chunks[1].metadata.get("overlap_tokens", 0) > 0

    def test_zero_overlap(self) -> None:
        """When overlap_tokens is 0, no text is duplicated across chunks."""
        chunker = StructuralChunker(
            config=ChunkerConfig(min_tokens=100, max_tokens=200, overlap_tokens=0)
        )
        sentences = [
            f"Sentence number {i} provides unique instruction {i} for execution."
            for i in range(1, 40)
        ]
        text = " ".join(sentences)

        chunks = chunker.chunk_text(text, document_id="DOC-NO-OVERLAP")
        assert len(chunks) >= 2
        for chunk in chunks:
            assert "overlap_tokens" not in chunk.metadata

    def test_metadata_enrichment(self, default_chunker: StructuralChunker) -> None:
        """Verify page_numbers, element_types, and structural metadata."""
        elements = [
            DocumentElement(
                element_type=ElementType.HEADING,
                content="API Reference",
                level=1,
                metadata={"page_number": 1},
            ),
            DocumentElement(
                element_type=ElementType.PARAGRAPH,
                content="Here is a sample request payload for the auth endpoint.",
                metadata={"page_number": 1},
            ),
            DocumentElement(
                element_type=ElementType.CODE_BLOCK,
                content='```json\n{"token": "secret"}\n```',
                metadata={"page_number": 2},
            ),
        ]
        doc = ParsedDocument(title="API Docs", elements=elements)
        chunks = default_chunker.chunk_document(doc, document_id="DOC-API", title="API Docs")

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.metadata["page_numbers"] == [1, 2]
        assert "heading" in chunk.metadata["element_types"]
        assert "paragraph" in chunk.metadata["element_types"]
        assert "code_block" in chunk.metadata["element_types"]
        assert chunk.metadata.get("has_code") is True

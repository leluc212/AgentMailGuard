"""Structural chunker for knowledge RAG ingestion.

Requirements:
- R9.3: Chunk on semantic boundaries (headings, sections, paragraphs, list groups, tables)
  in preference to fixed character splits.
- R9.4: Target configurable chunk size defaulting to 350-700 tokens with configurable overlap.
- R9.5: Attach metadata to every chunk: document_id, chunk_id, title, heading_path,
  section, category, version.
- R9.9: Content checksum (sha256:{hexdigest}) on chunk content for deduplication.
- R24.3: Unit tests for heading-heavy doc, table-heavy doc, long unbroken paragraph, tiny doc.
- specs/design.md §5.6 & §6.1: Knowledge chunk data model and boundary rules.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    KnowledgeChunk,
    ParsedDocument,
)
from packages.knowledge.parsers.text import PlainTextParser
from packages.knowledge.token_counter import TokenCounter

logger = logging.getLogger(__name__)

RE_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkerConfig:
    """Configuration options for structural chunking."""

    min_tokens: int = 350
    max_tokens: int = 700
    overlap_tokens: int = 50
    encoding_name: str = "cl100k_base"

    def __post_init__(self) -> None:
        if self.min_tokens <= 0:
            raise ValueError(f"min_tokens must be positive, got {self.min_tokens}")
        if self.max_tokens < self.min_tokens:
            raise ValueError(
                f"max_tokens ({self.max_tokens}) must be >= min_tokens ({self.min_tokens})"
            )
        if self.overlap_tokens < 0:
            raise ValueError(f"overlap_tokens cannot be negative, got {self.overlap_tokens}")
        if self.overlap_tokens >= self.max_tokens:
            raise ValueError(
                f"overlap_tokens ({self.overlap_tokens}) must be < max_tokens ({self.max_tokens})"
            )


class StructuralChunker:
    """Semantic boundary-aware structural chunker.

    Splits documents on structural hierarchy boundaries (headings, sections,
    paragraphs, list items, tables) targeting 350-700 tokens with configurable
    overlap, emitting authoritative KnowledgeChunk entities with complete metadata.
    """

    def __init__(
        self,
        config: ChunkerConfig | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.config = config or ChunkerConfig()
        self.token_counter = token_counter or TokenCounter(self.config.encoding_name)

    def chunk_document(
        self,
        parsed_doc: ParsedDocument,
        document_id: UUID | str,
        organization_id: UUID | str = "",
        title: str | None = None,
        category: str | None = None,
        version: int = 1,
    ) -> list[KnowledgeChunk]:
        """Transform a ParsedDocument into a sequence of KnowledgeChunk entities."""
        doc_title = title or parsed_doc.title or ""
        doc_category = category or parsed_doc.metadata.get("category")

        # Normalize and filter elements
        elements = self._prepare_elements(parsed_doc)
        if not elements:
            return []

        # Break oversized elements (tables row-by-row with headers, paragraphs by sentences)
        normalized_elements: list[DocumentElement] = []
        for elem in elements:
            normalized_elements.extend(self._normalize_element_size(elem))

        if not normalized_elements:
            return []

        # Group elements into chunks respecting boundaries and target token budgets
        return self._assemble_chunks(
            elements=normalized_elements,
            document_id=document_id,
            organization_id=organization_id,
            title=doc_title,
            category=doc_category,
            version=version,
        )

    def chunk_text(
        self,
        text: str,
        document_id: UUID | str,
        organization_id: UUID | str = "",
        title: str | None = None,
        category: str | None = None,
        version: int = 1,
    ) -> list[KnowledgeChunk]:
        """Convenience method to parse plain text and chunk it structurally."""
        if not text or not text.strip():
            return []

        parser = PlainTextParser()
        parsed_doc = parser.parse(text, filename=title)
        if title:
            parsed_doc.title = title

        return self.chunk_document(
            parsed_doc=parsed_doc,
            document_id=document_id,
            organization_id=organization_id,
            title=title,
            category=category,
            version=version,
        )

    def _prepare_elements(self, parsed_doc: ParsedDocument) -> list[DocumentElement]:
        """Extract clean non-empty structural elements from ParsedDocument."""
        elements: list[DocumentElement] = []
        for elem in parsed_doc.elements:
            content = elem.content.strip()
            if not content:
                continue
            elements.append(elem)

        if not elements and parsed_doc.text.strip():
            # If document has raw text but no parsed elements, fall back to PlainTextParser
            fallback_doc = PlainTextParser().parse(parsed_doc.text, filename=parsed_doc.title)
            return [e for e in fallback_doc.elements if e.content.strip()]

        return elements

    def _normalize_element_size(self, elem: DocumentElement) -> list[DocumentElement]:
        """Ensure an element does not exceed max_tokens, splitting internally if needed."""
        budget = max(self.config.min_tokens, self.config.max_tokens - self.config.overlap_tokens)
        elem_tokens = self.token_counter.count_tokens(elem.content)
        if elem_tokens <= budget:
            return [elem]

        if elem.element_type == ElementType.TABLE:
            return self._split_oversized_table(elem, budget)

        # Paragraphs, code blocks, list items, blockquotes, headings
        return self._split_oversized_text_element(elem, budget)

    def _split_oversized_table(self, elem: DocumentElement, budget: int) -> list[DocumentElement]:
        """Split a large table row-by-row while preserving column headers on every chunk."""
        table_data = elem.metadata.get("table_data")
        if table_data and isinstance(table_data, dict):
            headers = table_data.get("headers", [])
            rows = table_data.get("rows", [])
            if headers and rows:
                header_str = (
                    "| "
                    + " | ".join(str(h) for h in headers)
                    + " |\n| "
                    + " | ".join(["---"] * len(headers))
                    + " |\n"
                )
                header_tokens = self.token_counter.count_tokens(header_str)
                if header_tokens < budget:
                    sub_elements: list[DocumentElement] = []
                    current_rows: list[list[str]] = []
                    current_tokens = header_tokens

                    for row in rows:
                        row_str = "| " + " | ".join(str(c) for c in row) + " |\n"
                        row_tokens = self.token_counter.count_tokens(row_str)

                        if current_rows and (current_tokens + row_tokens > budget):
                            sub_content = (
                                header_str
                                + "".join(
                                    "| " + " | ".join(str(c) for c in r) + " |\n"
                                    for r in current_rows
                                ).strip()
                            )
                            sub_elements.append(
                                DocumentElement(
                                    element_type=ElementType.TABLE,
                                    content=sub_content,
                                    level=elem.level,
                                    heading_path=elem.heading_path,
                                    section=elem.section,
                                    metadata={
                                        **elem.metadata,
                                        "table_data": {"headers": headers, "rows": current_rows},
                                        "is_subtable": True,
                                    },
                                )
                            )
                            current_rows = []
                            current_tokens = header_tokens

                        current_rows.append(row)
                        current_tokens += row_tokens

                    if current_rows:
                        sub_content = (
                            header_str
                            + "".join(
                                "| " + " | ".join(str(c) for c in r) + " |\n" for r in current_rows
                            ).strip()
                        )
                        sub_elements.append(
                            DocumentElement(
                                element_type=ElementType.TABLE,
                                content=sub_content,
                                level=elem.level,
                                heading_path=elem.heading_path,
                                section=elem.section,
                                metadata={
                                    **elem.metadata,
                                    "table_data": {"headers": headers, "rows": current_rows},
                                    "is_subtable": True,
                                },
                            )
                        )
                    return sub_elements

        # Fallback for plain markdown table text (split lines preserving line 0 and line 1)
        lines = elem.content.strip().splitlines()
        if len(lines) >= 3 and lines[1].strip().startswith("|-"):
            header_lines = lines[0] + "\n" + lines[1] + "\n"
            header_tokens = self.token_counter.count_tokens(header_lines)
            data_lines = lines[2:]

            sub_elements = []
            curr_lines: list[str] = []
            curr_tokens = header_tokens

            for line in data_lines:
                l_tokens = self.token_counter.count_tokens(line + "\n")
                if curr_lines and (curr_tokens + l_tokens > budget):
                    sub_content = (header_lines + "\n".join(curr_lines)).strip()
                    sub_elements.append(
                        DocumentElement(
                            element_type=ElementType.TABLE,
                            content=sub_content,
                            level=elem.level,
                            heading_path=elem.heading_path,
                            section=elem.section,
                            metadata={**elem.metadata, "is_subtable": True},
                        )
                    )
                    curr_lines = []
                    curr_tokens = header_tokens

                curr_lines.append(line)
                curr_tokens += l_tokens

            if curr_lines:
                sub_content = (header_lines + "\n".join(curr_lines)).strip()
                sub_elements.append(
                    DocumentElement(
                        element_type=ElementType.TABLE,
                        content=sub_content,
                        level=elem.level,
                        heading_path=elem.heading_path,
                        section=elem.section,
                        metadata={**elem.metadata, "is_subtable": True},
                    )
                )
            return sub_elements

        # If not structured table, treat as plain text element
        return self._split_oversized_text_element(elem, budget)

    def _split_oversized_text_element(
        self, elem: DocumentElement, budget: int
    ) -> list[DocumentElement]:
        """Split a text element into sentence-bounded or word-bounded sub-elements."""
        chunks = self._split_text_content(elem.content, budget)
        sub_elements: list[DocumentElement] = []
        for chunk_text in chunks:
            sub_elements.append(
                DocumentElement(
                    element_type=elem.element_type,
                    content=chunk_text,
                    level=elem.level,
                    heading_path=elem.heading_path,
                    section=elem.section,
                    metadata=dict(elem.metadata),
                )
            )
        return sub_elements

    def _split_text_content(self, text: str, max_tokens: int) -> list[str]:
        """Split arbitrary long text on sentence boundaries, then words, guaranteeing max_tokens."""
        # 1. Split into sentence fragments
        sentences = [s.strip() for s in RE_SENTENCE_BOUNDARY.split(text.strip()) if s.strip()]
        if not sentences:
            sentences = [text.strip()]

        result_chunks: list[str] = []
        current_chunk_sentences: list[str] = []
        current_tokens = 0

        for sentence in sentences:
            sentence_tokens = self.token_counter.count_tokens(sentence)

            if sentence_tokens > max_tokens:
                # Flush existing buffer first
                if current_chunk_sentences:
                    result_chunks.append(" ".join(current_chunk_sentences))
                    current_chunk_sentences = []
                    current_tokens = 0

                # Split oversized sentence by words
                word_chunks = self._split_words_to_budget(sentence, max_tokens)
                result_chunks.extend(word_chunks)
                continue

            if current_chunk_sentences and (current_tokens + sentence_tokens > max_tokens):
                result_chunks.append(" ".join(current_chunk_sentences))
                current_chunk_sentences = [sentence]
                current_tokens = sentence_tokens
            else:
                current_chunk_sentences.append(sentence)
                current_tokens += sentence_tokens

        if current_chunk_sentences:
            result_chunks.append(" ".join(current_chunk_sentences))

        return result_chunks

    def _split_words_to_budget(self, text: str, max_tokens: int) -> list[str]:
        """Split a continuous sentence into word-bounded sub-strings <= max_tokens."""
        words = text.split()
        if not words:
            return []

        chunks: list[str] = []
        current_words: list[str] = []
        current_tokens = 0

        for word in words:
            word_tokens = self.token_counter.count_tokens(word)
            if word_tokens > max_tokens:
                # Extreme single word/token run (e.g. 5000-char string): slice via token IDs
                if current_words:
                    chunks.append(" ".join(current_words))
                    current_words = []
                    current_tokens = 0

                token_ids = self.token_counter.encode(word)
                for i in range(0, len(token_ids), max_tokens):
                    sub_word = self.token_counter.decode(token_ids[i : i + max_tokens])
                    if sub_word:
                        chunks.append(sub_word)
                continue

            if current_words and (current_tokens + word_tokens > max_tokens):
                chunks.append(" ".join(current_words))
                current_words = [word]
                current_tokens = word_tokens
            else:
                current_words.append(word)
                current_tokens += word_tokens

        if current_words:
            chunks.append(" ".join(current_words))

        return chunks

    def _extract_trailing_overlap(self, text: str, target_tokens: int) -> tuple[str, int]:
        """Extract trailing semantic units (sentences/words) from text up to target_tokens."""
        if not text or target_tokens <= 0:
            return "", 0

        sentences = [s.strip() for s in RE_SENTENCE_BOUNDARY.split(text.strip()) if s.strip()]
        if not sentences:
            sentences = [text.strip()]

        selected_sentences: list[str] = []
        current_tokens = 0

        for sentence in reversed(sentences):
            s_tokens = self.token_counter.count_tokens(sentence)
            if current_tokens + s_tokens <= target_tokens:
                selected_sentences.append(sentence)
                current_tokens += s_tokens
            else:
                if not selected_sentences:
                    # Last sentence alone exceeds target_tokens: take trailing words
                    words = sentence.split()
                    selected_words: list[str] = []
                    w_tokens = 0
                    for word in reversed(words):
                        word_t = self.token_counter.count_tokens(word)
                        if w_tokens + word_t <= target_tokens:
                            selected_words.append(word)
                            w_tokens += word_t
                        else:
                            break
                    if selected_words:
                        selected_words.reverse()
                        overlap = " ".join(selected_words)
                        return overlap, self.token_counter.count_tokens(overlap)
                break

        if selected_sentences:
            selected_sentences.reverse()
            overlap = " ".join(selected_sentences)
            return overlap, self.token_counter.count_tokens(overlap)

        return "", 0

    def _format_element_content(self, elem: DocumentElement) -> str:
        """Render element content with appropriate structural Markdown formatting."""
        if elem.element_type == ElementType.HEADING:
            hashes = "#" * (elem.level or 1)
            content = elem.content.strip()
            if content.startswith("#"):
                return content
            return f"{hashes} {content}"
        return elem.content.strip()

    def _join_elements_text(self, elements: list[DocumentElement]) -> str:
        """Combine elements into a readable Markdown representation."""
        if not elements:
            return ""

        parts: list[str] = []
        for i, elem in enumerate(elements):
            formatted = self._format_element_content(elem)
            if not formatted:
                continue

            if i > 0:
                prev = elements[i - 1]
                # Compact newline between consecutive list items
                if (
                    prev.element_type == ElementType.LIST_ITEM
                    and elem.element_type == ElementType.LIST_ITEM
                ):
                    parts.append("\n" + formatted)
                    continue

            parts.append("\n\n" + formatted if parts else formatted)

        return "".join(parts)

    def _assemble_chunks(
        self,
        elements: list[DocumentElement],
        document_id: UUID | str,
        organization_id: UUID | str,
        title: str,
        category: str | None,
        version: int,
    ) -> list[KnowledgeChunk]:
        """Accumulate elements into chunks respecting semantic boundaries and overlap."""
        chunks: list[KnowledgeChunk] = []
        accumulated: list[DocumentElement] = []
        accumulated_tokens = 0

        # Overlap carried over from preceding chunk
        overlap_prefix = ""
        overlap_prefix_tokens = 0

        def flush_chunk() -> None:
            nonlocal accumulated, accumulated_tokens, overlap_prefix, overlap_prefix_tokens
            if not accumulated:
                return

            main_text = self._join_elements_text(accumulated)
            full_content = f"{overlap_prefix}\n\n{main_text}" if overlap_prefix else main_text
            # Guarantee full_content never exceeds max_tokens due to overlap
            if (
                overlap_prefix
                and self.token_counter.count_tokens(full_content) > self.config.max_tokens
            ):
                main_tokens = self.token_counter.count_tokens(main_text)
                allowed_overlap = self.config.max_tokens - main_tokens
                if allowed_overlap > 0:
                    overlap_prefix, overlap_prefix_tokens = self._extract_trailing_overlap(
                        overlap_prefix, allowed_overlap
                    )
                    full_content = (
                        f"{overlap_prefix}\n\n{main_text}" if overlap_prefix else main_text
                    )
                else:
                    overlap_prefix = ""
                    overlap_prefix_tokens = 0
                    full_content = main_text

            chunk_tokens = self.token_counter.count_tokens(full_content)
            chunk_index = len(chunks)
            external_id = f"{document_id}-{chunk_index + 1:02d}"

            # Derive dominant heading_path and section
            heading_path: list[str] = []
            section: str | None = None

            for elem in accumulated:
                if elem.heading_path:
                    heading_path = list(elem.heading_path)
                    break
            if not heading_path and title:
                heading_path = [title]

            for elem in accumulated:
                if elem.element_type == ElementType.HEADING:
                    section = elem.content.strip().lstrip("#").strip()
                    break
                if elem.section:
                    section = elem.section
                    break
            if not section:
                section = heading_path[-1] if heading_path else (title or None)

            # Metadata enrichment
            page_numbers: set[int] = set()
            element_types: list[str] = []
            has_table = False
            has_code = False

            for elem in accumulated:
                element_types.append(elem.element_type.value)
                p_num = elem.metadata.get("page_number")
                if isinstance(p_num, int):
                    page_numbers.add(p_num)
                if elem.element_type == ElementType.TABLE:
                    has_table = True
                if elem.element_type == ElementType.CODE_BLOCK:
                    has_code = True

            checksum = f"sha256:{hashlib.sha256(full_content.encode('utf-8')).hexdigest()}"

            meta: dict[str, Any] = {
                "title": title,
                "chunk_id": external_id,
                "element_types": sorted(set(element_types)),
            }
            if page_numbers:
                meta["page_numbers"] = sorted(page_numbers)
            if has_table:
                meta["has_table"] = True
            if has_code:
                meta["has_code"] = True
            if overlap_prefix_tokens > 0:
                meta["overlap_tokens"] = overlap_prefix_tokens

            chunk = KnowledgeChunk(
                document_id=document_id,
                organization_id=organization_id,
                chunk_index=chunk_index,
                external_id=external_id,
                heading_path=heading_path,
                section=section,
                category=category,
                content=full_content,
                token_count=chunk_tokens,
                content_checksum=checksum,
                metadata=meta,
                version=version,
            )
            chunks.append(chunk)

            # Extract overlap for next chunk from main_text
            if self.config.overlap_tokens > 0:
                overlap_prefix, overlap_prefix_tokens = self._extract_trailing_overlap(
                    main_text, self.config.overlap_tokens
                )
            else:
                overlap_prefix = ""
                overlap_prefix_tokens = 0

            accumulated = []
            accumulated_tokens = 0

        for elem in elements:
            elem_tokens = self.token_counter.count_tokens(self._format_element_content(elem))

            # 1. Heading boundary check
            if elem.element_type == ElementType.HEADING:
                if accumulated and (
                    accumulated_tokens + overlap_prefix_tokens >= self.config.min_tokens
                ):
                    flush_chunk()
                    accumulated.append(elem)
                    accumulated_tokens += elem_tokens
                    continue

                if accumulated and (
                    accumulated_tokens + overlap_prefix_tokens + elem_tokens
                    > self.config.max_tokens
                ):
                    flush_chunk()
                    accumulated.append(elem)
                    accumulated_tokens += elem_tokens
                    continue

                accumulated.append(elem)
                accumulated_tokens += elem_tokens
                continue

            # 2. Non-heading element check
            if accumulated and (
                accumulated_tokens + overlap_prefix_tokens + elem_tokens > self.config.max_tokens
            ):
                flush_chunk()
                accumulated.append(elem)
                accumulated_tokens += elem_tokens
            else:
                accumulated.append(elem)
                accumulated_tokens += elem_tokens

        if accumulated:
            flush_chunk()

        return chunks

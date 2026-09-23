"""Pure domain entities and value objects for knowledge ingestion and RAG documents.

Requirements:
- R9.1: Ingestion pipeline data modeling (parse -> structure -> chunk -> embed -> persist).
- R9.2: Multi-format document structure representation (headings, sections, lists, tables).
- R9.5: Chunk metadata structure (doc_id, chunk_id, title, heading_path, section, category).
- specs/design.md §5.6 & §6.1: Knowledge document and chunk data models.
- GEMINI.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ElementType(StrEnum):
    """Semantic type of an extracted document element."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CODE_BLOCK = "code_block"
    BLOCKQUOTE = "blockquote"


@dataclass(frozen=True)
class DocumentElement:
    """A discrete structural element extracted from a raw document.

    Carries semantic tagging, hierarchy level, and breadcrumb path to preserve
    document structure for chunking (R9.3) and metadata tagging (R9.5).
    """

    element_type: ElementType
    content: str
    level: int | None = None  # Heading level (1..6) or list indentation level (1..N)
    # Breadcrumbs: e.g. ("User Guide", "Authentication", "API Keys")
    heading_path: tuple[str, ...] = field(default_factory=tuple)
    section: str | None = None  # Immediate enclosing heading title
    # Extra attributes: page_number, table_data, list_type, etc.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentSection:
    """A logical document section bounded by a heading and containing child elements."""

    title: str
    level: int
    heading_path: tuple[str, ...]
    elements: list[DocumentElement] = field(default_factory=list)


@dataclass
class ParsedDocument:
    """The normalized output of a document parser.

    Contains full clean text alongside the sequence of structural elements
    (headings, sections, list items, tables) and file-level metadata.
    """

    title: str | None = None
    elements: list[DocumentElement] = field(default_factory=list)
    text: str = ""
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_heading_hierarchy(self) -> list[tuple[int, str]]:
        """Return all headings in document order as (level, title) tuples."""
        return [
            (elem.level or 1, elem.content)
            for elem in self.elements
            if elem.element_type == ElementType.HEADING
        ]

    def get_elements_by_type(self, element_type: ElementType) -> list[DocumentElement]:
        """Filter document elements by structural element type."""
        return [elem for elem in self.elements if elem.element_type == element_type]

    def get_sections(self) -> list[DocumentSection]:
        """Aggregate sequential elements into bounded DocumentSection groupings."""
        sections: list[DocumentSection] = []
        current_section: DocumentSection | None = None

        for elem in self.elements:
            if elem.element_type == ElementType.HEADING:
                if current_section is not None:
                    sections.append(current_section)
                current_section = DocumentSection(
                    title=elem.content,
                    level=elem.level or 1,
                    heading_path=elem.heading_path,
                    elements=[elem],
                )
            else:
                if current_section is None:
                    # Content before the first heading belongs to a root section
                    current_section = DocumentSection(
                        title=self.title or "Root",
                        level=0,
                        heading_path=(),
                        elements=[],
                    )
                current_section.elements.append(elem)

        if current_section is not None:
            sections.append(current_section)

        return sections

    def to_text(self) -> str:
        """Return the linearized clean text of the document."""
        if self.text:
            return self.text
        return "\n\n".join(elem.content for elem in self.elements if elem.content.strip())


@dataclass
class KnowledgeDocument:
    """Authoritative knowledge base document entity (specs/design.md §6.1)."""

    id: UUID = field(default_factory=uuid4)
    organization_id: UUID | str = ""
    title: str = ""
    source_uri: str | None = None
    mime_type: str | None = None
    category: str | None = None
    version: int = 1
    checksum: str | None = None
    object_key: str | None = None
    status: str = "pending"  # pending|parsing|chunking|embedding|active|superseded|failed
    failure_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class KnowledgeChunk:
    """A semantic chunk of a knowledge document with vector & FTS metadata (design.md §6.1)."""

    id: UUID = field(default_factory=uuid4)
    document_id: UUID | str = ""
    organization_id: UUID | str = ""
    chunk_index: int = 0
    external_id: str | None = None  # e.g. "DOC-125-08" for citations (R16.5)
    heading_path: list[str] = field(default_factory=list)
    section: str | None = None
    category: str | None = None
    content: str = ""
    token_count: int = 0
    content_checksum: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1


@dataclass
class EmbeddingRecord:
    """Vector embedding record corresponding to a knowledge chunk (design.md §6.1, R5.7)."""

    chunk_id: UUID = field(default_factory=uuid4)
    organization_id: UUID | str = ""
    model: str = "text-embedding-3-small"
    dim: int = 1536
    embedding: list[float] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


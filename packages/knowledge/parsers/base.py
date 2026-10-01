"""Abstract base parser and shared structure extraction utilities for knowledge RAG.

Requirements:
- R9.2: Multi-format document parser abstraction.
- specs/design.md §5.6: Offline document parsing and structure extraction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    ParsedDocument,
)


class DocumentParsingError(Exception):
    """Raised when parsing a document fails due to corrupt or invalid content."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class UnsupportedDocumentTypeError(DocumentParsingError):
    """Raised when no parser exists for the provided MIME type or file extension."""


@dataclass
class HeadingStack:
    """Maintains active heading hierarchy to derive heading breadcrumb paths and sections."""

    _stack: list[tuple[int, str]] = field(default_factory=list)

    def push(self, level: int, title: str) -> tuple[tuple[str, ...], str | None]:
        """Push a heading at the specified level, popping any deeper or equal headings.

        Returns:
            A tuple of (heading_path, current_section_title).
        """
        clean_title = title.strip()
        # Pop headings at or below the current depth (e.g. encountering H2 pops prior H2 and H3)
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, clean_title))
        heading_path = tuple(item[1] for item in self._stack)
        section = clean_title
        return heading_path, section

    @property
    def current_heading_path(self) -> tuple[str, ...]:
        """Current breadcrumb tuple of enclosing headings."""
        return tuple(item[1] for item in self._stack)

    @property
    def current_section(self) -> str | None:
        """Immediate enclosing heading title."""
        if self._stack:
            return self._stack[-1][1]
        return None

    def clear(self) -> None:
        """Reset the heading stack."""
        self._stack.clear()


class DocumentParser(ABC):
    """Abstract base class for all format-specific document parsers."""

    supported_mime_types: set[str] = set()
    supported_extensions: set[str] = set()

    def can_parse(self, mime_type: str | None = None, filename: str | None = None) -> bool:
        """Check whether this parser handles the given MIME type or file extension."""
        if mime_type:
            normalized_mime = mime_type.lower().split(";")[0].strip()
            if normalized_mime in self.supported_mime_types:
                return True
        if filename:
            ext = Path(filename).suffix.lower()
            if ext in self.supported_extensions:
                return True
        return False

    @abstractmethod
    def parse(
        self,
        content: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        """Parse raw content into a structured ParsedDocument representation."""
        ...

    @staticmethod
    def decode_content(content: bytes | str) -> str:
        """Decode raw bytes into a string with resilient fallback encodings."""
        if isinstance(content, str):
            return content

        encodings = ("utf-8", "utf-8-sig", "latin-1", "cp1252")
        for enc in encodings:
            try:
                return content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def make_element(
        element_type: ElementType,
        content: str,
        level: int | None = None,
        heading_path: tuple[str, ...] = (),
        section: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentElement:
        """Factory helper to construct an immutable DocumentElement."""
        return DocumentElement(
            element_type=element_type,
            content=content.strip(),
            level=level,
            heading_path=heading_path,
            section=section,
            metadata=metadata or {},
        )

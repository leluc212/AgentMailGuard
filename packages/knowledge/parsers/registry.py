"""Registry and unified entrypoint for document parsers.

Requirements:
- R9.2: Support PDF, DOCX, HTML, Markdown, and plain text sources.
- specs/design.md §5.6: Offline document parsing and structure extraction.
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.domain.knowledge import ParsedDocument
from packages.knowledge.parsers.base import (
    DocumentParser,
    UnsupportedDocumentTypeError,
)
from packages.knowledge.parsers.docx import DOCXParser
from packages.knowledge.parsers.html import HTMLParser
from packages.knowledge.parsers.markdown import MarkdownParser
from packages.knowledge.parsers.pdf import PDFParser
from packages.knowledge.parsers.text import PlainTextParser


class ParserRegistry:
    """Registry managing format-specific document parsers."""

    def __init__(self, parsers: Sequence[DocumentParser] | None = None) -> None:
        self._parsers: list[DocumentParser] = list(parsers) if parsers is not None else []

    @classmethod
    def create_default(cls) -> ParserRegistry:
        """Create a registry pre-loaded with all canonical document parsers."""
        return cls(
            parsers=[
                PDFParser(),
                DOCXParser(),
                HTMLParser(),
                MarkdownParser(),
                PlainTextParser(),
            ]
        )

    def register(self, parser: DocumentParser, prepend: bool = False) -> None:
        """Register a new parser in the registry."""
        if prepend:
            self._parsers.insert(0, parser)
        else:
            self._parsers.append(parser)

    def get_parser(
        self,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> DocumentParser:
        """Resolve a suitable parser for the specified MIME type or filename.

        Raises:
            UnsupportedDocumentTypeError: If no registered parser matches.
        """
        for parser in self._parsers:
            if parser.can_parse(mime_type=mime_type, filename=filename):
                return parser

        raise UnsupportedDocumentTypeError(
            f"No parser available for document: mime_type='{mime_type}', filename='{filename}'",
            details={"mime_type": mime_type, "filename": filename},
        )

    def parse(
        self,
        content: bytes | str,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument:
        """Parse raw document content using the appropriate registered parser."""
        parser = self.get_parser(mime_type=mime_type, filename=filename)
        return parser.parse(content=content, filename=filename, mime_type=mime_type)


# Canonical default registry instance
_default_registry: ParserRegistry | None = None


def get_default_registry() -> ParserRegistry:
    """Return the global default ParserRegistry singleton."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ParserRegistry.create_default()
    return _default_registry


def get_parser(
    mime_type: str | None = None,
    filename: str | None = None,
) -> DocumentParser:
    """Resolve a parser using the default registry."""
    return get_default_registry().get_parser(mime_type=mime_type, filename=filename)


def parse_document(
    content: bytes | str,
    mime_type: str | None = None,
    filename: str | None = None,
) -> ParsedDocument:
    """Parse document content into a structured ParsedDocument using the default registry."""
    return get_default_registry().parse(
        content=content,
        mime_type=mime_type,
        filename=filename,
    )

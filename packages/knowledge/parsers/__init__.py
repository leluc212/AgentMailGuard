"""Document parsers package for knowledge RAG ingestion.

Requirements:
- R9.2: Support PDF, DOCX, HTML, Markdown, and plain text sources.
"""

from packages.knowledge.parsers.base import (
    DocumentParser,
    DocumentParsingError,
    HeadingStack,
    UnsupportedDocumentTypeError,
)
from packages.knowledge.parsers.docx import DOCXParser
from packages.knowledge.parsers.html import HTMLParser
from packages.knowledge.parsers.markdown import MarkdownParser
from packages.knowledge.parsers.pdf import PDFParser
from packages.knowledge.parsers.registry import (
    ParserRegistry,
    get_default_registry,
    get_parser,
    parse_document,
)
from packages.knowledge.parsers.text import PlainTextParser

__all__ = [
    "DOCXParser",
    "DocumentParser",
    "DocumentParsingError",
    "HTMLParser",
    "HeadingStack",
    "MarkdownParser",
    "PDFParser",
    "ParserRegistry",
    "PlainTextParser",
    "UnsupportedDocumentTypeError",
    "get_default_registry",
    "get_parser",
    "parse_document",
]

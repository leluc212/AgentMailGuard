"""Knowledge package: ingestion, parsers, chunking, and embedding."""

from packages.knowledge.chunker import ChunkerConfig, StructuralChunker
from packages.knowledge.embedder import (
    Embedder,
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingRateLimitError,
    EmbeddingResult,
    EmbeddingTimeoutError,
    FakeEmbedder,
    HttpEmbedder,
    get_embedder,
)
from packages.knowledge.parsers import (
    DocumentParser,
    DocumentParsingError,
    DOCXParser,
    HTMLParser,
    MarkdownParser,
    ParserRegistry,
    PDFParser,
    PlainTextParser,
    UnsupportedDocumentTypeError,
    get_default_registry,
    get_parser,
    parse_document,
)
from packages.knowledge.pipeline import (
    DocumentNotFoundError,
    IngestionError,
    IngestionResult,
    KnowledgeIngestionPipeline,
)
from packages.knowledge.token_counter import TokenCounter

__all__ = [
    "ChunkerConfig",
    "DOCXParser",
    "DocumentNotFoundError",
    "DocumentParser",
    "DocumentParsingError",
    "Embedder",
    "EmbeddingDimensionMismatchError",
    "EmbeddingError",
    "EmbeddingRateLimitError",
    "EmbeddingResult",
    "EmbeddingTimeoutError",
    "FakeEmbedder",
    "HTMLParser",
    "HttpEmbedder",
    "IngestionError",
    "IngestionResult",
    "KnowledgeIngestionPipeline",
    "MarkdownParser",
    "PDFParser",
    "ParserRegistry",
    "PlainTextParser",
    "StructuralChunker",
    "TokenCounter",
    "UnsupportedDocumentTypeError",
    "get_default_registry",
    "get_embedder",
    "get_parser",
    "parse_document",
]

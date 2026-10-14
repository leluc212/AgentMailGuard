"""L2 Doc Scanner - retrieved documents: per-chunk scan, cheap signals, set judge."""

from mailguard.layers.doc_scanner.chunk_scanner import RetrievedDocumentScanner
from mailguard.layers.doc_scanner.scanner import DocScanner
from mailguard.layers.doc_scanner.set_judge import deterministic_set_verdict

__all__ = ["DocScanner", "RetrievedDocumentScanner", "deterministic_set_verdict"]

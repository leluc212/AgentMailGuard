"""Knowledge document ingestion pipeline and versioning orchestration.

Requirements:
- R9.1: Asynchronous document ingestion: parse -> structure -> chunk -> enrich -> embed -> persist.
- R9.8: Version N+1 re-ingestion with zero-downtime atomic flip (never unsearchable).
- R9.9: Unchanged content_checksum skips re-embedding across versions (carry forward).
- R9.10: Per-document status tracking (pending, parsing, chunking, embedding, active, failed)
  with failure reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from uuid import UUID

from packages.core.storage import StorageProtocol
from packages.db.knowledge import KnowledgeStore
from packages.domain.knowledge import KnowledgeDocument
from packages.knowledge.chunker import ChunkerConfig, StructuralChunker
from packages.knowledge.embedder import Embedder
from packages.knowledge.parsers import ParserRegistry, get_default_registry

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Base exception for document ingestion failures."""


class DocumentNotFoundError(IngestionError):
    """Raised when the specified document does not exist in the store."""


class UnsupportedDocumentTypeError(IngestionError):
    """Raised when no suitable parser exists for the document MIME type/format."""


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@dataclass(frozen=True)
class IngestionResult:
    """Outcome summary of a document ingestion run."""

    document_id: UUID
    organization_id: UUID
    version: int
    total_chunks: int
    embedded_chunks: int
    carried_forward_chunks: int
    status: str


class KnowledgeIngestionPipeline:
    """Orchestrates parsing, chunking, deduplicated embedding, and atomic versioned persistence."""

    def __init__(
        self,
        store: KnowledgeStore,
        embedder: Embedder,
        parser_registry: ParserRegistry | None = None,
        chunker: StructuralChunker | None = None,
        storage: StorageProtocol | None = None,
        bucket_name: str = "knowledge-docs",
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.parser_registry = parser_registry or get_default_registry()
        self.chunker = chunker or StructuralChunker(ChunkerConfig())
        self.storage = storage
        self.bucket_name = bucket_name

    def _infer_mime_and_filename(
        self,
        doc: KnowledgeDocument,
        raw_filename: str | None,
        content_type: str | None,
    ) -> tuple[str | None, str]:
        """Infer best MIME type and filename from document fields and arguments."""
        filename = raw_filename or doc.title or "document.txt"
        mime = content_type or doc.mime_type

        # Try to infer from extension if mime is not explicitly set
        if not mime or mime == "application/octet-stream":
            lower_name = filename.lower()
            if lower_name.endswith(".pdf"):
                mime = "application/pdf"
            elif lower_name.endswith(".docx"):
                mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif lower_name.endswith(".html") or lower_name.endswith(".htm"):
                mime = "text/html"
            elif lower_name.endswith(".md") or lower_name.endswith(".markdown"):
                mime = "text/markdown"
            elif lower_name.endswith(".txt"):
                mime = "text/plain"

        return mime, filename

    async def ingest_document(
        self,
        document_id: UUID | str,
        organization_id: UUID | str,
        raw_bytes: bytes | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> IngestionResult:
        """Execute full async ingestion pipeline for a knowledge document.

        Stages:
        1. Fetch content (raw_bytes or MinIO object storage).
        2. Status 'parsing': parse into structured elements (R9.1, R9.2).
        3. Status 'chunking': semantic chunking & metadata enrichment (R9.3–R9.5).
        4. Status 'embedding': carry forward unchanged checksum embeddings (R9.9)
           and batch-embed only new/modified chunks (R9.6, R9.11).
        5. Persist: write chunk + content_tsv + embedding in atomic tx (R9.7).
        6. Status 'active': atomically flip version and prune old version chunks (R9.8).
        """
        doc_u = _to_uuid(document_id)
        org_u = _to_uuid(organization_id)

        # 1. Fetch document record
        doc = await self.store.get_document(org_u, doc_u)
        if doc is None:
            raise DocumentNotFoundError(f"Document {doc_u} not found for organization {org_u}")

        # Determine target version (R9.8)
        # If doc is already active or existing version has chunks, advance to version N+1
        is_reingest = doc.status == "active" or doc.version > 1
        target_version = (doc.version + 1) if is_reingest else max(1, doc.version)
        prev_version: int | None = doc.version if is_reingest else None

        try:
            # 2. Retrieve content bytes
            content: bytes
            if raw_bytes is not None:
                content = raw_bytes
            elif doc.object_key and self.storage:
                content = await self.storage.get_bytes(self.bucket_name, doc.object_key)
            elif doc.source_uri:
                import aiofiles

                async with aiofiles.open(doc.source_uri, mode="rb") as f:
                    content = await f.read()
            else:
                raise IngestionError(
                    f"No source content available for document {doc_u} "
                    "(missing raw_bytes, object_key, and source_uri)"
                )

            # 3. Stage: parsing (R9.1, R9.10)
            await self.store.update_document_status(org_u, doc_u, status="parsing")
            mime, resolved_filename = self._infer_mime_and_filename(doc, filename, content_type)

            try:
                parsed_doc = await asyncio.to_thread(
                    self.parser_registry.parse,
                    content=content,
                    mime_type=mime,
                    filename=resolved_filename,
                )
            except Exception as err:
                fmt = mime or resolved_filename
                raise UnsupportedDocumentTypeError(
                    f"No parser available for format '{fmt}' on doc {doc_u}: {err}"
                ) from err

            if not parsed_doc.text.strip() and not parsed_doc.elements:
                raise IngestionError(f"Document {doc_u} produced empty content")

            # 4. Stage: chunking & metadata enrichment (R9.1, R9.3, R9.4, R9.5, R9.10)
            await self.store.update_document_status(org_u, doc_u, status="chunking")
            chunks = await asyncio.to_thread(
                self.chunker.chunk_document,
                parsed_doc=parsed_doc,
                document_id=doc_u,
                organization_id=org_u,
                title=doc.title,
                category=doc.category,
                version=target_version,
            )
            if not chunks:
                raise IngestionError(f"Document {doc_u} produced 0 chunks after chunking")

            # 5. Stage: embedding deduplication & batch generation (R9.8, R9.9, R9.10)
            await self.store.update_document_status(org_u, doc_u, status="embedding")
            cached_embeddings: dict[str, list[float]] = {}

            if prev_version is not None:
                prev_chunks = await self.store.get_chunks_by_document(
                    org_u, doc_u, version=prev_version
                )
                for pc in prev_chunks:
                    if pc.content_checksum and pc.content_checksum not in cached_embeddings:
                        pair = await self.store.get_chunk_with_embedding(org_u, pc.id)
                        if pair and pair[1]:
                            cached_embeddings[pc.content_checksum] = pair[1]

            final_embeddings: list[list[float]] = [[] for _ in range(len(chunks))]
            to_embed_indices: list[int] = []
            to_embed_texts: list[str] = []
            carried_forward_count = 0

            for idx, chunk in enumerate(chunks):
                if (
                    chunk.content_checksum
                    and chunk.content_checksum in cached_embeddings
                    and len(cached_embeddings[chunk.content_checksum]) == self.embedder.dimension
                ):
                    # Carry embedding forward without re-embedding (R9.9)
                    final_embeddings[idx] = cached_embeddings[chunk.content_checksum]
                    carried_forward_count += 1
                else:
                    to_embed_indices.append(idx)
                    to_embed_texts.append(chunk.content)

            # Embed only new or modified chunks
            if to_embed_texts:
                emb_res = await self.embedder.embed_texts(to_embed_texts)
                for idx, vec in zip(to_embed_indices, emb_res.embeddings, strict=True):
                    final_embeddings[idx] = list(vec)

            # 6. Stage: persist chunks and embeddings in atomic transaction (R9.7)
            await self.store.persist_chunks_with_embeddings(
                chunks=chunks,
                embeddings=final_embeddings,
                model=self.embedder.model_name,
                dimension=self.embedder.dimension,
                organization_id=org_u,
            )

            # 7. Stage: atomic status flip & prune superseded version (R9.8, R9.10)
            await self.store.update_document_status(
                org_u,
                doc_u,
                status="active",
                version=target_version,
                failure_reason=None,
            )

            if prev_version is not None and prev_version != target_version:
                # Remove old version chunks now that target_version is active
                await self.store.delete_chunks_by_document(org_u, doc_u, version=prev_version)

            return IngestionResult(
                document_id=doc_u,
                organization_id=org_u,
                version=target_version,
                total_chunks=len(chunks),
                embedded_chunks=len(to_embed_texts),
                carried_forward_chunks=carried_forward_count,
                status="active",
            )

        except Exception as exc:
            error_msg = str(exc)
            logger.error(
                "Ingestion failed for doc %s (org %s): %s",
                doc_u,
                org_u,
                error_msg,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self.store.update_document_status(
                    org_u, doc_u, status="failed", failure_reason=error_msg
                )
            with contextlib.suppress(Exception):
                # Clean up any partial un-activated chunks from target version
                await self.store.delete_chunks_by_document(org_u, doc_u, version=target_version)
            raise

"""Knowledge document, chunk, and vector embedding persistence store.

Requirements:
- R9.7: Populate chunk tsvector at write time in the same transaction as chunk row.
- R5.6: Maintain GIN index over chunk content_tsv column.
- R5.7: Maintain HNSW index over embedding_record.embedding using vector_cosine_ops.
- R5.3: Mandatory organization_id predicate on every tenant-scoped query.
- specs/design.md §5.6 & §6.1: Knowledge document, chunk, and embedding schemas.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.knowledge import (
    EmbeddingRecord,
    KnowledgeChunk,
    KnowledgeDocument,
)

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _clean_pg_str(val: str | None) -> str | None:
    if val is None:
        return None
    return val.replace("\x00", "")


def _cosine_distance(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Calculate cosine distance (1.0 - cosine_similarity) between two vectors."""
    dot = 0.0
    norm1 = 0.0
    norm2 = 0.0
    for a, b in zip(v1, v2, strict=False):
        dot += a * b
        norm1 += a * a
        norm2 += b * b
    if norm1 == 0.0 or norm2 == 0.0:
        return 1.0
    sim = dot / (math.sqrt(norm1) * math.sqrt(norm2))
    return max(0.0, min(2.0, 1.0 - sim))


def _parse_embedding(raw: Any) -> list[float]:
    """Parse raw asyncpg/pgvector embedding column into a Python list of floats."""
    if raw is None:
        return []
    if hasattr(raw, "to_list"):
        return [float(x) for x in raw.to_list()]
    if hasattr(raw, "tolist"):
        return [float(x) for x in raw.tolist()]
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    return []


@runtime_checkable
class KnowledgeStore(Protocol):
    """Protocol defining knowledge document, chunk, and vector persistence operations."""

    async def insert_document(
        self,
        document: KnowledgeDocument,
    ) -> KnowledgeDocument:
        """Insert or update a knowledge document record."""
        ...

    async def get_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
    ) -> KnowledgeDocument | None:
        """Retrieve a knowledge document by ID within an organization (R5.3)."""
        ...

    async def update_document_status(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        status: str,
        version: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Update document status, optional version and failure reason (R9.1, R9.10)."""
        ...

    async def persist_chunks_with_embeddings(
        self,
        chunks: Sequence[KnowledgeChunk],
        embeddings: Sequence[Sequence[float]],
        model: str,
        dimension: int,
        organization_id: UUID | str,
    ) -> int:
        """Persist chunks and embeddings in an atomic transaction (R9.7, R5.6, R5.7, R5.3).

        Populates content_tsv at write time. If any chunk or embedding write fails,
        the entire transaction rolls back cleanly.
        Returns the number of persisted chunks.
        """
        ...

    async def get_chunk(
        self,
        organization_id: UUID | str,
        chunk_id: UUID | str,
    ) -> KnowledgeChunk | None:
        """Retrieve a chunk by ID within an organization (R5.3)."""
        ...

    async def get_chunk_with_embedding(
        self,
        organization_id: UUID | str,
        chunk_id: UUID | str,
    ) -> tuple[KnowledgeChunk, list[float]] | None:
        """Retrieve chunk and its vector embedding within an organization (R5.3)."""
        ...

    async def get_chunks_by_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int | None = None,
    ) -> list[KnowledgeChunk]:
        """Retrieve all chunks belonging to a document (optionally filtered by version) (R5.3)."""
        ...

    async def delete_chunks_by_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int | None = None,
    ) -> int:
        """Delete chunks belonging to a document (cascades to embedding_record) (R5.3)."""
        ...

    async def search_chunks_lexical(
        self,
        organization_id: UUID | str,
        query: str,
        limit: int = 20,
        category: str | None = None,
    ) -> list[KnowledgeChunk]:
        """Full-text search over chunk content_tsv using GIN index (R5.6, R5.3)."""
        ...

    async def search_chunks_vector(
        self,
        organization_id: UUID | str,
        query_vector: Sequence[float],
        limit: int = 20,
        category: str | None = None,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """HNSW cosine similarity vector search over embedding_record (R5.7, R5.3).

        Returns (chunk, cosine_distance) sorted by lowest distance.
        """
        ...


class InMemoryKnowledgeStore:
    """In-memory knowledge store for unit tests and isolated mocking."""

    def __init__(self) -> None:
        # Key: (organization_id, document_id) -> KnowledgeDocument
        self.documents: dict[tuple[UUID, UUID], KnowledgeDocument] = {}
        # Key: (organization_id, chunk_id) -> KnowledgeChunk
        self.chunks: dict[tuple[UUID, UUID], KnowledgeChunk] = {}
        # Key: (organization_id, chunk_id) -> EmbeddingRecord
        self.embeddings: dict[tuple[UUID, UUID], EmbeddingRecord] = {}

    async def insert_document(
        self,
        document: KnowledgeDocument,
    ) -> KnowledgeDocument:
        org_u = _to_uuid(document.organization_id)
        doc_u = _to_uuid(document.id)
        doc_copy = KnowledgeDocument(
            id=doc_u,
            organization_id=org_u,
            title=document.title,
            source_uri=document.source_uri,
            mime_type=document.mime_type,
            category=document.category,
            version=document.version,
            checksum=document.checksum,
            object_key=document.object_key,
            status=document.status,
            failure_reason=document.failure_reason,
            created_at=document.created_at,
            updated_at=datetime.now(UTC),
        )
        self.documents[(org_u, doc_u)] = doc_copy
        return doc_copy

    async def get_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
    ) -> KnowledgeDocument | None:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)
        return self.documents.get((org_u, doc_u))

    async def update_document_status(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        status: str,
        version: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)
        doc = self.documents.get((org_u, doc_u))
        if doc:
            doc.status = status
            if version is not None:
                doc.version = version
            doc.failure_reason = failure_reason
            doc.updated_at = datetime.now(UTC)

    async def persist_chunks_with_embeddings(
        self,
        chunks: Sequence[KnowledgeChunk],
        embeddings: Sequence[Sequence[float]],
        model: str,
        dimension: int,
        organization_id: UUID | str,
    ) -> int:
        org_u = _to_uuid(organization_id)
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Chunks count ({len(chunks)}) does not match embeddings count ({len(embeddings)})"
            )

        # Pre-validate all dimensions before committing (atomic simulation)
        for i, emb in enumerate(embeddings):
            if len(emb) != dimension:
                raise ValueError(
                    f"Embedding at index {i} dimension mismatch: "
                    f"expected {dimension}, got {len(emb)}"
                )

        now = datetime.now(UTC)
        for chunk, emb in zip(chunks, embeddings, strict=True):
            chunk_u = _to_uuid(chunk.id)
            doc_u = _to_uuid(chunk.document_id)

            chunk_copy = KnowledgeChunk(
                id=chunk_u,
                document_id=doc_u,
                organization_id=org_u,
                chunk_index=chunk.chunk_index,
                external_id=chunk.external_id,
                heading_path=list(chunk.heading_path),
                section=chunk.section,
                category=chunk.category,
                content=chunk.content,
                token_count=chunk.token_count,
                content_checksum=chunk.content_checksum,
                metadata=dict(chunk.metadata),
                version=chunk.version,
            )
            emb_record = EmbeddingRecord(
                chunk_id=chunk_u,
                organization_id=org_u,
                model=model,
                dim=dimension,
                embedding=list(emb),
                created_at=now,
            )

            self.chunks[(org_u, chunk_u)] = chunk_copy
            self.embeddings[(org_u, chunk_u)] = emb_record

        return len(chunks)

    async def get_chunk(
        self,
        organization_id: UUID | str,
        chunk_id: UUID | str,
    ) -> KnowledgeChunk | None:
        org_u = _to_uuid(organization_id)
        chunk_u = _to_uuid(chunk_id)
        return self.chunks.get((org_u, chunk_u))

    async def get_chunk_with_embedding(
        self,
        organization_id: UUID | str,
        chunk_id: UUID | str,
    ) -> tuple[KnowledgeChunk, list[float]] | None:
        org_u = _to_uuid(organization_id)
        chunk_u = _to_uuid(chunk_id)
        chunk = self.chunks.get((org_u, chunk_u))
        emb_rec = self.embeddings.get((org_u, chunk_u))
        if chunk and emb_rec:
            return (chunk, list(emb_rec.embedding))
        return None

    async def get_chunks_by_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int | None = None,
    ) -> list[KnowledgeChunk]:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)
        results = [
            c
            for c in self.chunks.values()
            if c.organization_id == org_u
            and c.document_id == doc_u
            and (version is None or c.version == version)
        ]
        results.sort(key=lambda c: c.chunk_index)
        return results

    async def delete_chunks_by_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int | None = None,
    ) -> int:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)
        to_delete = [
            k
            for k, c in self.chunks.items()
            if k[0] == org_u
            and c.document_id == doc_u
            and (version is None or c.version == version)
        ]
        for k in to_delete:
            del self.chunks[k]
            if k in self.embeddings:
                del self.embeddings[k]
        return len(to_delete)

    async def search_chunks_lexical(
        self,
        organization_id: UUID | str,
        query: str,
        limit: int = 20,
        category: str | None = None,
    ) -> list[KnowledgeChunk]:
        org_u = _to_uuid(organization_id)
        tokens = [q.lower().strip() for q in query.split() if q.strip()]
        if not tokens:
            return []

        scored: list[tuple[int, KnowledgeChunk]] = []
        for c in self.chunks.values():
            if c.organization_id != org_u:
                continue
            if category and c.category != category:
                continue
            haystack = f"{c.section or ''} {c.content}".lower()
            score = sum(1 for t in tokens if t in haystack)
            if score > 0:
                scored.append((score, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]

    async def search_chunks_vector(
        self,
        organization_id: UUID | str,
        query_vector: Sequence[float],
        limit: int = 20,
        category: str | None = None,
    ) -> list[tuple[KnowledgeChunk, float]]:
        org_u = _to_uuid(organization_id)
        scored: list[tuple[KnowledgeChunk, float]] = []

        for (o_id, c_id), emb_rec in self.embeddings.items():
            if o_id != org_u:
                continue
            chunk = self.chunks.get((o_id, c_id))
            if not chunk:
                continue
            if category and chunk.category != category:
                continue
            dist = _cosine_distance(query_vector, emb_rec.embedding)
            scored.append((chunk, dist))

        scored.sort(key=lambda x: x[1])
        return scored[:limit]


class PostgresKnowledgeStore:
    """PostgreSQL implementation of KnowledgeStore with GIN tsvector and HNSW vector indexing."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    def _row_to_doc(self, row: asyncpg.Record) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=_to_uuid(row["id"]),
            organization_id=_to_uuid(row["organization_id"]),
            title=row["title"],
            source_uri=row["source_uri"],
            mime_type=row["mime_type"],
            category=row["category"],
            version=int(row["version"]),
            checksum=row["checksum"],
            object_key=row["object_key"],
            status=row["status"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_chunk(self, row: asyncpg.Record) -> KnowledgeChunk:
        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        elif not isinstance(metadata, dict):
            metadata = {}

        return KnowledgeChunk(
            id=_to_uuid(row["id"]),
            document_id=_to_uuid(row["document_id"]),
            organization_id=_to_uuid(row["organization_id"]),
            chunk_index=int(row["chunk_index"]),
            external_id=row["external_id"],
            heading_path=list(row["heading_path"]) if row["heading_path"] is not None else [],
            section=row["section"],
            category=row["category"],
            content=row["content"] or "",
            token_count=int(row["token_count"]) if row["token_count"] is not None else 0,
            content_checksum=row["content_checksum"] or "",
            metadata=metadata,
            version=int(row["version"]),
        )

    async def insert_document(
        self,
        document: KnowledgeDocument,
    ) -> KnowledgeDocument:
        org_u = _to_uuid(document.organization_id)
        doc_u = _to_uuid(document.id)

        query = """
            INSERT INTO knowledge_document (
                id, organization_id, title, source_uri, mime_type,
                category, version, checksum, object_key, status,
                failure_reason, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12, $13
            )
            ON CONFLICT (id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                title = EXCLUDED.title,
                source_uri = EXCLUDED.source_uri,
                mime_type = EXCLUDED.mime_type,
                category = EXCLUDED.category,
                version = EXCLUDED.version,
                checksum = EXCLUDED.checksum,
                object_key = EXCLUDED.object_key,
                status = EXCLUDED.status,
                failure_reason = EXCLUDED.failure_reason,
                updated_at = EXCLUDED.updated_at
            RETURNING *;
        """
        now = datetime.now(UTC)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                doc_u,
                org_u,
                _clean_pg_str(document.title),
                document.source_uri,
                document.mime_type,
                document.category,
                document.version,
                document.checksum,
                document.object_key,
                document.status,
                document.failure_reason,
                document.created_at,
                now,
            )
            if not row:
                raise RuntimeError("Failed to insert or update knowledge document")
            return self._row_to_doc(row)

    async def get_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
    ) -> KnowledgeDocument | None:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)

        query = """
            SELECT * FROM knowledge_document
            WHERE id = $1 AND organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, doc_u, org_u)
            if row:
                return self._row_to_doc(row)
            return None

    async def update_document_status(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        status: str,
        version: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)

        query = """
            UPDATE knowledge_document
            SET status = $3,
                version = COALESCE($4, version),
                failure_reason = $5,
                updated_at = now()
            WHERE id = $1 AND organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, doc_u, org_u, status, version, failure_reason)

    async def persist_chunks_with_embeddings(
        self,
        chunks: Sequence[KnowledgeChunk],
        embeddings: Sequence[Sequence[float]],
        model: str,
        dimension: int,
        organization_id: UUID | str,
    ) -> int:
        """Persist chunks and embeddings in an atomic transaction (R9.7, R5.6, R5.7, R5.3).

        If any error occurs, conn.transaction() rolls back all chunk and embedding rows.
        """
        org_u = _to_uuid(organization_id)
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Chunks count ({len(chunks)}) does not match embeddings count ({len(embeddings)})"
            )

        # Pre-validate embedding dimensions
        for i, emb in enumerate(embeddings):
            if len(emb) != dimension:
                raise ValueError(
                    f"Embedding at index {i} dimension mismatch: "
                    f"expected {dimension}, got {len(emb)}"
                )

        chunk_query = """
            INSERT INTO knowledge_chunk (
                id, document_id, organization_id, chunk_index, external_id,
                heading_path, section, category, content, token_count,
                content_checksum, metadata, version, content_tsv
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12::jsonb, $13,
                setweight(to_tsvector('english', coalesce($7, '')), 'A')
                || setweight(to_tsvector('english', $9), 'B')
            )
            ON CONFLICT (id) DO UPDATE SET
                document_id = EXCLUDED.document_id,
                organization_id = EXCLUDED.organization_id,
                chunk_index = EXCLUDED.chunk_index,
                external_id = EXCLUDED.external_id,
                heading_path = EXCLUDED.heading_path,
                section = EXCLUDED.section,
                category = EXCLUDED.category,
                content = EXCLUDED.content,
                token_count = EXCLUDED.token_count,
                content_checksum = EXCLUDED.content_checksum,
                metadata = EXCLUDED.metadata,
                version = EXCLUDED.version,
                content_tsv = setweight(to_tsvector('english', coalesce(EXCLUDED.section, '')), 'A')
                              || setweight(to_tsvector('english', EXCLUDED.content), 'B');
        """

        embedding_query = """
            INSERT INTO embedding_record (
                chunk_id, organization_id, model, dim, embedding, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, now()
            )
            ON CONFLICT (chunk_id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                model = EXCLUDED.model,
                dim = EXCLUDED.dim,
                embedding = EXCLUDED.embedding;
        """

        async with self.pool.acquire() as conn, conn.transaction():
            for chunk, emb in zip(chunks, embeddings, strict=True):
                chunk_u = _to_uuid(chunk.id)
                doc_u = _to_uuid(chunk.document_id)
                heading_path = list(chunk.heading_path) if chunk.heading_path else []
                metadata_json = json.dumps(chunk.metadata or {})

                # 1. Insert chunk with generated tsvector (R9.7, R5.6)
                await conn.execute(
                    chunk_query,
                    chunk_u,
                    doc_u,
                    org_u,
                    chunk.chunk_index,
                    chunk.external_id,
                    heading_path,
                    _clean_pg_str(chunk.section),
                    chunk.category,
                    _clean_pg_str(chunk.content),
                    chunk.token_count,
                    chunk.content_checksum,
                    metadata_json,
                    chunk.version,
                )

                # 2. Insert embedding vector record (R5.7)
                await conn.execute(
                    embedding_query,
                    chunk_u,
                    org_u,
                    model,
                    dimension,
                    list(emb),
                )

        return len(chunks)

    async def get_chunk(
        self,
        organization_id: UUID | str,
        chunk_id: UUID | str,
    ) -> KnowledgeChunk | None:
        org_u = _to_uuid(organization_id)
        chunk_u = _to_uuid(chunk_id)

        query = """
            SELECT * FROM knowledge_chunk
            WHERE id = $1 AND organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, chunk_u, org_u)
            if row:
                return self._row_to_chunk(row)
            return None

    async def get_chunk_with_embedding(
        self,
        organization_id: UUID | str,
        chunk_id: UUID | str,
    ) -> tuple[KnowledgeChunk, list[float]] | None:
        org_u = _to_uuid(organization_id)
        chunk_u = _to_uuid(chunk_id)

        query = """
            SELECT c.*, e.embedding
            FROM knowledge_chunk c
            JOIN embedding_record e ON c.id = e.chunk_id
            WHERE c.id = $1 AND c.organization_id = $2 AND e.organization_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, chunk_u, org_u)
            if row:
                chunk = self._row_to_chunk(row)
                emb_list = _parse_embedding(row["embedding"])
                return (chunk, emb_list)
            return None

    async def get_chunks_by_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int | None = None,
    ) -> list[KnowledgeChunk]:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)

        params: tuple[Any, ...]
        if version is not None:
            query = """
                SELECT * FROM knowledge_chunk
                WHERE document_id = $1 AND organization_id = $2 AND version = $3
                ORDER BY chunk_index ASC;
            """
            params = (doc_u, org_u, version)
        else:
            query = """
                SELECT * FROM knowledge_chunk
                WHERE document_id = $1 AND organization_id = $2
                ORDER BY chunk_index ASC;
            """
            params = (doc_u, org_u)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_chunk(r) for r in rows]

    async def delete_chunks_by_document(
        self,
        organization_id: UUID | str,
        document_id: UUID | str,
        version: int | None = None,
    ) -> int:
        org_u = _to_uuid(organization_id)
        doc_u = _to_uuid(document_id)

        params: tuple[Any, ...]
        if version is not None:
            query = """
                DELETE FROM knowledge_chunk
                WHERE document_id = $1 AND organization_id = $2 AND version = $3;
            """
            params = (doc_u, org_u, version)
        else:
            query = """
                DELETE FROM knowledge_chunk
                WHERE document_id = $1 AND organization_id = $2;
            """
            params = (doc_u, org_u)

        async with self.pool.acquire() as conn:
            res = await conn.execute(query, *params)
            # res is e.g. "DELETE 5"
            parts = res.split()
            return int(parts[-1]) if parts and parts[-1].isdigit() else 0

    async def search_chunks_lexical(
        self,
        organization_id: UUID | str,
        query: str,
        limit: int = 20,
        category: str | None = None,
    ) -> list[KnowledgeChunk]:
        org_u = _to_uuid(organization_id)

        params: tuple[Any, ...]
        if category:
            sql = """
                SELECT * FROM knowledge_chunk
                WHERE organization_id = $1
                  AND category = $2
                  AND content_tsv @@ plainto_tsquery('english', $3)
                ORDER BY ts_rank(content_tsv, plainto_tsquery('english', $3)) DESC
                LIMIT $4;
            """
            params = (org_u, category, query, limit)
        else:
            sql = """
                SELECT * FROM knowledge_chunk
                WHERE organization_id = $1
                  AND content_tsv @@ plainto_tsquery('english', $2)
                ORDER BY ts_rank(content_tsv, plainto_tsquery('english', $2)) DESC
                LIMIT $3;
            """
            params = (org_u, query, limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [self._row_to_chunk(r) for r in rows]

    async def search_chunks_vector(
        self,
        organization_id: UUID | str,
        query_vector: Sequence[float],
        limit: int = 20,
        category: str | None = None,
    ) -> list[tuple[KnowledgeChunk, float]]:
        org_u = _to_uuid(organization_id)

        params: tuple[Any, ...]
        if category:
            sql = """
                SELECT c.*, (e.embedding <=> $3) AS distance
                FROM knowledge_chunk c
                JOIN embedding_record e ON c.id = e.chunk_id
                WHERE c.organization_id = $1
                  AND e.organization_id = $1
                  AND c.category = $2
                ORDER BY e.embedding <=> $3 ASC
                LIMIT $4;
            """
            params = (org_u, category, list(query_vector), limit)
        else:
            sql = """
                SELECT c.*, (e.embedding <=> $2) AS distance
                FROM knowledge_chunk c
                JOIN embedding_record e ON c.id = e.chunk_id
                WHERE c.organization_id = $1
                  AND e.organization_id = $1
                ORDER BY e.embedding <=> $2 ASC
                LIMIT $3;
            """
            params = (org_u, list(query_vector), limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            results: list[tuple[KnowledgeChunk, float]] = []
            for r in rows:
                chunk = self._row_to_chunk(r)
                dist = float(r["distance"])
                results.append((chunk, dist))
            return results

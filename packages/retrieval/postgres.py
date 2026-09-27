"""PostgreSQL-backed SearchBackend implementation.

Requirements:
- R10.1: Execute PostgreSQL FTS query and pgvector ANN query against chunk corpus.
- R10.2: Configurable top-N per branch, default 20.
- R10.4: Apply metadata filters (organization_id, category, document status) inside both branches.
- R10.7: SearchBackend interface abstraction.
- R10.8: Return candidate objects carrying ranks, scores, fused score, and metadata.
- R10.10: Filtered ANN under-fill mitigation (detect count < top_n,
  record retrieval_underfilled=true, export metric, widen ef_search and iterative_scan).
- specs/design.md §5.5: Hybrid search reference CTE SQL and HNSW walk tuning.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg

from packages.retrieval.models import Candidate, RetrievalQuery

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str | None) -> UUID | None:
    if val is None:
        return None
    return val if isinstance(val, UUID) else UUID(str(val))


class PostgresSearchBackend:
    """PostgreSQL and pgvector implementation of the SearchBackend protocol (R10.1, R10.7)."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        metrics: PipelineMetrics | None = None,
        default_top_n: int = 20,
        default_ef_search: int = 40,
        widened_ef_search: int = 200,
        dim: int = 1536,
    ) -> None:
        self.pool = pool
        self.metrics = metrics
        self.default_top_n = default_top_n
        self.default_ef_search = default_ef_search
        self.widened_ef_search = widened_ef_search
        self.dim = dim
        self.last_retrieval_underfilled: bool = False

    def _format_vector(self, vec: Sequence[float]) -> list[float]:
        """Ensure vector is a Python list of floats padded/truncated to self.dim."""
        v_list = [float(x) for x in vec]
        if len(v_list) < self.dim:
            v_list = v_list + [0.0] * (self.dim - len(v_list))
        elif len(v_list) > self.dim:
            v_list = v_list[: self.dim]
        return v_list

    def _parse_metadata(self, row: asyncpg.Record) -> dict[str, Any]:
        """Extract and merge structured source metadata from database row (R10.8)."""
        raw_meta = row.get("metadata")
        if isinstance(raw_meta, str):
            try:
                meta: dict[str, Any] = json.loads(raw_meta)
            except Exception:
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = dict(raw_meta)
        else:
            meta = {}

        if "heading_path" in row and row["heading_path"] is not None:
            meta["heading_path"] = list(row["heading_path"])
        if "section" in row and row["section"] is not None:
            meta["section"] = row["section"]
        if "category" in row and row["category"] is not None:
            meta["category"] = row["category"]
        if "external_id" in row and row["external_id"] is not None:
            meta["external_id"] = row["external_id"]

        return meta

    def _row_to_candidate(
        self,
        row: asyncpg.Record,
        *,
        lexical_only: bool = False,
        vector_only: bool = False,
    ) -> Candidate:
        """Map database record to Candidate domain object."""
        meta = self._parse_metadata(row)

        if lexical_only:
            lexical_rank = int(row["rnk"]) if row.get("rnk") is not None else None
            lexical_score = float(row["score"]) if row.get("score") is not None else None
            vector_rank = None
            vector_score = None
            fused_score = None
        elif vector_only:
            lexical_rank = None
            lexical_score = None
            vector_rank = int(row["rnk"]) if row.get("rnk") is not None else None
            vector_score = float(row["score"]) if row.get("score") is not None else None
            fused_score = None
        else:
            lexical_rank = int(row["lexical_rank"]) if row.get("lexical_rank") is not None else None
            vector_rank = int(row["vector_rank"]) if row.get("vector_rank") is not None else None
            lexical_score = (
                float(row["lexical_score"]) if row.get("lexical_score") is not None else None
            )
            vector_score = (
                float(row["vector_score"]) if row.get("vector_score") is not None else None
            )
            fused_score = float(row["fused_score"]) if row.get("fused_score") is not None else None

        return Candidate(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            content=str(row["content"]),
            metadata=meta,
            lexical_rank=lexical_rank,
            vector_rank=vector_rank,
            lexical_score=lexical_score,
            vector_score=vector_score,
            fused_score=fused_score,
            rerank_score=None,
        )

    async def lexical(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        """Execute PostgreSQL full-text search against chunk corpus (R10.1, R10.4)."""
        org_id = q.organization_id
        if not org_id:
            return []

        try:
            org_u = _to_uuid(org_id)
        except Exception:
            return []

        lex_text = q.lexical_text.strip()
        if not lex_text:
            return []

        limit_val = max(1, top_n or self.default_top_n)
        target_status = q.status
        target_category = q.category

        query_sql = """
            SELECT c.id AS chunk_id,
                   c.document_id,
                   c.content,
                   c.heading_path,
                   c.section,
                   c.category,
                   c.external_id,
                   c.metadata,
                   ts_rank_cd(c.content_tsv, q.query) AS score,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.query) DESC) AS rnk
            FROM knowledge_chunk c
            JOIN knowledge_document d ON d.id = c.document_id
            CROSS JOIN websearch_to_tsquery('english', $2) AS q(query)
            WHERE c.organization_id = $1
              AND d.status = $3
              AND ($4::text IS NULL OR d.category = $4)
              AND c.content_tsv @@ q.query
            ORDER BY rnk
            LIMIT $5;
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                query_sql,
                org_u,
                lex_text,
                target_status,
                target_category,
                limit_val,
            )
            return [self._row_to_candidate(r, lexical_only=True) for r in rows]

    async def vector(self, q: RetrievalQuery, top_n: int = 20) -> list[Candidate]:
        """Execute pgvector similarity search with under-fill mitigation (R10.1, R10.4, R10.10)."""
        org_id = q.organization_id
        if not org_id or not q.query_vector:
            return []

        try:
            org_u = _to_uuid(org_id)
        except Exception:
            return []

        limit_val = max(1, top_n or self.default_top_n)
        target_status = q.status
        target_category = q.category
        vec_str = self._format_vector(q.query_vector)

        query_sql = """
            SELECT c.id AS chunk_id,
                   c.document_id,
                   c.content,
                   c.heading_path,
                   c.section,
                   c.category,
                   c.external_id,
                   c.metadata,
                   (1.0 - (e.embedding <=> $2)) AS score,
                   ROW_NUMBER() OVER (ORDER BY e.embedding <=> $2 ASC) AS rnk
            FROM embedding_record e
            JOIN knowledge_chunk c    ON c.id = e.chunk_id
            JOIN knowledge_document d ON d.id = c.document_id
            WHERE c.organization_id = $1
              AND d.status = $3
              AND ($4::text IS NULL OR d.category = $4)
            ORDER BY e.embedding <=> $2 ASC
            LIMIT $5;
        """

        count_sql = """
            SELECT COUNT(*)
            FROM embedding_record e
            JOIN knowledge_chunk c    ON c.id = e.chunk_id
            JOIN knowledge_document d ON d.id = c.document_id
            WHERE c.organization_id = $1
              AND d.status = $2
              AND ($3::text IS NULL OR d.category = $3);
        """

        async with self.pool.acquire() as conn:
            # Prime pgvector extension GUCs in connection if needed
            await conn.execute("SELECT '[0]'::vector;")

            rows = await conn.fetch(
                query_sql,
                org_u,
                vec_str,
                target_status,
                target_category,
                limit_val,
            )

            # Check for filtered-ANN under-fill (R10.10)
            if len(rows) < limit_val:
                total_available = await conn.fetchval(
                    count_sql,
                    org_u,
                    target_status,
                    target_category,
                )
                total_avail_int = int(total_available or 0)

                if total_avail_int > len(rows):
                    self.last_retrieval_underfilled = True
                    if self.metrics is not None:
                        try:
                            self.metrics.retrieval_underfilled_total.labels(
                                tenant=str(org_id)
                            ).inc()
                        except Exception as m_err:
                            logger.debug("Failed to increment underfilled metric: %s", m_err)

                    logger.warning(
                        "Filtered-ANN underfill for tenant %s: returned %d vs %d available "
                        "(top_n=%d). Widening search walk to ef_search=%d.",
                        org_id,
                        len(rows),
                        total_avail_int,
                        limit_val,
                        self.widened_ef_search,
                    )

                    # Widen walk and enable iterative scan (R10.10)
                    try:
                        await conn.execute(f"SET hnsw.ef_search = {self.widened_ef_search};")
                        await conn.execute("SET hnsw.iterative_scan = 'relaxed_order';")
                        rows = await conn.fetch(
                            query_sql,
                            org_u,
                            vec_str,
                            target_status,
                            target_category,
                            limit_val,
                        )
                    finally:
                        # Reset connection session parameters
                        try:
                            await conn.execute(f"SET hnsw.ef_search = {self.default_ef_search};")
                            await conn.execute("SET hnsw.iterative_scan = 'off';")
                        except Exception:
                            pass

            return [self._row_to_candidate(r, vector_only=True) for r in rows]

    async def hybrid(
        self,
        q: RetrievalQuery,
        top_n: int = 20,
        k: int = 60,
        fuse_limit: int = 20,
    ) -> list[Candidate]:
        """Execute hybrid search using reference CTE from specs/design.md §5.5."""
        org_id = q.organization_id
        if not org_id:
            return []

        try:
            org_u = _to_uuid(org_id)
        except Exception:
            return []

        lex_text = q.lexical_text.strip()
        has_lex = bool(lex_text)
        has_vec = bool(q.query_vector)

        if not has_lex and not has_vec:
            return []
        if not has_vec:
            return await self.lexical(q, top_n=fuse_limit)
        if not has_lex:
            return await self.vector(q, top_n=fuse_limit)

        branch_top_n = max(1, top_n or self.default_top_n)
        final_fuse_limit = max(1, fuse_limit or branch_top_n)
        target_status = q.status
        target_category = q.category
        vec_str = self._format_vector(q.query_vector or [])

        hybrid_sql = """
            WITH lexical AS (
              SELECT c.id AS chunk_id,
                     c.document_id,
                     c.content,
                     c.heading_path,
                     c.section,
                     c.category,
                     c.external_id,
                     c.metadata,
                     ts_rank_cd(c.content_tsv, q.query) AS score,
                     ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.query) DESC) AS rnk
              FROM knowledge_chunk c
              JOIN knowledge_document d ON d.id = c.document_id
              CROSS JOIN websearch_to_tsquery('english', $2) AS q(query)
              WHERE c.organization_id = $1
                AND d.status = $3
                AND ($4::text IS NULL OR d.category = $4)
                AND c.content_tsv @@ q.query
              ORDER BY rnk
              LIMIT $5
            ),
            vector AS (
              SELECT c.id AS chunk_id,
                     c.document_id,
                     c.content,
                     c.heading_path,
                     c.section,
                     c.category,
                     c.external_id,
                     c.metadata,
                     (1.0 - (e.embedding <=> $6)) AS score,
                     ROW_NUMBER() OVER (ORDER BY e.embedding <=> $6 ASC) AS rnk
              FROM embedding_record e
              JOIN knowledge_chunk c    ON c.id = e.chunk_id
              JOIN knowledge_document d ON d.id = c.document_id
              WHERE c.organization_id = $1
                AND d.status = $3
                AND ($4::text IS NULL OR d.category = $4)
              ORDER BY e.embedding <=> $6 ASC
              LIMIT $5
            )
            SELECT COALESCE(l.chunk_id, v.chunk_id) AS chunk_id,
                   COALESCE(l.document_id, v.document_id) AS document_id,
                   COALESCE(l.content, v.content) AS content,
                   COALESCE(l.heading_path, v.heading_path) AS heading_path,
                   COALESCE(l.section, v.section) AS section,
                   COALESCE(l.category, v.category) AS category,
                   COALESCE(l.external_id, v.external_id) AS external_id,
                   COALESCE(l.metadata, v.metadata) AS metadata,
                   l.rnk AS lexical_rank,
                   v.rnk AS vector_rank,
                   l.score AS lexical_score,
                   v.score AS vector_score,
                   (COALESCE(1.0 / ($7 + l.rnk), 0.0) +
                    COALESCE(1.0 / ($7 + v.rnk), 0.0)) AS fused_score
            FROM lexical l
            FULL OUTER JOIN vector v USING (chunk_id)
            ORDER BY fused_score DESC
            LIMIT $8;
        """

        async with self.pool.acquire() as conn:
            await conn.execute("SELECT '[0]'::vector;")
            rows = await conn.fetch(
                hybrid_sql,
                org_u,
                lex_text,
                target_status,
                target_category,
                branch_top_n,
                vec_str,
                k,
                final_fuse_limit,
            )
            return [self._row_to_candidate(r) for r in rows]

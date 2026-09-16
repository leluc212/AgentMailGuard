"""PostgreSQL idempotency backend backed by processing_job UNIQUE constraint (R19.1–R19.4).

Queries and records completion state in the processing_job table using asyncpg connection pool.
"""

import json
import logging
import uuid
from typing import Any

import asyncpg

from packages.core.idempotency import (
    IdempotencyBackend,
    IdempotencyConflictError,
    IdempotencyRecord,
)

logger = logging.getLogger(__name__)


class PostgresIdempotencyBackend(IdempotencyBackend):
    """PostgreSQL storage backend leveraging processing_job.idempotency_key UNIQUE constraint."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def find_completed(self, key: str) -> IdempotencyRecord | None:
        """Query for an already completed operation in processing_job (R19.3).

        Parameters
        ----------
        key : str
            Deterministic idempotency key.

        Returns
        -------
        IdempotencyRecord | None
            Prior result record if present and in COMPLETED state, else None.
        """
        query = """
            SELECT id, state, idempotency_key, result_ref, created_at
            FROM processing_job
            WHERE idempotency_key = $1 AND state = 'COMPLETED';
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, key)
            if row is None:
                return None

            raw_result = row["result_ref"]
            if isinstance(raw_result, str):
                try:
                    deserialized_result = json.loads(raw_result)
                except Exception:
                    deserialized_result = raw_result
            else:
                deserialized_result = raw_result

            return IdempotencyRecord(
                key=row["idempotency_key"],
                result=deserialized_result,
                state=row["state"],
                created_at=row["created_at"],
            )

    async def mark_completed(
        self,
        key: str,
        result: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record completed result in processing_job, raising on concurrent race (R19.4).

        Parameters
        ----------
        key : str
            Deterministic idempotency key.
        result : Any
            Result to serialize into result_ref JSONB column.
        metadata : dict[str, Any] | None
            Optional metadata (organization_id, job_type, message_id, thread_id).
        """
        meta = metadata or {}
        org_id_raw = meta.get("organization_id")
        org_id = uuid.UUID(str(org_id_raw)) if org_id_raw else uuid.uuid4()

        job_id_raw = meta.get("job_id")
        job_id = uuid.UUID(str(job_id_raw)) if job_id_raw else uuid.uuid4()

        job_type = meta.get("job_type", "generic")

        msg_id_raw = meta.get("message_id")
        msg_id = uuid.UUID(str(msg_id_raw)) if msg_id_raw else None

        thd_id_raw = meta.get("thread_id")
        thd_id = uuid.UUID(str(thd_id_raw)) if thd_id_raw else None

        result_json = json.dumps(result) if not isinstance(result, str) else result

        insert_query = """
            INSERT INTO processing_job (
                id, organization_id, job_type, state, idempotency_key, result_ref,
                message_id, thread_id, created_at, updated_at
            ) VALUES (
                $1, $2, $3, 'COMPLETED', $4, $5::jsonb, $6, $7, now(), now()
            );
        """

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    insert_query,
                    job_id,
                    org_id,
                    job_type,
                    key,
                    result_json,
                    msg_id,
                    thd_id,
                )
                logger.debug("Idempotency key %s marked COMPLETED in processing_job", key)
        except asyncpg.UniqueViolationError as err:
            logger.warning("Database UNIQUE constraint triggered for idempotency key %s", key)
            raise IdempotencyConflictError(
                f"PostgreSQL duplicate idempotency key violation: {key}"
            ) from err

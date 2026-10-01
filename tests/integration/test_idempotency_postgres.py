"""Integration tests for PostgreSQL idempotency backend and execute_once helper (R19.1–R19.4).

Verifies real database UNIQUE constraint enforcement on processing_job table against
the live PostgreSQL container on localhost:5433.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.idempotency import derive_idempotency_key, execute_once
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.idempotency import PostgresIdempotencyBackend


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide a dedicated asyncpg connection pool connected to the test database."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def ensure_test_org(pool: asyncpg.Pool, org_id: uuid.UUID) -> None:
    """Ensure a parent organization row exists to satisfy foreign key constraints."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


@pytest.mark.asyncio
async def test_postgres_idempotency_mark_and_find(db_pool: asyncpg.Pool) -> None:
    """Verify mark_completed and find_completed against processing_job table (R19.3, R19.4)."""
    backend = PostgresIdempotencyBackend(db_pool)
    org_id = uuid.uuid4()
    await ensure_test_org(db_pool, org_id)

    key = derive_idempotency_key(org_id, uuid.uuid4(), "msg_001", "normalize")

    # Initial query should return None
    assert await backend.find_completed(key) is None

    # Mark completed with structured dict result
    test_result = {"status": "normalized", "chunks_count": 4}
    await backend.mark_completed(
        key,
        result=test_result,
        metadata={"organization_id": org_id, "job_type": "normalize"},
    )

    # Query should now return the completed record
    record = await backend.find_completed(key)
    assert record is not None
    assert record.key == key
    assert record.state == "COMPLETED"
    assert record.result == test_result


@pytest.mark.asyncio
async def test_postgres_execute_once_short_circuit(db_pool: asyncpg.Pool) -> None:
    """Verify execute_once short-circuits via DB without running side-effects (R19.1, R19.3)."""
    backend = PostgresIdempotencyBackend(db_pool)
    org_id = uuid.uuid4()
    await ensure_test_org(db_pool, org_id)

    key = derive_idempotency_key(org_id, uuid.uuid4(), "msg_002", "triage")
    op_calls = 0

    async def sample_op() -> dict[str, str]:
        nonlocal op_calls
        op_calls += 1
        return {"category": "billing", "priority": "high"}

    metadata = {"organization_id": org_id, "job_type": "triage"}

    # Run 1: Executes op and writes to DB
    res1 = await execute_once(key, sample_op, backend, metadata=metadata)
    assert res1 == {"category": "billing", "priority": "high"}
    assert op_calls == 1

    # Run 2: Fast-path short-circuit from DB
    res2 = await execute_once(key, sample_op, backend, metadata=metadata)
    assert res2 == {"category": "billing", "priority": "high"}
    assert op_calls == 1  # Not called again


@pytest.mark.asyncio
async def test_postgres_concurrent_race_integrity_error(db_pool: asyncpg.Pool) -> None:
    """Verify concurrent races hit PostgreSQL UNIQUE constraint and resolve cleanly (R19.4)."""
    backend = PostgresIdempotencyBackend(db_pool)
    org_id = uuid.uuid4()
    await ensure_test_org(db_pool, org_id)

    key = derive_idempotency_key(org_id, uuid.uuid4(), "msg_race", "dispatch")
    metadata = {"organization_id": org_id, "job_type": "dispatch"}

    call_count = 0

    async def racing_op() -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        # Introduce overlapping async delay to ensure race window
        await asyncio.sleep(0.05)
        return {"status": "sent", "execution_num": str(call_count)}

    # Launch 10 concurrent coroutines targeting the exact same idempotency key
    tasks = [
        execute_once(
            key, racing_op, backend, poll_attempts=15, poll_delay_s=0.03, metadata=metadata
        )
        for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)

    # All 10 coroutines must return identical results from the single winning commit
    winner_result = results[0]
    for r in results:
        assert r == winner_result

    # Assert PostgreSQL has exactly ONE row in processing_job for this idempotency key
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM processing_job WHERE idempotency_key = $1", key
        )
        assert count == 1


@pytest.mark.asyncio
async def test_postgres_failed_op_leaves_no_record(db_pool: asyncpg.Pool) -> None:
    """Verify operations that raise exceptions leave no record in PostgreSQL (R19.5)."""
    backend = PostgresIdempotencyBackend(db_pool)
    org_id = uuid.uuid4()
    await ensure_test_org(db_pool, org_id)

    key = derive_idempotency_key(org_id, uuid.uuid4(), "msg_fail", "summarize")
    metadata = {"organization_id": org_id, "job_type": "summarize"}

    async def failing_op() -> str:
        raise ValueError("Model inference rate limit exceeded")

    with pytest.raises(ValueError, match="Model inference rate limit exceeded"):
        await execute_once(key, failing_op, backend, metadata=metadata)

    # Verify no record exists in DB
    assert await backend.find_completed(key) is None
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM processing_job WHERE idempotency_key = $1", key
        )
        assert count == 0

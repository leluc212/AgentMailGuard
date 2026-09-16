"""Unit tests for idempotency key derivation and execute_once helper (R19.1–R19.4)."""

import asyncio
import uuid

import pytest

from packages.core.idempotency import (
    IdempotencyConflictError,
    InMemoryIdempotencyBackend,
    derive_idempotency_key,
    execute_once,
)


def test_derive_idempotency_key_determinism() -> None:
    """Verify deterministic SHA-256 key derivation across inputs (R19.2, design.md §9)."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = "msg_12345"
    op_type = "generate_reply"

    key1 = derive_idempotency_key(org_id, mbx_id, msg_id, op_type)
    key2 = derive_idempotency_key(org_id, mbx_id, msg_id, op_type)
    key3 = derive_idempotency_key(str(org_id), str(mbx_id), msg_id, op_type)

    assert len(key1) == 64
    assert key1 == key2
    assert key1 == key3  # UUID and string representations produce identical keys

    # Changing any component changes the key
    diff_op = derive_idempotency_key(org_id, mbx_id, msg_id, "triage")
    diff_msg = derive_idempotency_key(org_id, mbx_id, "msg_67890", op_type)
    diff_org = derive_idempotency_key(uuid.uuid4(), mbx_id, msg_id, op_type)

    assert key1 != diff_op
    assert key1 != diff_msg
    assert key1 != diff_org


@pytest.mark.asyncio
async def test_repeat_execution_short_circuit() -> None:
    """Verify second invocation short-circuits and skips side-effect execution (R19.1, R19.3)."""
    backend = InMemoryIdempotencyBackend()
    key = derive_idempotency_key("org_1", "mbx_1", "msg_1", "normalize")

    op_call_count = 0

    async def side_effect_operation() -> dict[str, str]:
        nonlocal op_call_count
        op_call_count += 1
        return {"draft_id": "draft_abc123", "status": "created"}

    # First execution: runs operation and persists result
    result1 = await execute_once(key, side_effect_operation, backend)
    assert result1 == {"draft_id": "draft_abc123", "status": "created"}
    assert op_call_count == 1

    # Second execution: short-circuits and returns prior result without running operation
    result2 = await execute_once(key, side_effect_operation, backend)
    assert result2 == {"draft_id": "draft_abc123", "status": "created"}
    assert op_call_count == 1  # Not called again


@pytest.mark.asyncio
async def test_concurrent_execution_race() -> None:
    """Verify concurrent races resolve cleanly with single winner and no errors (R19.4)."""
    backend = InMemoryIdempotencyBackend()
    key = derive_idempotency_key("org_1", "mbx_1", "msg_concurrent", "dispatch")

    op_executions = 0

    async def concurrent_op() -> dict[str, str]:
        nonlocal op_executions
        op_executions += 1
        # Overlapping concurrency
        await asyncio.sleep(0.02)
        return {"dispatch_status": "sent", "execution_id": f"exec_{op_executions}"}

    # Run 10 concurrent coroutines attempting the same idempotency key
    tasks = [
        execute_once(key, concurrent_op, backend, poll_attempts=10, poll_delay_s=0.01)
        for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)

    # All 10 coroutines must return the exact same winner's result
    winner_result = results[0]
    for r in results:
        assert r == winner_result

    # Underlying record in backend must be COMPLETED
    record = await backend.find_completed(key)
    assert record is not None
    assert record.state == "COMPLETED"
    assert record.result == winner_result


@pytest.mark.asyncio
async def test_partial_failure_allows_retry() -> None:
    """Verify failed operations do not mark completion and allow subsequent retries (R19.5)."""
    backend = InMemoryIdempotencyBackend()
    key = derive_idempotency_key("org_1", "mbx_1", "msg_err", "triage")

    attempt = 0

    async def flaky_operation() -> str:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError("Transient network glitch")
        return "triage_complete"

    # Attempt 1 fails: exception bubbles, key not marked completed
    with pytest.raises(RuntimeError, match="Transient network glitch"):
        await execute_once(key, flaky_operation, backend)

    assert await backend.find_completed(key) is None

    # Attempt 2 succeeds: operation runs and is now marked completed
    result = await execute_once(key, flaky_operation, backend)
    assert result == "triage_complete"
    assert attempt == 2

    # Verify backend now has completed record
    record = await backend.find_completed(key)
    assert record is not None
    assert record.result == "triage_complete"


@pytest.mark.asyncio
async def test_in_memory_duplicate_rejection() -> None:
    """Verify InMemoryIdempotencyBackend directly raises IdempotencyConflictError on duplicate."""
    backend = InMemoryIdempotencyBackend()
    key = "test_key_123"

    await backend.mark_completed(key, {"data": 1})

    with pytest.raises(IdempotencyConflictError, match="Duplicate idempotency key"):
        await backend.mark_completed(key, {"data": 2})

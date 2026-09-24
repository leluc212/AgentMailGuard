"""Integration tests for PostgresThreadStateStore with live PostgreSQL container (R8.1, R8.6).

Verifies:
- CRUD operations with all schema attributes (topic, intent, summary, open_questions, resolved_items, etc.)
- Strict multi-tenant isolation across >= 3 tenants (GEMINI.md §8)
- Optimistic concurrency control detecting version collisions (R8.6)
- High-concurrency worker updates with zero lost updates
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.thread_state import (
    OptimisticLockError,
    PostgresThreadStateStore,
    ThreadStateNotFoundError,
)
from packages.domain.entities import ThreadState


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
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


async def ensure_test_mailbox(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    address: str = "test@example.com",
) -> None:
    await ensure_test_org(pool, org_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Test Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            address,
        )


async def ensure_test_thread(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    thread_id: uuid.UUID,
    subject: str = "Test Subject",
) -> None:
    await ensure_test_mailbox(pool, org_id, mbx_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_thread (id, organization_id, mailbox_id, subject_normalized, message_count, status)
            VALUES ($1, $2, $3, $4, 1, 'open')
            ON CONFLICT (id) DO NOTHING;
            """,
            thread_id,
            org_id,
            mbx_id,
            subject,
        )


@pytest.mark.asyncio
async def test_postgres_thread_state_crud(db_pool: asyncpg.Pool) -> None:
    """Verify full CRUD lifecycle and field persistence for thread_state (R8.1)."""
    store = PostgresThreadStateStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thread_id)

    # Initial state should not exist
    initial = await store.get(org_id, thread_id)
    assert initial is None

    # Create thread state
    state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Service Cancellation",
        current_intent="cancel_subscription",
        summary="Customer wants to cancel annual subscription due to relocation.",
        open_questions=["Confirm billing period end date?", "Offer pause alternative?"],
        resolved_items=["Identity confirmed"],
        token_estimate=150,
        version=1,
    )
    created = await store.create(state)
    assert created.thread_id == thread_id
    assert created.organization_id == org_id
    assert created.topic == "Service Cancellation"
    assert created.current_intent == "cancel_subscription"
    assert created.summary == "Customer wants to cancel annual subscription due to relocation."
    assert created.open_questions == [
        "Confirm billing period end date?",
        "Offer pause alternative?",
    ]
    assert created.resolved_items == ["Identity confirmed"]
    assert created.token_estimate == 150
    assert created.version == 1

    # Fetch persisted row
    fetched = await store.get(org_id, thread_id)
    assert fetched is not None
    assert fetched.version == 1
    assert fetched.open_questions == [
        "Confirm billing period end date?",
        "Offer pause alternative?",
    ]

    # Update state with expected_version
    fetched.summary = "Customer accepted 3-month account pause instead of cancellation."
    fetched.resolved_items.append("Retention pause offered and accepted")
    fetched.open_questions = []
    fetched.token_estimate = 180

    updated = await store.update(fetched)
    assert updated.version == 2
    assert updated.summary == "Customer accepted 3-month account pause instead of cancellation."
    assert len(updated.resolved_items) == 2
    assert updated.open_questions == []
    assert updated.token_estimate == 180

    # Verify re-fetching yields version 2
    refetched = await store.get(org_id, thread_id)
    assert refetched is not None
    assert refetched.version == 2


@pytest.mark.asyncio
async def test_postgres_thread_state_multi_tenant_isolation(db_pool: asyncpg.Pool) -> None:
    """Verify strict multi-tenant isolation across >= 3 tenants (GEMINI.md §8)."""
    store = PostgresThreadStateStore(db_pool)

    # Setup 3 distinct organizations
    orgs = [uuid.uuid4() for _ in range(3)]
    threads = [uuid.uuid4() for _ in range(3)]

    for org_id, th_id in zip(orgs, threads, strict=True):
        mbx_id = uuid.uuid4()
        await ensure_test_thread(db_pool, org_id, mbx_id, th_id)
        state = ThreadState(
            thread_id=th_id,
            organization_id=org_id,
            topic=f"Tenant Topic for {org_id.hex[:6]}",
            summary=f"Secret summary for org {org_id}",
            version=1,
        )
        await store.create(state)

    # Verify each tenant can ONLY access its own thread state
    for i, (org_id, th_id) in enumerate(zip(orgs, threads, strict=True)):
        own_state = await store.get(org_id, th_id)
        assert own_state is not None
        assert own_state.summary == f"Secret summary for org {org_id}"

        # Cross-tenant read attempts must return None
        for j, (other_org_id, other_th_id) in enumerate(zip(orgs, threads, strict=True)):
            if i != j:
                cross_read = await store.get(other_org_id, th_id)
                assert cross_read is None, f"Tenant {other_org_id} was able to read thread {th_id}!"


@pytest.mark.asyncio
async def test_postgres_thread_state_optimistic_conflict(db_pool: asyncpg.Pool) -> None:
    """Verify OptimisticLockError on version mismatch between concurrent workers (R8.6)."""
    store = PostgresThreadStateStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thread_id)

    # Initial state v1
    initial = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        summary="Base conversation state",
        version=1,
    )
    await store.create(initial)

    # Worker 1 and Worker 2 both read version 1
    worker_1 = await store.get(org_id, thread_id)
    worker_2 = await store.get(org_id, thread_id)
    assert worker_1 is not None and worker_2 is not None
    assert worker_1.version == 1
    assert worker_2.version == 1

    # Worker 1 commits version 2
    worker_1.summary = "Worker 1 update"
    updated_1 = await store.update(worker_1)
    assert updated_1.version == 2

    # Worker 2 attempts update with stale version 1 -> raises OptimisticLockError
    worker_2.summary = "Worker 2 update"
    with pytest.raises(OptimisticLockError) as exc_info:
        await store.update(worker_2)

    assert exc_info.value.thread_id == thread_id
    assert exc_info.value.expected_version == 1
    assert exc_info.value.actual_version == 2

    # Test update on non-existent thread raises ThreadStateNotFoundError
    non_existent = ThreadState(
        thread_id=uuid.uuid4(),
        organization_id=org_id,
        summary="Non existent",
        version=1,
    )
    with pytest.raises(ThreadStateNotFoundError):
        await store.update(non_existent)


@pytest.mark.asyncio
async def test_postgres_thread_state_concurrent_workers_zero_lost_updates(
    db_pool: asyncpg.Pool,
) -> None:
    """Verify 10 concurrent workers updating thread_state via retry loop lose zero updates (R8.6)."""
    store = PostgresThreadStateStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thread_id)

    initial = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        summary="Concurrent update baseline",
        resolved_items=[],
        version=1,
    )
    await store.create(initial)

    num_workers = 10

    async def worker_task(worker_id: str) -> None:
        max_retries = 50
        for _ in range(max_retries):
            current = await store.get(org_id, thread_id)
            assert current is not None

            # Mutate state by recording worker's unique entry
            current.resolved_items.append(worker_id)
            current.summary = f"Updated by {worker_id}"

            try:
                await store.update(current, expected_version=current.version)
                return
            except OptimisticLockError:
                # Retry with jitter
                await asyncio.sleep(0.02 * (hash(worker_id) % 5 + 1))

        pytest.fail(f"Worker {worker_id} failed to commit update after {max_retries} retries")

    # Run all workers concurrently
    await asyncio.gather(*(worker_task(f"worker-{i:02d}") for i in range(num_workers)))

    # Fetch final state
    final = await store.get(org_id, thread_id)
    assert final is not None

    # Version should have incremented exactly num_workers times (from 1 to 11)
    assert final.version == num_workers + 1

    # Zero lost updates: all workers' updates must be present in resolved_items
    expected_items = {f"worker-{i:02d}" for i in range(num_workers)}
    actual_items = set(final.resolved_items)
    assert actual_items == expected_items
    assert len(final.resolved_items) == num_workers

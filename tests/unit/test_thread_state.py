"""Unit tests for ThreadState domain entity and in-memory store (R8.1, R8.6)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from packages.domain.entities import ThreadState


def test_thread_state_instantiation_defaults() -> None:
    """Verify default instantiation of ThreadState domain entity (R8.1)."""
    thread_id = uuid4()
    org_id = uuid4()
    state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
    )
    assert state.thread_id == thread_id
    assert state.organization_id == org_id
    assert state.topic is None
    assert state.current_intent is None
    assert state.summary is None
    assert state.open_questions == []
    assert state.resolved_items == []
    assert state.summarized_through_message_id is None
    assert state.token_estimate is None
    assert state.version == 1
    assert isinstance(state.updated_at, datetime)


@pytest.mark.asyncio
async def test_in_memory_thread_state_crud() -> None:
    from packages.db.thread_state import InMemoryThreadStateStore

    store = InMemoryThreadStateStore()
    org_id = uuid4()
    thread_id = uuid4()

    # Get non-existent
    assert await store.get(org_id, thread_id) is None

    # Create initial state
    state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Billing Inquiry",
        current_intent="request_invoice",
        summary="Customer asking for last month invoice",
        open_questions=["Which account ID?"],
        resolved_items=["Verified email address"],
        version=1,
    )
    created = await store.create(state)
    assert created.version == 1
    assert created.topic == "Billing Inquiry"

    # Fetch
    fetched = await store.get(org_id, thread_id)
    assert fetched is not None
    assert fetched.topic == "Billing Inquiry"
    assert fetched.open_questions == ["Which account ID?"]
    assert fetched.resolved_items == ["Verified email address"]

    # Update with correct version
    fetched.summary = "Customer provided account ID; invoice sent."
    fetched.resolved_items.append("Account ID provided")
    updated = await store.update(fetched)
    assert updated.version == 2
    assert len(updated.resolved_items) == 2


@pytest.mark.asyncio
async def test_in_memory_thread_state_optimistic_concurrency_conflict() -> None:
    from packages.db.thread_state import (
        InMemoryThreadStateStore,
        OptimisticLockError,
    )

    store = InMemoryThreadStateStore()
    org_id = uuid4()
    thread_id = uuid4()

    state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        summary="Initial summary",
        version=1,
    )
    await store.create(state)

    # Worker A and Worker B both read version 1
    worker_a_state = await store.get(org_id, thread_id)
    worker_b_state = await store.get(org_id, thread_id)
    assert worker_a_state is not None
    assert worker_b_state is not None
    assert worker_a_state.version == 1
    assert worker_b_state.version == 1

    # Worker A successfully updates to version 2
    worker_a_state.summary = "Worker A update"
    updated_a = await store.update(worker_a_state)
    assert updated_a.version == 2

    # Worker B attempts update with stale version 1 -> raises OptimisticLockError
    worker_b_state.summary = "Worker B update"
    with pytest.raises(OptimisticLockError) as exc_info:
        await store.update(worker_b_state)
    assert exc_info.value.thread_id == thread_id
    assert exc_info.value.expected_version == 1
    assert exc_info.value.actual_version == 2

    # Worker B resolves conflict by re-reading version 2 and updating to version 3
    refetched = await store.get(org_id, thread_id)
    assert refetched is not None
    assert refetched.version == 2
    assert refetched.summary == "Worker A update"
    refetched.summary = "Worker A update + Worker B addition"
    updated_b = await store.update(refetched)
    assert updated_b.version == 3
    assert updated_b.summary == "Worker A update + Worker B addition"

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

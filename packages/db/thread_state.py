"""Thread state persistence and optimistic concurrency store (R8.1, R8.6, design.md §6.1).

Maintains one thread_state row per thread containing topic, current_intent, summary,
open_questions[], resolved_items[], and version. Enforces optimistic concurrency
control on version to avoid lost updates across concurrent workers on the same thread.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import ThreadState

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _opt_uuid(val: UUID | str | None) -> UUID | None:
    if val is None or val == "":
        return None
    return val if isinstance(val, UUID) else UUID(str(val))


def _parse_json_list(val: Any) -> list[str]:
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            return []
    elif isinstance(val, list):
        return [str(x) for x in val]
    return []


def _row_to_thread_state(row: asyncpg.Record) -> ThreadState:
    return ThreadState(
        thread_id=row["thread_id"],
        organization_id=row["organization_id"],
        topic=row["topic"],
        current_intent=row["current_intent"],
        summary=row["summary"],
        open_questions=_parse_json_list(row["open_questions"]),
        resolved_items=_parse_json_list(row["resolved_items"]),
        summarized_through_message_id=_opt_uuid(row["summarized_through_message_id"]),
        token_estimate=row["token_estimate"],
        version=int(row["version"]),
        updated_at=row["updated_at"],
    )


class ThreadStateError(Exception):
    """Base exception for thread state persistence operations."""


class ThreadStateNotFoundError(ThreadStateError):
    """Raised when the specified thread state row does not exist."""


class OptimisticLockError(ThreadStateError):
    """Raised when an update fails because the version in the database has changed (R8.6)."""

    def __init__(
        self,
        thread_id: UUID,
        expected_version: int,
        actual_version: int | None = None,
        message: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        msg = (
            message
            or f"Optimistic lock conflict on thread '{thread_id}': expected version {expected_version}, found {actual_version}"
        )
        super().__init__(msg)


@runtime_checkable
class ThreadStateStore(Protocol):
    """Protocol for reading and mutating thread state with optimistic locking."""

    async def get(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> ThreadState | None:
        """Fetch thread state row by organization_id and thread_id."""
        ...

    async def create(
        self,
        state: ThreadState,
    ) -> ThreadState:
        """Insert initial thread state with version=1. Raises if already exists."""
        ...

    async def update(
        self,
        state: ThreadState,
        expected_version: int | None = None,
    ) -> ThreadState:
        """Update existing thread state conditioned on version == expected_version.

        If expected_version is None, state.version is used as expected_version.
        Atomically increments version by 1 and updates updated_at timestamp.
        Raises OptimisticLockError on version mismatch.
        Raises ThreadStateNotFoundError if the row does not exist.
        """
        ...

    async def save(
        self,
        state: ThreadState,
    ) -> ThreadState:
        """Upsert convenience: creates if absent, otherwise updates with optimistic locking."""
        ...


class InMemoryThreadStateStore:
    """In-memory thread state store with optimistic locking for tests."""

    def __init__(self) -> None:
        # Key: (org_id, thread_id) -> ThreadState
        self.states: dict[tuple[UUID, UUID], ThreadState] = {}

    async def get(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> ThreadState | None:
        key = (_to_uuid(organization_id), _to_uuid(thread_id))
        st = self.states.get(key)
        return copy.deepcopy(st) if st else None

    async def create(
        self,
        state: ThreadState,
    ) -> ThreadState:
        key = (_to_uuid(state.organization_id), _to_uuid(state.thread_id))
        if key in self.states:
            current = self.states[key]
            raise OptimisticLockError(
                thread_id=_to_uuid(state.thread_id),
                expected_version=0,
                actual_version=current.version,
                message=f"Thread state already exists for thread '{state.thread_id}'",
            )
        new_state = copy.deepcopy(state)
        new_state.version = 1
        new_state.updated_at = datetime.now(UTC)
        self.states[key] = new_state
        return copy.deepcopy(new_state)

    async def update(
        self,
        state: ThreadState,
        expected_version: int | None = None,
    ) -> ThreadState:
        key = (_to_uuid(state.organization_id), _to_uuid(state.thread_id))
        if key not in self.states:
            raise ThreadStateNotFoundError(
                f"Thread state for thread '{state.thread_id}' not found."
            )

        current = self.states[key]
        target_version = expected_version if expected_version is not None else state.version
        if current.version != target_version:
            raise OptimisticLockError(
                thread_id=_to_uuid(state.thread_id),
                expected_version=target_version,
                actual_version=current.version,
            )

        updated = copy.deepcopy(state)
        updated.version = current.version + 1
        updated.updated_at = datetime.now(UTC)
        self.states[key] = updated
        return copy.deepcopy(updated)

    async def save(
        self,
        state: ThreadState,
    ) -> ThreadState:
        key = (_to_uuid(state.organization_id), _to_uuid(state.thread_id))
        if key not in self.states:
            return await self.create(state)
        return await self.update(state)

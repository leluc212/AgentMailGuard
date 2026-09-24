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
        msg = message or (
            f"Optimistic lock conflict on thread '{thread_id}': "
            f"expected version {expected_version}, found {actual_version}"
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


class PostgresThreadStateStore:
    """PostgreSQL implementation of ThreadStateStore with atomic optimistic locking (R8.1, R8.6)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> ThreadState | None:
        """Fetch thread state row strictly scoped to organization_id."""
        query = """
            SELECT thread_id, organization_id, topic, current_intent, summary,
                   open_questions, resolved_items, summarized_through_message_id,
                   token_estimate, version, updated_at
            FROM thread_state
            WHERE organization_id = $1 AND thread_id = $2;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, _to_uuid(organization_id), _to_uuid(thread_id))

        if row is None:
            return None
        return _row_to_thread_state(row)

    async def create(
        self,
        state: ThreadState,
    ) -> ThreadState:
        """Insert initial thread state row with version=1."""
        th_id = _to_uuid(state.thread_id)
        org_id = _to_uuid(state.organization_id)
        msg_id = _opt_uuid(state.summarized_through_message_id)

        open_questions_json = json.dumps(state.open_questions or [])
        resolved_items_json = json.dumps(state.resolved_items or [])
        now = datetime.now(UTC)

        query = """
            INSERT INTO thread_state (
                thread_id, organization_id, topic, current_intent, summary,
                open_questions, resolved_items, summarized_through_message_id,
                token_estimate, version, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6::jsonb, $7::jsonb, $8,
                $9, 1, $10
            )
            RETURNING *;
        """

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    th_id,
                    org_id,
                    state.topic,
                    state.current_intent,
                    state.summary,
                    open_questions_json,
                    resolved_items_json,
                    msg_id,
                    state.token_estimate,
                    now,
                )
        except asyncpg.UniqueViolationError as exc:
            raise OptimisticLockError(
                thread_id=th_id,
                expected_version=0,
                message=f"Thread state already exists for thread '{th_id}'",
            ) from exc

        if row is None:
            raise RuntimeError(f"Failed to insert thread_state for thread '{th_id}'")

        return _row_to_thread_state(row)

    async def update(
        self,
        state: ThreadState,
        expected_version: int | None = None,
    ) -> ThreadState:
        """Atomically update thread state conditioned on version == expected_version."""
        th_id = _to_uuid(state.thread_id)
        org_id = _to_uuid(state.organization_id)
        target_version = expected_version if expected_version is not None else state.version
        msg_id = _opt_uuid(state.summarized_through_message_id)

        open_questions_json = json.dumps(state.open_questions or [])
        resolved_items_json = json.dumps(state.resolved_items or [])
        now = datetime.now(UTC)

        query = """
            UPDATE thread_state
            SET topic = $3,
                current_intent = $4,
                summary = $5,
                open_questions = $6::jsonb,
                resolved_items = $7::jsonb,
                summarized_through_message_id = $8,
                token_estimate = $9,
                version = version + 1,
                updated_at = $10
            WHERE thread_id = $1
              AND organization_id = $2
              AND version = $11
            RETURNING *;
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                th_id,
                org_id,
                state.topic,
                state.current_intent,
                state.summary,
                open_questions_json,
                resolved_items_json,
                msg_id,
                state.token_estimate,
                now,
                target_version,
            )

            if row is not None:
                return _row_to_thread_state(row)

            # Diagnostic check to provide precise error feedback
            check_row = await conn.fetchrow(
                "SELECT version FROM thread_state WHERE thread_id = $1 AND organization_id = $2;",
                th_id,
                org_id,
            )

        if check_row is None:
            raise ThreadStateNotFoundError(f"Thread state for thread '{th_id}' not found.")

        actual_version = int(check_row["version"])
        raise OptimisticLockError(
            thread_id=th_id,
            expected_version=target_version,
            actual_version=actual_version,
        )

    async def save(
        self,
        state: ThreadState,
    ) -> ThreadState:
        """Save thread state: create if absent or update with optimistic concurrency."""
        existing = await self.get(state.organization_id, state.thread_id)
        if existing is None:
            try:
                return await self.create(state)
            except OptimisticLockError:
                # Concurrent creation occurred, update existing
                return await self.update(state)
        return await self.update(state)


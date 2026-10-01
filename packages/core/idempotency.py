"""Deterministic idempotency key derivation and two-layered execution helper (R19.1–R19.4).

Implements fast-path application short-circuiting backed by database UNIQUE constraint
guarantees per specs/design.md §9.
"""

import asyncio
import hashlib
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

logger = logging.getLogger(__name__)


def derive_idempotency_key(
    organization_id: str | uuid.UUID,
    mailbox_id: str | uuid.UUID,
    provider_message_id: str,
    operation_type: str,
) -> str:
    """Derive a deterministic SHA-256 idempotency key (R19.2, design.md §9).

    Parameters
    ----------
    organization_id : str | uuid.UUID
        Tenant organization UUID.
    mailbox_id : str | uuid.UUID
        Target mailbox UUID.
    provider_message_id : str
        Provider-native message ID (e.g. Gmail message ID, Graph ID).
    operation_type : str
        Operation identifier (e.g. 'normalize', 'triage', 'generate_reply', 'dispatch').

    Returns
    -------
    str
        64-character lowercase hexadecimal SHA-256 digest.
    """
    raw_token = f"{organization_id}:{mailbox_id}:{provider_message_id}:{operation_type}"
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class IdempotencyConflictError(Exception):
    """Raised when an operation attempts to commit a duplicate idempotency key (R19.4)."""


@dataclass(frozen=True)
class IdempotencyRecord:
    """Immutable representation of a recorded idempotent operation result."""

    key: str
    result: Any
    state: str = "COMPLETED"
    created_at: datetime | None = None


class IdempotencyBackend(ABC):
    """Abstract storage backend for idempotency records."""

    @abstractmethod
    async def find_completed(self, key: str) -> IdempotencyRecord | None:
        """Query for an already completed operation by key (R19.3).

        Parameters
        ----------
        key : str
            Deterministic idempotency key.

        Returns
        -------
        IdempotencyRecord | None
            Prior result record if completed, else None.
        """

    @abstractmethod
    async def mark_completed(
        self,
        key: str,
        result: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist operation completion and result (R19.4).

        Must raise IdempotencyConflictError if another transaction concurrently
        committed the same idempotency key.

        Parameters
        ----------
        key : str
            Deterministic idempotency key.
        result : Any
            Operation result to persist.
        metadata : dict[str, Any] | None
            Optional contextual metadata (e.g. organization_id, job_type).
        """


class InMemoryIdempotencyBackend(IdempotencyBackend):
    """Thread/async-safe in-memory backend for unit tests and isolated testing."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def find_completed(self, key: str) -> IdempotencyRecord | None:
        async with self._lock:
            return self._records.get(key)

    async def mark_completed(
        self,
        key: str,
        result: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            if key in self._records:
                raise IdempotencyConflictError(
                    f"Duplicate idempotency key in memory backend: {key}"
                )
            self._records[key] = IdempotencyRecord(
                key=key,
                result=result,
                state="COMPLETED",
                created_at=datetime.now(UTC),
            )


async def execute_once[T](
    key: str,
    op: Callable[[], Awaitable[T]],
    backend: IdempotencyBackend,
    poll_attempts: int = 5,
    poll_delay_s: float = 0.05,
    metadata: dict[str, Any] | None = None,
) -> T:
    """Execute an asynchronous operation with two-layer idempotency protection (R19.1–R19.4).

    1. Fast path: checks if key is already completed, short-circuiting immediately (R19.3).
    2. Execution: runs `op()`.
    3. Persistence: marks key completed in backend.
    4. Concurrency: on conflict (IntegrityError / UniqueViolationError), polls for the
       winner's result and returns it without duplicate execution.

    Parameters
    ----------
    key : str
        Deterministic idempotency key.
    op : Callable[[], Awaitable[T]]
        Asynchronous operation to execute once.
    backend : IdempotencyBackend
        Idempotency persistence backend.
    poll_attempts : int
        Number of retry polls if concurrent race is lost.
    poll_delay_s : float
        Delay in seconds between poll attempts.
    metadata : dict[str, Any] | None
        Optional metadata passed to backend.

    Returns
    -------
    T
        Result of the operation (either freshly computed or retrieved from prior run).
    """
    # 1. Fast-path application check (R19.3)
    prior = await backend.find_completed(key)
    if prior is not None:
        logger.debug("Idempotency key %s short-circuited with prior result", key)
        return cast(T, prior.result)

    # 2. Execute operation
    result = await op()

    # 3. Mark completed with database UNIQUE constraint enforcement (R19.4)
    try:
        await backend.mark_completed(key, result, metadata=metadata)
        return result
    except IdempotencyConflictError as err:
        # Lost concurrent race: poll for winner's committed result
        logger.warning("Concurrent race on idempotency key %s; retrieving winner result", key)
        for attempt in range(poll_attempts):
            winner = await backend.find_completed(key)
            if winner is not None:
                logger.info(
                    "Idempotency race resolved for key %s on poll attempt %d", key, attempt + 1
                )
                return cast(T, winner.result)
            await asyncio.sleep(poll_delay_s)

        raise IdempotencyConflictError(
            f"Concurrent race on key '{key}': result not found after {poll_attempts} attempts"
        ) from err

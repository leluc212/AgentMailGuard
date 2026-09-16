"""Finite state machine and transition table for asynchronous processing jobs.

Requirements:
- R18.1: Core processing states.
- R18.2: Failure & dead-letter states.
- R18.3: Permit only declared transitions; illegal transitions raise.
- R18.4, R18.5: Atomically generate ProcessingEvent on transition.
- R18.7: Support operator replay from DEAD_LETTER to RETRY_PENDING.
- GEMINI.md §4: State transitions go through state machine; never hand-write state strings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from packages.domain.entities import Job, ProcessingEvent


class JobState(StrEnum):
    """Authoritative states of an asynchronous email processing job (R18.1, R18.2)."""

    # Active processing states (R18.1)
    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    CLASSIFIED = "CLASSIFIED"
    QUEUED = "QUEUED"
    CONTEXT_READY = "CONTEXT_READY"
    GENERATING = "GENERATING"
    DRAFTED = "DRAFTED"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"

    # Failure and replay states (R18.2, R18.7)
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class IllegalStateTransitionError(Exception):
    """Raised when an illegal or undeclared state transition is attempted (R18.3)."""

    def __init__(self, from_state: JobState | str, to_state: JobState | str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Illegal job state transition: cannot transition from '{from_state}' to '{to_state}'."
        )


# Authoritative transition table strictly implementing specs/design.md §8
TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.RECEIVED: {JobState.NORMALIZED, JobState.FAILED},
    JobState.NORMALIZED: {JobState.CLASSIFIED, JobState.FAILED},
    JobState.CLASSIFIED: {JobState.QUEUED, JobState.COMPLETED, JobState.FAILED},
    JobState.QUEUED: {JobState.CONTEXT_READY, JobState.FAILED},
    JobState.CONTEXT_READY: {JobState.GENERATING, JobState.FAILED},
    JobState.GENERATING: {JobState.DRAFTED, JobState.RETRY_PENDING, JobState.FAILED},
    JobState.RETRY_PENDING: {JobState.GENERATING, JobState.FAILED},
    JobState.DRAFTED: {JobState.DISPATCHED, JobState.COMPLETED, JobState.FAILED},
    JobState.DISPATCHED: {JobState.COMPLETED, JobState.FAILED},
    JobState.FAILED: {JobState.DEAD_LETTER, JobState.RETRY_PENDING},
    JobState.DEAD_LETTER: {JobState.RETRY_PENDING},  # operator replay (R18.7)
    JobState.COMPLETED: set(),
}


def validate_transition(
    current: JobState | str, target: JobState | str
) -> tuple[JobState, JobState]:
    """Validate whether transitioning from current to target state is legal.

    Raises:
        IllegalStateTransitionError: If the transition is not declared in TRANSITIONS.
    """
    try:
        current_state = JobState(current)
    except ValueError as err:
        raise IllegalStateTransitionError(current, target) from err

    try:
        target_state = JobState(target)
    except ValueError as err:
        raise IllegalStateTransitionError(current, target) from err

    allowed = TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise IllegalStateTransitionError(current_state, target_state)

    return current_state, target_state


def transition_job(
    job: Job,
    target_state: JobState | str,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> tuple[Job, ProcessingEvent]:
    """Execute a validated state transition on a Job and return the updated Job & ProcessingEvent.

    Guarantees:
    - Verifies legal transition per R18.3.
    - Updates job.state and job.updated_at.
    - Generates matching ProcessingEvent carrying trace_id, states, and timestamp (R18.4).
    - Caller must commit both job and event in the same transaction (R18.5).
    """
    from_state, to_state = validate_transition(job.state, target_state)
    now = datetime.now(UTC)

    job.state = to_state.value
    job.updated_at = now

    event = ProcessingEvent(
        organization_id=job.organization_id,
        job_id=job.id,
        message_id=job.message_id,
        event_type="state_transition",
        state_from=from_state.value,
        state_to=to_state.value,
        payload=payload or {},
        trace_id=trace_id or job.trace_id,
        created_at=now,
    )

    return job, event

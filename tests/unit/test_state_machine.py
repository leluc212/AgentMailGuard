"""Exhaustive unit tests for processing state machine and transition table.

Requirements:
- R18.1: Core states (RECEIVED..COMPLETED).
- R18.2: Failure states (RETRY_PENDING, FAILED, DEAD_LETTER).
- R18.3: Permit only declared transitions; illegal transitions raise.
- R18.4, R18.5: Emit ProcessingEvent carrying trace_id, from-state, to-state.
- R18.7: Support operator replay from DEAD_LETTER to RETRY_PENDING.
- R24.3: Maintain unit tests for every pure component.
"""

from uuid import uuid4

import pytest

from packages.domain.entities import Job, ProcessingEvent
from packages.domain.state_machine import (
    TRANSITIONS,
    IllegalStateTransitionError,
    JobState,
    transition_job,
    validate_transition,
)


def test_job_state_enum_completeness() -> None:
    """Verify all 12 job states defined in R18.1 and R18.2 are present."""
    expected_states = {
        "RECEIVED",
        "NORMALIZED",
        "CLASSIFIED",
        "QUEUED",
        "CONTEXT_READY",
        "GENERATING",
        "DRAFTED",
        "DISPATCHED",
        "COMPLETED",
        "RETRY_PENDING",
        "FAILED",
        "DEAD_LETTER",
    }
    actual_states = {s.value for s in JobState}
    assert actual_states == expected_states
    assert len(JobState) == 12


def test_all_declared_legal_transitions() -> None:
    """Exhaustively verify every single declared legal transition in TRANSITIONS table."""
    total_transitions = 0
    for from_state, allowed_targets in TRANSITIONS.items():
        for to_state in allowed_targets:
            src, dst = validate_transition(from_state, to_state)
            assert src == from_state
            assert dst == to_state
            total_transitions += 1

    # Exactly 26 declared legal transitions (including CLASSIFIED -> DRAFTED and the
    # RETRY_PENDING -> DISPATCHED replay of a dead-lettered dispatch, ADR-0009)
    assert total_transitions == 26


def test_classified_to_drafted_transition() -> None:
    """Verify CLASSIFIED -> DRAFTED transition for deterministic template reply (R6.13)."""
    src, dst = validate_transition(JobState.CLASSIFIED, JobState.DRAFTED)
    assert src == JobState.CLASSIFIED
    assert dst == JobState.DRAFTED


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (JobState.RECEIVED, JobState.DRAFTED),
        (JobState.RECEIVED, JobState.GENERATING),
        (JobState.RECEIVED, JobState.COMPLETED),
        (JobState.NORMALIZED, JobState.GENERATING),
        (JobState.CLASSIFIED, JobState.DISPATCHED),
        (JobState.QUEUED, JobState.DISPATCHED),
        (JobState.CONTEXT_READY, JobState.COMPLETED),
        (JobState.GENERATING, JobState.RECEIVED),
        (JobState.DRAFTED, JobState.RECEIVED),
        (JobState.DISPATCHED, JobState.GENERATING),
        (JobState.DISPATCHED, JobState.RETRY_PENDING),
        (JobState.COMPLETED, JobState.GENERATING),
        (JobState.COMPLETED, JobState.RECEIVED),
        (JobState.COMPLETED, JobState.FAILED),
        (JobState.DEAD_LETTER, JobState.COMPLETED),
        (JobState.DEAD_LETTER, JobState.DRAFTED),
    ],
)
def test_illegal_transitions_raise(from_state: JobState, to_state: JobState) -> None:
    """Verify illegal or undeclared state transitions raise IllegalStateTransitionError (R18.3)."""
    with pytest.raises(IllegalStateTransitionError) as exc_info:
        validate_transition(from_state, to_state)

    assert f"cannot transition from '{from_state}' to '{to_state}'" in str(exc_info.value)


def test_self_transitions_illegal() -> None:
    """Verify no state can transition to itself."""
    for state in JobState:
        with pytest.raises(IllegalStateTransitionError):
            validate_transition(state, state)


def test_terminal_completed_state() -> None:
    """Verify COMPLETED has zero outgoing transitions."""
    assert TRANSITIONS[JobState.COMPLETED] == set()
    for target in JobState:
        with pytest.raises(IllegalStateTransitionError):
            validate_transition(JobState.COMPLETED, target)


def test_operator_replay_transition() -> None:
    """Verify operator replay path from DEAD_LETTER to RETRY_PENDING (R18.7)."""
    src, dst = validate_transition(JobState.DEAD_LETTER, JobState.RETRY_PENDING)
    assert src == JobState.DEAD_LETTER
    assert dst == JobState.RETRY_PENDING


def test_retry_pending_to_dispatched_transition() -> None:
    """ADR-0009: operator replay of a dead-lettered dispatch resumes at DISPATCHED (R18.7)."""
    src, dst = validate_transition(JobState.RETRY_PENDING, JobState.DISPATCHED)
    assert src == JobState.RETRY_PENDING
    assert dst == JobState.DISPATCHED
    assert TRANSITIONS[JobState.RETRY_PENDING] == {
        JobState.GENERATING,
        JobState.DISPATCHED,
        JobState.FAILED,
    }


def test_dead_lettered_dispatch_replays_back_to_dispatched() -> None:
    """design.md §5.8: DISPATCHED -> FAILED -> DEAD_LETTER -> RETRY_PENDING -> DISPATCHED."""
    job = Job(
        organization_id=uuid4(),
        state=JobState.DISPATCHED.value,
        idempotency_key=str(uuid4()),
    )
    steps: list[tuple[str | None, str | None]] = []
    for target in (
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.RETRY_PENDING,
        JobState.DISPATCHED,
    ):
        job, event = transition_job(job, target, payload={"reason": "replay-path"})
        steps.append((event.state_from, event.state_to))

    assert steps == [
        ("DISPATCHED", "FAILED"),
        ("FAILED", "DEAD_LETTER"),
        ("DEAD_LETTER", "RETRY_PENDING"),
        ("RETRY_PENDING", "DISPATCHED"),
    ]
    assert job.state == JobState.DISPATCHED.value


def test_invalid_state_strings() -> None:
    """Verify invalid state string inputs raise IllegalStateTransitionError."""
    with pytest.raises(IllegalStateTransitionError):
        validate_transition("UNKNOWN_STATE", JobState.RECEIVED)
    with pytest.raises(IllegalStateTransitionError):
        validate_transition(JobState.RECEIVED, "NON_EXISTENT")


def test_transition_job_execution_and_event() -> None:
    """Verify transition_job updates Job state and generates matching ProcessingEvent (R18.4)."""
    org_id = uuid4()
    msg_id = uuid4()
    job = Job(
        organization_id=org_id,
        message_id=msg_id,
        state=JobState.RECEIVED.value,
        trace_id="trace-abc-123",
    )
    initial_updated_at = job.updated_at

    updated_job, event = transition_job(
        job=job,
        target_state=JobState.NORMALIZED,
        payload={"mime_size": 4096},
    )

    # Job updated
    assert updated_job.state == JobState.NORMALIZED.value
    assert updated_job.updated_at >= initial_updated_at

    # Event generated
    assert isinstance(event, ProcessingEvent)
    assert event.organization_id == org_id
    assert event.job_id == job.id
    assert event.message_id == msg_id
    assert event.event_type == "state_transition"
    assert event.state_from == JobState.RECEIVED.value
    assert event.state_to == JobState.NORMALIZED.value
    assert event.trace_id == "trace-abc-123"
    assert event.payload == {"mime_size": 4096}
    assert event.created_at == updated_job.updated_at

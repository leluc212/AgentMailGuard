"""Async-safe correlation context propagation for structured logging (R21.3).

Tracks correlation IDs across asyncio tasks and execution contexts:
- trace_id: Distributed trace ID
- message_id: Target email message UUID
- thread_id: Conversation thread UUID
- job_id: Execution job UUID
- organization_id: Tenant isolation UUID
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

# Global contextvar holding the active correlation dictionary
_CORRELATION_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "correlation_context",
    default=None,
)

CORRELATION_KEYS = (
    "trace_id",
    "message_id",
    "thread_id",
    "job_id",
    "organization_id",
)


def get_correlation_context() -> dict[str, Any]:
    """Return a copy of the current active correlation context dictionary."""
    ctx = _CORRELATION_CONTEXT.get()
    return ctx.copy() if ctx is not None else {}


def set_correlation_context(
    trace_id: str | None = None,
    message_id: str | None = None,
    thread_id: str | None = None,
    job_id: str | None = None,
    organization_id: str | None = None,
    **extra: Any,
) -> Token[dict[str, Any] | None]:
    """Set correlation IDs in the current async context and return a reset token."""
    current = get_correlation_context()

    updates: dict[str, Any] = {}
    if trace_id is not None:
        updates["trace_id"] = str(trace_id)
    if message_id is not None:
        updates["message_id"] = str(message_id)
    if thread_id is not None:
        updates["thread_id"] = str(thread_id)
    if job_id is not None:
        updates["job_id"] = str(job_id)
    if organization_id is not None:
        updates["organization_id"] = str(organization_id)

    for k, v in extra.items():
        if v is not None:
            updates[k] = v

    current.update(updates)
    return _CORRELATION_CONTEXT.set(current)


def reset_correlation_context(token: Token[dict[str, Any] | None]) -> None:
    """Reset the correlation context using a previously returned Token."""
    _CORRELATION_CONTEXT.reset(token)


def clear_correlation_context() -> None:
    """Clear all correlation IDs from the current context."""
    _CORRELATION_CONTEXT.set(None)


@contextmanager
def bind_log_context(
    trace_id: str | None = None,
    message_id: str | None = None,
    thread_id: str | None = None,
    job_id: str | None = None,
    organization_id: str | None = None,
    **extra: Any,
) -> Iterator[dict[str, Any]]:
    """Context manager binding correlation IDs for the duration of a block.

    Example
    -------
    with bind_log_context(trace_id="abc", job_id="job-1"):
        logger.info("Processing job")  # Emits trace_id and job_id in JSON
    """
    token = set_correlation_context(
        trace_id=trace_id,
        message_id=message_id,
        thread_id=thread_id,
        job_id=job_id,
        organization_id=organization_id,
        **extra,
    )
    try:
        yield get_correlation_context()
    finally:
        reset_correlation_context(token)

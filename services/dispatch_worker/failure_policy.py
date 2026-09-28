"""Dispatch failure routing (tasks 6.5, 6.6; design.md §5.8 "errors").

Retryable (the job stays DISPATCHED; the broker retry ladder redelivers, a Retry-After
picks its tier): 429 / rate-limit 403 (RateLimited), 5xx and network faults (Transient) and
anything unknown. Permanent (DISPATCHED -> FAILED -> DEAD_LETTER with the provider error
kept on the job): 400 (Permanent), 404 on send (NotFound), auth failures (AuthExpired), a
null provider thread id, a provider draft that vanished without a sent copy, and a dispatch
key held by another draft. Requirements: R17.5, R18.2, R19.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from packages.adapters.exceptions import (
    AuthExpired,
    PermanentProviderError,
    RetryableProviderError,
)
from packages.broker.consumer import FatalError
from packages.db.dispatch import DispatchKeyConflictError
from packages.dispatch.reply import MissingProviderThreadError, MissingRecipientError
from packages.dispatch.service import DispatchPermanentError


class Disposition(StrEnum):
    """What the consumer does with a delivery whose dispatch failed."""

    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class FailureDecision:
    """A disposition and the reason recorded with it (the job's last_error on dead-letter)."""

    disposition: Disposition
    reason: str


_PERMANENT: tuple[type[BaseException], ...] = (
    DispatchPermanentError,
    MissingProviderThreadError,
    MissingRecipientError,
    DispatchKeyConflictError,
    PermanentProviderError,  # AuthExpired (handled first), NotFound, Permanent
    FatalError,
)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def classify_dispatch_failure(exc: BaseException) -> FailureDecision:
    """Map a dispatch failure to retry or dead-letter; unknown exceptions are retried."""
    if isinstance(exc, AuthExpired):
        return FailureDecision(
            Disposition.DEAD_LETTER,
            f"{_describe(exc)} (the mailbox credentials expired or were revoked; "
            "refresh the token, then replay the job)",
        )
    if isinstance(exc, _PERMANENT):
        return FailureDecision(Disposition.DEAD_LETTER, _describe(exc))
    return FailureDecision(Disposition.RETRY, _describe(exc))


def provider_retry_after(exc: BaseException) -> float | None:
    """Seconds the provider asked us to wait (Retry-After), for retryable provider errors."""
    if not isinstance(exc, RetryableProviderError):
        return None
    return exc.retry_after_s

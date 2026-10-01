"""Provider exception taxonomy.

Requirements:
- R1.5: Common error taxonomy: RateLimited, AuthExpired, NotFound, Transient, Permanent.
- R1.6: RateLimited carries optional provider-supplied retry_after (seconds).
- R17.5: Dispatch tells retryable from permanent provider failures (task 6.3a).
- design.md §5.1: Error taxonomy and failure handling modes.
- design.md §5.8: 429/5xx/rate-limit 403 retry with Retry-After; 400/404/auth dead-letter.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

# Kept from the pre-6.3a adapters: an unparseable Retry-After still waits a minute.
DEFAULT_RETRY_AFTER_S = 60.0


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP Retry-After header into seconds (R1.6, RFC 9110 §10.2.3).

    Accepts delay-seconds ("30") and HTTP-date ("Mon, 28 Sep 2026 12:02:00 GMT").
    Returns None when the header is absent or blank, 0.0 for a past date, and
    DEFAULT_RETRY_AFTER_S when the header is present but unparseable.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds):
            return DEFAULT_RETRY_AFTER_S
        return max(seconds, 0.0)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return DEFAULT_RETRY_AFTER_S
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max((when - current).total_seconds(), 0.0)


class ProviderError(Exception):
    """Base exception for all mail provider adapter operations (R1.5)."""

    def __init__(
        self,
        message: str = "",
        *,
        provider: str | None = None,
        mailbox_id: str | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.mailbox_id = mailbox_id
        self.raw_error = raw_error

    def __str__(self) -> str:
        parts = [self.message]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.mailbox_id:
            parts.append(f"mailbox_id={self.mailbox_id}")
        return " | ".join(filter(None, parts))


class RetryableProviderError(ProviderError):
    """A failure the broker retry ladder may retry (R17.5, design.md §5.8).

    `retry_after_s` is the provider's Retry-After hint in seconds, if it sent one.
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after_s: float | None = None,
        provider: str | None = None,
        mailbox_id: str | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            mailbox_id=mailbox_id,
            raw_error=raw_error,
        )
        self.retry_after_s = retry_after_s


class PermanentProviderError(ProviderError):
    """A failure no retry will fix; dispatch dead-letters it (R17.5, design.md §5.8)."""


class RateLimited(RetryableProviderError):  # noqa: N818
    """Provider API rate limit exceeded (R1.5, R1.6).

    Carries an optional retry_after duration in seconds as instructed by provider
    headers (e.g. Retry-After). `retry_after_s` holds the same value.
    """

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        *,
        retry_after: float | None = None,
        provider: str | None = None,
        mailbox_id: str | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            retry_after_s=retry_after,
            provider=provider,
            mailbox_id=mailbox_id,
            raw_error=raw_error,
        )
        self.retry_after = retry_after


class AuthExpired(PermanentProviderError):  # noqa: N818
    """Provider OAuth or access credentials expired or revoked (R1.5).

    Indicates the mailbox requires administrative re-authentication.
    """


class NotFound(PermanentProviderError):  # noqa: N818
    """Requested message, thread, draft, or mailbox resource does not exist (R1.5)."""


class Transient(RetryableProviderError):  # noqa: N818
    """Temporary failure (e.g. timeout, provider 5xx) eligible for retry (R1.5)."""


class Permanent(PermanentProviderError):  # noqa: N818
    """Fatal non-retryable failure (e.g. malformed request, invalid payload) (R1.5)."""

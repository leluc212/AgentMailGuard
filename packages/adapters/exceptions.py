"""Provider exception taxonomy.

Requirements:
- R1.5: Common error taxonomy: RateLimited, AuthExpired, NotFound, Transient, Permanent.
- R1.6: RateLimited carries optional provider-supplied retry_after (seconds).
- design.md §5.1: Error taxonomy and failure handling modes.
"""

from __future__ import annotations

from typing import Any


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


class RateLimited(ProviderError):  # noqa: N818
    """Provider API rate limit exceeded (R1.5, R1.6).

    Carries an optional retry_after duration in seconds as instructed by provider
    headers (e.g. Retry-After).
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
            provider=provider,
            mailbox_id=mailbox_id,
            raw_error=raw_error,
        )
        self.retry_after = retry_after


class AuthExpired(ProviderError):  # noqa: N818
    """Provider OAuth or access credentials expired or revoked (R1.5).

    Indicates the mailbox requires administrative re-authentication.
    """


class NotFound(ProviderError):  # noqa: N818
    """Requested message, thread, draft, or mailbox resource does not exist (R1.5)."""


class Transient(ProviderError):  # noqa: N818
    """Temporary failure (e.g. timeout, provider 5xx) eligible for retry (R1.5)."""


class Permanent(ProviderError):  # noqa: N818
    """Fatal non-retryable failure (e.g. malformed request, invalid payload) (R1.5)."""

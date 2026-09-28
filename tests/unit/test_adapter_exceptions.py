"""Unit tests for the common mail provider error taxonomy.

Requirements:
- R1.5: Translate provider errors into common taxonomy:
  RateLimited, AuthExpired, NotFound, Transient, Permanent.
- R1.6: RateLimited carries retry_after hint.
"""

from datetime import UTC, datetime

from packages.adapters.exceptions import (
    DEFAULT_RETRY_AFTER_S,
    AuthExpired,
    NotFound,
    Permanent,
    PermanentProviderError,
    ProviderError,
    RateLimited,
    RetryableProviderError,
    Transient,
    parse_retry_after,
)


def test_provider_error_hierarchy() -> None:
    """Verify all exceptions inherit from ProviderError and Exception (R1.5)."""
    assert issubclass(RateLimited, ProviderError)
    assert issubclass(AuthExpired, ProviderError)
    assert issubclass(NotFound, ProviderError)
    assert issubclass(Transient, ProviderError)
    assert issubclass(Permanent, ProviderError)
    assert issubclass(ProviderError, Exception)


def test_rate_limited_retry_after_attribute() -> None:
    """Verify RateLimited captures retry_after in seconds (R1.6)."""
    err = RateLimited("Too many requests", retry_after=30.0, provider="gmail")
    assert err.retry_after == 30.0
    assert err.provider == "gmail"
    assert "Too many requests" in str(err)

    err_no_retry = RateLimited()
    assert err_no_retry.retry_after is None
    assert err_no_retry.message == "Provider rate limit exceeded"


def test_auth_expired_attributes() -> None:
    """Verify AuthExpired captures mailbox and raw error metadata (R1.5)."""
    raw = {"error": "invalid_grant"}
    err = AuthExpired("Token revoked", mailbox_id="mbx-123", raw_error=raw)
    assert err.mailbox_id == "mbx-123"
    assert err.raw_error == raw
    assert "Token revoked" in str(err)


def test_not_found_attributes() -> None:
    """Verify NotFound error initialization."""
    err = NotFound("Message msg-999 not found", provider="graph")
    assert err.provider == "graph"
    assert "Message msg-999 not found" in str(err)


def test_transient_and_permanent_distinction() -> None:
    """Verify Transient and Permanent error differentiation."""
    transient = Transient("Connection reset by peer", provider="imap")
    permanent = Permanent("Malformed RFC822 payload", provider="imap")

    assert isinstance(transient, Transient)
    assert not isinstance(transient, Permanent)
    assert isinstance(permanent, Permanent)
    assert not isinstance(permanent, Transient)


def test_retryable_and_permanent_marker_bases() -> None:
    """6.3a / R17.5: dispatch classifies provider errors by two bases, not five classes."""
    for retryable in (RateLimited, Transient):
        assert issubclass(retryable, RetryableProviderError)
        assert not issubclass(retryable, PermanentProviderError)
    for permanent in (AuthExpired, NotFound, Permanent):
        assert issubclass(permanent, PermanentProviderError)
        assert not issubclass(permanent, RetryableProviderError)
    assert issubclass(RetryableProviderError, ProviderError)
    assert issubclass(PermanentProviderError, ProviderError)


def test_rate_limited_exposes_retry_after_s_alias() -> None:
    """R1.6 / 6.6: RateLimited keeps retry_after and exposes the same value as retry_after_s."""
    err = RateLimited("slow down", retry_after=42.0, provider="gmail")
    assert err.retry_after == 42.0
    assert err.retry_after_s == 42.0
    assert RateLimited().retry_after_s is None


def test_transient_carries_optional_retry_after_s() -> None:
    """6.6: a 503 with Retry-After keeps the hint; the default is None."""
    assert Transient("busy", retry_after_s=120.0).retry_after_s == 120.0
    assert Transient("busy").retry_after_s is None


def test_parse_retry_after_delta_seconds() -> None:
    """RFC 9110 delay-seconds form."""
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after("-5") == 0.0


def test_parse_retry_after_http_date() -> None:
    """RFC 9110 HTTP-date form is measured from `now`; a past date means retry now."""
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Mon, 28 Sep 2026 12:02:00 GMT", now=now) == 120.0
    assert parse_retry_after("Mon, 28 Sep 2026 11:00:00 GMT", now=now) == 0.0


def test_parse_retry_after_absent_or_garbage() -> None:
    """No header means no hint; an unparseable header keeps the historical 60 s fallback."""
    assert parse_retry_after(None) is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("soon") == DEFAULT_RETRY_AFTER_S
    assert parse_retry_after("nan") == DEFAULT_RETRY_AFTER_S

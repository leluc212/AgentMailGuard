"""Unit tests for the common mail provider error taxonomy.

Requirements:
- R1.5: Translate provider errors into common taxonomy:
  RateLimited, AuthExpired, NotFound, Transient, Permanent.
- R1.6: RateLimited carries retry_after hint.
"""

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
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

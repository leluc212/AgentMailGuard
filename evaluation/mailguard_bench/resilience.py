"""Rate-limit detection and back-off for the benchmark run (task 7.19, spec §4b, §5).

The Gemini free-tier limit is unknown. A case that hits HTTP 429, whether in rag-email's
generation call or in a guard LLM stage, is retried with exponential back-off. A case that
still fails becomes an ``error`` row and is never counted as defended.

A 429 that says a quota, a balance, a spend limit or a daily cap is used up is not a rate limit
(``packages.core.provider_limits``; task 7.29): waiting does not lift it, so it is never backed off
and retried here. Its error carries ``quota_exhausted``, and the route breaker stops the run on it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from packages.core.provider_limits import is_quota_text, quota_exhaustion, quota_in_text
from packages.llm.protocol import LLMQuotaExhaustedError

_RATE_LIMIT = re.compile(
    r"\b(?:status|HTTP)\s*:?\s*429\b|\bRESOURCE_EXHAUSTED\b|\bToo Many Requests\b"
    # OpenRouter's HTTP 402 for its in-flight budget is transient (Retry-After): back off and
    # retry. Its other 402s (no credit, key limit) are not, and stop the run (route.RouteBreaker).
    r"|\bopenrouter_in_flight_budget\b",
    re.IGNORECASE,
)


class RateLimitedError(RuntimeError):
    """A guard LLM stage was rate limited; the whole case must be retried."""


def text_is_rate_limited(text: str) -> bool:
    """Whether an error's text is a transient rate limit (never a used-up quota)."""
    return bool(_RATE_LIMIT.search(text)) and not is_quota_text(text)


def response_quota(response: httpx.Response) -> str | None:
    """What an HTTP error response says ran out (``provider_limits.quota_exhaustion``), or None."""
    try:
        body = response.json()
    except ValueError:
        return None
    return quota_exhaustion(response.status_code, body)


def exhausted_quota(exc: BaseException) -> str | None:
    """What ran out when ``exc`` or its cause/context chain is a used-up quota, else None.

    ``LLMQuotaExhaustedError`` names it; an ``httpx.HTTPStatusError`` (the guard's provider chains
    one) is read from its response body; a text marked ``quota_exhausted`` says so itself.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LLMQuotaExhaustedError):
            return current.quota
        if isinstance(current, httpx.HTTPStatusError):
            found = response_quota(current.response)
            if found is not None:
                return found
        noted = quota_in_text(str(current))
        if noted is not None:
            return noted
        current = current.__cause__ or current.__context__
    return None


def is_rate_limited(exc: BaseException) -> bool:
    """True when ``exc`` or anything in its cause/context chain is an HTTP 429 rate limit.

    A used-up quota anywhere in the chain (``exhausted_quota``) makes it False: it is a stop, not
    a back-off.
    """
    if exhausted_quota(exc) is not None:
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RateLimitedError):
            return True
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code == 429:
            return True
        if text_is_rate_limited(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential back-off: attempt n waits min(cap, base * 2**(n-1)) seconds."""

    max_attempts: int = 6
    base_s: float = 2.0
    cap_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_s < 0 or self.cap_s < 0:
            raise ValueError("back-off delays must be >= 0")

    def delay(self, attempt: int) -> float:
        return float(min(self.cap_s, self.base_s * 2 ** (attempt - 1)))


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Mask every non-empty secret in ``text`` (error rows never carry the API key)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text

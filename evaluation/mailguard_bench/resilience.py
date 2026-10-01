"""Rate-limit detection and back-off for the benchmark run (task 7.19, spec §4b, §5).

The Gemini free-tier limit is unknown. A case that hits HTTP 429, whether in rag-email's
generation call or in a guard LLM stage, is retried with exponential back-off. A case that
still fails becomes an ``error`` row and is never counted as defended.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

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
    return bool(_RATE_LIMIT.search(text))


def is_rate_limited(exc: BaseException) -> bool:
    """True when ``exc`` or anything in its cause/context chain is an HTTP 429."""
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

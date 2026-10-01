"""Quota and credit exhaustion: the provider errors that waiting does not lift (task 7.29).

A provider answers HTTP 429 for two different things. A per-minute rate limit clears within
seconds, so the caller backs off and retries. A used-up quota, an exhausted prepaid balance, a
spend limit or a daily cap does not clear by waiting a few seconds (a daily cap resets the next
day), so retrying only fills the logs; the caller stops instead and says what ran out. This module
tells the two apart from what the provider documents; it reads a response the caller already has
and calls nothing.

Sources, read 2026-10-01:

- OpenAI, Error codes (https://developers.openai.com/api/docs/guides/error-codes). The 429
  entries ``credit_balance_exhausted`` ("Your organization has no prepaid credits remaining"),
  ``organization_spend_limit_exceeded``, ``project_spend_limit_exceeded`` and
  ``organization_usage_limit_exceeded``; "For billing-related errors, inspect ``error.code`` to
  identify the specific cause. The broader ``error.type`` can still be ``insufficient_quota``";
  and "Retrying billing, spend, or quota errors won't restore API access." The two per-minute
  429s, "Rate limit reached for requests" ("You are sending requests too quickly") and "Slow
  down" (``slow_down``, type ``rate_limit_error``), are transient: back off and retry.
- OpenAI, Rate limits (https://developers.openai.com/api/docs/guides/rate-limits): limits are
  measured as RPM, RPD, TPM, TPD and IPM, "requests per day" and "tokens per day" being the daily
  ones. A 429 whose message names a per-day measure is a daily cap; one that names a per-minute
  measure is transient.
- Gemini, Rate limits (https://ai.google.dev/gemini-api/docs/rate-limits): "Requests per day
  (RPD) quotas reset at midnight Pacific time" and "If you hit a spend-based rate limit, the API
  returns a ``429 RESOURCE_EXHAUSTED`` error". A RESOURCE_EXHAUSTED for a per-minute quota is
  transient; one whose details name a per-day quota (Google's quota ids spell it ``PerDay``) or a
  spend limit is not.

OpenRouter's no-credit HTTP 402 is classified where it always was (``limit_source``, the
route breaker and the ai-worker's failure policy); it is not a 429.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

QUOTA_MARKER = "quota_exhausted"
"""Leads the text of every error this module classifies, within the first characters, so the
200-character cut of a job's last error and of a triage stage's error keeps it; the benchmark's
route breaker stops a run on it."""

OPENAI_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)
"""OpenAI's documented ``error.code`` (and, for ``insufficient_quota``, ``error.type``) values of a
429 that retrying does not lift."""

DAILY_LIMIT = "daily_limit"
SPEND_LIMIT = "spend_limit"

_DAILY = re.compile(
    r"\b(?:requests|tokens) per day\b|\((?:RPD|TPD)\)|PerDay|\bper[- ]day\b", re.IGNORECASE
)
_SPEND = re.compile(r"\bspend[- ]based\b|\bspend(?:ing)? limit\b", re.IGNORECASE)


def _error_objects(body: Any) -> list[Mapping[str, Any]]:
    """The ``error`` objects of a response body: ``{"error": {...}}``, a list of those (Google's
    OpenAI-compatible endpoint), or a bare error object."""
    items = body if isinstance(body, list) else [body]
    found: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        error = item.get("error")
        if isinstance(error, Mapping):
            found.append(error)
        elif "code" in item or "type" in item or "status" in item:
            found.append(item)
    return found


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def quota_exhaustion(status: int | None, body: Any) -> str | None:
    """Why a provider's 429 is a quota or credit exhaustion that waiting does not lift, or None.

    Args:
        status: The HTTP status of the response (or the ``code`` of an error carried in an
            HTTP 200 body). Only 429 is classified.
        body: The parsed JSON body (a mapping, a list, or anything else, which counts as no body).

    Returns:
        OpenAI's documented code (``insufficient_quota``, ``credit_balance_exhausted``, the spend
        and usage limits), ``daily_limit`` for a per-day cap (OpenAI's RPD or TPD, Gemini's per-day
        quotas), ``spend_limit`` for Gemini's spend-based limit; None for a per-minute rate limit,
        a body that says nothing, or any other status.
    """
    if status != 429:
        return None
    for error in _error_objects(body):
        code, kind = _text(error.get("code")), _text(error.get("type"))
        if code in OPENAI_QUOTA_CODES:
            return code
        if kind == "insufficient_quota":
            return kind
        described = " ".join(
            (_text(error.get("message")), json.dumps(error.get("details") or [], default=str))
        )
        if _DAILY.search(described):
            return DAILY_LIMIT
        if _SPEND.search(described):
            return SPEND_LIMIT
    return None


def quota_note(reason: str) -> str:
    """The classifying words an error text starts with: ``quota_exhausted: <reason>``."""
    return f"{QUOTA_MARKER}: {reason}"


_NOTED = re.compile(re.escape(QUOTA_MARKER) + r"(?::\s*([A-Za-z0-9_.-]+))?")


def is_quota_text(text: str) -> bool:
    """Whether an error's text is one this module classified (it carries ``QUOTA_MARKER``)."""
    return QUOTA_MARKER in text


def quota_in_text(text: str) -> str | None:
    """What ran out, as an error text that carries ``QUOTA_MARKER`` names it; None without one.

    ``quota_exhausted: daily_limit`` gives ``daily_limit``; the marker alone gives ``unknown``.
    """
    found = _NOTED.search(text)
    if found is None:
        return None
    return found.group(1) or "unknown"

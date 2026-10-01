"""Quota and credit exhaustion versus a per-minute rate limit (task 7.29; owner decision B).

The bodies are the shapes the providers document (OpenAI's error-code and rate-limit guides,
Gemini's rate-limit page; see ``packages/core/provider_limits.py``). No network.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.core.provider_limits import (
    DAILY_LIMIT,
    OPENAI_QUOTA_CODES,
    QUOTA_MARKER,
    SPEND_LIMIT,
    is_quota_text,
    quota_exhaustion,
    quota_in_text,
    quota_note,
)


def openai_error(code: str | None, kind: str, message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


def gemini_error(quota_id: str, message: str = "You exceeded your current quota") -> list[Any]:
    """Google's OpenAI-compatible endpoint answers a list of error objects."""
    return [
        {
            "error": {
                "code": 429,
                "message": message,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}],
                    }
                ],
            }
        }
    ]


@pytest.mark.parametrize("code", sorted(OPENAI_QUOTA_CODES))
def test_every_documented_openai_billing_code_is_a_quota_exhaustion(code: str) -> None:
    body = openai_error(code, "insufficient_quota", "Your organization has no credits left.")
    assert quota_exhaustion(429, body) == code


def test_the_broader_insufficient_quota_type_is_one_too() -> None:
    body = openai_error(None, "insufficient_quota", "You exceeded your current quota.")
    assert quota_exhaustion(429, body) == "insufficient_quota"


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached for gpt-4o-mini in organization org-x on requests per day (RPD): "
        "Limit 10000, Used 10000, Requested 1. Please try again in 8.64s.",
        "Rate limit reached for gpt-4o-mini in organization org-x on tokens per day (TPD): "
        "Limit 2000000, Used 1999000, Requested 1500.",
    ],
)
def test_a_429_that_names_a_per_day_measure_is_a_daily_cap(message: str) -> None:
    body = openai_error("rate_limit_exceeded", "requests", message)
    assert quota_exhaustion(429, body) == DAILY_LIMIT


@pytest.mark.parametrize(
    "body",
    [
        openai_error(
            "rate_limit_exceeded",
            "requests",
            "Rate limit reached for gpt-4o-mini in organization org-x on requests per min "
            "(RPM): Limit 500, Used 500, Requested 1. Please try again in 120ms.",
        ),
        openai_error(
            "rate_limit_exceeded",
            "tokens",
            "Rate limit reached for gpt-4o-mini on tokens per min (TPM): Limit 200000.",
        ),
        openai_error("slow_down", "rate_limit_error", "Your request rate increased too quickly."),
        gemini_error("GenerateRequestsPerMinutePerProjectPerModel"),
        {"error": {"message": "Too Many Requests"}},
        "not json at all",
        None,
    ],
)
def test_a_per_minute_rate_limit_is_never_a_quota_exhaustion(body: Any) -> None:
    assert quota_exhaustion(429, body) is None


def test_a_gemini_per_day_quota_is_a_daily_cap_and_a_spend_limit_is_named() -> None:
    assert quota_exhaustion(429, gemini_error("EmbedContentRequestsPerDayPerProjectPerModel")) == (
        DAILY_LIMIT
    )
    spend = gemini_error("x", message="You hit your spend-based rate limit for this project.")
    assert quota_exhaustion(429, spend) == SPEND_LIMIT


@pytest.mark.parametrize("status", [None, 200, 400, 401, 402, 404, 500, 503])
def test_only_a_429_is_classified(status: int | None) -> None:
    body = openai_error("insufficient_quota", "insufficient_quota", "no credit")
    assert quota_exhaustion(status, body) is None


def test_the_note_leads_with_the_marker_and_reads_back() -> None:
    note = quota_note("credit_balance_exhausted")
    assert note == "quota_exhausted: credit_balance_exhausted"
    text = f"LLM request failed with status 429 ({note}): {{...}}"
    assert is_quota_text(text) and quota_in_text(text) == "credit_balance_exhausted"
    assert quota_in_text(QUOTA_MARKER) == "unknown"
    assert quota_in_text("LLM request failed with status 429: slow down") is None

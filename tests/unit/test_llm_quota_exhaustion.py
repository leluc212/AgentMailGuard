"""A used-up quota stops; a per-minute rate limit is backed off (task 7.29; owner decision B).

Through the real clients over ``httpx.MockTransport``: rag-email's LLM client, the embedder and
the ai-worker's failure policy. No network, no key. The benchmark's side (its breaker and its
rate-limit back-off) is tested in ``test_mailguard_bench_route_failures.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.knowledge.embedder import (
    EmbeddingQuotaExhaustedError,
    EmbeddingRateLimitError,
    HttpEmbedder,
)
from packages.llm.client import HttpLLMProvider
from packages.llm.protocol import (
    ChatMessage,
    LLMQuotaExhaustedError,
    LLMResponseError,
    ModelTier,
)
from services.ai_worker.failure_policy import Disposition, classify_generation_failure

MESSAGES = [ChatMessage(role="user", content="hello")]
# OpenAI pretty-prints its error bodies; the code comes after a long message (error-code guide).
INSUFFICIENT_QUOTA = {
    "error": {
        "message": "You exceeded your current quota, please check your plan and billing "
        "details. For more information on this error, read the docs: "
        "https://platform.openai.com/docs/guides/error-codes/api-errors.",
        "type": "insufficient_quota",
        "param": None,
        "code": "insufficient_quota",
    }
}
DAILY_CAP = {
    "error": {
        "message": "Rate limit reached for gpt-4o-mini in organization org-x on requests per "
        "day (RPD): Limit 10000, Used 10000, Requested 1. Please try again in 8.64s.",
        "type": "requests",
        "param": None,
        "code": "rate_limit_exceeded",
    }
}
PER_MINUTE = {
    "error": {
        "message": "Rate limit reached for gpt-4o-mini in organization org-x on requests per "
        "min (RPM): Limit 500, Used 500, Requested 1. Please try again in 120ms.",
        "type": "requests",
        "param": None,
        "code": "rate_limit_exceeded",
    }
}


class Endpoint:
    def __init__(self, body: Any, status: int = 429) -> None:
        self.body, self.status = body, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = json.dumps(self.body, indent=4).encode()  # as OpenAI sends it
        return httpx.Response(
            self.status, content=content, headers={"content-type": "application/json"}
        )


def llm(endpoint: Endpoint) -> HttpLLMProvider:
    return HttpLLMProvider(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model_map=dict.fromkeys(ModelTier, "gpt-4o-mini"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
    )


@pytest.mark.parametrize(
    ("body", "quota"),
    [(INSUFFICIENT_QUOTA, "insufficient_quota"), (DAILY_CAP, "daily_limit")],
)
async def test_a_used_up_quota_raises_the_quota_error_with_the_marker_first(
    body: dict[str, Any], quota: str
) -> None:
    with pytest.raises(LLMQuotaExhaustedError) as caught:
        await llm(Endpoint(body)).generate(messages=MESSAGES)

    assert caught.value.status_code == 429 and caught.value.quota == quota
    assert str(caught.value).startswith(
        f"LLM request failed with status 429 (quota_exhausted: {quota})"
    )


async def test_a_per_minute_rate_limit_stays_a_retried_429() -> None:
    with pytest.raises(LLMResponseError) as caught:
        await llm(Endpoint(PER_MINUTE)).generate(messages=MESSAGES)

    assert not isinstance(caught.value, LLMQuotaExhaustedError)
    decision = classify_generation_failure(caught.value, job_state="GENERATING")
    assert decision.disposition is Disposition.RETRY


async def test_a_quota_error_in_an_http_200_body_is_the_quota_error_too() -> None:
    body = {"error": {"code": 429, "message": "per-day limit for this key: requests per day"}}
    with pytest.raises(LLMQuotaExhaustedError) as caught:
        await llm(Endpoint(body, status=200)).generate(messages=MESSAGES)

    assert "in an HTTP 200 body" in str(caught.value) and caught.value.quota == "daily_limit"


async def test_the_ai_worker_dead_letters_a_used_up_quota_instead_of_holding_the_job() -> None:
    with pytest.raises(LLMQuotaExhaustedError) as caught:
        await llm(Endpoint(INSUFFICIENT_QUOTA)).generate(messages=MESSAGES)

    decision = classify_generation_failure(caught.value, job_state="GENERATING")
    assert decision.disposition is Disposition.DEAD_LETTER
    assert "quota_exhausted: insufficient_quota" in decision.reason


# --- the embedder -----------------------------------------------------------------------------


def embedder(endpoint: Endpoint, retries: int = 3) -> HttpEmbedder:
    return HttpEmbedder(
        api_key="embed-test",
        max_retries=retries,
        retry_delay_s=0.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
    )


async def test_the_embedder_stops_at_once_on_a_used_up_quota_and_never_echoes_the_body() -> None:
    endpoint = Endpoint(INSUFFICIENT_QUOTA)
    with pytest.raises(EmbeddingQuotaExhaustedError) as caught:
        await embedder(endpoint).embed_texts(["case text that must not leak"])

    assert len(endpoint.requests) == 1  # no retries: waiting does not lift it
    assert str(caught.value) == (
        "Embedding request failed with status 429 (quota_exhausted: insufficient_quota)"
    )
    assert "billing" not in str(caught.value)


async def test_the_embedder_still_retries_a_per_minute_rate_limit() -> None:
    endpoint = Endpoint(PER_MINUTE)
    with pytest.raises(EmbeddingRateLimitError):
        await embedder(endpoint, retries=2).embed_texts(["x"])

    assert len(endpoint.requests) == 3


async def test_a_gemini_daily_embedding_quota_is_a_quota_error() -> None:
    body = [
        {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota",
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"violations": [{"quotaId": "EmbedContentRequestsPerDayPerProject"}]}],
            }
        }
    ]
    with pytest.raises(EmbeddingQuotaExhaustedError) as caught:
        await embedder(Endpoint(body)).embed_texts(["x"])

    assert caught.value.quota == "daily_limit"

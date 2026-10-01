"""The kit under the owner decisions of 2026-10-01 (task 7.29; ADR-0014): the one-call embedding
check before any model spend (D).

The kit's fakes (``mailguard_kit_fixtures``) stand in for docker, the runner and the guard-worker;
the embedding endpoint is an ``httpx.MockTransport``. No test reaches a real endpoint or a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluation.mailguard_bench.kit.campaign import run_campaign
from evaluation.mailguard_bench.kit.embedding_check import (
    PROBE_TEXT,
    EmbeddingCheckError,
    embedding_settings,
    probe_embedding,
)
from tests.unit.mailguard_kit_fixtures import (  # noqa: F401  (bench_fixture is the `bench` fixture)
    EMBED_KEY,
    HOST_ENV,
    STACK_UP,
    Bench,
    EmbeddingEndpoint,
    bench_fixture,
    opts,
    sequence,
)

REPO = Path(__file__).resolve().parents[2]


# --- D: one embedding call before any model spend ---------------------------------------------


def test_the_run_makes_one_embedding_call_through_the_services_embedder_before_the_stack(
    bench: Bench,
) -> None:
    assert run_campaign(bench.ctx, opts(configs=("C0",))) == 0

    (request,) = bench.embedding.requests
    sent = json.loads(request.content)
    assert request.url == "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
    assert sent == {"input": [PROBE_TEXT], "model": "gemini-embedding-001", "dimensions": 1536}
    assert request.headers["authorization"] == f"Bearer {EMBED_KEY}"
    steps = [s["step"] for s in bench.kit_log()]
    assert steps.index("embedding_check") < steps.index("stack")
    (check,) = [s for s in bench.kit_log() if s["step"] == "embedding_check"]
    assert (check["status"], check["dimension"], check["embedding_model"]) == (
        "ok",
        1536,
        "gemini-embedding-001",
    )
    printed = "\n".join(bench.out + bench.err)
    assert "ok embedding gemini-embedding-001" in printed
    assert EMBED_KEY not in printed and EMBED_KEY not in json.dumps(bench.kit_log())


def test_an_endpoint_that_ignores_dimensions_stops_the_run_before_the_stack_is_touched(
    bench: Bench,
) -> None:
    bench.embedding.width = 3072  # gemini-embedding-001's own width, when `dimensions` is ignored

    assert run_campaign(bench.ctx, opts()) == 1

    assert STACK_UP not in sequence(bench.host) and "live.run C0" not in sequence(bench.host)
    text = "\n".join(bench.err)
    assert "embedding check refused this run" in text
    assert "did not return 1536-dimension vectors" in text and "ignores the `dimensions`" in text
    (check,) = [s for s in bench.kit_log() if s["step"] == "embedding_check"]
    assert check["status"] == "failed" and "3072" in check["reason"]


@pytest.mark.parametrize(
    ("status", "body", "words"),
    [
        (400, {"error": {"message": "dimensions not supported"}}, "text-embedding-ada-002"),
        (401, {"error": {"message": "bad key"}}, "refused the key"),
        (404, {"error": {"message": "no model"}}, "no such model or URL"),
        (
            429,
            {"error": {"message": "x", "type": "insufficient_quota", "code": "insufficient_quota"}},
            "quota_exhausted: insufficient_quota",
        ),
        (429, {"error": {"message": "Rate limit reached on requests per min (RPM)"}}, "wait a"),
    ],
)
def test_a_refused_embedding_call_is_one_request_and_says_what_to_fix(
    bench: Bench, status: int, body: dict[str, Any], words: str
) -> None:
    bench.embedding.status, bench.embedding.body = status, body

    assert run_campaign(bench.ctx, opts()) == 1

    assert len(bench.embedding.requests) == 1  # one call, never the embedder's retries
    assert words in "\n".join(bench.err)
    assert STACK_UP not in sequence(bench.host)


def test_a_dry_run_names_the_embedding_check_and_calls_nothing(bench: Bench) -> None:
    assert run_campaign(bench.ctx, opts(dry_run=True)) == 0

    assert bench.embedding.requests == []
    assert any("one embedding call" in line for line in bench.out)


async def test_the_fake_embedder_is_never_checked_or_accepted() -> None:
    settings = embedding_settings({**HOST_ENV, "EMBEDDING__MOCK": "true"})
    with pytest.raises(EmbeddingCheckError, match="fake embedder"):
        await probe_embedding(settings, transport=EmbeddingEndpoint().transport())


async def test_an_empty_answer_is_refused_rather_than_padded_with_zeros() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "usage": {}})

    with pytest.raises(EmbeddingCheckError, match="0 vector"):
        await probe_embedding(embedding_settings(HOST_ENV), transport=httpx.MockTransport(handler))


def test_the_check_reads_the_runners_settings_for_one_request() -> None:
    settings = embedding_settings({**HOST_ENV, "EMBEDDING__MAX_RETRIES": "5", "OTHER": "x"})
    assert settings.max_retries == 0 and settings.mock is False
    assert settings.model_name == "gemini-embedding-001" and settings.dimension == 1536
    with pytest.raises(EmbeddingCheckError, match="EMBEDDING__DIMENSION") as caught:
        embedding_settings({**HOST_ENV, "EMBEDDING__DIMENSION": "wide"})
    assert EMBED_KEY not in str(caught.value)


def test_the_doctor_stays_free_of_calls() -> None:
    doctor = (REPO / "evaluation" / "mailguard_bench" / "kit" / "doctor.py").read_text("utf-8")
    assert "embedding_check" not in doctor and "get_embedder" not in doctor

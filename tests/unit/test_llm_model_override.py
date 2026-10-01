"""A ``model`` passed to ``generate`` names the concrete model, and the result reports it (R14.5).

The thread summarizer sends its own model (SUMMARIZATION__SUMMARIZER_MODEL, R8.3) in place of the
tier's. The request body always carried it, but both HTTP providers still reported the tier's model
in ``LLMResult.model``, so the token counters, ``estimated_ai_cost_total`` and the ``llm_inference``
log line were recorded under the wrong model and price (R21.4, R21.6).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from packages.context.summarizer import ThreadSummarizer
from packages.core.settings import ModelPricing, SummarizationSettings
from packages.db.thread_state import InMemoryThreadStateStore
from packages.domain.entities import EmailAddress, NormalizedMessage
from packages.llm.anthropic import AnthropicLLMProvider
from packages.llm.budget import CallKind
from packages.llm.client import HttpLLMProvider, LocalLLMProvider, OpenAILLMProvider
from packages.llm.inference_metrics import INFERENCE_LOG_EVENT
from packages.llm.instrumented import InstrumentedLLMProvider
from packages.llm.protocol import ChatMessage, ModelTier
from packages.observability.metrics import create_pipeline_metrics

OVERRIDE = "qwen2.5:7b-instruct"
TIER_MODEL = "gpt-4o-mini"  # HttpLLMProvider's FAST tier
PRICES = {
    OVERRIDE: ModelPricing(input_per_m=0.10, output_per_m=0.20),
    TIER_MODEL: ModelPricing(input_per_m=0.15, output_per_m=0.60),
}
MESSAGES = [ChatMessage(role="user", content="Summarize this thread.")]
SUMMARY = {
    "topic": "Refund",
    "current_intent": "refund_request",
    "summary": "The customer wants a refund for a duplicate charge.",
    "open_questions": [],
    "resolved_items": [],
}

Provider = HttpLLMProvider | AnthropicLLMProvider


def _wire(kind: str, sent: list[dict[str, Any]]) -> Provider:
    """A provider of the given kind whose requests land in ``sent`` and are answered offline."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if kind == "anthropic":
            body: dict[str, Any] = {
                "content": [{"type": "text", "text": json.dumps(SUMMARY)}],
                "usage": {"input_tokens": 50, "output_tokens": 30},
                "stop_reason": "end_turn",
            }
        else:
            body = {
                "choices": [{"message": {"content": json.dumps(SUMMARY)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 30},
            }
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if kind == "anthropic":
        return AnthropicLLMProvider(api_key="test-key", client=client)
    http_kinds: dict[str, type[HttpLLMProvider]] = {
        "http": HttpLLMProvider,
        "openai": OpenAILLMProvider,
        "local": LocalLLMProvider,
    }
    return http_kinds[kind](api_key="test-key", client=client)


KINDS = ["http", "openai", "local", "anthropic"]


@pytest.mark.parametrize("kind", KINDS)
async def test_the_result_names_the_model_the_request_was_sent_to(kind: str) -> None:
    sent: list[dict[str, Any]] = []
    provider = _wire(kind, sent)

    result = await provider.generate(messages=MESSAGES, tier=ModelTier.FAST, model=OVERRIDE)

    assert [body["model"] for body in sent] == [OVERRIDE]
    assert result.model == OVERRIDE


@pytest.mark.parametrize("kind", KINDS)
async def test_without_an_override_the_tier_model_is_sent_and_reported(kind: str) -> None:
    sent: list[dict[str, Any]] = []
    provider = _wire(kind, sent)

    result = await provider.generate(messages=MESSAGES, tier=ModelTier.FAST)

    tier_model = provider.resolve_model(ModelTier.FAST)
    assert [body["model"] for body in sent] == [tier_model]
    assert result.model == tier_model


@pytest.mark.parametrize("override", [None, ""])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_blank_override_means_the_tier_model(kind: str, override: str | None) -> None:
    sent: list[dict[str, Any]] = []
    provider = _wire(kind, sent)

    result = await provider.generate(messages=MESSAGES, tier=ModelTier.FAST, model=override)

    tier_model = provider.resolve_model(ModelTier.FAST)
    assert [body["model"] for body in sent] == [tier_model]
    assert result.model == tier_model


@pytest.mark.parametrize("kind", KINDS)
async def test_other_params_still_reach_the_request(kind: str) -> None:
    sent: list[dict[str, Any]] = []
    provider = _wire(kind, sent)

    await provider.generate(messages=MESSAGES, tier=ModelTier.FAST, model=OVERRIDE, seed=42)

    assert sent[0]["seed"] == 42 and sent[0]["model"] == OVERRIDE


def _thread(count: int) -> list[NormalizedMessage]:
    org_id, thread_id = uuid4(), uuid4()
    start = datetime.now(UTC) - timedelta(hours=count)
    return [
        NormalizedMessage(
            message_id=uuid4(),
            thread_id=thread_id,
            mailbox_id=uuid4(),
            organization_id=org_id,
            provider="mock",
            provider_message_id=f"m-{i}",
            sender=EmailAddress(email="alice@example.com"),
            received_at=start + timedelta(hours=i),
            body_text_clean=f"Message {i} about the duplicate charge.",
        )
        for i in range(count)
    ]


async def test_summary_tokens_cost_and_log_are_booked_under_the_configured_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through InstrumentedLLMProvider, as the ai-worker wires the summarizer (R21.4, R21.6)."""
    sent: list[dict[str, Any]] = []
    metrics = create_pipeline_metrics()
    summarizer = ThreadSummarizer(
        llm=InstrumentedLLMProvider(
            _wire("http", sent), kind=CallKind.SUMMARIZE, metrics=metrics, price_table=PRICES
        ),
        store=InMemoryThreadStateStore(),
        settings=SummarizationSettings(summarizer_model=OVERRIDE),
    )
    messages = _thread(5)
    caplog.set_level(logging.INFO)

    result = await summarizer.summarize_thread(
        messages[0].organization_id, messages[0].thread_id, messages
    )

    assert result.summarized and [body["model"] for body in sent] == [OVERRIDE]
    booked = {"model": OVERRIDE, "tier": "fast"}
    assert metrics.registry.get_sample_value("input_tokens_total", booked) == 50
    assert metrics.registry.get_sample_value("output_tokens_total", booked) == 30
    assert metrics.registry.get_sample_value("estimated_ai_cost_total", booked) == pytest.approx(
        (50 * 0.10 + 30 * 0.20) / 1e6, abs=1e-9
    )
    tier_booked = {"model": TIER_MODEL, "tier": "fast"}
    assert metrics.registry.get_sample_value("input_tokens_total", tier_booked) is None
    assert metrics.registry.get_sample_value("estimated_ai_cost_total", tier_booked) is None
    lines = [r.__dict__["fields"] for r in caplog.records if r.getMessage() == INFERENCE_LOG_EVENT]
    assert [(line["kind"], line["model"]) for line in lines] == [("summarize", OVERRIDE)]
    assert lines[0]["estimated_cost_usd"] == pytest.approx(11e-6, abs=1e-9)

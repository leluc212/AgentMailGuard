"""SUMMARIZATION__SUMMARIZER_MODEL picks the model that writes thread summaries (R8.3).

Unset (the default), the summarizer stays on the FAST tier. Only a value the operator set is
honoured: the field's default names an example model, which must never be sent to whatever
endpoint the FAST tier points at.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from packages.context.summarizer import SummarizationResult, ThreadSummarizer
from packages.core.settings import AIWorkerSettings, SummarizationSettings
from packages.db.thread_state import InMemoryThreadStateStore
from packages.domain.entities import EmailAddress, NormalizedMessage
from packages.knowledge.token_counter import TokenCounter
from packages.llm.client import HttpLLMProvider
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import LLMProvider, ModelTier
from services.ai_worker.main import build_consumers
from tests.stubs.worker_resources import fake_worker_resources

SUMMARY = {
    "topic": "Refund",
    "current_intent": "refund_request",
    "summary": "The customer wants a refund for a duplicate charge.",
    "open_questions": [],
    "resolved_items": [],
}
CONFIGURED = "qwen2.5:7b-instruct"


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


async def _summarize(settings: SummarizationSettings, provider: LLMProvider) -> SummarizationResult:
    messages = _thread(5)
    summarizer = ThreadSummarizer(llm=provider, store=InMemoryThreadStateStore(), settings=settings)
    return await summarizer.summarize_thread(
        messages[0].organization_id, messages[0].thread_id, messages
    )


def _http_provider(sent: list[dict[str, Any]]) -> HttpLLMProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = {
            "choices": [{"message": {"content": json.dumps(SUMMARY)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30},
        }
        return httpx.Response(200, json=body)

    return HttpLLMProvider(
        api_key="test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_configured_model_is_requested_on_the_fast_tier() -> None:
    provider = FakeLLMProvider(default_response=SUMMARY)

    result = await _summarize(SummarizationSettings(summarizer_model=CONFIGURED), provider)

    call = provider.recorded_calls[0]
    assert call["params"] == {"model": CONFIGURED}
    assert call["tier"] == ModelTier.FAST
    assert result.summarized and result.model == CONFIGURED


async def test_unset_model_leaves_the_fast_tier_in_charge() -> None:
    provider = FakeLLMProvider(default_response=SUMMARY)

    result = await _summarize(SummarizationSettings(), provider)

    assert SummarizationSettings().summarizer_model == "gpt-4o-mini"  # an example, not a choice
    assert provider.recorded_calls[0]["params"] == {}
    assert result.model == "fake-fast-model"


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_model_means_unset(blank: str) -> None:
    """Compose forwards an unset host variable as an empty string."""
    provider = FakeLLMProvider(default_response=SUMMARY)

    await _summarize(SummarizationSettings(summarizer_model=blank), provider)

    assert provider.recorded_calls[0]["params"] == {}


async def test_no_model_is_reported_when_nothing_was_summarized() -> None:
    provider = FakeLLMProvider(default_response=SUMMARY)
    messages = _thread(2)
    summarizer = ThreadSummarizer(
        llm=provider,
        store=InMemoryThreadStateStore(),
        settings=SummarizationSettings(summarizer_model=CONFIGURED),
    )

    result = await summarizer.summarize_thread(
        messages[0].organization_id, messages[0].thread_id, messages
    )

    assert not result.summarized and result.model is None
    assert provider.recorded_calls == []


async def test_configured_model_reaches_the_request_body() -> None:
    """Through the real HTTP provider: the payload names the configured model."""
    sent: list[dict[str, Any]] = []

    result = await _summarize(
        SummarizationSettings(summarizer_model=CONFIGURED), _http_provider(sent)
    )

    assert [body["model"] for body in sent] == [CONFIGURED]
    assert result.model == CONFIGURED


async def test_unset_model_sends_the_fast_tier_model() -> None:
    sent: list[dict[str, Any]] = []

    result = await _summarize(SummarizationSettings(), _http_provider(sent))

    assert [body["model"] for body in sent] == ["gpt-4o-mini"]  # HttpLLMProvider's FAST tier
    assert result.model == "gpt-4o-mini"


def test_environment_variable_reaches_the_ai_worker_summarizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUMMARIZATION__SUMMARIZER_MODEL", CONFIGURED)
    settings = AIWorkerSettings(_env_file=None)

    consumers = build_consumers(fake_worker_resources(settings), token_counter=TokenCounter())

    summarizer = consumers[0].summarizer
    assert summarizer is not None and summarizer.model == CONFIGURED


def test_other_summarization_variables_do_not_select_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUMMARIZATION__MIN_MESSAGES_THRESHOLD", "3")
    settings = AIWorkerSettings(_env_file=None)

    consumers = build_consumers(fake_worker_resources(settings), token_counter=TokenCounter())

    summarizer = consumers[0].summarizer
    assert summarizer is not None and summarizer.model is None

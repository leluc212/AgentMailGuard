"""Demo fault markers (task 9.6, R25.11, ADR-0018 decision 11)."""

from __future__ import annotations

import pytest

from packages.core.run_context import run_options_scope
from packages.core.settings import DemoSettings, LLMTiersSettings
from packages.llm.fake import DEMO_FAULT_INVALID_JSON, DEMO_FAULT_TIMEOUT, FakeLLMProvider
from packages.llm.model_profile import ProfileRoutedLLMProvider, build_profile_provider
from packages.llm.protocol import ChatMessage, LLMTimeoutError

REPLY_SCHEMA = {"required": ["draft"]}


def _ask(marker: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=f"Is the X200 under warranty? {marker}")]


async def test_a_timeout_marker_raises_a_timeout() -> None:
    with pytest.raises(LLMTimeoutError):
        await FakeLLMProvider(fault_markers=True).generate(messages=_ask(DEMO_FAULT_TIMEOUT))


async def test_an_invalid_json_marker_answers_no_schema() -> None:
    result = await FakeLLMProvider(fault_markers=True).generate(
        messages=_ask(DEMO_FAULT_INVALID_JSON), schema=REPLY_SCHEMA
    )

    assert result.content == {"demo_fault": "invalid-json"}


@pytest.mark.parametrize("marker", [DEMO_FAULT_TIMEOUT, DEMO_FAULT_INVALID_JSON])
async def test_markers_are_ignored_unless_enabled(marker: str) -> None:
    result = await FakeLLMProvider().generate(messages=_ask(marker), schema=REPLY_SCHEMA)

    assert "draft" in result.content


async def test_a_live_provider_never_reads_markers() -> None:
    live = FakeLLMProvider()  # stands in for a real provider: built without fault_markers
    routed = ProfileRoutedLLMProvider(fake=FakeLLMProvider(fault_markers=True), live=live)

    with run_options_scope({"model_profile": "live"}):
        result = await routed.generate(messages=_ask(DEMO_FAULT_TIMEOUT), schema=REPLY_SCHEMA)

    assert "draft" in result.content
    assert len(live.recorded_calls) == 1


@pytest.mark.parametrize(
    ("enabled", "markers", "active"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_markers_need_the_demo_enabled(enabled: bool, markers: bool, active: bool) -> None:
    settings = DemoSettings(enabled=enabled, fault_markers_enabled=markers)

    assert settings.fault_markers_active is active


async def test_the_builder_passes_the_switch_to_the_fake_only() -> None:
    routed = build_profile_provider(LLMTiersSettings(provider="fake"), fault_markers=True)

    with pytest.raises(LLMTimeoutError):
        await routed.generate(messages=_ask(DEMO_FAULT_TIMEOUT))

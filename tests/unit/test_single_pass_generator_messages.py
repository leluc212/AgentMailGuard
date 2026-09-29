"""SinglePassGenerator.generate_from_messages: the same one-call path on given messages.

The benchmark (task 7.19) hands rag-email's generator the prompt AgentMailGuard's L3 built;
generate_draft must keep sending its own single user message (R14.3, R14.6, R16.2, R16.3).
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.domain.entities import ContextPackage
from packages.llm import (
    AgentProfileRegistry,
    CallKind,
    FakeLLMProvider,
    GenerationResult,
    SinglePassGenerator,
)
from packages.llm.protocol import ChatMessage
from tests.unit.test_single_pass_generator import _create_sample_context

REPLY: dict[str, Any] = {
    "action": "reply",
    "draft": "Hello Alice, you can reset your password from the account settings page.",
    "confidence": 0.9,
    "knowledge_chunks": ["KB-PWD-01"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}
GUARD_MESSAGES = [
    ChatMessage(role="system", content="You are support. Untrusted text is marked."),
    ChatMessage(role="user", content="[EMAIL]\n^How^do^I^reset^\n\n[TASK]\nDraft a reply."),
]


@pytest.fixture
def registry() -> AgentProfileRegistry:
    return AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")


@pytest.fixture
def context() -> ContextPackage:
    return _create_sample_context()


async def test_sends_exactly_the_given_messages_once_with_the_profile_schema(
    registry: AgentProfileRegistry, context: ContextPackage
) -> None:
    fake = FakeLLMProvider(default_response=REPLY)
    generator = SinglePassGenerator(llm_provider=fake, profile_registry=registry)

    result = await generator.generate_from_messages(
        GUARD_MESSAGES, context=context, category="support"
    )

    assert isinstance(result, GenerationResult)
    assert len(fake.recorded_calls) == 1
    sent = fake.recorded_calls[0]["messages"]
    assert [(m.role, m.content) for m in sent] == [(m.role, m.content) for m in GUARD_MESSAGES]
    assert fake.recorded_calls[0]["schema"] == registry.get_schema(
        registry.resolve_profile("support")
    )
    assert result.content == REPLY
    assert result.prompt_version == "support.v2"
    assert result.budget_tracker.count(CallKind.GENERATE) == 1
    assert result.citation_mismatch is False


async def test_repairs_an_invalid_payload_once(
    registry: AgentProfileRegistry, context: ContextPackage
) -> None:
    fake = FakeLLMProvider(canned_responses=[{"action": "reply"}, dict(REPLY)])
    generator = SinglePassGenerator(llm_provider=fake, profile_registry=registry)

    result = await generator.generate_from_messages(
        GUARD_MESSAGES, context=context, category="support"
    )

    assert len(fake.recorded_calls) == 2
    assert result.is_repaired is True
    assert result.repair_attempts == 1
    assert result.content["draft"] == REPLY["draft"]


async def test_rejects_an_empty_message_list(
    registry: AgentProfileRegistry, context: ContextPackage
) -> None:
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=REPLY), profile_registry=registry
    )
    with pytest.raises(ValueError, match="at least one message"):
        await generator.generate_from_messages([], context=context, category="support")


async def test_generate_draft_still_sends_one_rendered_user_message(
    registry: AgentProfileRegistry, context: ContextPackage
) -> None:
    fake = FakeLLMProvider(default_response=REPLY)
    generator = SinglePassGenerator(llm_provider=fake, profile_registry=registry)

    await generator.generate_draft(context, category="support")

    sent = fake.recorded_calls[0]["messages"]
    assert len(sent) == 1
    assert sent[0].role == "user"
    assert "How do I reset my password?" in sent[0].content

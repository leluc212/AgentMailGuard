"""The offline provider answers the draft and summary schemas (R24.5; 4.13b D5)."""

from __future__ import annotations

import json
from pathlib import Path

from packages.context.policy import THREAD_SUMMARY_SCHEMA
from packages.llm.fake import FAKE_REPLY, FAKE_THREAD_SUMMARY, FakeLLMProvider
from packages.llm.protocol import ChatMessage
from packages.llm.validation import validate_draft_payload

REPLY_SCHEMA = json.loads(Path("schemas/reply.v1.json").read_text())
MESSAGES = [ChatMessage(role="user", content="Please help")]


async def test_fake_answers_the_reply_schema_with_a_valid_draft() -> None:
    result = await FakeLLMProvider().generate(messages=MESSAGES, schema=REPLY_SCHEMA)
    assert result.content == FAKE_REPLY
    validate_draft_payload(result.content, REPLY_SCHEMA)


async def test_fake_answers_the_summary_schema() -> None:
    result = await FakeLLMProvider().generate(messages=MESSAGES, schema=THREAD_SUMMARY_SCHEMA)
    assert result.content == FAKE_THREAD_SUMMARY
    assert set(THREAD_SUMMARY_SCHEMA.get("required", [])) <= set(result.content)


async def test_fake_keeps_the_triage_default_for_other_schemas() -> None:
    result = await FakeLLMProvider().generate(messages=MESSAGES, schema={"type": "object"})
    assert result.content["category"] == "support"


async def test_explicit_default_response_still_wins() -> None:
    explicit = {"action": "reply"}
    result = await FakeLLMProvider(default_response=explicit).generate(
        messages=MESSAGES, schema=REPLY_SCHEMA
    )
    assert result.content == explicit

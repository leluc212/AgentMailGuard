"""Unit tests for draft reply schema validation and repair prompts (R16.1, R16.2, R16.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from packages.llm import (
    ChatMessage,
    DraftReplyPayload,
    DraftSchemaContractError,
    DraftValidationError,
    LLMSchemaValidationError,
    UnvalidatedDraftError,
    build_repair_messages,
    validate_draft_payload,
)


@pytest.fixture
def reply_schema() -> dict[str, Any]:
    schema_path = Path("schemas/reply.v1.json")
    assert schema_path.is_file(), "schemas/reply.v1.json must exist"
    return cast(dict[str, Any], json.loads(schema_path.read_text(encoding="utf-8")))


@pytest.fixture
def valid_payload_dict() -> dict[str, Any]:
    return {
        "action": "reply",
        "draft": "Thank you for reaching out. We have resolved the issue.",
        "confidence": 0.95,
        "knowledge_chunks": ["chunk-101", "chunk-102"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }


def test_reply_schema_requires_all_six_fields(reply_schema: dict[str, Any]) -> None:
    """Verify schemas/reply.v1.json requires all 6 R16.1 fields."""
    expected_required = [
        "action",
        "draft",
        "confidence",
        "knowledge_chunks",
        "thread_summary_updated",
        "model_tier",
    ]
    required = reply_schema.get("required", [])
    for field_name in expected_required:
        assert field_name in required, f"Field '{field_name}' must be required in schema"
    assert len(required) == len(expected_required)

    properties = reply_schema.get("properties", {})
    for field_name in expected_required:
        assert field_name in properties, (
            f"Field '{field_name}' must be defined in schema properties"
        )
    assert reply_schema.get("additionalProperties") is False


def test_validate_valid_draft_payload(
    valid_payload_dict: dict[str, Any], reply_schema: dict[str, Any]
) -> None:
    """Verify validate_draft_payload parses and validates a valid dictionary payload.

    Covers R16.1 and R16.2.
    """
    result = validate_draft_payload(valid_payload_dict, schema=reply_schema)

    assert isinstance(result, DraftReplyPayload)
    assert result.action == "reply"
    assert result.draft == "Thank you for reaching out. We have resolved the issue."
    assert result.confidence == 0.95
    assert result.knowledge_chunks == ["chunk-101", "chunk-102"]
    assert result.thread_summary_updated is False
    assert result.model_tier == "routine"


def test_validate_raw_json_string_payload(
    valid_payload_dict: dict[str, Any], reply_schema: dict[str, Any]
) -> None:
    """Verify validate_draft_payload parses and validates a valid JSON string (R16.2)."""
    json_str = json.dumps(valid_payload_dict)
    result = validate_draft_payload(json_str, schema=reply_schema)

    assert isinstance(result, DraftReplyPayload)
    assert result.action == "reply"
    assert result.confidence == 0.95
    assert result.model_tier == "routine"


@pytest.mark.parametrize(
    "missing_field",
    [
        "action",
        "draft",
        "confidence",
        "knowledge_chunks",
        "thread_summary_updated",
        "model_tier",
    ],
)
def test_validate_missing_field_raises_draft_validation_error(
    valid_payload_dict: dict[str, Any],
    reply_schema: dict[str, Any],
    missing_field: str,
) -> None:
    """Verify missing any of the 6 required fields raises DraftValidationError (R16.1, R16.2)."""
    payload = dict(valid_payload_dict)
    del payload[missing_field]

    # Test with schema
    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(payload, schema=reply_schema)
    assert missing_field in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()

    # Test without schema (default behavior still requires all fields)
    with pytest.raises(DraftValidationError):
        validate_draft_payload(payload)


@pytest.mark.parametrize(
    "invalid_action",
    [
        "invalid",
        "reply_all",
        "send",
        "",
        "REPLY",
    ],
)
def test_validate_invalid_action_enum(
    valid_payload_dict: dict[str, Any],
    reply_schema: dict[str, Any],
    invalid_action: str,
) -> None:
    """Verify action must be one of reply, forward, escalate, no_reply (R16.1)."""
    payload = dict(valid_payload_dict)
    payload["action"] = invalid_action

    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(payload, schema=reply_schema)
    assert "action" in str(exc_info.value).lower()


@pytest.mark.parametrize("invalid_confidence", [-0.01, 1.01, -100.0, 5.0])
def test_validate_confidence_out_of_bounds(
    valid_payload_dict: dict[str, Any],
    reply_schema: dict[str, Any],
    invalid_confidence: float,
) -> None:
    """Verify confidence must be between 0.0 and 1.0 inclusive (R16.1)."""
    payload = dict(valid_payload_dict)
    payload["confidence"] = invalid_confidence

    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(payload, schema=reply_schema)
    assert "confidence" in str(exc_info.value).lower()


def test_validate_empty_draft_rejected(
    valid_payload_dict: dict[str, Any], reply_schema: dict[str, Any]
) -> None:
    """Verify draft must not be empty (R16.1)."""
    payload = dict(valid_payload_dict)
    payload["draft"] = ""

    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(payload, schema=reply_schema)
    assert "draft" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "invalid_chunks",
    [
        [123, 456],
        ["valid-1", None],
        [{"id": "c1"}],
        "not-a-list",
    ],
)
def test_validate_knowledge_chunks_non_string_items(
    valid_payload_dict: dict[str, Any],
    reply_schema: dict[str, Any],
    invalid_chunks: Any,
) -> None:
    """Verify knowledge_chunks must be a list of strings (R16.1)."""
    payload = dict(valid_payload_dict)
    payload["knowledge_chunks"] = invalid_chunks

    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(payload, schema=reply_schema)
    assert "knowledge_chunks" in str(exc_info.value).lower()


def test_validate_extra_properties_rejected(
    valid_payload_dict: dict[str, Any], reply_schema: dict[str, Any]
) -> None:
    """Verify extra properties not defined in schema are rejected (R16.1)."""
    payload = dict(valid_payload_dict)
    payload["unexpected_field"] = "hallucinated data"

    with pytest.raises(DraftValidationError) as exc_info:
        validate_draft_payload(payload, schema=reply_schema)
    assert (
        "unexpected_field" in str(exc_info.value).lower()
        or "extra" in str(exc_info.value).lower()
        or "unrecognized" in str(exc_info.value).lower()
    )


@pytest.mark.parametrize(
    "malformed_content",
    [
        "{ broken json",
        "not json at all",
        "",
        "123",
        "[1, 2, 3]",
        "null",
        None,
        12345,
    ],
)
def test_validate_malformed_json_string(
    reply_schema: dict[str, Any],
    malformed_content: Any,
) -> None:
    """Verify malformed JSON strings or invalid types raise DraftValidationError (R16.2)."""
    with pytest.raises(DraftValidationError):
        validate_draft_payload(malformed_content, schema=reply_schema)


def test_build_repair_messages_structure(reply_schema: dict[str, Any]) -> None:
    """Verify build_repair_messages formats the conversation with invalid output and repair
    instructions (R16.3).
    """
    original_messages = [
        ChatMessage(role="system", content="System instruction"),
        ChatMessage(role="user", content="Draft a reply to customer email."),
    ]
    invalid_content = '{"action": "reply", "draft": ""}'
    validation_error = "draft: String should have at least 1 character"

    repair_messages = build_repair_messages(
        original_messages=original_messages,
        invalid_content=invalid_content,
        validation_error=validation_error,
        schema=reply_schema,
    )

    assert len(repair_messages) == len(original_messages) + 2

    # Preserves original messages
    assert repair_messages[0] == original_messages[0]
    assert repair_messages[1] == original_messages[1]

    # Assistant message contains the invalid content
    assistant_msg = repair_messages[2]
    assert assistant_msg.role == "assistant"
    assert assistant_msg.content == invalid_content

    # User message contains the error diagnosis and schema repair instructions
    user_msg = repair_messages[3]
    assert user_msg.role == "user"
    assert validation_error in user_msg.content
    assert "schemas/reply.v1.json" in user_msg.content or "ReplySchemaV1" in user_msg.content
    assert "Return ONLY a valid JSON object" in user_msg.content
    assert "Do NOT include markdown formatting" in user_msg.content


def test_build_repair_messages_with_dict_invalid_content() -> None:
    """Verify build_repair_messages serializes a dict invalid_content properly."""
    original_messages = [ChatMessage(role="user", content="Hello")]
    invalid_dict = {"action": "bad_action", "draft": "Hello"}

    repair_messages = build_repair_messages(
        original_messages=original_messages,
        invalid_content=invalid_dict,
        validation_error="action is invalid",
        schema=None,
    )

    assistant_msg = repair_messages[1]
    assert assistant_msg.role == "assistant"
    # Should be valid serialized JSON
    parsed = json.loads(assistant_msg.content)
    assert parsed == invalid_dict


def test_exception_inheritance() -> None:
    """Verify DraftValidationError and UnvalidatedDraftError inherit from
    LLMSchemaValidationError.
    """
    assert issubclass(DraftValidationError, LLMSchemaValidationError)
    assert issubclass(UnvalidatedDraftError, LLMSchemaValidationError)


def test_validate_already_instantiated_draft_payload() -> None:
    """Verify validate_draft_payload passes through an already instantiated DraftReplyPayload."""
    instance = DraftReplyPayload(
        action="reply",
        draft="Pass-through draft.",
        confidence=0.88,
        knowledge_chunks=["chunk-1"],
        thread_summary_updated=True,
        model_tier="routine",
    )
    result = validate_draft_payload(instance)
    assert result is instance


def test_draft_reply_payload_requires_all_fields_on_instantiation() -> None:
    """Verify DraftReplyPayload requires knowledge_chunks and thread_summary_updated."""
    with pytest.raises(ValidationError):
        # Missing knowledge_chunks and thread_summary_updated
        DraftReplyPayload(  # type: ignore[call-arg]
            action="reply",
            draft="Draft without defaults",
            confidence=0.9,
            model_tier="routine",
        )


@pytest.mark.parametrize(
    "fence",
    [
        "```json\n{content}\n```",
        "```\n{content}\n```",
        "  ```json\n{content}\n```  ",
    ],
)
def test_validate_draft_payload_markdown_code_fence_stripping(fence: str) -> None:
    """Verify validate_draft_payload strips markdown code fences when LLM outputs them."""
    inner = json.dumps(
        {
            "action": "reply",
            "draft": "Cleaned response.",
            "confidence": 0.9,
            "knowledge_chunks": [],
            "thread_summary_updated": False,
            "model_tier": "routine",
        }
    )
    wrapped = fence.format(content=inner)
    payload = validate_draft_payload(wrapped)
    assert payload.action == "reply"
    assert payload.draft == "Cleaned response."


@pytest.mark.parametrize("conf", [0.0, 0.5, 1.0])
def test_confidence_valid_boundary_values(conf: float) -> None:
    """Verify exact valid confidence boundary values 0.0 and 1.0 are accepted."""
    data = {
        "action": "reply",
        "draft": "Boundary draft",
        "confidence": conf,
        "knowledge_chunks": [],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }
    payload = validate_draft_payload(data)
    assert payload.confidence == conf


def test_schema_that_diverges_from_the_contract_is_rejected_loudly() -> None:
    """A per-profile schema the Pydantic contract cannot enforce must fail, not pass silently.

    `AgentProfile.output_schema` is a per-profile knob, but `DraftReplyPayload` is a fixed
    six-field model: it cannot honour a narrowed action enum or tightened confidence bounds.
    Accepting such a schema would silently under-validate every draft for that profile, so
    the divergence is a configuration error rather than something a repair retry can fix.
    """
    narrowed = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reply", "escalate"]},
            "draft": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "knowledge_chunks": {"type": "array", "items": {"type": "string"}},
            "thread_summary_updated": {"type": "boolean"},
            "model_tier": {"type": "string"},
        },
        "required": [
            "action",
            "draft",
            "confidence",
            "knowledge_chunks",
            "thread_summary_updated",
            "model_tier",
        ],
        "additionalProperties": False,
    }
    payload = {
        "action": "forward",
        "draft": "Forwarding this to billing.",
        "confidence": 0.9,
        "knowledge_chunks": [],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }

    with pytest.raises(DraftSchemaContractError, match="action"):
        validate_draft_payload(payload, schema=narrowed)


def test_schema_with_tightened_confidence_bounds_is_rejected() -> None:
    """A confidence floor the fixed model cannot enforce is a contract error, not a pass."""
    tightened = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reply", "forward", "escalate", "no_reply"]},
            "draft": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.5, "maximum": 1.0},
            "knowledge_chunks": {"type": "array", "items": {"type": "string"}},
            "thread_summary_updated": {"type": "boolean"},
            "model_tier": {"type": "string"},
        },
        "additionalProperties": False,
    }

    with pytest.raises(DraftSchemaContractError, match="confidence"):
        validate_draft_payload(
            {
                "action": "reply",
                "draft": "Hello.",
                "confidence": 0.1,
                "knowledge_chunks": [],
                "thread_summary_updated": False,
                "model_tier": "routine",
            },
            schema=tightened,
        )


def test_schema_contract_error_is_not_a_validation_error() -> None:
    """A misconfigured schema must not be routed into the repair path.

    DraftValidationError means "the model got it wrong, ask again". A schema the code
    cannot enforce means "the deployment is wrong"; retrying the model cannot fix it, so
    it must not inherit from the type the generator repairs on.
    """
    assert not issubclass(DraftSchemaContractError, DraftValidationError)
    assert not issubclass(DraftSchemaContractError, LLMSchemaValidationError)


def test_canonical_reply_schema_satisfies_the_contract(reply_schema: dict[str, Any]) -> None:
    """The shipped schemas/reply.v1.json must pass the contract check it is the model of."""
    payload = {
        "action": "reply",
        "draft": "Hello Alice.",
        "confidence": 0.9,
        "knowledge_chunks": ["chunk-1"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }
    assert validate_draft_payload(payload, schema=reply_schema).action == "reply"

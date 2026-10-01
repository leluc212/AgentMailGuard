"""Draft reply schema validation engine and repair prompt constructor (R16.1, R16.2, R16.3)."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.llm.protocol import ChatMessage, LLMError, LLMSchemaValidationError

REQUIRED_REPLY_FIELDS: list[str] = [
    "action",
    "draft",
    "confidence",
    "knowledge_chunks",
    "thread_summary_updated",
    "model_tier",
]

ALLOWED_ACTIONS: frozenset[str] = frozenset({"reply", "forward", "escalate", "no_reply"})
CONFIDENCE_BOUNDS: tuple[float, float] = (0.0, 1.0)

# JSON Schema keywords DraftReplyPayload actually enforces. Anything else in a property
# declaration would be silently ignored, so the contract check rejects it rather than
# pretending to honour it. Keeping this list short is the point: this is a fixed six-field
# contract, not a general JSON Schema implementation.
ENFORCED_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {"type", "description", "title", "enum", "items", "minimum", "maximum"}
)
CANONICAL_PROPERTY_TYPES: dict[str, str] = {
    "action": "string",
    "draft": "string",
    "confidence": "number",
    "knowledge_chunks": "array",
    "thread_summary_updated": "boolean",
    "model_tier": "string",
}


class DraftSchemaContractError(LLMError):
    """Raised when a supplied output schema declares constraints this module cannot enforce.

    `DraftReplyPayload` is a fixed six-field model, while `AgentProfile.output_schema` is a
    per-profile knob. A profile pointing at a schema that narrows the action enum or tightens
    the confidence bounds would be silently under-validated, so the divergence is reported as
    a deployment error. Deliberately NOT an `LLMSchemaValidationError`: no repair retry can
    fix a misconfigured schema, so this must never be routed into the repair path (R16.3).
    """


class DraftValidationError(LLMSchemaValidationError):
    """Raised when generated draft content fails schema validation (R16.2)."""


class UnvalidatedDraftError(LLMSchemaValidationError):
    """Raised when a draft remains unvalidated after repair retry (R16.3). Never persist.

    ``finish_reason`` is the provider's stop reason for the last response, when known; a
    length stop means the output was truncated at ``max_tokens``.
    """

    def __init__(self, message: str, *, finish_reason: str | None = None) -> None:
        super().__init__(message, finish_reason=finish_reason)


class DraftReplyPayload(BaseModel):
    """Pydantic model enforcing the canonical draft reply schema (R16.1, design.md §5.7)."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="Action to take: reply, forward, escalate, or no_reply")
    draft: str = Field(..., min_length=1, description="Generated draft email reply body")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Generation confidence score")
    knowledge_chunks: list[str] = Field(..., description="Knowledge chunk IDs cited")
    thread_summary_updated: bool = Field(..., description="Whether thread summary was updated")
    model_tier: str = Field(..., min_length=1, description="Model tier used for generation")

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"reply", "forward", "escalate", "no_reply"}
        if v not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}, got {v!r}")
        return v


def assert_schema_matches_contract(schema: dict[str, Any] | None) -> None:
    """Verify a supplied schema declares nothing `DraftReplyPayload` cannot enforce.

    Fail-closed by design. `DraftReplyPayload` is a fixed six-field model, so any keyword a
    schema declares that the model does not implement would be silently ignored — which is
    exactly the silent under-validation this check exists to prevent. Every property is
    therefore checked against an allow-list of enforced keywords, and anything outside it is
    rejected rather than assumed harmless.

    A constraint that is *wider* than the model's is accepted: the model is then stricter
    than the declaration, so nothing escapes validation.

    Raises:
        DraftSchemaContractError: If the schema diverges from the enforceable contract.
    """
    if schema is None:
        return

    if schema.get("additionalProperties") is True:
        raise DraftSchemaContractError(
            "Output schema sets additionalProperties: true, but DraftReplyPayload is "
            "extra='forbid'; unknown fields would be rejected, not accepted as declared."
        )

    required = schema.get("required")
    if required is not None and set(required) != set(REQUIRED_REPLY_FIELDS):
        raise DraftSchemaContractError(
            f"Output schema 'required' is {sorted(required)}, but DraftReplyPayload demands "
            f"all of {REQUIRED_REPLY_FIELDS} unconditionally. A shorter list would reject "
            "payloads that genuinely conform to the declared schema."
        )

    properties = schema.get("properties")
    if properties is None:
        return
    if not isinstance(properties, dict):
        raise DraftSchemaContractError(
            f"Output schema 'properties' must be an object, got {type(properties).__name__}."
        )

    declared = set(properties)
    canonical = set(REQUIRED_REPLY_FIELDS)
    if declared != canonical:
        raise DraftSchemaContractError(
            "Output schema properties diverge from the enforceable draft contract: "
            f"unexpected {sorted(declared - canonical)}, missing {sorted(canonical - declared)}. "
            f"DraftReplyPayload enforces exactly {REQUIRED_REPLY_FIELDS}."
        )

    for name, declaration in properties.items():
        if not isinstance(declaration, dict):
            raise DraftSchemaContractError(
                f"Output schema property {name!r} must be an object, got "
                f"{type(declaration).__name__}."
            )

        unenforced = set(declaration) - ENFORCED_SCHEMA_KEYWORDS
        if unenforced:
            raise DraftSchemaContractError(
                f"Output schema property {name!r} declares {sorted(unenforced)}, which "
                "DraftReplyPayload does not enforce; it would be silently ignored."
            )

        declared_type = declaration.get("type")
        expected_type = CANONICAL_PROPERTY_TYPES[name]
        if declared_type is not None and declared_type != expected_type:
            raise DraftSchemaContractError(
                f"Output schema declares {name!r} as type {declared_type!r}, but "
                f"DraftReplyPayload enforces {expected_type!r}."
            )

        enum_values = declaration.get("enum")
        if enum_values is not None:
            if name != "action":
                raise DraftSchemaContractError(
                    f"Output schema restricts {name!r} to {sorted(enum_values)}, but "
                    "DraftReplyPayload enforces an enum only on 'action'."
                )
            if set(enum_values) != ALLOWED_ACTIONS:
                raise DraftSchemaContractError(
                    f"Output schema restricts 'action' to {sorted(enum_values)}, but "
                    f"DraftReplyPayload enforces {sorted(ALLOWED_ACTIONS)}; the difference "
                    "would not be enforced."
                )

        if "items" in declaration and name != "knowledge_chunks":
            raise DraftSchemaContractError(
                f"Output schema declares 'items' on {name!r}, which DraftReplyPayload only "
                "enforces for 'knowledge_chunks'."
            )
        if name == "knowledge_chunks":
            item_type = declaration.get("items", {}).get("type", "string")
            if item_type != "string":
                raise DraftSchemaContractError(
                    f"Output schema declares knowledge_chunks items as {item_type!r}, but "
                    "DraftReplyPayload enforces a list of strings."
                )

        minimum = declaration.get("minimum")
        maximum = declaration.get("maximum")
        if (minimum is not None or maximum is not None) and name != "confidence":
            raise DraftSchemaContractError(
                f"Output schema bounds {name!r}, but DraftReplyPayload enforces numeric "
                "bounds only on 'confidence'."
            )
        if name == "confidence":
            # Wider is safe (the model is stricter); narrower would go unenforced.
            if minimum is not None and minimum > CONFIDENCE_BOUNDS[0]:
                raise DraftSchemaContractError(
                    f"Output schema sets a 'confidence' floor of {minimum}, tighter than the "
                    f"{CONFIDENCE_BOUNDS[0]} DraftReplyPayload enforces; it would be ignored."
                )
            if maximum is not None and maximum < CONFIDENCE_BOUNDS[1]:
                raise DraftSchemaContractError(
                    f"Output schema caps 'confidence' at {maximum}, tighter than the "
                    f"{CONFIDENCE_BOUNDS[1]} DraftReplyPayload enforces; it would be ignored."
                )


def validate_draft_payload(
    content: Any,
    schema: dict[str, Any] | None = None,
) -> DraftReplyPayload:
    """Validate generated reply dictionary or raw string against the reply schema (R16.1, R16.2).

    Raises:
        DraftValidationError: If content fails schema validation or cannot be parsed as JSON.
        DraftSchemaContractError: If `schema` declares constraints this module cannot enforce.
    """
    assert_schema_matches_contract(schema)

    if isinstance(content, DraftReplyPayload):
        return content

    if isinstance(content, str):
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise DraftValidationError(f"Payload is not valid JSON: {exc}") from exc
    elif isinstance(content, dict):
        data = content
    else:
        raise DraftValidationError(
            f"Expected dict or JSON string payload, got {type(content).__name__}"
        )

    if not isinstance(data, dict):
        raise DraftValidationError(f"Expected JSON object payload, got {type(data).__name__}")

    # 1. Enforce required fields (from schema if provided, else canonical R16.1 fields)
    if (
        schema is not None
        and "required" in schema
        and isinstance(schema["required"], (list, tuple))
    ):
        required_keys = schema["required"]
    else:
        required_keys = REQUIRED_REPLY_FIELDS

    missing = [key for key in required_keys if key not in data]
    if missing:
        raise DraftValidationError(f"Payload missing required field(s): {', '.join(missing)}")

    # 2. Enforce additionalProperties restriction if defined in schema
    if (
        schema is not None
        and schema.get("additionalProperties") is False
        and "properties" in schema
    ):
        allowed_properties = set(schema["properties"].keys())
        extra = set(data.keys()) - allowed_properties
        if extra:
            raise DraftValidationError(
                f"Payload contains unrecognized property(ies): {', '.join(sorted(extra))}"
            )

    # 3. Validate against Pydantic schema model
    try:
        return DraftReplyPayload.model_validate(data)
    except ValidationError as exc:
        raise DraftValidationError(f"Draft schema validation failed: {exc}") from exc


def build_repair_messages(
    original_messages: list[ChatMessage],
    invalid_content: Any,
    validation_error: str,
    schema: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    """Construct chat messages instructing the model to repair invalid JSON output (R16.3)."""
    if isinstance(invalid_content, str):
        assistant_content = invalid_content
    elif isinstance(invalid_content, (dict, list)):
        try:
            assistant_content = json.dumps(invalid_content, indent=2)
        except Exception:
            assistant_content = str(invalid_content)
    else:
        assistant_content = str(invalid_content)

    schema_str = json.dumps(schema, indent=2) if schema else ""
    user_repair_instruction = (
        f"Your previous response failed schema validation with the following error:\n"
        f"{validation_error}\n\n"
        "Please repair the response. Return ONLY a valid JSON object strictly conforming to "
        "the following JSON schema:\n"
        f"{schema_str}\n"
        "Do NOT include markdown formatting, code blocks, or explanatory commentary."
    )

    return [
        *original_messages,
        ChatMessage(role="assistant", content=assistant_content),
        ChatMessage(role="user", content=user_repair_instruction),
    ]

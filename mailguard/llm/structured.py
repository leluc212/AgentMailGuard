"""Schema-validated LLM calls with a single repair attempt (never trust invalid output)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from mailguard.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)

logger = logging.getLogger(__name__)


def compact_schema(model: type[BaseModel]) -> dict[str, object]:
    """JSON schema of a pydantic model without the title (smaller prompts, Ollama-friendly)."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _schema_failure(
    error: ValidationError, content: dict[str, Any]
) -> LLMSchemaValidationError:
    """Classify why ``content`` did not validate (see ``fallback_reason``)."""
    if set(content) == {"raw_text"}:  # parse_json_or_text: the model answered with prose
        reason = "non_json"
    elif all(e["type"] == "missing" for e in error.errors()):
        reason = "schema_missing"
    else:
        reason = "invalid_fields"
    return LLMSchemaValidationError(str(error), reason=reason)


def fallback_reason(exc: BaseException) -> str:
    """Machine-readable reason an AI stage fell back to its cheap result.

    One of ``timeout | non_json | schema_missing | invalid_fields | error``.
    """
    if isinstance(exc, LLMSchemaValidationError):
        return exc.reason
    if isinstance(exc, LLMTimeoutError | TimeoutError):
        return "timeout"
    return "error"


def mark_llm_fallback(metadata: dict[str, Any], exc: BaseException) -> None:
    """Make an AI-stage failure visible in a verdict's metadata (same keys for every stage).

    The verdict keeps its cheap result and its ``error`` stays empty; counting
    ``metadata["llm_fallback"]`` per layer gives the fallback rate of that stage.
    """
    metadata["llm_fallback"] = True
    metadata["llm_fallback_reason"] = fallback_reason(exc)
    metadata["llm_error"] = (str(exc) or type(exc).__name__)[:200]


async def call_structured[T: BaseModel](
    provider: LLMProvider,
    messages: list[ChatMessage],
    output_model: type[T],
    *,
    tier: ModelTier = ModelTier.FAST,
    max_tokens: int = 600,
    temperature: float = 0.0,
    repair: bool = True,
) -> tuple[T, LLMResult]:
    """Call ``provider`` and validate into ``output_model``; retry once with the error appended."""
    schema = compact_schema(output_model)
    result = await provider.generate(
        messages=messages, schema=schema, tier=tier, max_tokens=max_tokens, temperature=temperature
    )
    try:
        return output_model.model_validate(result.content), result
    except ValidationError as first_error:
        if not repair:
            raise _schema_failure(first_error, result.content) from first_error
        logger.debug("structured output invalid, attempting one repair: %s", first_error)
        repair_messages = [
            *messages,
            ChatMessage(role="assistant", content=str(result.content)[:2000]),
            ChatMessage(
                role="user",
                content=(
                    "Your previous answer did not match the required JSON schema: "
                    f"{first_error.errors()[:3]}. Return ONLY a valid JSON object for the schema."
                ),
            ),
        ]
        result2 = await provider.generate(
            messages=repair_messages,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        try:
            parsed = output_model.model_validate(result2.content)
        except ValidationError as second_error:
            raise _schema_failure(second_error, result2.content) from second_error
        result2.input_tokens += result.input_tokens
        result2.output_tokens += result.output_tokens
        result2.latency_ms += result.latency_ms
        return parsed, result2

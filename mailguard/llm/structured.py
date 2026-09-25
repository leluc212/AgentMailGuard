"""Schema-validated LLM calls with a single repair attempt (never trust invalid output)."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from mailguard.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResult,
    LLMSchemaValidationError,
    ModelTier,
)

logger = logging.getLogger(__name__)


def compact_schema(model: type[BaseModel]) -> dict[str, object]:
    """JSON schema of a pydantic model without the title (smaller prompts, Ollama-friendly)."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


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
            raise LLMSchemaValidationError(str(first_error)) from first_error
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
            raise LLMSchemaValidationError(str(second_error)) from second_error
        result2.input_tokens += result.input_tokens
        result2.output_tokens += result.output_tokens
        result2.latency_ms += result.latency_ms
        return parsed, result2

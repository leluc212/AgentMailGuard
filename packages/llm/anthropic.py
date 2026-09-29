"""Anthropic Claude hosted API LLMProvider implementation using httpx (R14.5, R14.7).

Provides Claude Messages API invocation with tool-use structured JSON output,
system prompt separation, tier-based model resolution, latency tracking, and error handling.
Direct vendor SDK imports outside packages/llm are strictly prohibited per CLAUDE.md §4.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from packages.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)

logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODELS: dict[ModelTier, str] = {
    ModelTier.FAST: "claude-3-5-haiku-20241022",
    ModelTier.ROUTINE: "claude-3-5-haiku-20241022",
    ModelTier.STRONG: "claude-3-5-sonnet-20241022",
    ModelTier.HIGH_CAPABILITY: "claude-3-5-sonnet-20241022",
    ModelTier.FALLBACK: "claude-3-haiku-20240307",
}


class AnthropicLLMProvider(LLMProvider):
    """Hosted Anthropic Claude implementation of the LLMProvider protocol (R14.5, R14.7)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_map: dict[ModelTier, str] | None = None,
        timeout_s: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (
            base_url or os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com/v1"
        ).rstrip("/")
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or ""
        self._timeout_s = timeout_s
        self._model_map = dict(DEFAULT_ANTHROPIC_MODELS)
        if model_map:
            self._model_map.update(model_map)

        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def resolve_model(self, tier: ModelTier) -> str:
        """Resolve a ModelTier to a concrete Anthropic model name."""
        return self._model_map.get(tier, self._model_map[ModelTier.FAST])

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
        **params: Any,
    ) -> LLMResult:
        """Execute a completion request against the Anthropic Messages API."""
        start_time = time.perf_counter()
        model = self.resolve_model(tier)

        # Separate system messages from conversational messages per Anthropic API spec
        system_parts = [m.content for m in messages if m.role == "system"]
        system_content = "\n\n".join(system_parts) if system_parts else None

        formatted_messages = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        # Messages array cannot be empty in Claude Messages API
        if not formatted_messages:
            formatted_messages = [{"role": "user", "content": "Begin."}]

        payload: dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_content:
            payload["system"] = system_content

        # Handle structured schema via Anthropic tool-use mechanism
        if schema is not None:
            payload["tools"] = [
                {
                    "name": "structured_response",
                    "description": "Structured output response adhering to required schema",
                    "input_schema": schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": "structured_response"}

        if params:
            payload.update(params)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key

        endpoint = f"{self._base_url}/messages"
        client = await self._get_client()

        try:
            response = await client.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
            logger.error("Anthropic call timed out after %d ms for model %s", elapsed_ms, model)
            raise LLMTimeoutError(f"Anthropic request timed out after {self._timeout_s}s") from exc
        except httpx.HTTPStatusError as exc:
            elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
            logger.error(
                "Anthropic call failed with HTTP %s (%d ms): %s",
                exc.response.status_code,
                elapsed_ms,
                exc.response.text,
            )
            raise LLMResponseError(
                f"Anthropic request failed with status {exc.response.status_code}: "
                f"{exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
            logger.error("Anthropic transport error after %d ms: %s", elapsed_ms, exc)
            raise LLMResponseError(f"Anthropic transport error: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Unexpected error during Anthropic generation: {exc}") from exc

        # Extract content
        content_blocks = data.get("content", [])
        content: dict[str, Any] = {}

        if schema is not None:
            tool_block = next(
                (b for b in content_blocks if b.get("type") == "tool_use"),
                None,
            )
            if tool_block is not None and isinstance(tool_block.get("input"), dict):
                content = tool_block["input"]
            else:
                text_fallback = "".join(
                    b.get("text", "") for b in content_blocks if b.get("type") == "text"
                )
                raise LLMSchemaValidationError(
                    f"Failed to extract structured tool_use block from Anthropic "
                    f"response: {content_blocks}",
                    raw_content=text_fallback or str(content_blocks),
                    finish_reason=data.get("stop_reason"),
                )
        else:
            text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            content = {"raw_text": "".join(text_parts)}

        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        stop_reason = str(data.get("stop_reason", "end_turn"))
        elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))

        return LLMResult(
            content=content,
            model=model,
            tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            raw_finish_reason=stop_reason,
            raw_response=data,
        )

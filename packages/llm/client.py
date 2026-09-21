"""HTTP-based LLMProvider implementation using httpx (R14.5, design.md §5.7).

Provides OpenAI / LiteLLM-compatible chat completion invocation with structured
JSON outputs, tier-based model resolution, latency tracking, and error handling.
Direct vendor SDK imports outside packages/llm are strictly prohibited per GEMINI.md §4.
"""

from __future__ import annotations

import json
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

DEFAULT_MODEL_MAP: dict[ModelTier, str] = {
    ModelTier.FAST: "gpt-4o-mini",
    ModelTier.ROUTINE: "gpt-4o-mini",
    ModelTier.STRONG: "gpt-4o",
    ModelTier.HIGH_CAPABILITY: "gpt-4o",
    ModelTier.FALLBACK: "claude-3-haiku",
}


class HttpLLMProvider(LLMProvider):
    """HTTP client implementation of the LLMProvider protocol."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_map: dict[ModelTier, str] | None = None,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self._api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self._timeout_s = timeout_s
        self._model_map = dict(DEFAULT_MODEL_MAP)
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
        """Resolve a ModelTier to a concrete model name identifier."""
        return self._model_map.get(tier, self._model_map[ModelTier.FAST])

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMResult:
        """Execute a structured completion request against an OpenAI-compatible API."""
        start_time = time.perf_counter()
        model = self.resolve_model(tier)

        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        payload: dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": schema,
                },
            }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        endpoint = f"{self._base_url}/chat/completions"
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
            logger.error("LLM call timed out after %d ms for model %s", elapsed_ms, model)
            raise LLMTimeoutError(f"LLM request timed out after {self._timeout_s}s") from exc
        except httpx.HTTPStatusError as exc:
            elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
            logger.error(
                "LLM call failed with HTTP %s (%d ms): %s",
                exc.response.status_code,
                elapsed_ms,
                exc.response.text,
            )
            raise LLMResponseError(
                f"LLM request failed with status {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))
            logger.error("LLM transport error after %d ms: %s", elapsed_ms, exc)
            raise LLMResponseError(f"LLM transport error: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Unexpected error during LLM generation: {exc}") from exc

        choices = data.get("choices", [])
        if not choices:
            raise LLMResponseError(f"Empty choices list received from LLM: {data}")

        choice = choices[0]
        raw_content = choice.get("message", {}).get("content", "")
        finish_reason = choice.get("finish_reason", "stop")

        # Parse structured content
        if schema is not None:
            try:
                content: dict[str, Any] = json.loads(raw_content)
                if not isinstance(content, dict):
                    raise ValueError(f"Parsed JSON must be an object, got {type(content).__name__}")
            except Exception as exc:
                raise LLMSchemaValidationError(
                    f"Failed to parse structured JSON response from model {model}: {exc} "
                    f"(content: {raw_content[:200]!r})"
                ) from exc
        else:
            try:
                parsed = json.loads(raw_content)
                content = parsed if isinstance(parsed, dict) else {"raw_text": raw_content}
            except Exception:
                content = {"raw_text": raw_content}

        usage = data.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))

        return LLMResult(
            content=content,
            model=model,
            tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            raw_finish_reason=finish_reason,
            raw_response=data,
        )

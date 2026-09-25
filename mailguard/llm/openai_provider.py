"""OpenAI-compatible chat completions provider (GPT-4o-mini and any compatible gateway)."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from mailguard.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMTimeoutError,
    ModelTier,
)

logger = logging.getLogger(__name__)


def parse_json_or_text(raw: str) -> dict[str, Any]:
    """Parse a JSON object from model output; tolerate ``` fences; fall back to raw_text."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        # try to locate the outermost object
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {"raw_text": raw}


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        json_mode: str = "json_object",  # json_object | json_schema | none
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self._api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self._timeout_s = timeout_s
        self._json_mode = json_mode
        self._client = client

    async def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMResult:
        start = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema is not None and self._json_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "guard_output", "strict": False, "schema": schema},
            }
        elif schema is not None and self._json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        client = await self._client_or_new()
        try:
            resp = await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"OpenAI request timed out after {self._timeout_s}s") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMResponseError(
                f"OpenAI HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMResponseError(f"OpenAI transport error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise LLMError(str(exc)) from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError(f"Empty choices from OpenAI: {data}")
        raw = choices[0].get("message", {}).get("content", "") or ""
        usage = data.get("usage", {})
        return LLMResult(
            content=parse_json_or_text(raw),
            model=str(data.get("model") or self.model),
            tier=tier,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            raw_finish_reason=str(choices[0].get("finish_reason", "stop")),
            raw_response=data,
        )

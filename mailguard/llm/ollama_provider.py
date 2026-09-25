"""Ollama chat provider for local models (Qwen2.5-7B-Instruct, Llama-3.1-8B-Instruct).

Uses ``POST /api/chat`` with ``stream=false``. When a JSON schema is supplied it is
passed through Ollama's structured-output ``format`` field (Ollama >= 0.5); set
``schema_format=False`` for older servers to fall back to ``format="json"``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from mailguard.llm.openai_provider import parse_json_or_text
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


class OllamaProvider(LLMProvider):
    def __init__(
        self,
        model: str = "qwen2.5:7b-instruct",
        base_url: str = "http://localhost:11434",
        timeout_s: float = 60.0,
        schema_format: bool = True,
        num_ctx: int = 8192,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._schema_format = schema_format
        self._num_ctx = num_ctx
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
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self._num_ctx,
            },
        }
        if schema is not None:
            payload["format"] = schema if self._schema_format else "json"
        client = await self._client_or_new()
        try:
            resp = await client.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"Ollama request timed out after {self._timeout_s}s") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMResponseError(
                f"Ollama HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMResponseError(f"Ollama transport error: {exc}") from exc
        except Exception as exc:  # pragma: no cover
            raise LLMError(str(exc)) from exc

        raw = (data.get("message") or {}).get("content", "") or ""
        return LLMResult(
            content=parse_json_or_text(raw),
            model=str(data.get("model") or self.model),
            tier=tier,
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            raw_finish_reason=str(data.get("done_reason", "stop")),
            raw_response={k: v for k, v in data.items() if k != "message"},
        )

    async def health(self) -> bool:
        try:
            client = await self._client_or_new()
            resp = await client.get(f"{self._base_url}/api/tags")
            return resp.status_code == 200
        except Exception:
            return False


def ollama_tags_available(base_url: str = "http://localhost:11434") -> list[str]:
    """Synchronous helper for scripts: list locally pulled model tags."""
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []

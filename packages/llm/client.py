"""HTTP-based LLMProvider implementation using httpx (R14.5, design.md §5.7).

Provides OpenAI / LiteLLM-compatible chat completion invocation with structured
JSON outputs, tier-based model resolution, latency tracking, and error handling.
Direct vendor SDK imports outside packages/llm are strictly prohibited per CLAUDE.md §4.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from packages.core.settings import ProviderRouting
from packages.llm.protocol import (
    CallProvenance,
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMProviderMismatchError,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)
from packages.llm.provenance import (
    METADATA_HEADER,
    METADATA_HEADER_VALUE,
    MISMATCH_MARKER,
    check_pinned_route,
    parse_provenance,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_MAP: dict[ModelTier, str] = {
    ModelTier.FAST: "gpt-4o-mini",
    ModelTier.ROUTINE: "gpt-4o-mini",
    ModelTier.STRONG: "gpt-4o",
    ModelTier.HIGH_CAPABILITY: "gpt-4o",
    ModelTier.FALLBACK: "claude-3-haiku",
}


def _limit_source(response: httpx.Response) -> str | None:
    """OpenRouter's ``error.metadata.limit_source`` of an error body, when it has one."""
    try:
        body = response.json()
        source = body["error"]["metadata"]["limit_source"]
    except (ValueError, KeyError, TypeError):
        return None
    return source if isinstance(source, str) else None


def _retry_after_s(response: httpx.Response) -> float | None:
    """The ``Retry-After`` header in seconds, when it is a number of seconds."""
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


class HttpLLMProvider(LLMProvider):
    """HTTP client implementation of the LLMProvider protocol."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_map: dict[ModelTier, str] | None = None,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
        provider_routing: ProviderRouting | None = None,
        response_metadata: bool = False,
    ) -> None:
        """``provider_routing`` pins the serving provider on a router such as OpenRouter.

        It is sent as the request's ``provider`` object. With ``response_metadata`` the router
        is asked for its metadata and every result carries ``provenance``. A pin with fallbacks
        off is also verified on each response: a call another provider served, or served after
        a fallback attempt, raises ``LLMProviderMismatchError`` instead of returning a result.
        """
        if provider_routing is not None and not response_metadata:
            raise ValueError("a provider pin needs response_metadata=True, or it cannot be checked")
        self._provider_routing = provider_routing
        self._response_metadata = response_metadata
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

    @property
    def provider_routing(self) -> dict[str, Any] | None:
        """The ``provider`` object sent with every request; None when not routed."""
        return None if self._provider_routing is None else self._provider_routing.request_object()

    @property
    def response_metadata(self) -> bool:
        """Whether the router is asked for its metadata (which provider served each call)."""
        return self._response_metadata

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
        **params: Any,
    ) -> LLMResult:
        """Execute a structured completion request against an OpenAI-compatible API."""
        start_time = time.perf_counter()
        # A ``model`` param replaces the tier's model (the summarizer's, R8.3). Taken out of
        # ``params`` so the payload and ``LLMResult.model`` cannot name different models.
        model: str = params.pop("model", None) or self.resolve_model(tier)

        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        payload: dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if params:
            payload.update(params)

        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": schema,
                },
            }

        if self._provider_routing is not None:
            payload["provider"] = self._provider_routing.request_object()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._response_metadata:
            headers[METADATA_HEADER] = METADATA_HEADER_VALUE
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
                f"LLM request failed with status {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
                limit_source=_limit_source(exc.response),
                retry_after_s=_retry_after_s(exc.response),
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

        provenance: CallProvenance | None = None
        if self._response_metadata:
            provenance = parse_provenance(data, response.headers, requested_model=model)
            if self._provider_routing is not None:
                problem = check_pinned_route(provenance.to_dict(), self._provider_routing)
                if problem is not None:
                    raise LLMProviderMismatchError(
                        f"{MISMATCH_MARKER}: {problem} (model {model})",
                        expected=tuple(self._provider_routing.order),
                        served=provenance.served_provider,
                        provenance=provenance,
                    )

        choice = choices[0]
        # `or ""` not a default: a refusal, content filter, or tool-call-only message sends
        # the key present with a JSON null, and a None here would surface as a TypeError
        # from the parse handler below — not an LLMError, so nothing would retry or repair it.
        raw_content = choice.get("message", {}).get("content") or ""
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
                    f"(content: {raw_content[:200]!r})",
                    raw_content=raw_content,
                    finish_reason=finish_reason,
                    provenance=provenance,
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
            provenance=provenance,
        )


DEFAULT_OPENAI_MODELS: dict[ModelTier, str] = {
    ModelTier.FAST: "gpt-4o-mini",
    ModelTier.ROUTINE: "gpt-4o-mini",
    ModelTier.STRONG: "gpt-4o",
    ModelTier.HIGH_CAPABILITY: "gpt-4o",
    ModelTier.FALLBACK: "gpt-4o-mini",
}

DEFAULT_LOCAL_MODELS: dict[ModelTier, str] = {
    ModelTier.FAST: "llama3.2:3b",
    ModelTier.ROUTINE: "llama3.2:3b",
    ModelTier.STRONG: "llama3.3:70b",
    ModelTier.HIGH_CAPABILITY: "llama3.3:70b",
    ModelTier.FALLBACK: "llama3.2:1b",
}


class OpenAILLMProvider(HttpLLMProvider):
    """Hosted OpenAI API implementation of the LLMProvider protocol (R14.5, R14.7)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_map: dict[ModelTier, str] | None = None,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
        provider_routing: ProviderRouting | None = None,
        response_metadata: bool = False,
    ) -> None:
        super().__init__(
            base_url=base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            api_key=api_key or os.getenv("OPENAI_API_KEY") or "",
            model_map=model_map or DEFAULT_OPENAI_MODELS,
            timeout_s=timeout_s,
            client=client,
            provider_routing=provider_routing,
            response_metadata=response_metadata,
        )


class LocalLLMProvider(HttpLLMProvider):
    """OpenAI-compatible local endpoint implementation (e.g. Ollama, vLLM) (R14.5, R14.7)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_map: dict[ModelTier, str] | None = None,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or os.getenv("LOCAL_LLM_BASE_URL") or "http://localhost:11434/v1",
            api_key=api_key or os.getenv("LOCAL_LLM_API_KEY") or "",
            model_map=model_map or DEFAULT_LOCAL_MODELS,
            timeout_s=timeout_s,
            client=client,
        )

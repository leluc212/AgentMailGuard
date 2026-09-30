"""OpenRouter provider routing for the guard's OpenAI-compatible provider.

The benchmark can serve the guard's judges through OpenRouter with one provider pinned. The
provider then sends the routing object in the request body and asks OpenRouter for its routing
metadata, and it exposes on each result which provider served the call so the caller (rag-email's
benchmark) can record it and refuse a call another provider served. Nothing here uses the
network: every request goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mailguard.config.settings import MailGuardSettings
from mailguard.llm.openai_provider import OpenAIProvider
from mailguard.llm.protocol import ChatMessage, LLMResponseError
from mailguard.llm.registry import ModelRegistry

LLAMA = "meta-llama/llama-3.1-8b-instruct"
PIN = {
    "order": ["coreweave"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "quantizations": ["bf16"],
}
MESSAGES = [ChatMessage(role="user", content="hello")]


def completion(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "gen-abc123",
        "model": LLAMA,
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "cost": 0.0000037},
        "openrouter_metadata": {
            "requested": LLAMA,
            "summary": "available=1, selected=CoreWeave",
            "attempt": 1,
            "endpoints": {
                "available": [
                    {
                        "provider": "CoreWeave",
                        "model": LLAMA,
                        "selected": True,
                        "attempts": [{"provider": "CoreWeave", "model": LLAMA, "status": 200}],
                    }
                ]
            },
        },
    }
    body.update(overrides)
    return body


class Recorder:
    """A mock transport that keeps the requests it saw and answers with one body."""

    def __init__(
        self,
        body: dict[str, Any] | None = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.body = completion() if body is None else body
        self.status = status
        self.headers = headers or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    @property
    def sent(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def provider(rec: Recorder, **kwargs: Any) -> OpenAIProvider:
    return OpenAIProvider(
        model=LLAMA,
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-test",
        client=rec.client(),
        **kwargs,
    )


async def test_without_routing_the_request_is_unchanged() -> None:
    rec = Recorder({"choices": [{"message": {"content": "{}"}}]})
    result = await provider(rec).generate(messages=MESSAGES, schema={"type": "object"})

    assert "provider" not in rec.sent
    assert "x-openrouter-metadata" not in rec.requests[-1].headers
    assert result.provenance is None


async def test_the_routing_object_is_sent_as_the_provider_field() -> None:
    rec = Recorder()
    await provider(rec, provider_routing=PIN, response_metadata=True).generate(messages=MESSAGES)

    assert rec.sent["provider"] == PIN
    assert rec.sent["model"] == LLAMA
    # OpenRouter's ProviderPreferences schema rejects unknown keys: nothing else is added to it.
    assert set(rec.sent["provider"]) <= set(PIN)


async def test_the_metadata_header_is_sent_when_asked() -> None:
    rec = Recorder()
    await provider(rec, response_metadata=True).generate(messages=MESSAGES)

    assert rec.requests[-1].headers["x-openrouter-metadata"] == "enabled"
    assert "provider" not in rec.sent  # metadata alone pins nothing


async def test_the_served_provider_is_exposed_on_the_result() -> None:
    rec = Recorder()
    result = await provider(rec, provider_routing=PIN, response_metadata=True).generate(
        messages=MESSAGES
    )

    prov = result.provenance
    assert prov is not None
    assert prov.requested_model == LLAMA
    assert prov.served_provider == "CoreWeave"
    assert prov.attempt == 1
    assert prov.generation_id == "gen-abc123"
    assert (prov.prompt_tokens, prov.completion_tokens) == (12, 5)
    assert prov.cost == pytest.approx(0.0000037)
    assert prov.finish_reason == "stop"
    assert prov.to_dict()["served_provider"] == "CoreWeave"


async def test_a_fallback_attempt_is_visible_in_the_provenance() -> None:
    body = completion()
    body["openrouter_metadata"]["attempt"] = 2
    body["openrouter_metadata"]["endpoints"]["available"][0]["provider"] = "DeepInfra"
    rec = Recorder(body)
    result = await provider(rec, provider_routing=PIN, response_metadata=True).generate(
        messages=MESSAGES
    )

    assert result.provenance is not None
    assert result.provenance.served_provider == "DeepInfra"
    assert result.provenance.attempt == 2  # the caller, not the provider, calls that an error


async def test_the_generation_id_falls_back_to_the_response_header() -> None:
    body = completion()
    del body["id"]
    rec = Recorder(body, headers={"X-Generation-Id": "gen-from-header"})
    result = await provider(rec, response_metadata=True).generate(messages=MESSAGES)

    assert result.provenance is not None
    assert result.provenance.generation_id == "gen-from-header"


async def test_missing_metadata_leaves_the_served_provider_unknown() -> None:
    body = completion()
    del body["openrouter_metadata"]
    rec = Recorder(body)
    result = await provider(rec, provider_routing=PIN, response_metadata=True).generate(
        messages=MESSAGES
    )

    assert result.provenance is not None
    assert result.provenance.served_provider is None
    assert result.provenance.attempt is None


async def test_the_served_provider_is_read_from_the_summary_without_endpoint_details() -> None:
    body = completion()
    body["openrouter_metadata"] = {"summary": "available=1, selected=Phala", "attempt": 1}
    rec = Recorder(body)
    result = await provider(rec, response_metadata=True).generate(messages=MESSAGES)

    assert result.provenance is not None
    assert result.provenance.served_provider == "Phala"


async def test_an_http_error_still_raises_with_the_body() -> None:
    rec = Recorder({"error": {"code": 402, "message": "no credit"}}, status=402)
    with pytest.raises(LLMResponseError, match="402"):
        await provider(rec, provider_routing=PIN, response_metadata=True).generate(
            messages=MESSAGES
        )


def test_the_routing_object_takes_only_documented_keys() -> None:
    with pytest.raises(ValueError, match="unsupported provider routing key"):
        OpenAIProvider(model=LLAMA, provider_routing={"order": ["x"], "made_up": True})


def test_the_registry_builds_a_pinned_provider_from_a_spec(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        f"""
models:
  {LLAMA}:
    backend: openai
    model: {LLAMA}
    json_mode: json_object
    timeout_s: 120
    provider_routing:
      order: [coreweave]
      allow_fallbacks: false
      require_parameters: true
      quantizations: [bf16]
    response_metadata: true
""",
        encoding="utf-8",
    )
    settings = MailGuardSettings(_env_file=None)  # type: ignore[call-arg]

    built = ModelRegistry(settings, models_path=path).get(LLAMA)

    assert isinstance(built, OpenAIProvider)
    assert built.provider_routing == PIN
    assert built.response_metadata is True


def test_a_spec_without_routing_builds_the_provider_as_before(tmp_path: Path) -> None:
    settings = MailGuardSettings(_env_file=None)  # type: ignore[call-arg]

    built = ModelRegistry(settings, models_path=tmp_path / "none.yaml").get("gpt-4o-mini")

    assert isinstance(built, OpenAIProvider)
    assert built.provider_routing is None
    assert built.response_metadata is False


def test_the_shipped_models_yaml_pins_both_openrouter_models() -> None:
    settings = MailGuardSettings(_env_file=None)  # type: ignore[call-arg]
    registry = ModelRegistry(settings)

    qwen = registry.get("qwen/qwen-2.5-7b-instruct")
    llama = registry.get(LLAMA)

    assert isinstance(qwen, OpenAIProvider) and isinstance(llama, OpenAIProvider)
    assert qwen.provider_routing == {
        "order": ["phala"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert llama.provider_routing == PIN
    assert qwen.response_metadata and llama.response_metadata

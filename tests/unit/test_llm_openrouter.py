"""OpenRouter provider routing in packages/llm (work package R4; ADR-0012 decision 9).

The benchmark can serve Qwen2.5-7B and Llama-3.1-8B through OpenRouter with one provider pinned.
The LLM client (the only place that talks HTTP to a model, CLAUDE.md section 4) then sends the
routing object in the request body and asks for the router's metadata, records which provider
served each call, and refuses a call another provider served. Every request here goes to an
``httpx.MockTransport``: nothing calls the network (R24.5).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from evaluation.mailguard_bench.route import route_failure
from packages.core.settings import AppSettings, LLMTiersSettings, ProviderRouting
from packages.llm.client import HttpLLMProvider
from packages.llm.factory import create_llm_provider
from packages.llm.protocol import (
    ChatMessage,
    LLMProviderMismatchError,
    LLMResponseError,
    LLMSchemaValidationError,
    ModelTier,
)
from packages.llm.provenance import check_pinned_route

LLAMA = "meta-llama/llama-3.1-8b-instruct"
QWEN = "qwen/qwen-2.5-7b-instruct"
LLAMA_ROUTING = {
    "order": ["coreweave"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "quantizations": ["bf16"],
}
QWEN_ROUTING = {"order": ["phala"], "allow_fallbacks": False, "require_parameters": True}
MESSAGES = [ChatMessage(role="user", content="hello")]
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def completion(
    *,
    provider: str | None = "CoreWeave",
    attempt: int | None = 1,
    content: str = '{"ok": true}',
    model: str = LLAMA,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "gen-abc123",
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "cost": 0.0000037},
    }
    if provider is not None or attempt is not None:
        meta: dict[str, Any] = {"requested": model}
        if provider is not None:
            meta["summary"] = f"available=1, selected={provider}"
            meta["endpoints"] = {
                "available": [{"provider": provider, "model": model, "selected": True}]
            }
        if attempt is not None:
            meta["attempt"] = attempt
        body["openrouter_metadata"] = meta
    return body


class Recorder:
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

    @property
    def sent(self) -> dict[str, Any]:
        sent: dict[str, Any] = json.loads(self.requests[-1].content)
        return sent


def provider(
    rec: Recorder,
    *,
    routing: dict[str, Any] | None = LLAMA_ROUTING,
    metadata: bool = True,
    model: str = LLAMA,
) -> HttpLLMProvider:
    return HttpLLMProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        model_map=dict.fromkeys(ModelTier, model),
        provider_routing=None if routing is None else ProviderRouting.model_validate(routing),
        response_metadata=metadata,
        client=httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)),
    )


# --- settings ----------------------------------------------------------------------------


def test_the_routing_setting_is_read_from_a_json_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM__OPENAI_PROVIDER_ROUTING", json.dumps(LLAMA_ROUTING))
    monkeypatch.setenv("LLM__OPENAI_RESPONSE_METADATA", "true")

    llm = AppSettings(_env_file=None).llm

    assert llm.openai_provider_routing is not None
    assert llm.openai_provider_routing.order == ["coreweave"]
    assert llm.openai_provider_routing.allow_fallbacks is False
    assert llm.openai_provider_routing.request_object() == LLAMA_ROUTING
    assert llm.openai_response_metadata is True


def test_blank_routing_values_mean_no_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker Compose forwards an unset host variable as '': that must mean "not routed".
    monkeypatch.setenv("LLM__OPENAI_PROVIDER_ROUTING", "")
    monkeypatch.setenv("LLM__OPENAI_RESPONSE_METADATA", "")

    llm = AppSettings(_env_file=None).llm

    assert llm.openai_provider_routing is None
    assert llm.openai_response_metadata is False


def test_a_routing_key_openrouter_does_not_document_is_refused() -> None:
    with pytest.raises(ValidationError, match="made_up"):
        ProviderRouting.model_validate({"order": ["x"], "made_up": True})


def test_a_pinned_route_needs_a_provider_and_the_metadata_that_verifies_it() -> None:
    with pytest.raises(ValidationError):
        ProviderRouting.model_validate({"order": []})
    # A pin the response cannot be checked against is no pin: refused at startup.
    with pytest.raises(ValidationError, match="LLM__OPENAI_RESPONSE_METADATA"):
        LLMTiersSettings(
            provider="openai",
            openai_provider_routing=ProviderRouting.model_validate(LLAMA_ROUTING),
            openai_response_metadata=False,
        )


def test_the_default_routing_pins_and_forbids_fallbacks() -> None:
    routing = ProviderRouting.model_validate({"order": ["phala"]})
    assert routing.request_object() == {
        "order": ["phala"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


# --- the request -------------------------------------------------------------------------


async def test_the_routing_object_and_the_metadata_header_are_sent() -> None:
    rec = Recorder()
    await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)

    assert rec.sent["provider"] == LLAMA_ROUTING
    assert rec.sent["model"] == LLAMA
    assert rec.requests[-1].headers["x-openrouter-metadata"] == "enabled"
    assert rec.requests[-1].headers["authorization"] == "Bearer sk-or-test"


async def test_the_strict_json_schema_is_kept_and_no_healing_plugin_is_sent() -> None:
    rec = Recorder()
    await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)

    assert rec.sent["response_format"]["json_schema"]["strict"] is True
    assert rec.sent["response_format"]["type"] == "json_schema"
    # The response-healing plugin would repair malformed JSON and hide the model's real output.
    assert "plugins" not in rec.sent
    assert "models" not in rec.sent  # no cross-model fallback either


async def test_without_routing_nothing_of_it_is_sent() -> None:
    rec = Recorder(completion(provider=None, attempt=None))
    result = await provider(rec, routing=None, metadata=False).generate(messages=MESSAGES)

    assert "provider" not in rec.sent
    assert "x-openrouter-metadata" not in rec.requests[-1].headers
    assert result.provenance is None


# --- provenance --------------------------------------------------------------------------


async def test_every_call_records_which_provider_served_it() -> None:
    rec = Recorder()
    result = await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)

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
    assert result.input_tokens == 12  # usage is unchanged


async def test_the_provider_field_of_the_body_names_the_server_when_there_is_no_metadata() -> None:
    body = completion(provider=None, attempt=None)
    body["provider"] = "CoreWeave"
    rec = Recorder(body)
    result = await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)

    prov = result.provenance
    assert prov is not None
    assert prov.served_provider == "CoreWeave"
    assert prov.attempt is None
    assert prov.provider_source == "response.provider"
    assert prov.to_dict()["provider_source"] == "response.provider"


async def test_the_body_provider_field_of_another_provider_is_still_a_mismatch() -> None:
    body = completion(provider=None, attempt=None)
    body["provider"] = "DeepInfra"
    with pytest.raises(LLMProviderMismatchError) as caught:
        await provider(Recorder(body)).generate(messages=MESSAGES)

    assert caught.value.served == "DeepInfra"
    assert caught.value.provenance.provider_source == "response.provider"


async def test_the_metadata_is_read_before_the_body_provider_field() -> None:
    body = completion()
    body["provider"] = "DeepInfra"
    result = await provider(Recorder(body)).generate(messages=MESSAGES)

    assert result.provenance is not None
    assert result.provenance.served_provider == "CoreWeave"
    assert result.provenance.provider_source == "openrouter_metadata.endpoints"


async def test_a_fallback_attempt_in_the_metadata_still_fails_beside_a_body_provider() -> None:
    body = completion(attempt=2)
    body["provider"] = "CoreWeave"
    with pytest.raises(LLMProviderMismatchError, match="attempt 2"):
        await provider(Recorder(body)).generate(messages=MESSAGES)


async def test_the_generation_id_falls_back_to_the_response_header() -> None:
    body = completion()
    del body["id"]
    rec = Recorder(body, headers={"X-Generation-Id": "gen-from-header"})
    result = await provider(rec).generate(messages=MESSAGES)

    assert result.provenance is not None
    assert result.provenance.generation_id == "gen-from-header"


async def test_a_call_served_by_another_provider_is_an_error() -> None:
    rec = Recorder(completion(provider="DeepInfra"))
    with pytest.raises(LLMProviderMismatchError) as caught:
        await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)

    assert "provider_mismatch" in str(caught.value)
    assert isinstance(caught.value, LLMResponseError)  # the runners already treat it as an error
    assert caught.value.served == "DeepInfra"
    assert caught.value.expected == ("coreweave",)
    assert caught.value.provenance.served_provider == "DeepInfra"


async def test_a_second_attempt_is_an_error_even_on_the_pinned_provider() -> None:
    rec = Recorder(completion(attempt=2))
    with pytest.raises(LLMProviderMismatchError, match="attempt 2"):
        await provider(rec).generate(messages=MESSAGES)


async def test_missing_metadata_cannot_be_verified_so_it_is_an_error() -> None:
    rec = Recorder(completion(provider=None, attempt=None))
    with pytest.raises(LLMProviderMismatchError, match="did not say which provider"):
        await provider(rec).generate(messages=MESSAGES)


async def test_the_mismatch_wins_over_a_schema_failure_in_the_same_answer() -> None:
    rec = Recorder(completion(provider="Groq", content="not json"))
    with pytest.raises(LLMProviderMismatchError):
        await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)


async def test_a_schema_failure_carries_the_provenance_of_its_call() -> None:
    rec = Recorder(completion(content="not json"))
    with pytest.raises(LLMSchemaValidationError) as caught:
        await provider(rec).generate(messages=MESSAGES, schema=SCHEMA)

    assert caught.value.provenance is not None
    assert caught.value.provenance.served_provider == "CoreWeave"


@pytest.mark.parametrize(
    ("served", "expected"),
    [
        ("CoreWeave", ["coreweave"]),
        ("coreweave", ["coreweave"]),
        ("coreweave/bf16", ["coreweave"]),
        ("Phala", ["phala"]),
        ("Together", ["phala", "together"]),
    ],
)
def test_the_served_name_matches_the_pinned_slug(served: str, expected: list[str]) -> None:
    routing = ProviderRouting(order=expected)
    prov = {"served_provider": served, "attempt": 1}
    assert check_pinned_route(prov, routing) is None


def test_an_absent_attempt_is_accepted_when_the_provider_is_pinned() -> None:
    """With fallbacks off and one provider named, a matching server is enough; only a reported
    attempt above 1 shows a fallback."""
    routing = ProviderRouting(order=["coreweave"])
    assert check_pinned_route({"served_provider": "CoreWeave", "attempt": None}, routing) is None
    assert check_pinned_route({"served_provider": "CoreWeave"}, routing) is None
    assert check_pinned_route({"served_provider": "CoreWeave", "attempt": 0}, routing) is not None


@pytest.mark.parametrize("served", ["DeepInfra", "core", "", None])
def test_another_name_does_not_match(served: str | None) -> None:
    routing = ProviderRouting(order=["coreweave"])
    assert check_pinned_route({"served_provider": served, "attempt": 1}, routing) is not None


def test_a_route_that_allows_fallbacks_verifies_nothing() -> None:
    routing = ProviderRouting(order=["coreweave"], allow_fallbacks=True)
    assert check_pinned_route({"served_provider": "DeepInfra", "attempt": 2}, routing) is None


# --- errors ------------------------------------------------------------------------------


async def test_a_402_keeps_its_status_and_the_limit_that_caused_it() -> None:
    body = {
        "error": {
            "code": 402,
            "message": "in-flight budget",
            "metadata": {"limit_source": "openrouter_in_flight_budget"},
        }
    }
    rec = Recorder(body, status=402, headers={"Retry-After": "7"})
    with pytest.raises(LLMResponseError) as caught:
        await provider(rec).generate(messages=MESSAGES)

    assert caught.value.status_code == 402
    assert caught.value.limit_source == "openrouter_in_flight_budget"
    assert caught.value.retry_after_s == 7.0
    assert "status 402" in str(caught.value)


async def test_a_plain_error_status_has_no_limit_source() -> None:
    rec = Recorder({"error": {"code": 503, "message": "no provider"}}, status=503)
    with pytest.raises(LLMResponseError) as caught:
        await provider(rec).generate(messages=MESSAGES)

    assert caught.value.status_code == 503
    assert caught.value.limit_source is None
    assert caught.value.retry_after_s is None


async def test_the_limit_source_survives_the_200_character_cut_of_an_error_row() -> None:
    # The benchmark's error rows keep the first 200 characters of a job's last error, and the
    # route classifier reads them: a long body must not push the limit source past the cut,
    # or the transient in-flight budget would read as "no credit" and stop the run.
    body = {
        "error": {
            "code": 402,
            "message": "x" * 400,
            "metadata": {"limit_source": "openrouter_in_flight_budget"},
        }
    }
    rec = Recorder(body, status=402)
    with pytest.raises(LLMResponseError) as caught:
        await provider(rec).generate(messages=MESSAGES)

    cut = f"LLMResponseError: {caught.value}"[:200]
    assert "status 402 (openrouter_in_flight_budget)" in cut
    assert route_failure(_error_row(cut)) is None  # transient: retried, never a stop


@pytest.mark.parametrize(
    ("code", "source", "reason"),
    [
        (402, "openrouter_credits", "no_credit"),
        (402, "openrouter_in_flight_budget", None),
        (502, None, "no_provider"),
        (404, None, "no_provider"),
    ],
)
async def test_an_error_in_the_body_of_an_http_200_is_classified_by_its_code(
    code: int, source: str | None, reason: str | None
) -> None:
    # OpenRouter can answer 200 with only an error object; it is that status, not a malformed
    # answer, so the failure policy and the route breaker see what it is.
    error: dict[str, Any] = {"code": code, "message": "upstream said no"}
    if source is not None:
        error["metadata"] = {"limit_source": source}
    rec = Recorder({"error": error})
    with pytest.raises(LLMResponseError) as caught:
        await provider(rec).generate(messages=MESSAGES)

    assert caught.value.status_code == code
    assert caught.value.limit_source == source
    assert "in an HTTP 200 body" in str(caught.value)
    assert route_failure(_error_row(f"LLMResponseError: {caught.value}"[:200])) == reason


async def test_a_200_body_error_without_a_numeric_code_has_no_status() -> None:
    rec = Recorder({"error": {"code": "server_error", "message": "x"}})
    with pytest.raises(LLMResponseError) as caught:
        await provider(rec).generate(messages=MESSAGES)

    assert caught.value.status_code is None
    assert "error in the response body" in str(caught.value)


async def test_empty_choices_without_an_error_is_still_an_empty_answer() -> None:
    rec = Recorder({"id": "gen-1", "choices": []})
    with pytest.raises(LLMResponseError, match="Empty choices"):
        await provider(rec).generate(messages=MESSAGES)


def _error_row(message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"kind": "PipelineJobError", "message": message}}


# --- the factory -------------------------------------------------------------------------


def test_the_factory_hands_the_openai_provider_its_routing() -> None:
    settings = LLMTiersSettings(
        provider="openai",
        openai_api_key="sk-or-test",
        openai_base_url="https://openrouter.ai/api/v1",
        fast_model=QWEN,
        strong_model=QWEN,
        fallback_model=QWEN,
        openai_provider_routing=ProviderRouting.model_validate(QWEN_ROUTING),
        openai_response_metadata=True,
    )

    built = create_llm_provider(settings)

    assert isinstance(built, HttpLLMProvider)
    assert built.provider_routing == QWEN_ROUTING
    assert built.response_metadata is True


def test_the_factory_leaves_an_unrouted_provider_alone() -> None:
    built = create_llm_provider(LLMTiersSettings(provider="openai", openai_api_key="k"))

    assert isinstance(built, HttpLLMProvider)
    assert built.provider_routing is None
    assert built.response_metadata is False

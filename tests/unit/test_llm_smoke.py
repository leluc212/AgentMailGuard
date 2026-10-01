"""scripts/llm_smoke.py reports parse/validate, finish_reason and tokens, never the key (5.0).

Requirements: R14.7, R24.5. Every request goes to an httpx.MockTransport; nothing is live.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from packages.core.settings import AppSettings, LLMTiersSettings, ModelPricing

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"
KEY = "test-gemini-key-5f2c9a"


def _load_smoke() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the script imports its sibling stack_smoke
    spec = importlib.util.spec_from_file_location("llm_smoke", SCRIPTS / "llm_smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the script uses `from __future__ import annotations` and a
    # @dataclass, and dataclasses resolves string annotations via sys.modules[__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()

TRIAGE = {
    "category": "general_inquiry",
    "intent": "order_status",
    "priority": "normal",
    "reply_required": True,
    "workflow_hint": "ai",
    "retrieval_required": True,
    "confidence": 0.9,
    "reasoning": "asks for an order status",
}
DRAFT = {
    "action": "reply",
    "draft": "Your order is on its way; the courier link follows once it is scanned.",
    "confidence": 0.8,
    "knowledge_chunks": ["smoke-order-status-procedure"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


def _settings(**llm: Any) -> AppSettings:
    fields: dict[str, Any] = {
        "provider": "openai",
        "openai_api_key": KEY,
        "openai_base_url": GEMINI,
        "fast_model": "gemma-4-26b-a4b-it",
        "strong_model": "gemma-4-31b-it",
        "fallback_model": "gemini-3.1-flash-lite",
    }
    fields.update(llm)
    return AppSettings(_env_file=None, llm=LLMTiersSettings.model_validate(fields))


def _completion(content: str, finish_reason: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 321, "completion_tokens": 45},
    }


def _client(
    seen: list[httpx.Request],
    *,
    draft_content: str | None = None,
    draft_finish: str = "stop",
    draft_status: int = 200,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["schema"]
        if "draft" in schema.get("required", []):
            if draft_status != 200:
                # A provider that echoes the key in its error body must still not leak it.
                return httpx.Response(
                    draft_status, json={"error": {"message": f"API key {KEY} not valid"}}
                )
            content = json.dumps(DRAFT) if draft_content is None else draft_content
            return httpx.Response(200, json=_completion(content, draft_finish))
        return httpx.Response(200, json=_completion(json.dumps(TRIAGE), "stop"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_both_calls_pass_on_schema_valid_json() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen) as http:
        triage, draft = await smoke.run(_settings(), http)

    assert (triage.name, triage.ok, draft.name, draft.ok) == ("triage", True, "draft", True)
    for report in (triage, draft):
        assert report.finish_reason == "stop"
        assert (report.input_tokens, report.output_tokens) == (321, 45)
        assert report.model == "gemma-4-26b-a4b-it"
    assert "cites the given chunk: yes" in draft.detail

    assert len(seen) == 2
    for request in seen:
        assert str(request.url) == f"{GEMINI}/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
    bodies = [json.loads(r.content) for r in seen]
    assert [b["max_tokens"] for b in bodies] == [250, 1000]
    assert [b["model"] for b in bodies] == ["gemma-4-26b-a4b-it", "gemma-4-26b-a4b-it"]


async def test_truncated_draft_reports_length_and_fails() -> None:
    seen: list[httpx.Request] = []
    truncated = '{"action": "reply", "draft": "Your ord'
    async with _client(seen, draft_content=truncated, draft_finish="length") as http:
        _, draft = await smoke.run(_settings(), http)

    assert draft.ok is False
    assert draft.finish_reason == "length"
    assert "did not parse" in draft.detail
    # HttpLLMProvider raises before it reads `usage`; the script records it from the response.
    assert (draft.input_tokens, draft.output_tokens) == (321, 45)


async def test_parsed_but_invalid_draft_fails_validation() -> None:
    seen: list[httpx.Request] = []
    missing_tier = {k: v for k, v in DRAFT.items() if k != "model_tier"}
    async with _client(seen, draft_content=json.dumps(missing_tier)) as http:
        _, draft = await smoke.run(_settings(), http)

    assert draft.ok is False
    assert "failed validation" in draft.detail
    assert draft.finish_reason == "stop"
    assert draft.input_tokens == 321


async def test_output_never_contains_the_key() -> None:
    seen: list[httpx.Request] = []
    settings = _settings()
    async with _client(seen, draft_status=401) as http:
        reports = await smoke.run(settings, http)

    lines = smoke.describe_config(settings.llm) + smoke.render_reports(reports)
    text = "\n".join(lines)
    assert KEY not in text
    assert f"set ({len(KEY)} chars)" in text
    assert reports[1].ok is False
    assert "LLMResponseError" in reports[1].detail


async def test_refuses_the_fake_provider() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(smoke.SmokeFailure, match="LLM__PROVIDER is fake"):
            await smoke.run(_settings(provider="fake"), http)


def test_config_warns_about_models_missing_from_the_price_table() -> None:
    unpriced = "\n".join(smoke.describe_config(_settings().llm))
    assert "warn" in unpriced and "gemma-4-26b-a4b-it" in unpriced

    priced = _settings(
        price_table={
            "gemma-4-26b-a4b-it": ModelPricing(input_per_m=0, output_per_m=0),
            "gemma-4-31b-it": ModelPricing(input_per_m=0, output_per_m=0),
            "gemini-3.1-flash-lite": ModelPricing(input_per_m=0.25, output_per_m=1.5),
        }
    )
    assert "warn" not in "\n".join(smoke.describe_config(priced.llm))

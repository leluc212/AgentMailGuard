"""The guard's judges on the OpenRouter route (work package R4), with the real guard installed.

Skipped when mailguard is not importable (CI). Run under the overlay (make mailguard-bench-test).
The guard's OpenAIProvider talks to an ``httpx.MockTransport``: nothing calls the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("mailguard")

from mailguard.config.settings import MailGuardSettings  # noqa: E402
from mailguard.llm.openai_provider import OpenAIProvider  # noqa: E402
from mailguard.llm.protocol import ChatMessage  # noqa: E402
from mailguard.llm.registry import ModelRegistry  # noqa: E402

from evaluation.mailguard_bench import guard_smoke  # noqa: E402
from evaluation.mailguard_bench.counting import CountingProvider  # noqa: E402
from evaluation.mailguard_bench.guard_env import GUARD_MODELS_YAML, GuardEnvError  # noqa: E402
from evaluation.mailguard_bench.model_profiles import get_profile  # noqa: E402

PROFILES = ("qwen2.5-7b-openrouter", "llama-3.1-8b-openrouter")


def _guard_response(model: str, provider: str, attempt: int = 1) -> dict[str, Any]:
    return {
        "id": "gen-guard-1",
        "model": model,
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 4, "cost": 0.000006},
        "openrouter_metadata": {
            "summary": f"available=1, selected={provider}",
            "attempt": attempt,
            "endpoints": {"available": [{"provider": provider, "selected": True}]},
        },
    }


def _registry_provider(profile_name: str, body: dict[str, Any]) -> tuple[OpenAIProvider, list[Any]]:
    """The guard's provider for a profile, built by the guard's registry from guard_models.yaml."""
    profile = get_profile(profile_name)
    provider = ModelRegistry(
        MailGuardSettings(_env_file=None),
        models_path=GUARD_MODELS_YAML,
    ).get(profile.model)
    assert isinstance(provider, OpenAIProvider)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider, seen


@pytest.mark.parametrize("name", PROFILES)
def test_the_guard_registry_builds_the_provider_with_the_profiles_pin(name: str) -> None:
    profile = get_profile(name)
    provider, _ = _registry_provider(name, _guard_response(profile.model, "x"))

    assert provider.provider_routing == profile.routing
    assert provider.response_metadata is True
    assert provider.model == profile.model


@pytest.mark.parametrize(("name", "served"), [(PROFILES[0], "Phala"), (PROFILES[1], "CoreWeave")])
async def test_a_judge_call_sends_the_pin_and_records_the_served_provider(
    name: str, served: str
) -> None:
    profile = get_profile(name)
    provider, seen = _registry_provider(name, _guard_response(profile.model, served))
    counting = CountingProvider(provider)
    counting.begin_case()

    await counting.generate(messages=[ChatMessage(role="user", content="hi")], schema={})
    snapshot = counting.snapshot()

    sent = json.loads(seen[0].content)
    assert sent["provider"] == profile.routing
    assert seen[0].headers["x-openrouter-metadata"] == "enabled"
    assert snapshot["provenance"][0]["served_provider"] == served
    assert "route_violations" not in snapshot


async def test_a_judge_call_served_by_another_provider_is_recorded_as_a_violation() -> None:
    profile = get_profile(PROFILES[1])
    provider, _ = _registry_provider(PROFILES[1], _guard_response(profile.model, "Groq"))
    counting = CountingProvider(provider)
    counting.begin_case()

    await counting.generate(messages=[ChatMessage(role="user", content="hi")], schema={})

    (violation,) = counting.snapshot()["route_violations"]
    assert violation.startswith("provider_mismatch:") and "Groq" in violation


async def test_a_judge_call_with_only_the_body_provider_field_is_verified_by_it() -> None:
    profile = get_profile(PROFILES[1])
    body = _guard_response(profile.model, "x")
    del body["openrouter_metadata"]
    body["provider"] = "CoreWeave"
    provider, _ = _registry_provider(PROFILES[1], body)
    counting = CountingProvider(provider)
    counting.begin_case()

    await counting.generate(messages=[ChatMessage(role="user", content="hi")], schema={})
    snapshot = counting.snapshot()

    assert snapshot["provenance"][0]["served_provider"] == "CoreWeave"
    assert snapshot["provenance"][0]["provider_source"] == "response.provider"
    assert "route_violations" not in snapshot


def _probe_paths(monkeypatch: pytest.MonkeyPatch, provider: OpenAIProvider) -> Any:
    monkeypatch.setattr(
        guard_smoke,
        "build_guard_pipeline",
        lambda *_a, **_k: SimpleNamespace(l1=SimpleNamespace(judge=provider)),
    )
    return SimpleNamespace(l1_model=Path("clf.joblib"))


async def test_the_probe_passes_and_names_the_provider_that_served_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = get_profile(PROFILES[1])
    provider, _ = _registry_provider(PROFILES[1], _guard_response(profile.model, "CoreWeave"))

    await guard_smoke.live_probe(
        _probe_paths(monkeypatch, provider), profile.model, profile.routing
    )

    assert "served_by=CoreWeave" in capsys.readouterr().out


async def test_the_probe_fails_when_another_provider_served_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_profile(PROFILES[0])
    provider, _ = _registry_provider(PROFILES[0], _guard_response(profile.model, "Together"))

    with pytest.raises(GuardEnvError, match="provider_mismatch"):
        await guard_smoke.live_probe(
            _probe_paths(monkeypatch, provider), profile.model, profile.routing
        )


# --- the executor turns a pin violation into an error of the case --------------------------

GUARD_ALL_CLEAR: dict[str, Any] = {
    "is_injection": False,
    "is_poisoned": False,
    "safe": True,
    "confidence": 0.9,
    "techniques": [],
    "injected_instructions": [],
    "rationale": "ordinary customer request",
    "user_intent": "help installing a printer driver",
    "requested_actions": [],
    "entities": {},
    "contains_assistant_instructions": False,
    "instructions_to_assistant": [],
    "violations": [],
}
REPLY: dict[str, Any] = {
    "action": "reply",
    "draft": "Thanks for reaching out, please reinstall the driver from the vendor page.",
    "confidence": 0.8,
    "knowledge_chunks": [],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


def _routed_guard_fake(served: str) -> Any:
    from mailguard.llm.fake import FakeLLMProvider as GuardFakeLLM
    from mailguard.llm.protocol import CallProvenance

    profile = get_profile(PROFILES[1])

    class RoutedFake(GuardFakeLLM):  # type: ignore[misc,unused-ignore]
        provider_routing = profile.routing

        async def generate(self, **kwargs: Any) -> Any:
            result = await super().generate(**kwargs)
            result.provenance = CallProvenance(
                requested_model=profile.model, served_provider=served, attempt=1
            )
            return result

    return RoutedFake(default_response=GUARD_ALL_CLEAR, model_name="fake:fake")


async def _run_c3(tmp_path: Path, served: str) -> Any:
    from datetime import UTC, datetime
    from uuid import uuid4

    from evaluation.mailguard_bench.case_adapter import (
        EvalCase,
        PreparedCase,
        classification_for,
        to_normalized_message,
    )
    from evaluation.mailguard_bench.guard_build import build_guard
    from evaluation.mailguard_bench.guarded_reply import GuardedCaseExecutor
    from packages.domain.entities import ContextPackage
    from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator

    guard = build_guard(
        "C3",
        model_name="fake",
        audit_log_path=tmp_path / "audit.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
    )
    guard.guard_llm.inner = _routed_guard_fake(served)
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=REPLY),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    case = EvalCase.from_dict(
        {
            "case_id": "g-1",
            "kind": "benign",
            "category": "support",
            "email": {"sender_email": "x@partner.example", "subject": "Help", "body_text": "Hi"},
        }
    )
    org = uuid4()
    message = to_normalized_message(case, organization_id=org, received_at=datetime.now(UTC))
    prepared = PreparedCase(
        case=case,
        organization_id=org,
        message=message,
        classification=classification_for(case),
        context=ContextPackage(
            agent_instructions="You are an enterprise support assistant.",
            category_instructions="Address technical questions.",
            current_message=message,
        ),
        retrieval_query="Hi",
        ingested={},
        retrieved=(),
        context_ms=0,
    )
    executor = GuardedCaseExecutor(
        pipeline=guard.pipeline, generator=generator, guard_llm=guard.guard_llm
    )
    return await executor.execute(prepared)


async def test_guard_calls_on_the_pinned_provider_leave_the_case_scored(tmp_path: Path) -> None:
    execution = await _run_c3(tmp_path, "CoreWeave")

    assert execution.guard_errors == ()
    calls = execution.record["guard_llm"]
    assert calls["calls"] >= 1
    assert {p["served_provider"] for p in calls["provenance"]} == {"CoreWeave"}


async def test_a_guard_call_served_elsewhere_makes_the_case_an_error_not_a_defence(
    tmp_path: Path,
) -> None:
    execution = await _run_c3(tmp_path, "DeepInfra")

    assert any(error.startswith("provider_mismatch:") for error in execution.guard_errors)
    assert any("DeepInfra" in error for error in execution.guard_errors)


async def test_the_probe_calls_nothing_when_the_guards_provider_is_not_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_profile(PROFILES[1])
    unpinned = OpenAIProvider(model=profile.model)  # what the guard at 1a3ef62 would build
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    unpinned._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(GuardEnvError, match="provider routing"):
        await guard_smoke.live_probe(
            _probe_paths(monkeypatch, unpinned), profile.model, profile.routing
        )
    assert seen == []


def _build(tmp_path: Path, model: str, expected: Any) -> Any:
    from evaluation.mailguard_bench.guard_build import build_guard

    return build_guard(
        "C3",
        model_name=model,
        audit_log_path=tmp_path / "audit.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
        expected_route=expected,
    )


@pytest.mark.parametrize("name", PROFILES)
def test_build_guard_accepts_the_registry_entry_that_matches_the_run_pin(
    tmp_path: Path, name: str
) -> None:
    profile = get_profile(name)

    guard = _build(tmp_path, profile.model, profile.routing)

    assert guard.describe()["provider_routing"] == profile.routing


def test_build_guard_refuses_a_model_whose_registry_entry_is_not_the_run_pin(
    tmp_path: Path,
) -> None:
    profile = get_profile(PROFILES[1])

    with pytest.raises(GuardEnvError, match="provider routing"):
        _build(tmp_path, "gpt-4o-mini", profile.routing)  # registered, but sends no pin


def test_an_unrouted_run_builds_and_describes_the_guard_as_before(tmp_path: Path) -> None:
    guard = _build(tmp_path, "fake", None)

    assert "provider_routing" not in guard.describe()

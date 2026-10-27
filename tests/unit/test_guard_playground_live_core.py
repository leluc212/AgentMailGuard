"""core/models.py and core/runner.py (tasks 7.1, 7.2; Requirements 7.5, 7.7, 7.14-7.16, 9.4).

No network, no credentials: the real MailGuardPipeline runs with the guard's FakeLLMProvider
patched in place of the OpenAI provider.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.guard_playground.core import models as models_mod
from services.guard_playground.core import runner as runner_mod
from services.guard_playground.core.models import (
    DRAFT_MODELS,
    credential_present,
    get_model,
    missing_credential_message,
    model_choices,
)
from services.guard_playground.core.runner import LiveRunError, RunRegistry, classify
from services.guard_playground.core.schema import EmailInput
from services.guard_playground.guard_import import import_guard
from services.guard_playground.settings import PlaygroundSettings
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
)

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")

REPO = PlaygroundSettings().repo_root
L1 = PlaygroundSettings().l1_model_path
BENIGN = EmailInput(
    subject="Warranty",
    body="Hi, what is the warranty period for the X200? It rattles after 8 months.",
    chunks=[],
    source="custom",
    case_id=None,
)


def test_three_models_in_fixed_order() -> None:
    assert [k for k, _l, _r in model_choices()] == ["gpt-4o-mini", "qwen", "llama"]
    assert [m.label for m in DRAFT_MODELS] == [
        "gpt-4o-mini",
        "Qwen2.5-7B-Instruct",
        "Llama-3.1-8B-Instruct",
    ]
    with pytest.raises(ValueError):
        get_model("gpt-5")


def test_routing_matches_the_benchmark_registry() -> None:
    registry = yaml.safe_load(
        (REPO / "evaluation" / "mailguard_bench" / "guard_models.yaml").read_text(encoding="utf-8")
    )["models"]
    for m in DRAFT_MODELS:
        entry = registry[m.model_id]
        assert (entry.get("provider_routing") or None) == (
            dict(m.provider_routing) if m.provider_routing else None
        )
        assert bool(entry.get("response_metadata")) == m.response_metadata


def test_credentials_are_checked_by_name_never_shown() -> None:
    gpt, qwen = get_model("gpt-4o-mini"), get_model("qwen")
    assert not credential_present(gpt, {})
    assert credential_present(gpt, {"BENCH_OPENAI_API_KEY": "sk-secret-value"})
    assert not credential_present(qwen, {"BENCH_OPENAI_API_KEY": "sk-secret-value"})
    message = missing_credential_message(qwen)
    assert "Qwen2.5-7B-Instruct" in message and "BENCH_OPENROUTER_API_KEY" in message


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("OpenAI HTTP 401: invalid key", "authentication error"),
        ("OpenAI HTTP 429: slow down", "rate limit"),
        ("OpenAI transport error: connection refused", "connection error"),
        ("OpenAI HTTP 500: oops", "provider error"),
    ],
)
def test_error_categories(text: str, category: str) -> None:
    assert classify(RuntimeError(text)) == category


# --------------------------------------------------------------------------- real pipeline


@pytest.fixture(name="fake_models")
def fixture_fake_models(
    guard_import_isolation: None, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Replace the OpenAI provider with the guard's fake; records every pipeline build."""
    status = import_guard(REPO / "agentmailguard")
    if not status.available:
        pytest.skip(f"mailguard unavailable: {status.error}")
    from mailguard.llm.fake import FakeLLMProvider

    def fake_provider(m: Any) -> Any:
        def respond(messages: list[Any], schema: Any) -> dict[str, Any]:
            return {"draft": "The X200 has a 24-month warranty.", "knowledge_chunks": []}

        return FakeLLMProvider(responder=respond, model_name=m.model_id)

    monkeypatch.setattr(models_mod, "make_provider", fake_provider)
    builds: list[dict[str, Any]] = []
    real_build = runner_mod.build_pipeline

    def recording_build(preset: str, settings: Any, providers: dict[str, Any]) -> Any:
        pipeline = real_build(preset, settings, providers)
        builds.append({"preset": preset, "audit_enabled": pipeline.policy.audit_enabled})
        return pipeline

    monkeypatch.setattr(runner_mod, "build_pipeline", recording_build)
    return builds


async def test_execute_runs_the_real_pipeline_and_reports_stages(
    fake_models: list[dict[str, Any]],
) -> None:
    stages: list[str] = []
    result = await runner_mod.execute(
        BENIGN, "C3", "gpt-4o-mini", l1_model_path=L1, progress=stages.append
    )
    assert result.mode == "live" and result.draft_model == "gpt-4o-mini"
    assert set(result.guard_models.values()) == {"gpt-4o-mini"}
    assert result.draft_generated
    assert "Generation" in stages and stages[0] == "L1 Inbound Guard"
    assert fake_models == [{"preset": "C3", "audit_enabled": False}]  # Requirement 9.4


async def test_a_failed_model_call_is_a_live_failure(
    fake_models: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailguard.llm.fake import FakeLLMProvider
    from mailguard.llm.protocol import LLMResponseError

    def failing(m: Any) -> Any:
        return FakeLLMProvider(error_to_raise=LLMResponseError("OpenAI HTTP 401: bad key"))

    monkeypatch.setattr(models_mod, "make_provider", failing)
    with pytest.raises(LiveRunError) as caught:
        await runner_mod.execute(BENIGN, "C3", "gpt-4o-mini", l1_model_path=L1)
    assert caught.value.category == "authentication error"


# --------------------------------------------------------------------------- registry


async def _wait(registry: RunRegistry, run_id: str) -> Any:
    for _ in range(200):
        state = registry.get(run_id)
        assert state is not None
        if state.status != "running":
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


async def test_registry_timeout_and_failure() -> None:
    async def slow(*_a: Any, progress: Any, **_k: Any) -> Any:
        progress("L1 Inbound Guard")
        await asyncio.sleep(5)

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise LiveRunError("rate limit", "OpenAI HTTP 429")

    slow_reg = RunRegistry(slow, timeout_s=0.05)
    state = await _wait(slow_reg, slow_reg.start(BENIGN, "C3", "qwen", l1_model_path=L1))
    assert state.status == "timeout" and "timed out" in state.error_message
    assert state.stage == "L1 Inbound Guard" and state.result is None

    bad_reg = RunRegistry(broken)
    state = await _wait(bad_reg, bad_reg.start(BENIGN, "C0", "llama", l1_model_path=L1))
    assert (state.status, state.error_category) == ("failed", "rate limit")


def test_registry_unknown_run(tmp_path: Path) -> None:
    assert RunRegistry().get("nope") is None

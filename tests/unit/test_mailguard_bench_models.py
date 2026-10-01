"""Per-model profiles for the AgentMailGuard benchmark (task 7.19; owner decision 2026-09-29).

The benchmark runs GPT-4o-mini (OpenAI), Llama-3.1-8B and Qwen2.5-7B (both on the owner's
desktop Ollama) and the first test run's Gemma, each live. These tests only check how a profile
is turned into process settings; they make no call.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

import pytest
import yaml

from evaluation.mailguard_bench.model_profiles import (
    BENCH_MODELS,
    PROFILES,
    ModelProfileError,
    get_profile,
    profile_env,
)

GUARD_MODELS_YAML = Path("evaluation/mailguard_bench/guard_models.yaml")


def test_every_benchmark_model_has_a_profile() -> None:
    assert set(PROFILES) == {"gemma-4-26b", "gpt-4o-mini", "llama-3.1-8b-local", "qwen2.5-7b"}
    assert get_profile("gpt-4o-mini").model == "gpt-4o-mini"
    assert get_profile("llama-3.1-8b-local").model == "llama3.1:8b"
    assert get_profile("qwen2.5-7b").model == "qwen2.5:7b-instruct"
    assert get_profile("gemma-4-26b").model == "gemma-4-26b-a4b-it"
    assert {p.model for p in PROFILES.values()} == BENCH_MODELS
    with pytest.raises(ModelProfileError, match="unknown model profile 'gpt-5'"):
        get_profile("gpt-5")


def test_a_profile_sets_one_live_openai_compatible_endpoint_for_every_tier() -> None:
    env = profile_env(get_profile("gpt-4o-mini"), {"BENCH_OPENAI_API_KEY": "sk-test"})
    assert env["LLM__PROVIDER"] == "openai"
    assert env["LLM__OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert env["LLM__OPENAI_API_KEY"] == "sk-test"
    for tier in ("LLM__FAST_MODEL", "LLM__STRONG_MODEL", "LLM__FALLBACK_MODEL"):
        assert env[tier] == "gpt-4o-mini"
    prices = json.loads(env["LLM__PRICE_TABLE"])
    assert prices["gpt-4o-mini"] == {"input_per_m": 0.15, "output_per_m": 0.6}


def test_a_missing_key_fails_before_any_call_and_names_the_variable() -> None:
    with pytest.raises(ModelProfileError, match="BENCH_OPENAI_API_KEY"):
        profile_env(get_profile("gpt-4o-mini"), {})
    with pytest.raises(ModelProfileError, match="LLM__OPENAI_API_KEY"):
        profile_env(get_profile("gemma-4-26b"), {"LLM__OPENAI_API_KEY": "  "})


def test_local_qwen_needs_no_key_and_its_ollama_url_can_be_moved() -> None:
    qwen = profile_env(get_profile("qwen2.5-7b"), {})
    assert qwen["LLM__OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert qwen["LLM__OPENAI_API_KEY"] == "ollama"
    assert json.loads(qwen["LLM__PRICE_TABLE"])["qwen2.5:7b-instruct"] == {
        "input_per_m": 0.0,
        "output_per_m": 0.0,
    }
    moved = profile_env(
        get_profile("qwen2.5-7b"), {"BENCH_OLLAMA_BASE_URL": "http://10.0.0.5:11434/v1/"}
    )
    assert moved["LLM__OPENAI_BASE_URL"] == "http://10.0.0.5:11434/v1"


def test_local_llama_runs_on_the_same_ollama_as_qwen_with_no_key() -> None:
    # Owner decision 2026-09-29 (evening): Llama-3.1-8B runs only on the desktop's Ollama (its
    # 4-bit build, like Qwen's); the OpenRouter path was removed after its account had no credit.
    llama = profile_env(get_profile("llama-3.1-8b-local"), {})
    assert llama["LLM__OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert llama["LLM__OPENAI_API_KEY"] == "ollama"
    for tier in ("LLM__FAST_MODEL", "LLM__STRONG_MODEL", "LLM__FALLBACK_MODEL"):
        assert llama[tier] == "llama3.1:8b"
    assert json.loads(llama["LLM__PRICE_TABLE"])["llama3.1:8b"] == {
        "input_per_m": 0.0,
        "output_per_m": 0.0,
    }
    moved = profile_env(
        get_profile("llama-3.1-8b-local"), {"BENCH_OLLAMA_BASE_URL": "http://10.0.0.5:11434/v1/"}
    )
    assert moved["LLM__OPENAI_BASE_URL"] == "http://10.0.0.5:11434/v1"


def test_the_gemma_profile_reuses_the_existing_gemini_settings() -> None:
    env = profile_env(get_profile("gemma-4-26b"), {"LLM__OPENAI_API_KEY": "gemini-key"})
    assert env["LLM__OPENAI_BASE_URL"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert env["LLM__OPENAI_API_KEY"] == "gemini-key"


def test_every_profile_model_is_registered_for_the_guard_judges() -> None:
    registered = yaml.safe_load(GUARD_MODELS_YAML.read_text(encoding="utf-8"))["models"]
    for profile in PROFILES.values():
        spec = registered[profile.model]
        assert spec["backend"] == "openai"
        assert spec["model"] == profile.model
        assert "api_key" not in spec and "base_url" not in spec  # keys never land in a file


def test_the_runner_applies_a_profile_to_the_process_and_the_guard_model() -> None:
    from evaluation.mailguard_bench.runner import apply_model_profile, parse_args

    args = parse_args(["--config", "C3", "--run", "r", "--model-profile", "gpt-4o-mini"])
    updates = apply_model_profile(args, {"BENCH_OPENAI_API_KEY": "sk-test"})
    assert updates["LLM__FAST_MODEL"] == "gpt-4o-mini"
    assert args.guard_model == "gpt-4o-mini"  # one model in both roles

    plain = parse_args(["--config", "C3", "--run", "r"])
    assert apply_model_profile(plain, {}) == {}  # no profile: the process settings stand
    with pytest.raises(SystemExit):
        parse_args(["--config", "C3", "--run", "r", "--model-profile", "gpt-5"])


def test_runner_and_probe_resolve_a_profile_the_same_way() -> None:
    from evaluation.mailguard_bench.model_profiles import resolve_profile

    updates, model = resolve_profile("llama-3.1-8b-local", {}, "x")
    assert model == "llama3.1:8b"  # the model, not the profile's name
    assert updates["LLM__OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert resolve_profile(None, {}, "gemma-4-26b-a4b-it") == ({}, "gemma-4-26b-a4b-it")


# The owner keeps the keys in .env (demo-runbook §9.8 step 3) and `uv run` does not load it,
# so the runner and the probe must read it themselves, the way AppSettings reads .env.
PROFILE_SETTINGS = (
    "LLM__PROVIDER",
    "LLM__OPENAI_BASE_URL",
    "LLM__OPENAI_API_KEY",
    "LLM__FAST_MODEL",
    "LLM__STRONG_MODEL",
    "LLM__FALLBACK_MODEL",
    "LLM__PRICE_TABLE",
)


class _StopAfterProfileError(Exception):
    """Raised by a stand-in for the first step after the profile is applied."""


def _stop_after_profile(*_args: object, **_kwargs: object) -> NoReturn:
    raise _StopAfterProfileError


@pytest.fixture
def repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The working directory of a run, with no OpenAI key in the process environment."""
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ):  # the run writes its settings into os.environ; all undone
        os.environ.pop("BENCH_OPENAI_API_KEY", None)
        for name in PROFILE_SETTINGS:
            os.environ.pop(name, None)
        yield tmp_path


def _run_runner_until_profile_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    from evaluation.mailguard_bench import runner

    monkeypatch.setattr(runner, "guard_paths_from_env", _stop_after_profile)
    args = runner.parse_args(["--config", "C3", "--run", "r", "--model-profile", "gpt-4o-mini"])
    with pytest.raises(_StopAfterProfileError):
        asyncio.run(runner.run(args))


def test_the_runner_reads_a_key_kept_only_in_dot_env(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo_dir / ".env").write_text("BENCH_OPENAI_API_KEY=sk-from-dot-env\n", encoding="utf-8")
    _run_runner_until_profile_applied(monkeypatch)
    assert os.environ["LLM__OPENAI_API_KEY"] == "sk-from-dot-env"
    assert os.environ["LLM__FAST_MODEL"] == "gpt-4o-mini"


def test_the_probe_reads_a_key_kept_only_in_dot_env(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mailguard")
    from evaluation.mailguard_bench import guard_smoke

    (repo_dir / ".env").write_text("BENCH_OPENAI_API_KEY=sk-from-dot-env\n", encoding="utf-8")
    monkeypatch.setattr(guard_smoke, "guard_paths_from_env", _stop_after_profile)
    with pytest.raises(_StopAfterProfileError):
        guard_smoke.run(["--live-probe", "--model-profile", "gpt-4o-mini"])
    assert os.environ["LLM__OPENAI_API_KEY"] == "sk-from-dot-env"


def test_a_key_set_in_the_environment_wins_over_dot_env(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo_dir / ".env").write_text("BENCH_OPENAI_API_KEY=sk-from-dot-env\n", encoding="utf-8")
    os.environ["BENCH_OPENAI_API_KEY"] = "sk-from-environment"
    _run_runner_until_profile_applied(monkeypatch)
    assert os.environ["LLM__OPENAI_API_KEY"] == "sk-from-environment"


def test_without_dot_env_a_missing_key_still_names_the_variable(repo_dir: Path) -> None:
    from evaluation.mailguard_bench import runner

    args = runner.parse_args(["--config", "C3", "--run", "r", "--model-profile", "gpt-4o-mini"])
    with pytest.raises(ModelProfileError, match="BENCH_OPENAI_API_KEY"):
        asyncio.run(runner.run(args))

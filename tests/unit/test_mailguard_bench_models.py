"""Per-model profiles for the AgentMailGuard benchmark (task 7.19; owner decision 2026-09-29).

The benchmark runs GPT-4o-mini (OpenAI), Llama-3.1-8B (OpenRouter), Qwen2.5-7B (Ollama on the
owner's desktop) and the first test run's Gemma, each live. These tests only check how a
profile is turned into process settings; they make no call.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_the_four_benchmark_models_have_profiles() -> None:
    assert set(PROFILES) == {"gemma-4-26b", "gpt-4o-mini", "llama-3.1-8b", "qwen2.5-7b"}
    assert get_profile("gpt-4o-mini").model == "gpt-4o-mini"
    assert get_profile("llama-3.1-8b").model == "meta-llama/llama-3.1-8b-instruct"
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

    llama = profile_env(get_profile("llama-3.1-8b"), {"BENCH_OPENROUTER_API_KEY": "or-test"})
    assert llama["LLM__OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert llama["LLM__OPENAI_API_KEY"] == "or-test"
    assert llama["LLM__FAST_MODEL"] == "meta-llama/llama-3.1-8b-instruct"


def test_a_missing_key_fails_before_any_call_and_names_the_variable() -> None:
    with pytest.raises(ModelProfileError, match="BENCH_OPENAI_API_KEY"):
        profile_env(get_profile("gpt-4o-mini"), {})
    with pytest.raises(ModelProfileError, match="BENCH_OPENROUTER_API_KEY"):
        profile_env(get_profile("llama-3.1-8b"), {"BENCH_OPENROUTER_API_KEY": "  "})


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

    updates, model = resolve_profile("llama-3.1-8b", {"BENCH_OPENROUTER_API_KEY": "or-test"}, "x")
    assert model == "meta-llama/llama-3.1-8b-instruct"
    assert updates["LLM__OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert resolve_profile(None, {}, "gemma-4-26b-a4b-it") == ({}, "gemma-4-26b-a4b-it")

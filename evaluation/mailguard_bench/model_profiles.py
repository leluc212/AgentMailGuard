"""Per-model profiles for the AgentMailGuard benchmark (task 7.19; owner decision 2026-09-29).

One benchmark RUN uses one model, live, in both roles: rag-email's generation call and the
guard's L1/L2 judges. Every profile is an OpenAI-compatible endpoint, so rag-email's `openai`
provider and the guard's `openai` backend reach it by base URL alone:

    gpt-4o-mini   OpenAI                        key from BENCH_OPENAI_API_KEY
    llama-3.1-8b  OpenRouter                    key from BENCH_OPENROUTER_API_KEY
    qwen2.5-7b    Ollama on the owner's desktop no key (BENCH_OLLAMA_BASE_URL moves the host)
    gemma-4-26b   Gemini API (the first test run) key from LLM__OPENAI_API_KEY

`profile_env` returns the rag-email settings for the process; keys are read from the
environment and never written to a file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass


class ModelProfileError(ValueError):
    """An unknown profile, or a profile whose key is not set."""


@dataclass(frozen=True)
class ModelProfile:
    """One live model endpoint for a benchmark run."""

    name: str
    model: str
    base_url: str
    api_key_env: str | None
    input_per_m: float
    output_per_m: float
    base_url_env: str | None = None
    fixed_api_key: str | None = None


PROFILES: dict[str, ModelProfile] = {
    profile.name: profile
    for profile in (
        ModelProfile(
            name="gemma-4-26b",
            model="gemma-4-26b-a4b-it",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key_env="LLM__OPENAI_API_KEY",
            input_per_m=0.042,
            output_per_m=0.22,
        ),
        ModelProfile(
            name="gpt-4o-mini",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key_env="BENCH_OPENAI_API_KEY",
            input_per_m=0.15,
            output_per_m=0.60,
        ),
        ModelProfile(
            name="llama-3.1-8b",
            model="meta-llama/llama-3.1-8b-instruct",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="BENCH_OPENROUTER_API_KEY",
            input_per_m=0.05,
            output_per_m=0.08,
        ),
        ModelProfile(
            name="qwen2.5-7b",
            model="qwen2.5:7b-instruct",
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            input_per_m=0.0,
            output_per_m=0.0,
            base_url_env="BENCH_OLLAMA_BASE_URL",
            fixed_api_key="ollama",
        ),
    )
}

BENCH_MODELS: frozenset[str] = frozenset(profile.model for profile in PROFILES.values())


def get_profile(name: str) -> ModelProfile:
    """Look up a benchmark model profile by its short name."""
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ModelProfileError(f"unknown model profile {name!r}; known: {known}") from None


def profile_env(profile: ModelProfile, environ: Mapping[str, str]) -> dict[str, str]:
    """rag-email settings that point every tier of this process at the profile's model.

    Raises:
        ModelProfileError: If the profile needs a key and its environment variable is unset.
    """
    base_url = profile.base_url
    if profile.base_url_env and (environ.get(profile.base_url_env) or "").strip():
        base_url = environ[profile.base_url_env].strip()
    if profile.fixed_api_key is not None:
        api_key = profile.fixed_api_key
    else:
        assert profile.api_key_env is not None
        api_key = (environ.get(profile.api_key_env) or "").strip()
        if not api_key:
            raise ModelProfileError(
                f"model profile {profile.name!r} needs its key in {profile.api_key_env} (.env)"
            )
    price_table = {
        profile.model: {"input_per_m": profile.input_per_m, "output_per_m": profile.output_per_m}
    }
    return {
        "LLM__PROVIDER": "openai",
        "LLM__OPENAI_BASE_URL": base_url.rstrip("/"),
        "LLM__OPENAI_API_KEY": api_key,
        "LLM__FAST_MODEL": profile.model,
        "LLM__STRONG_MODEL": profile.model,
        "LLM__FALLBACK_MODEL": profile.model,
        "LLM__PRICE_TABLE": json.dumps(price_table),
    }


def resolve_profile(
    name: str | None, environ: Mapping[str, str], default_model: str
) -> tuple[dict[str, str], str]:
    """Process settings and model for an optional profile name (no name: nothing changes)."""
    if not name:
        return {}, default_model
    profile = get_profile(name)
    return profile_env(profile, environ), profile.model

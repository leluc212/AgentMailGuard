"""Per-model profiles for the AgentMailGuard benchmark (task 7.19; owner decision 2026-09-29).

One benchmark RUN uses one model, live, in both roles: rag-email's generation call and the
guard's L1/L2 judges. Every profile is an OpenAI-compatible endpoint, so rag-email's `openai`
provider and the guard's `openai` backend reach it by base URL alone:

    gpt-4o-mini         OpenAI                        key from BENCH_OPENAI_API_KEY
    llama-3.1-8b-local  Ollama on the owner's desktop no key (4-bit build, like Qwen's)
    qwen2.5-7b          Ollama on the owner's desktop no key (BENCH_OLLAMA_BASE_URL moves the host)
    gemma-4-26b         Gemini API (the first test run) key from LLM__OPENAI_API_KEY
    qwen2.5-7b-openrouter    OpenRouter, Phala pinned      key from BENCH_OPENROUTER_API_KEY
    llama-3.1-8b-openrouter  OpenRouter, CoreWeave bf16    key from BENCH_OPENROUTER_API_KEY

v1 ran Llama-3.1-8B and Qwen2.5-7B on the desktop's Ollama (owner decision 2026-09-29, evening;
the first OpenRouter profile was removed after its account had no credit). The v2 benchmark is
full cloud (owner decision 2026-10-01, ADR-0014): ``gpt-4o-mini`` and the two ``-openrouter``
profiles. Each of these pins ONE provider with fallbacks off and structured-output support
required, asks OpenRouter for its routing metadata, and so records the provider that served every
call (``packages/llm/provenance.py``). Their results are not comparable with the local 4-bit runs,
and reports say so. The embedding is not a profile's: it is the runner's choice (``stack_env``).

`profile_env` returns the rag-email settings for the process; keys are read from the
environment or `.env` (the environment wins) and never written to a file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
    provider_pin: str | None = None
    """The one provider slug the route is pinned to (OpenRouter ``provider.order``)."""
    quantizations: tuple[str, ...] = ()
    """Precisions the pinned endpoint may serve; empty when it reports none (Phala: unknown)."""

    @property
    def routing(self) -> dict[str, object] | None:
        """OpenRouter's ``provider`` request object for this profile; None when not routed."""
        if self.provider_pin is None:
            return None
        routing: dict[str, object] = {
            "order": [self.provider_pin],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        if self.quantizations:
            routing["quantizations"] = list(self.quantizations)
        return routing


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
            name="llama-3.1-8b-local",
            model="llama3.1:8b",
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            input_per_m=0.0,
            output_per_m=0.0,
            base_url_env="BENCH_OLLAMA_BASE_URL",
            fixed_api_key="ollama",
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
        # The full-cloud route (task 7.29, ADR-0014). Slugs, providers and prices from OpenRouter's
        # live API, 2026-09-30, re-read 2026-10-01. Qwen has ONE provider (Phala, precision
        # undisclosed), so no precision filter (it would exclude it). Only CoreWeave lists
        # structured outputs for Llama, at bf16.
        ModelProfile(
            name="qwen2.5-7b-openrouter",
            model="qwen/qwen-2.5-7b-instruct",
            base_url=OPENROUTER_BASE_URL,
            api_key_env="BENCH_OPENROUTER_API_KEY",
            input_per_m=0.10,
            output_per_m=0.20,
            base_url_env="BENCH_OPENROUTER_BASE_URL",
            provider_pin="phala",
        ),
        ModelProfile(
            name="llama-3.1-8b-openrouter",
            model="meta-llama/llama-3.1-8b-instruct",
            base_url=OPENROUTER_BASE_URL,
            api_key_env="BENCH_OPENROUTER_API_KEY",
            input_per_m=0.22,
            output_per_m=0.22,
            base_url_env="BENCH_OPENROUTER_BASE_URL",
            provider_pin="coreweave",
            quantizations=("bf16",),
        ),
    )
}

BENCH_MODELS: frozenset[str] = frozenset(profile.model for profile in PROFILES.values())


_QUANT = re.compile(r"[-:_.]q\d+(?:_[a-z0-9]+)*$")
_DATE = re.compile(r"[-_]?(?:\d{4}-\d{2}-\d{2}|\d{8})")
_NOISE_TOKENS = frozenset({"instruct", "chat", "it", "latest", "fp16", "bf16"})


def model_key(name: str) -> str:
    """A model's family and size, without the spelling: ``qwen2.5:7b-instruct-q4_K_M``,
    ``Qwen2.5-7B-Instruct`` and ``qwen2.5:7b`` are all ``qwen2.57b``.

    Drops the provider prefix, a date suffix, a quantisation tag and ``instruct``/``chat``/
    ``latest``, lowercases and joins what is left, so variants of one model compare equal while
    another size or version (``gpt-4o`` against ``gpt-4o-mini``) does not.
    """
    text = name.strip().lower().rsplit("/", 1)[-1]
    text = _DATE.sub("", _QUANT.sub("", text))
    tokens = [t for t in re.split(r"[-:_\s]+", text) if t and t not in _NOISE_TOKENS]
    return "".join(tokens)


def benchmarked_model_names() -> frozenset[str]:
    """Every name a model under test goes by: its profile names and its model ids."""
    return frozenset(
        name.lower() for profile in PROFILES.values() for name in (profile.name, profile.model)
    )


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
    routing = profile.routing
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
        # Always stated, blank when not routed: the host processes take these over `.env`, so a
        # routing line left there by an OpenRouter run cannot reach a run on another endpoint.
        "LLM__OPENAI_PROVIDER_ROUTING": json.dumps(routing) if routing is not None else "",
        "LLM__OPENAI_RESPONSE_METADATA": "true" if routing is not None else "",
    }


def with_dot_env(environ: Mapping[str, str], env_file: str | Path = ".env") -> dict[str, str]:
    """The process environment over the `.env` file, the precedence AppSettings uses.

    `uv run` does not load `.env`, so a key kept there (demo-runbook §9.8) reaches a profile
    only through this merge; a variable set in the environment still wins.
    """
    from_file = dotenv_values(env_file, encoding="utf-8")
    return {**{k: v for k, v in from_file.items() if v is not None}, **environ}


def resolve_profile(
    name: str | None, environ: Mapping[str, str], default_model: str
) -> tuple[dict[str, str], str]:
    """Process settings and model for an optional profile name (no name: nothing changes)."""
    if not name:
        return {}, default_model
    profile = get_profile(name)
    return profile_env(profile, environ), profile.model

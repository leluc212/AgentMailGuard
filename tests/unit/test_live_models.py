"""Live model catalog and per-job live model choice (task 9.16, ADR-0019, R25.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.model_profiles import PROFILES
from packages.core.run_context import current_live_model, run_options_scope
from packages.core.settings import LLMTiersSettings
from packages.llm.live_models import LiveModelCatalogError, load_live_models
from packages.llm.model_profile import (
    ModelProfileUnavailableError,
    ProfileRoutedLLMProvider,
    build_profile_provider,
)
from packages.llm.protocol import LLMProvider, LLMResult

CATALOG = Path("config/live_models.yaml")
DEMO_MODELS = ("llama-3.1-8b-openrouter", "qwen3.5-9b-openrouter")  # ADR-0020


class _Named(LLMProvider):
    def __init__(self, name: str) -> None:
        self.name = name

    async def generate(self, **_: Any) -> LLMResult:
        raise AssertionError(f"{self.name} called")


def test_a_catalog_model_named_like_a_benchmark_profile_runs_as_the_benchmark_runs_it() -> None:
    catalog = load_live_models(CATALOG, environ={})
    assert [m.name for m in catalog.models] == list(DEMO_MODELS)
    shared = [m for m in catalog.models if m.name in PROFILES]
    assert [m.name for m in shared] == ["llama-3.1-8b-openrouter"]
    for model in shared:
        profile = PROFILES[model.name]
        assert model.model == profile.model
        assert model.base_url == profile.base_url.rstrip("/")
        assert model.input_per_m == profile.input_per_m
        assert model.output_per_m == profile.output_per_m
        assert model.provider_routing == profile.routing


def test_a_model_is_available_only_with_its_key_set() -> None:
    catalog = load_live_models(CATALOG, environ={"DEMO_OPENROUTER_API_KEY": "k"})
    assert catalog.available("qwen3.5-9b-openrouter")
    assert catalog.available("llama-3.1-8b-openrouter")
    assert not catalog.available("gpt-4o-mini")  # left the catalog (ADR-0020)
    assert not catalog.available("unknown")
    assert set(catalog.build_providers(timeout_s=5)) == set(DEMO_MODELS)
    keyless = load_live_models(CATALOG, environ={})
    assert not any(keyless.available(name) for name in DEMO_MODELS)
    assert keyless.build_providers(timeout_s=5) == {}


def test_an_unpinned_model_sends_no_routing() -> None:
    model = load_live_models(CATALOG, environ={}).get("qwen3.5-9b-openrouter")
    assert model is not None
    settings = model.tier_settings("k", timeout_s=5)
    assert settings.openai_provider_routing is None
    assert settings.openai_response_metadata is False


def test_a_routed_model_asks_for_the_routers_metadata() -> None:
    model = load_live_models(CATALOG, environ={}).get("llama-3.1-8b-openrouter")
    assert model is not None
    settings = model.tier_settings("k", timeout_s=5)
    assert settings.openai_response_metadata is True
    assert settings.fast_model == settings.strong_model == settings.fallback_model == model.model


def test_an_absent_catalog_is_empty_and_a_bad_one_is_refused(tmp_path: Path) -> None:
    assert load_live_models(tmp_path / "missing.yaml", environ={}).models == ()
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: [{name: x}]", encoding="utf-8")
    with pytest.raises(LiveModelCatalogError):
        load_live_models(bad, environ={})


def test_the_job_picks_its_live_model_and_fake_ignores_it() -> None:
    a, b, fake = _Named("a"), _Named("b"), _Named("fake")
    routed = ProfileRoutedLLMProvider(fake=fake, live=None, live_models={"a": a, "b": b})
    assert routed.provider_for("live", "b") is b
    assert routed.provider_for("fake", "b") is fake
    with pytest.raises(ModelProfileUnavailableError):
        routed.provider_for("live", "c")  # no key on this worker
    with pytest.raises(ModelProfileUnavailableError):
        routed.provider_for("live", None)  # two catalog models, none named


def test_without_a_name_the_llm_provider_or_the_only_catalog_model_runs() -> None:
    stack, only = _Named("stack"), _Named("only")
    assert ProfileRoutedLLMProvider(fake=_Named("f"), live=stack).provider_for("live") is stack
    single = ProfileRoutedLLMProvider(fake=_Named("f"), live=None, live_models={"only": only})
    assert single.provider_for("live") is only
    assert single.default_profile == "fake"  # a job without options keeps the configured fake


async def test_the_live_view_follows_the_jobs_live_model() -> None:
    seen: list[str] = []

    class _Records(_Named):
        async def generate(self, **_: Any) -> LLMResult:
            seen.append(self.name)
            return None  # type: ignore[return-value]  # the view returns what it was given

    routed = ProfileRoutedLLMProvider(
        fake=_Named("f"), live=None, live_models={"a": _Records("a"), "b": _Records("b")}
    )
    view = routed.live_view()
    assert view is not None
    with run_options_scope({"model_profile": "live", "live_model": "b"}):
        assert current_live_model() == "b"
        await view.generate(messages=[])
    assert seen == ["b"]
    assert ProfileRoutedLLMProvider(fake=_Named("f"), live=None).live_view() is None


def test_build_profile_provider_adds_the_keyed_catalog_models() -> None:
    catalog = load_live_models(CATALOG, environ={"DEMO_OPENROUTER_API_KEY": "k"})
    routed = build_profile_provider(LLMTiersSettings(provider="fake"), live_models=catalog)
    assert routed.live is None
    assert set(routed.live_models) == set(DEMO_MODELS)
    assert routed.has_live

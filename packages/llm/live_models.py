"""Catalog of the live models a demo job can pick (task 9.16, ADR-0019, R25.4).

``config/live_models.yaml`` lists OpenAI-compatible endpoints by name. A model is *available*
when the environment variable named by its ``api_key_env`` holds a key; the key itself is read
from the environment at start-up and never stored in the catalog, a log or a response.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from packages.core.settings import LLMTiersSettings, ModelPricing
from packages.llm.factory import create_llm_provider
from packages.llm.protocol import LLMProvider

DEFAULT_LIVE_MODELS_PATH = "config/live_models.yaml"


class LiveModelCatalogError(ValueError):
    """The catalog file is malformed."""


@dataclass(frozen=True)
class LiveModel:
    """One live model endpoint of the catalog."""

    name: str
    label: str
    model: str
    base_url: str
    api_key_env: str
    input_per_m: float
    output_per_m: float
    provider_routing: dict[str, Any] | None = None

    def api_key(self, environ: Mapping[str, str]) -> str:
        """The key from the environment, or '' when unset."""
        return (environ.get(self.api_key_env) or "").strip()

    def tier_settings(self, api_key: str, *, timeout_s: float) -> LLMTiersSettings:
        """rag-email settings that send every tier to this model."""
        return LLMTiersSettings.model_validate(
            {
                "provider": "openai",
                "openai_base_url": self.base_url,
                "openai_api_key": api_key,
                "fast_model": self.model,
                "strong_model": self.model,
                "fallback_model": self.model,
                "timeout_s": timeout_s,
                "openai_provider_routing": self.provider_routing,
                # a routing pin is only checkable with the router's metadata (LLMTiersSettings)
                "openai_response_metadata": self.provider_routing is not None,
                "price_table": {self.model: self.pricing().model_dump()},
            }
        )

    def pricing(self) -> ModelPricing:
        return ModelPricing(input_per_m=self.input_per_m, output_per_m=self.output_per_m)


@dataclass(frozen=True)
class LiveModelCatalog:
    """The catalog and, per model, whether its key is set in the environment."""

    models: tuple[LiveModel, ...]
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def available(self, name: str) -> bool:
        model = self.get(name)
        return model is not None and bool(model.api_key(self.environ))

    def get(self, name: str) -> LiveModel | None:
        return next((m for m in self.models if m.name == name), None)

    def prices(self) -> dict[str, ModelPricing]:
        """The price of each model this process can call (its key is set), for R21.6."""
        return {m.model: m.pricing() for m in self.models if m.api_key(self.environ)}

    def with_prices(self, base: dict[str, ModelPricing]) -> dict[str, ModelPricing]:
        """``base`` plus the callable models' prices; ``base`` wins, and is returned as is
        when no catalog model has a key (the stack before ADR-0019)."""
        extra = self.prices()
        return {**extra, **base} if extra else base

    def build_providers(
        self, *, timeout_s: float, client: httpx.AsyncClient | None = None
    ) -> dict[str, LLMProvider]:
        """A provider per available model, by name."""
        providers: dict[str, LLMProvider] = {}
        for model in self.models:
            key = model.api_key(self.environ)
            if key:
                providers[model.name] = create_llm_provider(
                    model.tier_settings(key, timeout_s=timeout_s), client=client
                )
        return providers


def _parse(raw: Any) -> tuple[LiveModel, ...]:
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
        raise LiveModelCatalogError("live models catalog needs a `models` list")
    models: list[LiveModel] = []
    for entry in raw["models"]:
        try:
            models.append(
                LiveModel(
                    name=str(entry["name"]),
                    label=str(entry.get("label") or entry["name"]),
                    model=str(entry["model"]),
                    base_url=str(entry["base_url"]).rstrip("/"),
                    api_key_env=str(entry["api_key_env"]),
                    input_per_m=float(entry.get("input_per_m", 0.0)),
                    output_per_m=float(entry.get("output_per_m", 0.0)),
                    provider_routing=entry.get("provider_routing"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveModelCatalogError(f"bad live model entry {entry!r}: {exc}") from exc
    names = [m.name for m in models]
    if len(set(names)) != len(names):
        raise LiveModelCatalogError(f"duplicate live model names in {names}")
    return tuple(models)


def load_live_models(
    path: str | Path = DEFAULT_LIVE_MODELS_PATH, environ: Mapping[str, str] | None = None
) -> LiveModelCatalog:
    """The catalog at ``path``; an absent file is an empty catalog (the stack before 9.16)."""
    env = dict(os.environ) if environ is None else dict(environ)
    file = Path(path)
    if not file.is_file():
        return LiveModelCatalog(models=(), environ=env)
    return LiveModelCatalog(
        models=_parse(yaml.safe_load(file.read_text(encoding="utf-8"))), environ=env
    )

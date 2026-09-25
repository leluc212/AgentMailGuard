"""Model registry: name -> provider, driven by ``configs/models.yaml``.

The three models evaluated in the paper:

    qwen2.5-7b-instruct   -> Ollama   (local)
    llama-3.1-8b-instruct -> Ollama   (local)
    gpt-4o-mini           -> OpenAI API
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.llm.fake import FakeLLMProvider
from mailguard.llm.ollama_provider import OllamaProvider
from mailguard.llm.openai_provider import OpenAIProvider
from mailguard.llm.protocol import LLMProvider

logger = logging.getLogger(__name__)

DEFAULT_MODELS: dict[str, dict[str, Any]] = {
    "fake": {"backend": "fake"},
    "qwen2.5-7b-instruct": {"backend": "ollama", "tag": "qwen2.5:7b-instruct", "num_ctx": 8192},
    "llama-3.1-8b-instruct": {
        "backend": "ollama",
        "tag": "llama3.1:8b-instruct-q4_K_M",
        "num_ctx": 8192,
    },
    "gpt-4o-mini": {"backend": "openai", "model": "gpt-4o-mini", "json_mode": "json_object"},
}


class ModelRegistry:
    def __init__(
        self, settings: MailGuardSettings | None = None, models_path: Path | None = None
    ) -> None:
        self._settings = settings or get_settings()
        path = models_path or self._settings.resolve(self._settings.guard_models.models_path)
        self._models: dict[str, dict[str, Any]] = dict(DEFAULT_MODELS)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            for name, spec in (loaded.get("models") or {}).items():
                self._models[name] = dict(spec)
        self._cache: dict[str, LLMProvider] = {}

    def names(self) -> list[str]:
        return sorted(self._models)

    def spec(self, name: str) -> dict[str, Any]:
        if name not in self._models:
            raise KeyError(f"Unknown guard model '{name}'. Known: {self.names()}")
        return self._models[name]

    def get(self, name: str) -> LLMProvider:
        if name in self._cache:
            return self._cache[name]
        spec = self.spec(name)
        backend = spec.get("backend", "fake")
        provider: LLMProvider
        if backend == "fake":
            provider = FakeLLMProvider(model_name=f"fake:{name}")
        elif backend == "ollama":
            provider = OllamaProvider(
                model=str(spec.get("tag", name)),
                base_url=str(spec.get("base_url") or self._settings.ollama.base_url),
                timeout_s=float(spec.get("timeout_s") or self._settings.ollama.timeout_s),
                num_ctx=int(spec.get("num_ctx", 8192)),
                schema_format=bool(spec.get("schema_format", True)),
            )
        elif backend == "openai":
            provider = OpenAIProvider(
                model=str(spec.get("model", "gpt-4o-mini")),
                base_url=spec.get("base_url"),
                api_key=spec.get("api_key"),
                timeout_s=float(spec.get("timeout_s", 30.0)),
                json_mode=str(spec.get("json_mode", "json_object")),
            )
        else:
            raise ValueError(f"Unsupported backend '{backend}' for model '{name}'")
        self._cache[name] = provider
        return provider


def build_provider(name: str, settings: MailGuardSettings | None = None) -> LLMProvider:
    return ModelRegistry(settings).get(name)

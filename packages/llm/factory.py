"""Factory for instantiating LLMProvider implementations (R14.7, R24.5)."""

from __future__ import annotations

from typing import Any

import httpx

from packages.core.settings import LLMTiersSettings
from packages.llm.anthropic import DEFAULT_ANTHROPIC_MODELS, AnthropicLLMProvider
from packages.llm.client import (
    DEFAULT_LOCAL_MODELS,
    LocalLLMProvider,
    OpenAILLMProvider,
)
from packages.llm.fake import FakeLLMProvider
from packages.llm.protocol import LLMProvider, ModelTier


def create_llm_provider(
    settings: LLMTiersSettings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> LLMProvider:
    """Instantiate an LLMProvider based on configuration.

    Supports:
        - "fake": Deterministic offline stub (FakeLLMProvider) for testing/CI (R24.5)
        - "openai": Hosted OpenAI API (OpenAILLMProvider) (R14.7)
        - "anthropic": Hosted Anthropic Messages API (AnthropicLLMProvider) (R14.7)
        - "local": OpenAI-compatible local server like Ollama / vLLM (LocalLLMProvider) (R14.7)

    Args:
        settings: LLMTiersSettings configuration. If None, default settings are used (fake).
        client: Optional shared httpx.AsyncClient instance.
        **kwargs: Overrides or extra arguments passed to provider constructors.

    Returns:
        Configured LLMProvider instance.

    Raises:
        ValueError: If provider name is unsupported.
    """
    if settings is None:
        settings = LLMTiersSettings()

    provider_name = settings.provider.lower().strip()

    if provider_name == "fake":
        return FakeLLMProvider(**kwargs)

    if provider_name == "openai":
        model_map = kwargs.pop("model_map", None)
        if model_map is None:
            if settings.force_single_tier:
                model_map = dict.fromkeys(ModelTier, settings.strong_model)
            else:
                model_map = {
                    ModelTier.FAST: settings.fast_model,
                    ModelTier.ROUTINE: settings.fast_model,
                    ModelTier.STRONG: settings.strong_model,
                    ModelTier.HIGH_CAPABILITY: settings.strong_model,
                    ModelTier.FALLBACK: settings.fallback_model,
                }
        base_url = kwargs.pop("base_url", settings.openai_base_url)
        api_key = kwargs.pop("api_key", settings.openai_api_key)
        timeout_s = kwargs.pop("timeout_s", settings.timeout_s)
        provider_routing = kwargs.pop("provider_routing", settings.openai_provider_routing)
        response_metadata = kwargs.pop("response_metadata", settings.openai_response_metadata)
        return OpenAILLMProvider(
            base_url=base_url,
            api_key=api_key,
            model_map=model_map,
            timeout_s=timeout_s,
            client=client,
            provider_routing=provider_routing,
            response_metadata=response_metadata,
            **kwargs,
        )

    if provider_name == "anthropic":
        model_map = kwargs.pop("model_map", None)
        if model_map is None:
            if settings.force_single_tier:
                strong = (
                    settings.strong_model
                    if "claude" in settings.strong_model
                    else DEFAULT_ANTHROPIC_MODELS[ModelTier.STRONG]
                )
                model_map = dict.fromkeys(ModelTier, strong)
            elif "claude" in settings.fast_model:
                model_map = {
                    ModelTier.FAST: settings.fast_model,
                    ModelTier.ROUTINE: settings.fast_model,
                    ModelTier.STRONG: settings.strong_model,
                    ModelTier.HIGH_CAPABILITY: settings.strong_model,
                    ModelTier.FALLBACK: settings.fallback_model,
                }
            else:
                model_map = DEFAULT_ANTHROPIC_MODELS
        base_url = kwargs.pop("base_url", settings.anthropic_base_url)
        api_key = kwargs.pop("api_key", settings.anthropic_api_key)
        timeout_s = kwargs.pop("timeout_s", settings.timeout_s)
        return AnthropicLLMProvider(
            base_url=base_url,
            api_key=api_key,
            model_map=model_map,
            timeout_s=timeout_s,
            client=client,
            **kwargs,
        )

    if provider_name in ("local", "ollama", "vllm"):
        model_map = kwargs.pop("model_map", None)
        if model_map is None:
            model_map = DEFAULT_LOCAL_MODELS
        base_url = kwargs.pop("base_url", settings.local_base_url)
        api_key = kwargs.pop("api_key", settings.local_api_key)
        timeout_s = kwargs.pop("timeout_s", settings.timeout_s)
        return LocalLLMProvider(
            base_url=base_url,
            api_key=api_key,
            model_map=model_map,
            timeout_s=timeout_s,
            client=client,
            **kwargs,
        )

    raise ValueError(
        f"Unsupported LLM provider: {settings.provider!r}. "
        "Supported providers are 'fake', 'openai', 'anthropic', 'local'."
    )

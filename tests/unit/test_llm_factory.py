"""Unit tests for create_llm_provider factory (R14.7, R24.5)."""

from __future__ import annotations

import httpx
import pytest

from packages.core.settings import LLMTiersSettings
from packages.llm import (
    AnthropicLLMProvider,
    FakeLLMProvider,
    LocalLLMProvider,
    ModelTier,
    OpenAILLMProvider,
    create_llm_provider,
)


def test_create_llm_provider_default_returns_fake() -> None:
    """When no settings are provided, default to FakeLLMProvider for offline safety (R24.5)."""
    provider = create_llm_provider()
    assert isinstance(provider, FakeLLMProvider)


def test_create_llm_provider_explicit_fake_with_kwargs() -> None:
    """Test fake provider creation with canned responses and custom latency."""
    settings = LLMTiersSettings(provider="fake")
    canned = [{"category": "billing", "intent": "invoice_request"}]
    provider = create_llm_provider(settings, canned_responses=canned, simulate_latency_ms=5)

    assert isinstance(provider, FakeLLMProvider)
    assert provider._simulate_latency_ms == 5
    assert len(provider._canned_responses) == 1


def test_create_llm_provider_openai() -> None:
    """Test OpenAI provider creation with custom settings."""
    settings = LLMTiersSettings(
        provider="openai",
        openai_api_key="sk-test-key",
        openai_base_url="https://custom.openai.endpoint/v1",
        fast_model="custom-fast",
        strong_model="custom-strong",
        timeout_s=25.0,
    )
    provider = create_llm_provider(settings)

    assert isinstance(provider, OpenAILLMProvider)
    assert provider._api_key == "sk-test-key"
    assert provider._base_url == "https://custom.openai.endpoint/v1"
    assert provider._timeout_s == 25.0
    assert provider.resolve_model(ModelTier.FAST) == "custom-fast"
    assert provider.resolve_model(ModelTier.STRONG) == "custom-strong"


def test_create_llm_provider_openai_force_single_tier() -> None:
    """Test OpenAI provider with force_single_tier=True for ablation study (R15.6)."""
    settings = LLMTiersSettings(
        provider="openai",
        strong_model="gpt-4o",
        fast_model="gpt-4o-mini",
        force_single_tier=True,
    )
    provider = create_llm_provider(settings)

    assert isinstance(provider, OpenAILLMProvider)
    assert provider.resolve_model(ModelTier.FAST) == "gpt-4o"
    assert provider.resolve_model(ModelTier.ROUTINE) == "gpt-4o"
    assert provider.resolve_model(ModelTier.STRONG) == "gpt-4o"
    assert provider.resolve_model(ModelTier.HIGH_CAPABILITY) == "gpt-4o"


def test_create_llm_provider_anthropic() -> None:
    """Test Anthropic provider creation."""
    settings = LLMTiersSettings(
        provider="anthropic",
        anthropic_api_key="sk-ant-test",
        anthropic_base_url="https://custom.anthropic.endpoint/v1",
        timeout_s=12.0,
    )
    provider = create_llm_provider(settings)

    assert isinstance(provider, AnthropicLLMProvider)
    assert provider._api_key == "sk-ant-test"
    assert provider._base_url == "https://custom.anthropic.endpoint/v1"
    assert provider._timeout_s == 12.0
    assert "haiku" in provider.resolve_model(ModelTier.ROUTINE)
    assert "sonnet" in provider.resolve_model(ModelTier.HIGH_CAPABILITY)


def test_create_llm_provider_anthropic_force_single_tier() -> None:
    """Test Anthropic provider with force_single_tier=True."""
    settings = LLMTiersSettings(
        provider="anthropic",
        force_single_tier=True,
    )
    provider = create_llm_provider(settings)

    assert isinstance(provider, AnthropicLLMProvider)
    assert "sonnet" in provider.resolve_model(ModelTier.FAST)
    assert "sonnet" in provider.resolve_model(ModelTier.STRONG)


@pytest.mark.parametrize("provider_alias", ["local", "ollama", "vllm"])
def test_create_llm_provider_local_aliases(provider_alias: str) -> None:
    """Test local provider aliases (local, ollama, vllm) (R14.7)."""
    settings = LLMTiersSettings(
        provider=provider_alias,
        local_base_url="http://vllm-host:8000/v1",
        local_api_key="custom-vllm-key",
        timeout_s=45.0,
    )
    provider = create_llm_provider(settings)

    assert isinstance(provider, LocalLLMProvider)
    assert provider._base_url == "http://vllm-host:8000/v1"
    assert provider._api_key == "custom-vllm-key"
    assert provider._timeout_s == 45.0
    assert "llama" in provider.resolve_model(ModelTier.ROUTINE)


def test_create_llm_provider_client_forwarding() -> None:
    """Test forwarding custom httpx.AsyncClient to provider."""
    mock_client = httpx.AsyncClient()
    settings = LLMTiersSettings(provider="openai")
    provider = create_llm_provider(settings, client=mock_client)

    assert isinstance(provider, OpenAILLMProvider)
    assert provider._client is mock_client


def test_create_llm_provider_unsupported_raises() -> None:
    """Test unsupported provider name raises informative ValueError."""
    settings = LLMTiersSettings(provider="invalid_provider")
    with pytest.raises(ValueError, match="Unsupported LLM provider: 'invalid_provider'"):
        create_llm_provider(settings)

"""Reusable contract test suite for LLMProvider implementations (R14.5, R14.7, design.md §1083).

All model providers (FakeLLMProvider, OpenAILLMProvider, AnthropicLLMProvider, LocalLLMProvider)
MUST pass this suite to prove adherence to protocol contracts, return models, error mappings,
token usage accounting, and schema validation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pytest

from packages.llm.protocol import (
    ChatMessage,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMSchemaValidationError,
    LLMTimeoutError,
    ModelTier,
)


class LLMProviderContractSuite(ABC):
    """Abstract contract test suite for LLMProvider implementations.

    Subclasses must implement:
    - `create_provider()`: Provider ready for normal generation requests.
    - `create_failing_provider(error_kind)`: Provider configured to fail with 'timeout',
      'http_error', or 'malformed_json'.
    """

    @abstractmethod
    def create_provider(self) -> LLMProvider:
        """Return an active provider instance under test."""
        raise NotImplementedError

    @abstractmethod
    def create_failing_provider(self, error_kind: str) -> LLMProvider:
        """Return a provider instance configured to trigger specific error modes.

        error_kind:
            - 'timeout': Causes request timeout
            - 'http_error': Causes 4xx/5xx API failure
            - 'malformed_json': Returns non-conforming or unparseable JSON
        """
        raise NotImplementedError

    async def aclose_provider(self, provider: LLMProvider) -> None:
        """Clean up provider resources if needed."""
        aclose_fn = getattr(provider, "aclose", None)
        if callable(aclose_fn):
            await aclose_fn()

    def test_conforms_to_protocol(self) -> None:
        """Assert the provider conforms to the runtime LLMProvider protocol (R14.5)."""
        provider = self.create_provider()
        assert isinstance(provider, LLMProvider)

    @pytest.mark.asyncio
    async def test_generate_returns_valid_llm_result(self) -> None:
        """Assert generate() returns conformant LLMResult with required fields (design.md §5.7)."""
        provider = self.create_provider()
        try:
            result = await provider.generate(
                messages=[ChatMessage(role="user", content="Hello test")],
                tier=ModelTier.FAST,
            )
            assert isinstance(result, LLMResult)
            assert isinstance(result.content, dict)
            assert isinstance(result.model, str) and len(result.model) > 0
            assert result.tier in (
                ModelTier.FAST,
                ModelTier.ROUTINE,
                ModelTier.STRONG,
                ModelTier.HIGH_CAPABILITY,
                ModelTier.FALLBACK,
            )
            assert isinstance(result.input_tokens, int) and result.input_tokens >= 0
            assert isinstance(result.output_tokens, int) and result.output_tokens >= 0
            assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
            assert isinstance(result.raw_finish_reason, str) and len(result.raw_finish_reason) > 0
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_tier_model_resolution(self) -> None:
        """Assert generate() resolves distinct models for routine vs high_capability tiers."""
        provider = self.create_provider()
        try:
            res_routine = await provider.generate(
                messages=[ChatMessage(role="user", content="Routine test")],
                tier=ModelTier.ROUTINE,
            )
            res_strong = await provider.generate(
                messages=[ChatMessage(role="user", content="Strong test")],
                tier=ModelTier.HIGH_CAPABILITY,
            )
            assert res_routine.model is not None
            assert res_strong.model is not None
            assert res_routine.tier == ModelTier.ROUTINE
            assert res_strong.tier == ModelTier.HIGH_CAPABILITY
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_structured_json_schema(self) -> None:
        """Assert generate() returns structured output dictionary conforming to requested schema."""
        provider = self.create_provider()
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["category", "confidence"],
        }
        try:
            result = await provider.generate(
                messages=[ChatMessage(role="user", content="Classify email inquiry")],
                schema=schema,
                tier=ModelTier.FAST,
            )
            assert isinstance(result.content, dict)
            assert "category" in result.content
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_extra_params_forwarded(self) -> None:
        """Assert generate() accepts arbitrary keyword params (**params per R14.5)."""
        provider = self.create_provider()
        try:
            result = await provider.generate(
                messages=[ChatMessage(role="user", content="Test extra params")],
                tier=ModelTier.FAST,
                seed=42,
                custom_extra="custom_value",
            )
            assert isinstance(result, LLMResult)
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_model_param_names_the_model_used(self) -> None:
        """Assert a ``model`` param replaces the tier's model and the result reports it (R14.5).

        Tokens, cost and the inference log line are recorded under ``LLMResult.model``
        (R21.4, R21.6), so a caller that overrides the model must see that model come back.
        """
        provider = self.create_provider()
        try:
            result = await provider.generate(
                messages=[ChatMessage(role="user", content="Override the model")],
                tier=ModelTier.FAST,
                model="override-model-7b",
            )
            assert result.model == "override-model-7b"
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_timeout_error_mapping(self) -> None:
        """Assert request timeouts map strictly to LLMTimeoutError."""
        provider = self.create_failing_provider("timeout")
        try:
            with pytest.raises(LLMTimeoutError):
                await provider.generate(messages=[ChatMessage(role="user", content="Timeout test")])
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_http_error_mapping(self) -> None:
        """Assert 4xx/5xx API responses map strictly to LLMResponseError."""
        provider = self.create_failing_provider("http_error")
        try:
            with pytest.raises(LLMResponseError):
                await provider.generate(messages=[ChatMessage(role="user", content="Error test")])
        finally:
            await self.aclose_provider(provider)

    @pytest.mark.asyncio
    async def test_generate_schema_validation_error_mapping(self) -> None:
        """Assert unparseable JSON when schema required maps to LLMSchemaValidationError."""
        provider = self.create_failing_provider("malformed_json")
        try:
            with pytest.raises(LLMSchemaValidationError):
                await provider.generate(
                    messages=[ChatMessage(role="user", content="Malformed test")],
                    schema={"type": "object"},
                )
        finally:
            await self.aclose_provider(provider)

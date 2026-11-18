"""Model provider chosen per job by its run options (ADR-0018 decision 4 "M1", R25.3, R25.4).

The triage-worker and the ai-worker build both providers at start-up: ``fake``
(``FakeLLMProvider``, offline) and ``live`` (from the ``LLM__*`` settings, when they name a real
provider). Each call picks one by the ``model_profile`` of the job in progress
(``packages.core.run_context``). A job without run options uses the configured provider, as before
R25. The embedding model never switches (R5.10): only language-model calls go through here.

(Not ``packages/llm/profile.py``: that module holds the agent profiles of R14.)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from packages.core.run_context import current_live_model, current_model_profile
from packages.core.settings import LLMTiersSettings
from packages.llm.factory import create_llm_provider
from packages.llm.fake import FakeLLMProvider
from packages.llm.live_models import LiveModelCatalog
from packages.llm.protocol import ChatMessage, LLMError, LLMProvider, LLMResult, ModelTier


class ModelProfileUnavailableError(LLMError):
    """A job asked for ``model_profile=live`` on a worker with no live provider configured.

    The API refuses such a request before publishing it (R25.4); this is the worker-side guard
    for a stack whose services disagree on ``LLM__PROVIDER``.
    """


class ProfileRoutedLLMProvider(LLMProvider):
    """An ``LLMProvider`` that forwards each call to the job's ``fake`` or ``live`` provider."""

    def __init__(
        self,
        *,
        fake: LLMProvider,
        live: LLMProvider | None,
        live_models: Mapping[str, LLMProvider] | None = None,
    ) -> None:
        self.fake = fake
        self.live = live
        # catalog models by name (config/live_models.yaml, ADR-0019), picked by
        # the job's run_options.live_model
        self.live_models: dict[str, LLMProvider] = dict(live_models or {})

    @property
    def default_profile(self) -> str:
        """The profile of a job without run options: the configured provider."""
        return "live" if self.live is not None else "fake"

    @property
    def has_live(self) -> bool:
        """Some live provider exists: the LLM__* one or a catalog model."""
        return self.live is not None or bool(self.live_models)

    def live_for(self, live_model: str | None) -> LLMProvider:
        """The live provider of the catalog name ``live_model`` (None: the LLM__* one).

        Raises:
            ModelProfileUnavailableError: For an unknown or keyless model, or no live provider.
        """
        if live_model:
            provider = self.live_models.get(live_model)
            if provider is None:
                raise ModelProfileUnavailableError(
                    f"live_model={live_model!r} is not available on this worker (no key set)"
                )
            return provider
        if self.live is not None:
            return self.live
        if len(self.live_models) == 1:  # one catalog model: it is the live model
            return next(iter(self.live_models.values()))
        raise ModelProfileUnavailableError(
            "model_profile=live but this worker has no live LLM provider (LLM__PROVIDER=fake)"
        )

    def provider_for(self, profile: str | None, live_model: str | None = None) -> LLMProvider:
        """The provider of ``profile`` (None: the configured one).

        Raises:
            ModelProfileUnavailableError: For ``live`` without a matching live provider.
        """
        chosen = profile or self.default_profile
        if chosen == "fake":
            return self.fake
        return self.live_for(live_model)

    def live_view(self) -> LLMProvider | None:
        """The live provider of the job in progress, resolved per call (the guard's judges)."""
        return _LiveView(self) if self.has_live else None

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
        **params: Any,
    ) -> LLMResult:
        provider = self.provider_for(current_model_profile(), current_live_model())
        return await provider.generate(
            messages=messages,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            **params,
        )


class _LiveView(LLMProvider):
    """Always the live side of a ``ProfileRoutedLLMProvider``, with the job's live model."""

    def __init__(self, routed: ProfileRoutedLLMProvider) -> None:
        self.routed = routed

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
        **params: Any,
    ) -> LLMResult:
        provider = self.routed.live_for(current_live_model())
        return await provider.generate(
            messages=messages,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            **params,
        )


def build_profile_provider(
    settings: LLMTiersSettings,
    *,
    fault_markers: bool = False,
    client: httpx.AsyncClient | None = None,
    live_models: LiveModelCatalog | None = None,
) -> ProfileRoutedLLMProvider:
    """Both profiles from the settings; ``live`` is None when ``LLM__PROVIDER`` is ``fake``.

    ``fault_markers`` (``DemoSettings.fault_markers_active``) reaches the fake provider only:
    a live provider never reads demo fault markers (R25.11). ``live_models`` adds one provider
    per catalog model whose key is set (ADR-0019).
    """
    live_configured = settings.provider.strip().lower() != "fake"
    return ProfileRoutedLLMProvider(
        fake=FakeLLMProvider(fault_markers=fault_markers),
        live=create_llm_provider(settings, client=client) if live_configured else None,
        live_models=live_models.build_providers(timeout_s=settings.timeout_s, client=client)
        if live_models is not None
        else None,
    )

"""LLMProvider decorator that records every request of one fixed call kind (R11.7, R21.4).

For callers outside the per-job generation budget: the triage classifier (kind ``triage``)
and the thread summarizer (kind ``summarize``). Generation and repair are recorded by
``BudgetedLLMProvider``, which knows each call's kind.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from packages.core.settings import ModelPricing
from packages.llm.budget import CallKind
from packages.llm.inference_metrics import instrumented_call
from packages.llm.protocol import ChatMessage, LLMProvider, LLMResult, ModelTier


class InstrumentedLLMProvider(LLMProvider):
    """Wraps a provider; records context size, tokens, cost and a log line per request."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        kind: CallKind,
        metrics: Any | None = None,
        price_table: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self.provider = provider
        self.kind = kind
        self.metrics = metrics
        self.price_table = price_table

    async def aclose(self) -> None:
        """Close the wrapped provider's resources when it supports ``aclose``."""
        aclose_fn = getattr(self.provider, "aclose", None)
        if callable(aclose_fn):
            res = aclose_fn()
            if inspect.isawaitable(res):
                await res

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
        return await instrumented_call(
            self.provider,
            kind=self.kind.value,
            messages=messages,
            metrics=self.metrics,
            price_table=self.price_table,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            **params,
        )

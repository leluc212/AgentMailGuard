"""Per-inference cost estimation from the configured price table (R21.6, design.md §10).

Lives in ``packages.core`` so persistence (``packages.llm.drafts``) and metrics
(``packages.observability.metrics``) share one formula without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping

from packages.core.settings import ModelPricing

COST_DECIMAL_PLACES = 6
"""Scale of ``generated_draft.cost_estimate`` (``NUMERIC(10,6)``)."""

_TOKENS_PER_PRICE_UNIT = 1_000_000


def estimate_inference_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_table: Mapping[str, ModelPricing],
) -> float | None:
    """Return the USD cost of one inference, or ``None`` when the model has no price.

    Args:
        model: Concrete model identifier reported by the provider.
        input_tokens: Prompt tokens billed.
        output_tokens: Completion tokens billed.
        price_table: Configured ``{model: ModelPricing}`` table (``LLMTiersSettings.price_table``).

    Returns:
        Cost rounded to ``COST_DECIMAL_PLACES``, or ``None`` if ``model`` is not priced.
        An unknown price is an unknown cost, never a free call.

    Raises:
        ValueError: If either token count is negative.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(
            f"token counts must be non-negative, got input={input_tokens} output={output_tokens}"
        )
    pricing = price_table.get(model)
    if pricing is None:
        return None
    cost = (
        input_tokens * pricing.input_per_m + output_tokens * pricing.output_per_m
    ) / _TOKENS_PER_PRICE_UNIT
    return round(cost, COST_DECIMAL_PLACES)

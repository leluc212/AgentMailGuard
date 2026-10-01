"""Per-inference cost from the configured price table (R21.6, design.md §10)."""

from __future__ import annotations

import pytest

from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing
from packages.observability.metrics import create_pipeline_metrics, record_ai_cost

PRICES = {"model-a": ModelPricing(input_per_m=0.15, output_per_m=0.60)}


def test_cost_combines_input_and_output_prices() -> None:
    # 1,200 in * 0.15/M + 300 out * 0.60/M = 0.00018 + 0.00018
    assert estimate_inference_cost("model-a", 1_200, 300, PRICES) == pytest.approx(0.00036)


def test_cost_is_rounded_to_the_column_scale() -> None:
    # NUMERIC(10,6): 1 input token costs 0.00000015, which rounds to 0.0
    assert estimate_inference_cost("model-a", 1, 0, PRICES) == 0.0
    assert estimate_inference_cost("model-a", 7, 0, PRICES) == 0.000001


def test_zero_tokens_cost_nothing() -> None:
    assert estimate_inference_cost("model-a", 0, 0, PRICES) == 0.0


def test_unknown_model_has_unknown_cost() -> None:
    """A missing price is not a free call: None, never 0.0 (Review Focus 3)."""
    assert estimate_inference_cost("model-missing", 1_000, 1_000, PRICES) is None


def test_negative_token_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="token counts"):
        estimate_inference_cost("model-a", -1, 0, PRICES)


def test_metrics_cost_matches_estimate() -> None:
    metrics = create_pipeline_metrics()
    cost = record_ai_cost("model-a", "routine", 1_200, 300, PRICES, metrics=metrics)
    assert cost == estimate_inference_cost("model-a", 1_200, 300, PRICES)


def test_metrics_unknown_model_still_returns_zero() -> None:
    """record_ai_cost feeds a counter, which cannot take None; its contract is unchanged."""
    metrics = create_pipeline_metrics()
    assert record_ai_cost("model-missing", "routine", 10, 10, PRICES, metrics=metrics) == 0.0

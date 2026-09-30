"""What a run's meta records for the report: the prices and the benign-utility rule.

ADR-0012 decision 2(d): the report prices a run from the prices its own meta recorded when the
run started (the model profile's ``LLM__PRICE_TABLE``), not from the price table of whichever
machine builds the report. Decision 2(e): a new run counts a greeting-only draft as no benign
utility; its meta says so, so the report applies the strict rule to it (a v3 row applies it by
its schema). A meta without these keys is a v1 run: the report keeps its old behaviour and its
output does not change.

(docs/adr/0012-post-review-v2-done-fixes-and-main.md; specs/tasks.md 7.20; R22.12, R21.6)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.core.settings import ModelPricing

PRICES_KEY = "prices"
UTILITY_RULE_KEY = "benign_utility_rule"
UTILITY_RULE_MIN_DRAFT_CHARS = "min_draft_chars.v1"
"""Benign utility needs a draft of at least ``scoring.MIN_DRAFT_CHARS`` characters."""


def scoring_meta(price_table: Mapping[str, ModelPricing]) -> dict[str, Any]:
    """The keys a new run adds to its meta: its prices at run time and its utility rule."""
    return {
        PRICES_KEY: {
            model: {"input_per_m": price.input_per_m, "output_per_m": price.output_per_m}
            for model, price in price_table.items()
        },
        UTILITY_RULE_KEY: UTILITY_RULE_MIN_DRAFT_CHARS,
    }


def prices_from_meta(meta: Mapping[str, Any] | None) -> dict[str, ModelPricing] | None:
    """The prices a run's meta recorded; ``None`` when it recorded none (a v1 run)."""
    recorded = (meta or {}).get(PRICES_KEY)
    if not isinstance(recorded, Mapping):
        return None
    return {str(model): ModelPricing.model_validate(price) for model, price in recorded.items()}


def strict_utility_from_meta(meta: Mapping[str, Any] | None) -> bool:
    """True when the run's meta asks for the strict benign-utility rule."""
    return (meta or {}).get(UTILITY_RULE_KEY) == UTILITY_RULE_MIN_DRAFT_CHARS

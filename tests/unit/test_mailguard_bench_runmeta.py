"""What a new run records in its meta for the report: the prices and the utility rule.

ADR-0012 decisions 2(d) and 2(e). A run whose meta has neither keeps the report's old behaviour
(the price table of the machine that builds the report, the legacy benign-utility rule), so every
v1 run folder reports exactly as before. Pure: no guard, no model, no database.
"""

from __future__ import annotations

import json

from evaluation.mailguard_bench.model_profiles import get_profile, profile_env
from evaluation.mailguard_bench.runmeta import (
    UTILITY_RULE_MIN_DRAFT_CHARS,
    prices_from_meta,
    scoring_meta,
    strict_utility_from_meta,
)
from packages.core.settings import LLMTiersSettings, ModelPricing


def test_the_meta_records_the_prices_the_run_was_started_with() -> None:
    table = {"gpt-4o-mini": ModelPricing(input_per_m=0.15, output_per_m=0.6)}

    meta = scoring_meta(table)

    assert meta["prices"] == {"gpt-4o-mini": {"input_per_m": 0.15, "output_per_m": 0.6}}
    assert meta["benign_utility_rule"] == UTILITY_RULE_MIN_DRAFT_CHARS
    json.dumps(meta)  # it goes into a JSON file


def test_prices_read_back_from_a_meta_are_the_recorded_ones() -> None:
    table = {"gpt-4o-mini": ModelPricing(input_per_m=0.15, output_per_m=0.6)}

    assert prices_from_meta(scoring_meta(table)) == table


def test_a_meta_without_prices_has_none_so_the_caller_falls_back_to_its_own_table() -> None:
    assert prices_from_meta({"generation_model": "m"}) is None
    assert prices_from_meta(None) is None
    assert prices_from_meta({"prices": "not a table"}) is None


def test_an_empty_price_table_is_a_recording_of_no_prices_not_a_missing_one() -> None:
    assert prices_from_meta(scoring_meta({})) == {}


def test_the_recorded_price_table_is_the_model_profiles_at_run_time() -> None:
    profile = get_profile("gpt-4o-mini")
    env = profile_env(profile, {"BENCH_OPENAI_API_KEY": "k"})
    llm = LLMTiersSettings.model_validate({"price_table": json.loads(env["LLM__PRICE_TABLE"])})

    recorded = prices_from_meta(scoring_meta(llm.price_table))

    assert recorded == {
        profile.model: ModelPricing(
            input_per_m=profile.input_per_m, output_per_m=profile.output_per_m
        )
    }


def test_only_a_meta_with_the_rule_asks_for_the_strict_utility() -> None:
    assert strict_utility_from_meta(scoring_meta({})) is True
    assert strict_utility_from_meta({"generation_model": "m"}) is False
    assert strict_utility_from_meta(None) is False
    assert strict_utility_from_meta({"benign_utility_rule": "something.else"}) is False

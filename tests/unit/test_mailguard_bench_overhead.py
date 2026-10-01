"""Overhead aggregation of benchmark records (specs/tasks.md 7.19; R21.5, R21.6; SC4, SC5, SC9)."""

from __future__ import annotations

from typing import Any

import pytest

from evaluation.mailguard_bench.overhead import Percentiles, overhead
from evaluation.mailguard_bench.scoring import RawRecord
from packages.core.settings import ModelPricing

FREE = {"gemma-4-26b-a4b-it": ModelPricing(input_per_m=0.0, output_per_m=0.0)}
PAID = {"gemma-4-26b-a4b-it": ModelPricing(input_per_m=1.0, output_per_m=2.0)}


def rec(
    total: int,
    guard: int,
    gen: int,
    *,
    gen_calls: int = 1,
    status: str = "ok",
    model: str = "gemma-4-26b-a4b-it",
) -> RawRecord:
    data: dict[str, Any] = {
        "case_id": f"c{total}-{guard}-{gen}-{status}",
        "config": "C3",
        "status": status,
        "total_latency_ms": total,
        "guard_latency_ms": guard,
        "generation_latency_ms": gen,
        "generation": {
            "model": model,
            "calls": gen_calls,
            "input_tokens": 1000 * gen_calls,
            "output_tokens": 100 * gen_calls,
        },
        "guard_llm": {"model": model, "calls": 2, "input_tokens": 2000, "output_tokens": 0},
    }
    return RawRecord.from_dict(data)


def test_percentiles_interpolate_linearly() -> None:
    p = Percentiles.of([float(v) for v in range(1, 101)])
    assert p.p50 == pytest.approx(50.5)
    assert p.p95 == pytest.approx(95.05)
    assert p.p99 == pytest.approx(99.01)
    assert Percentiles.of([]) == Percentiles(0.0, 0.0, 0.0, 0)


def test_generation_latency_skips_inbound_blocks_and_errors_are_ignored() -> None:
    records = [rec(1000, 200, 800), rec(300, 300, 0, gen_calls=0), rec(9, 9, 9, status="error")]
    o = overhead("C3", records, FREE)
    assert o.n == 2
    assert o.generation_ms.n == 1 and o.generation_ms.p50 == 800.0
    assert o.guard_ms.p50 == pytest.approx(250.0)
    assert o.generation_calls_per_email == pytest.approx(0.5)
    assert o.guard_calls_per_email == pytest.approx(2.0)
    assert o.cost_per_email_usd == 0.0


def test_cost_uses_price_table_and_unpriced_is_unknown() -> None:
    o = overhead("C0", [rec(1000, 0, 1000)], PAID)
    # generation 1000*1 + 100*2 = 1200 ; guard 2000*1 = 2000 ; per 1M tokens
    assert o.cost_per_email_usd == pytest.approx(0.0032)
    unknown = overhead("C0", [rec(1000, 0, 1000, model="mystery-model")], PAID)
    assert unknown.cost_per_email_usd is None
    assert unknown.unpriced_models == ("mystery-model",)


def test_sc4_sc5_flags() -> None:
    fast = overhead("C3", [rec(2000, 500, 1500)] * 3, FREE)
    slow = overhead("C3", [rec(12000, 500, 11500)] * 3, FREE)
    assert fast.meets_sc4 and fast.meets_sc5
    assert not slow.meets_sc4 and not slow.meets_sc5

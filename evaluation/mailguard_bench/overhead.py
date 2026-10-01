"""Overhead per email: latency percentiles, tokens, model calls and cost (spec §4b).

Latency is split into guard layers and the one generation call. Cost uses rag-email's
configured price table (``LLM__PRICE_TABLE``) through ``estimate_inference_cost``; an
unpriced model makes the cost unknown, never free (R21.6, SC9).

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4b; specs/tasks.md
7.19; R21.5, R21.6, R22.12; SC4, SC5, SC9)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from evaluation.mailguard_bench.scoring import RawRecord, TokenUse
from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing

SC4_TYPICAL_MS = 6000.0
SC5_P95_MS = 10000.0


@dataclass(frozen=True)
class Percentiles:
    """p50 / p95 / p99 in milliseconds (linear interpolation); zeros when empty."""

    p50: float
    p95: float
    p99: float
    n: int

    @classmethod
    def of(cls, values: Sequence[float]) -> Percentiles:
        """Percentiles of ``values``."""
        if not values:
            return cls(0.0, 0.0, 0.0, 0)
        p50, p95, p99 = np.percentile(np.asarray(values, dtype=float), [50, 95, 99])
        return cls(float(p50), float(p95), float(p99), len(values))


@dataclass(frozen=True)
class Overhead:
    """Per-email overhead of one configuration over its ``ok`` records."""

    config: str
    n: int
    total_ms: Percentiles
    guard_ms: Percentiles
    generation_ms: Percentiles
    generation_calls_per_email: float
    guard_calls_per_email: float
    generation_tokens_per_email: float
    guard_tokens_per_email: float
    cost_per_email_usd: float | None
    unpriced_models: tuple[str, ...]

    @property
    def meets_sc4(self) -> bool:
        """Typical (p50) end-to-end latency within SC4's 6 s."""
        return self.total_ms.n > 0 and self.total_ms.p50 <= SC4_TYPICAL_MS

    @property
    def meets_sc5(self) -> bool:
        """p95 end-to-end latency within SC5's 10 s."""
        return self.total_ms.n > 0 and self.total_ms.p95 <= SC5_P95_MS


def _cost(use: TokenUse, prices: Mapping[str, ModelPricing]) -> float | None:
    if use.calls == 0 and use.input_tokens == 0 and use.output_tokens == 0:
        return 0.0
    return estimate_inference_cost(use.model, use.input_tokens, use.output_tokens, prices)


def overhead(
    config: str, records: Sequence[RawRecord], prices: Mapping[str, ModelPricing]
) -> Overhead:
    """Aggregate the overhead of ``records`` (error records are skipped).

    ``generation_ms`` covers only cases where the generation call ran (an inbound
    block skips it); ``total_ms`` and ``guard_ms`` cover every scored case.
    """
    ok = [r for r in records if r.ok]
    n = len(ok)
    unpriced: set[str] = set()
    total_cost = 0.0
    for record in ok:
        for use in (record.generation, record.guard_llm):
            cost = _cost(use, prices)
            if cost is None:
                unpriced.add(use.model or "<unnamed>")
            else:
                total_cost += cost

    def per_email(value: float) -> float:
        return value / n if n else 0.0

    return Overhead(
        config=config,
        n=n,
        total_ms=Percentiles.of([float(r.total_latency_ms) for r in ok]),
        guard_ms=Percentiles.of([float(r.guard_latency_ms) for r in ok]),
        generation_ms=Percentiles.of(
            [float(r.generation_latency_ms) for r in ok if r.generation.calls > 0]
        ),
        generation_calls_per_email=per_email(sum(r.generation.calls for r in ok)),
        guard_calls_per_email=per_email(sum(r.guard_llm.calls for r in ok)),
        generation_tokens_per_email=per_email(
            sum(r.generation.input_tokens + r.generation.output_tokens for r in ok)
        ),
        guard_tokens_per_email=per_email(
            sum(r.guard_llm.input_tokens + r.guard_llm.output_tokens for r in ok)
        ),
        cost_per_email_usd=(None if unpriced else per_email(total_cost)),
        unpriced_models=tuple(sorted(unpriced)),
    )

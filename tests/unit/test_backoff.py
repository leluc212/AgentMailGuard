"""Unit tests for exponential backoff and jitter algorithms (R19.5, R3.4)."""

import random

import pytest

from packages.broker.backoff import calculate_exponential_backoff, resolve_retry_tier_delay
from packages.core.settings import RetryLadderSettings


def test_deterministic_exponential_backoff_progression() -> None:
    """Verify deterministic exponential growth when jitter_mode='none'."""
    base = 2.0
    factor = 3.0
    max_s = 100.0

    # attempt 1: 2.0 * 3^0 = 2.0
    assert (
        calculate_exponential_backoff(
            1, base_s=base, factor=factor, max_s=max_s, jitter_mode="none"
        )
        == 2.0
    )
    # attempt 2: 2.0 * 3^1 = 6.0
    assert (
        calculate_exponential_backoff(
            2, base_s=base, factor=factor, max_s=max_s, jitter_mode="none"
        )
        == 6.0
    )
    # attempt 3: 2.0 * 3^2 = 18.0
    assert (
        calculate_exponential_backoff(
            3, base_s=base, factor=factor, max_s=max_s, jitter_mode="none"
        )
        == 18.0
    )
    # attempt 4: 2.0 * 3^3 = 54.0
    assert (
        calculate_exponential_backoff(
            4, base_s=base, factor=factor, max_s=max_s, jitter_mode="none"
        )
        == 54.0
    )
    # attempt 5: 2.0 * 3^4 = 162.0 -> capped at max_s (100.0)
    assert (
        calculate_exponential_backoff(
            5, base_s=base, factor=factor, max_s=max_s, jitter_mode="none"
        )
        == 100.0
    )


def test_full_jitter_bounds() -> None:
    """Verify full jitter stays within [0, raw_delay]."""
    rng = random.Random(42)
    base = 10.0
    factor = 2.0
    max_s = 100.0

    for attempt in range(1, 6):
        raw = min(max_s, base * (factor ** (attempt - 1)))
        for _ in range(20):
            val = calculate_exponential_backoff(
                attempt, base_s=base, factor=factor, max_s=max_s, jitter_mode="full", rng=rng
            )
            assert 0.0 <= val <= raw


def test_equal_jitter_bounds() -> None:
    """Verify equal jitter stays within [raw_delay/2, raw_delay]."""
    rng = random.Random(42)
    base = 10.0
    factor = 2.0
    max_s = 100.0

    for attempt in range(1, 5):
        raw = min(max_s, base * (factor ** (attempt - 1)))
        half = raw / 2.0
        for _ in range(20):
            val = calculate_exponential_backoff(
                attempt, base_s=base, factor=factor, max_s=max_s, jitter_mode="equal", rng=rng
            )
            assert half <= val <= raw


def test_decorrelated_jitter_bounds() -> None:
    """Verify decorrelated jitter stays within [base_s, max_s]."""
    rng = random.Random(42)
    base = 5.0
    factor = 2.0
    max_s = 60.0

    for attempt in range(1, 5):
        for _ in range(20):
            val = calculate_exponential_backoff(
                attempt,
                base_s=base,
                factor=factor,
                max_s=max_s,
                jitter_mode="decorrelated",
                rng=rng,
            )
            assert base <= val <= max_s


def test_invalid_jitter_mode_raises() -> None:
    """Unknown jitter mode raises ValueError."""
    with pytest.raises(ValueError, match="Unknown jitter_mode"):
        calculate_exponential_backoff(1, jitter_mode="invalid")  # type: ignore[arg-type]


def test_resolve_retry_tier_delay() -> None:
    """Verify mapping of attempts to discrete queue tiers."""
    settings = RetryLadderSettings(tier_1_delay_s=30, tier_2_delay_s=300, tier_3_delay_s=1800)

    assert resolve_retry_tier_delay(0, settings) == 30
    assert resolve_retry_tier_delay(1, settings) == 30
    assert resolve_retry_tier_delay(2, settings) == 300
    assert resolve_retry_tier_delay(3, settings) == 1800
    assert resolve_retry_tier_delay(4, settings) == 1800

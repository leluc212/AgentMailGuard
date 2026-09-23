"""Exponential backoff and jitter calculation algorithms.

Requirements:
- R19.5: Exponential backoff with jitter on transient failures.
- R3.4, R7.2: Discrete queue retry intervals (30s, 5m, 30m).
"""

from __future__ import annotations

import random
from typing import Literal

from packages.core.settings import RetryLadderSettings

JitterMode = Literal["full", "equal", "decorrelated", "none"]


def calculate_exponential_backoff(
    attempt: int,
    base_s: float = 1.0,
    factor: float = 2.0,
    max_s: float = 1800.0,
    jitter_mode: JitterMode = "full",
    rng: random.Random | None = None,
) -> float:
    """Calculate exponential backoff duration in seconds with optional jitter (R19.5).

    Parameters
    ----------
    attempt : int
        Current retry attempt (1-indexed; if <= 0, normalized to 1).
    base_s : float
        Base backoff delay in seconds.
    factor : float
        Multiplication factor per attempt.
    max_s : float
        Upper ceiling cap in seconds.
    jitter_mode : JitterMode
        Jitter strategy:
        - 'none': deterministic exponential backoff (min(max_s, base_s * factor^(attempt - 1))).
        - 'full': uniform random in [0, raw_delay].
        - 'equal': half deterministic, half uniform random [raw_delay/2, raw_delay].
        - 'decorrelated': uniform random between base_s and min(max_s, raw_delay * 3).
    rng : random.Random | None
        Optional seeded random instance for deterministic testing.

    Returns
    -------
    float
        Calculated sleep/backoff interval in seconds.
    """
    _rng = rng if rng is not None else random

    eff_attempt = max(1, attempt)
    # Exponential progression: attempt 1 -> base_s * factor^0 = base_s
    raw_delay = min(max_s, base_s * (factor ** (eff_attempt - 1)))

    if jitter_mode == "none":
        return max(0.0, raw_delay)
    elif jitter_mode == "full":
        return max(0.0, _rng.uniform(0.0, raw_delay))
    elif jitter_mode == "equal":
        half = raw_delay / 2.0
        return max(0.0, half + _rng.uniform(0.0, half))
    elif jitter_mode == "decorrelated":
        return max(0.0, min(max_s, _rng.uniform(base_s, raw_delay * 3.0)))
    else:
        raise ValueError(f"Unknown jitter_mode '{jitter_mode}'")


def resolve_retry_tier_delay(
    attempt: int,
    settings: RetryLadderSettings | None = None,
) -> int:
    """Map retry attempt count to discrete RabbitMQ retry queue delay tier (R3.4, R7.2).

    Parameters
    ----------
    attempt : int
        Retry attempt count (1-indexed).
    settings : RetryLadderSettings | None
        Configured ladder thresholds.

    Returns
    -------
    int
        Delay in seconds matching a declared RabbitMQ retry queue (30s, 300s, or 1800s).
    """
    cfg = settings or RetryLadderSettings()
    if attempt <= 1:
        return cfg.tier_1_delay_s
    elif attempt == 2:
        return cfg.tier_2_delay_s
    else:
        return cfg.tier_3_delay_s

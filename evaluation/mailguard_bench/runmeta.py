"""What a run's meta records for the report: the prices and the benign-utility rule.

ADR-0012 decision 2(d): the report prices a run from the prices its own meta recorded when the
run started (the model profile's ``LLM__PRICE_TABLE``), not from the price table of whichever
machine builds the report. Decision 2(e): a new run counts a greeting-only draft as no benign
utility; its meta says so, so the report applies the strict rule to it (a v3 row applies it by
its schema). A meta without these keys, or without ``scheme: v3``, is a pre-v3 run: the v3
reader refuses it
(``require_v3_meta``) and only the legacy reader (``legacy_reader.py``) reads its folder.

(docs/adr/0012-post-review-v2-done-fixes-and-main.md; specs/tasks.md 7.20; R22.12, R21.6)
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.scheme import SCHEME_V3, scheme_of_meta
from evaluation.mailguard_bench.scoring import LEGACY_READER_HINT, LegacyReportError
from packages.core.settings import ModelPricing

PRICES_KEY = "prices"
UTILITY_RULE_KEY = "benign_utility_rule"
GUARDED_PROMPT_KEY = "guarded_prompt_version"
UTILITY_RULE_MIN_DRAFT_CHARS = "min_draft_chars.v1"
"""Benign utility needs a draft of at least ``scoring.MIN_DRAFT_CHARS`` characters."""


def scoring_meta(price_table: Mapping[str, ModelPricing]) -> dict[str, Any]:
    """The keys a new run adds to its meta: its prices at run time, its utility rule and prompt.

    ``guarded_prompt_version`` is the prompt of the guarded configs (``guarded_reply``); every
    config of a run records it, so two prompt versions are never mixed in one report.
    """
    from evaluation.mailguard_bench.guarded_reply import GUARDED_PROMPT_VERSION

    return {
        GUARDED_PROMPT_KEY: GUARDED_PROMPT_VERSION,
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


def guarded_prompt_version_from_meta(meta: Mapping[str, Any] | None) -> str | None:
    """The guarded prompt version a run's meta recorded, ``None`` when it recorded none."""
    recorded = (meta or {}).get(GUARDED_PROMPT_KEY)
    return None if recorded is None else str(recorded)


def require_v3_meta(meta: Mapping[str, Any] | None, *, name: str = "run meta") -> None:
    """Refuse a pre-v3 run meta (Q1=b): a meta without ``scheme: v3`` is v1 or v2.

    Raises:
        LegacyReportError: Naming the legacy reader.
    """
    if meta is None:
        return
    scheme = scheme_of_meta(meta)
    if scheme != SCHEME_V3:
        raise LegacyReportError(
            f"{name} records scheme {scheme}: the v3 reader refuses pre-v3 runs "
            f"({LEGACY_READER_HINT})"
        )


def keep_recorded_scoring_meta(meta: Mapping[str, Any], meta_file: Path) -> dict[str, Any]:
    """``meta`` for a resumed run: the scoring keys are the ones its first invocation recorded.

    The runners rewrite the meta file on every invocation, and the scoring keys sit outside the
    resume fingerprint. Without this a run started before the keys existed would flip to the strict
    utility rule half-way, and a run resumed on another machine would take that machine's prices.
    When ``meta_file`` does not exist yet this is the run's first invocation and ``meta`` stands.
    """
    kept = dict(meta)
    if not meta_file.exists():
        return kept
    existing = json.loads(meta_file.read_text(encoding="utf-8"))
    for key in (PRICES_KEY, UTILITY_RULE_KEY, GUARDED_PROMPT_KEY):
        if key in existing:
            kept[key] = existing[key]
        else:
            kept.pop(key, None)
    return kept

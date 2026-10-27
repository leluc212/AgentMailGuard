"""Property tests for the Guard Playground core (design.md "Correctness Properties").

Property 1 (Replay round-trip), Property 7 (Xóa khóa) and Property 8 (Lọc case).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from services.guard_playground.core.cases import ALL, CASE_KINDS, Case, CaseCatalog, filter_cases
from services.guard_playground.core.redact import MIN_SECRET_LENGTH, REDACTED, redact
from services.guard_playground.core.schema import ACTIONS, LAYER_ORDER, PRESETS, RunResult

PROPERTY_SETTINGS = settings(max_examples=100, deadline=None)

# --------------------------------------------------------------------------- strategies

_text = st.text(max_size=40)  # full unicode (hypothesis excludes surrogates by default)
_opt_text = st.none() | _text
_float = st.floats(allow_nan=False, allow_infinity=False)
_opt_float = st.none() | _float
_source = st.sampled_from(("pinned", "fixture", "custom", "external"))
_timezones = st.sampled_from(
    (UTC, timezone(timedelta(hours=7)), timezone(timedelta(hours=-5, minutes=-30)))
)
_generated_at = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=_timezones
)

_finding = st.fixed_dictionaries(
    {
        "detector": _text,
        "rule_id": _opt_text,
        "score": _float,
        "technique": _opt_text,
        "excerpt": _text,
    }
)


def _layer_row(name: str) -> st.SearchStrategy[dict[str, Any]]:
    return st.fixed_dictionaries(
        {
            "layer": st.just(name),
            "ran": st.booleans(),
            "not_run_reason": st.sampled_from((None, "disabled by preset", "stopped by policy")),
            "severity": _text,
            "score": _float,
            "decided_by": _text,
            "latency_ms": st.integers(min_value=0, max_value=10**9),
            "findings": st.lists(_finding, max_size=3),
            "error": _opt_text,
            "fallback_reason": _opt_text,
            "judge_configured": st.booleans(),
        }
    )


_decision = st.none() | st.fixed_dictionaries(
    {
        "action": st.sampled_from(ACTIONS),
        "matched_rule_id": _text,
        "risk_tier": _text,
        "requires_human": st.booleans(),
    }
)

_run_result = st.fixed_dictionaries(
    {
        "generated_at": _generated_at,
        "mode": st.sampled_from(("live", "replay")),
        "source": _source,
        "case_id": _opt_text,
        "preset": st.sampled_from(PRESETS),
        "draft_model": _text,
        "guard_models": st.dictionaries(_text, _text, max_size=3),
        "email": st.fixed_dictionaries(
            {
                "subject": _text,
                "body": _text,
                "chunks": st.lists(
                    st.fixed_dictionaries({"chunk_id": _text, "content": _text}), max_size=3
                ),
                "source": _source,
                "case_id": _opt_text,
            }
        ),
        "layers": st.tuples(*(_layer_row(name) for name in LAYER_ORDER)).map(list),
        "user_intent": _opt_text,
        "stripped": st.lists(
            st.fixed_dictionaries({"text": _text, "reason": _text, "score": _opt_float}),
            max_size=3,
        ),
        "removed_ratio": _float,
        "chunks": st.lists(
            st.fixed_dictionaries(
                {
                    "chunk_id": _text,
                    "content": _text,
                    "score": _opt_float,
                    "quarantined": st.booleans(),
                    "reason_rule_id": _opt_text,
                    "reason_technique": _opt_text,
                }
            ),
            max_size=3,
        ),
        "quarantine_ratio": _float,
        "inbound": _decision,
        "outbound": _decision,
        "gate_passes": st.none() | st.integers(min_value=0, max_value=100),
        "isolation_applied": st.booleans(),
        "draft_generated": st.booleans(),
        "draft_body": _opt_text,
        "guarded_prompt": _opt_text,
        "thresholds": st.fixed_dictionaries(
            {"block": _opt_float, "flag": _opt_float, "strip": _opt_float, "quarantine": _opt_float}
        ),
        "error": _opt_text,
    }
).map(RunResult.model_validate)


# --------------------------------------------------------------------------- Property 1


# Feature: guard-playground, Property 1: Replay round-trip
@PROPERTY_SETTINGS
@given(result=_run_result)
def test_property_1_replay_round_trip(result: RunResult) -> None:
    """**Validates: Requirements 7.11**"""
    restored = RunResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert result.final == (result.outbound or result.inbound)
    assert restored.final == result.final


# --------------------------------------------------------------------------- Property 7

# Keys exclude "[" and "]": an occurrence of a key in the output could then only straddle an
# inserted "[redacted]" marker, which needs a bracket inside the key. Also excludes NUL and
# surrogates, which an environment variable value cannot hold.
_key_alphabet = st.characters(blacklist_categories=("Cs",), blacklist_characters=("\x00", "[", "]"))
# The one bracket-free key of length >= 8 that fits inside the marker is "redacted" itself;
# a secret equal to the marker word cannot be removed by any marker-based redaction.
_keys = st.lists(
    st.text(alphabet=_key_alphabet, min_size=MIN_SECRET_LENGTH, max_size=24).filter(
        lambda key: key not in REDACTED
    ),
    min_size=1,
    max_size=5,
    unique=True,
)


@st.composite
def _keys_and_text(draw: st.DrawFn) -> tuple[list[str], str]:
    keys = draw(_keys)
    pieces = draw(st.lists(st.tuples(st.text(max_size=20), st.sampled_from(keys)), max_size=6))
    tail = draw(st.text(max_size=20))
    return keys, "".join(prefix + key for prefix, key in pieces) + tail


# Feature: guard-playground, Property 7: Xóa khóa
@PROPERTY_SETTINGS
@given(data=_keys_and_text())
def test_property_7_redact_removes_every_key(data: tuple[list[str], str]) -> None:
    """**Validates: Requirements 8.3, 8.4, 11.5**"""
    keys, text = data
    env = {f"HYP_{index}_KEY": key for index, key in enumerate(keys)}
    with patch.dict(os.environ, env):
        redacted = redact(text)
    for key in keys:
        assert key not in redacted
    if not any(key in text for key in keys):
        return
    assert REDACTED in redacted


# --------------------------------------------------------------------------- Property 8

_SOURCES = ("src-a", "src-b", "src-c", "fixture", "unknown")
_UNSEEN_SOURCE = "never-generated-source"

_case = st.builds(
    Case,
    case_id=st.text(min_size=1, max_size=10),
    kind=st.sampled_from(CASE_KINDS),
    source=st.sampled_from(_SOURCES),
    origin=st.sampled_from(("pinned", "fixture")),
    subject=st.text(max_size=10),
    body=st.text(min_size=1, max_size=10),
)


@st.composite
def _catalog_and_filter(draw: st.DrawFn) -> tuple[CaseCatalog, str, str]:
    cases = draw(st.lists(_case, max_size=20))
    kind = draw(st.sampled_from((ALL, *CASE_KINDS)))
    seen = sorted({case.source for case in cases})
    source = draw(st.sampled_from((ALL, _UNSEEN_SOURCE, *seen)))
    return CaseCatalog(cases=cases), kind, source


# Feature: guard-playground, Property 8: Lọc case
@PROPERTY_SETTINGS
@given(data=_catalog_and_filter())
def test_property_8_filter_cases_matches_both_filters(data: tuple[CaseCatalog, str, str]) -> None:
    """**Validates: Requirements 1.2, 1.6**"""
    catalog, kind, source = data
    result = filter_cases(catalog, kind=kind, source=source)

    def matches(case: Case) -> bool:
        return (kind == ALL or case.kind == kind) and (source == ALL or case.source == source)

    # Every returned case matches both filters, and result is the catalog order of matches.
    assert all(matches(case) for case in result)
    remaining = iter(catalog.cases)
    assert all(any(case is other for other in remaining) for case in result)
    assert len(result) == sum(1 for case in catalog.cases if matches(case))
    if kind == ALL and source == ALL:
        assert result == catalog.cases
    if source == _UNSEEN_SOURCE:
        assert result == []

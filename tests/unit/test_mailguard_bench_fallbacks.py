"""How a failed AI step of a guard layer is classified (ADR-0012 decisions 2(b) and 4; R22.12).

A guard that marks a failed AI step (``metadata["llm_fallback"]``) keeps the row scored and the
failure is counted per layer and reason; a real layer crash (the verdict's ``error``) and a
guard that only writes ``llm_error`` (the v1 guard) stay error rows. Pure: no guard install.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from evaluation.mailguard_bench.guarded_reply import classify_ai_step_failures
from evaluation.mailguard_bench.scoring import AiStepFallback

L1 = "l1_injection_scanner"
L2 = "l2_intent_extractor"
L3B = "l3b_document_scanner"


def verdict(layer: str, *, error: str | None = None, **metadata: Any) -> SimpleNamespace:
    return SimpleNamespace(layer=layer, error=error, metadata=metadata)


def marked(reason: str, text: str = "boom") -> dict[str, Any]:
    return {"llm_fallback": True, "llm_fallback_reason": reason, "llm_error": text}


def test_a_marked_fallback_is_recorded_and_is_not_an_error() -> None:
    split = classify_ai_step_failures([verdict(L2, **marked("non_json", "prose"))])

    assert split.fallbacks == (AiStepFallback(layer=L2, reason="non_json", error="prose"),)
    assert split.errors == ()


def test_llm_error_without_the_mark_keeps_the_v1_classification_as_an_error() -> None:
    split = classify_ai_step_failures([verdict(L1, llm_error="LLMTimeoutError: timed out")])

    assert split.fallbacks == ()
    assert split.errors == (f"{L1}: llm_error: LLMTimeoutError: timed out",)


def test_a_layer_crash_is_never_a_fallback_even_when_it_also_has_a_mark() -> None:
    crashed = verdict(L1, error="RuntimeError: boom", **marked("error"))

    split = classify_ai_step_failures([crashed])

    assert split.errors == (f"{L1}: RuntimeError: boom",)
    assert split.fallbacks == ()


def test_a_verdict_without_metadata_or_with_no_failure_records_nothing() -> None:
    split = classify_ai_step_failures(
        [SimpleNamespace(layer=L1, error=None, metadata=None), verdict(L2, llm_used=True)]
    )

    assert split.fallbacks == () and split.errors == ()


def test_a_mark_without_a_reason_reads_as_error() -> None:
    split = classify_ai_step_failures([verdict(L3B, llm_fallback=True)])

    assert split.fallbacks == (AiStepFallback(layer=L3B, reason="error", error=""),)


def test_every_l3b_chunk_that_fell_back_is_its_own_entry() -> None:
    split = classify_ai_step_failures(
        [verdict(L3B, **marked("timeout")), verdict(L3B, **marked("timeout"))]
    )

    assert [f.layer for f in split.fallbacks] == [L3B, L3B]


@pytest.mark.parametrize(
    ("reason", "is_schema_fallback"),
    [
        ("non_json", True),
        ("schema_missing", True),
        ("invalid_fields", False),
        ("timeout", False),
        ("error", False),
    ],
)
def test_l2_schema_fallback_means_the_answer_did_not_carry_the_schema(
    reason: str, is_schema_fallback: bool
) -> None:
    split = classify_ai_step_failures([verdict(L2, **marked(reason))])

    assert split.l2_schema_fallback is is_schema_fallback


def test_another_layers_non_json_answer_is_not_an_l2_schema_fallback() -> None:
    split = classify_ai_step_failures([verdict(L1, **marked("non_json"))])

    assert split.l2_schema_fallback is False


def test_a_fallback_serialises_to_the_audit_shape() -> None:
    assert AiStepFallback(layer=L2, reason="timeout", error="t").to_dict() == {
        "layer": L2,
        "reason": "timeout",
        "error": "t",
    }

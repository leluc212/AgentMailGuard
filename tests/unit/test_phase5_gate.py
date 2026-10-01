"""scripts/phase5_gate.py checks what the Phase 5 gate claims (5.6; R13.3, R13.5, R16.1)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from packages.core.settings import AppSettings, EmbeddingSettings, LLMTiersSettings
from packages.db.fixtures import CUST_ALICE_ID

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
DOC_A = "0b3f6f0e-0000-4000-8000-00000000000a"
DOC_B = "0b3f6f0e-0000-4000-8000-00000000000b"
GEMINI_KEY = "gemini-secret-key-for-test"


def _load_gate() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the script imports its sibling stack_smoke
    spec = importlib.util.spec_from_file_location("phase5_gate", SCRIPTS / "phase5_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _gemini(*, mock_embedder: bool) -> AppSettings:
    return AppSettings(
        _env_file=None,
        llm=LLMTiersSettings(
            provider="openai",
            openai_api_key=GEMINI_KEY,
            openai_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            fast_model="gemma-4-26b-a4b-it",
            strong_model="gemma-4-31b-it",
            fallback_model="gemini-3.1-flash-lite",
        ),
        embedding=EmbeddingSettings(mock=mock_embedder),
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "retrieved_chunks_count": 1,
        "business_data_degraded": False,
        "customer_status": "FOUND",
        "business_fact_statuses": [
            {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
        ],
    }
    payload.update(overrides)
    return payload


def test_the_fixture_pair_is_alices_order_and_email() -> None:
    assert gate.ORDER["order_number"] == "ORD-82915"
    assert gate.ORDER["customer_id"] == CUST_ALICE_ID
    assert gate.EMAIL.sender_email == gate.ALICE["email"]
    assert "82915" in gate.EMAIL.body_text
    # R13.3: the procedure the draft cites cannot supply the status.
    assert str(gate.ORDER["status"]).lower() not in gate.PROCEDURE.content.lower()


def test_check_settings_rejects_the_fake_model() -> None:
    with pytest.raises(gate.SmokeFailure, match="real model"):
        gate.check_settings(AppSettings(_env_file=None))


def test_check_settings_rejects_a_live_embedder() -> None:
    with pytest.raises(gate.SmokeFailure, match="EMBEDDING__MOCK"):
        gate.check_settings(_gemini(mock_embedder=False))


def test_check_settings_accepts_gemini_and_never_prints_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate.check_settings(_gemini(mock_embedder=True))
    out = capsys.readouterr().out
    assert "gemma-4-26b-a4b-it" in out
    assert GEMINI_KEY not in out


def test_lane_categories_follow_the_ai_worker_lanes() -> None:
    categories = gate.lane_categories(AppSettings(_env_file=None))
    assert {"support", "billing", "sales", "general_inquiry"} <= set(categories)
    assert len(categories) == len(set(categories))


def test_check_context_accepts_a_found_order() -> None:
    gate.check_context(_payload(), "ORD-82915")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"business_data_degraded": True}, "degraded"),
        ({"customer_status": None}, "customer"),
        ({"customer_status": "UNKNOWN_SENDER", "business_fact_statuses": []}, "customer"),
        (
            {"business_fact_statuses": [{"reference": "ORD-82915", "status": "NOT_FOUND"}]},
            "ORD-82915",
        ),
        ({"retrieved_chunks_count": 0}, "retrieved"),
    ],
)
def test_check_context_rejects(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(gate.SmokeFailure, match=message):
        gate.check_context(_payload(**overrides), "ORD-82915")


def test_check_draft_accepts_the_status_and_a_procedure_citation() -> None:
    citations = json.dumps([{"citation_id": "c1", "chunk_id": "x", "document_id": DOC_A}])
    hit = gate.check_draft(
        "Your order ORD-82915 has Shipped.", citations, False, "shipped", {DOC_A, DOC_B}
    )
    assert hit == DOC_A


def test_check_draft_accepts_the_spaced_form_of_an_underscored_status() -> None:
    citations = [{"document_id": DOC_B}]
    hit = gate.check_draft(
        "It is out for delivery today.", citations, False, "out_for_delivery", {DOC_B}
    )
    assert hit == DOC_B


@pytest.mark.parametrize(
    ("body", "citations", "mismatch", "message"),
    [
        ("We are looking into it.", [{"document_id": DOC_A}], False, "seeded status"),
        ("It was unshipped.", [{"document_id": DOC_A}], False, "seeded status"),
        ("It has shipped.", [{"document_id": "another-doc"}], False, "procedure"),
        ("It has shipped.", [], False, "procedure"),
        ("It has shipped.", [{"document_id": DOC_A}], True, "mismatch"),
    ],
)
def test_check_draft_rejects(body: str, citations: Any, mismatch: bool, message: str) -> None:
    with pytest.raises(gate.SmokeFailure, match=message):
        gate.check_draft(body, citations, mismatch, "shipped", {DOC_A})

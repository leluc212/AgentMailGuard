"""Tests of the AgentMailGuard wiring that need the guard installed (task 7.19; ADR-0010).

Run with `make mailguard-test` (uv overlay of the pinned worktree). In CI, where mailguard is
not installed, the whole module is skipped. No network: the Gemini model is only registered,
never called, and the offline checks use the guard's built-in "fake" backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mailguard")

from evaluation.mailguard_bench import guard_smoke  # noqa: E402
from evaluation.mailguard_bench.guard_env import (  # noqa: E402
    DEFAULT_GUARD_MODEL,
    GUARD_MODELS_YAML,
    GuardEnvError,
)
from evaluation.mailguard_bench.guard_factory import (  # noqa: E402
    build_guard_pipeline,
    guard_settings,
    live_layers,
    require_live,
)

SAMPLE_EMAIL = {
    "message_id": "t-1",
    "sender_email": "customer@example.invalid",
    "subject": "Order question",
    "body_text": "Where is my order ORD-1?",
}


@pytest.fixture
def gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")


def test_guard_settings_point_every_stage_at_one_model(tmp_path: Path) -> None:
    settings = guard_settings(DEFAULT_GUARD_MODEL, l1_model_path=tmp_path / "clf.joblib")
    gm = settings.guard_models
    assert {gm.judge, gm.extractor, gm.doc_scanner, gm.output_judge} == {DEFAULT_GUARD_MODEL}
    assert Path(gm.models_path) == GUARD_MODELS_YAML.resolve()
    assert settings.l1.ml_model_path == str((tmp_path / "clf.joblib").resolve())
    assert settings.l3b.llm_enabled is False
    assert settings.l4.llm_enabled is False


def test_gemma_resolves_to_the_openai_backend_without_a_call(
    gemini_env: None, tmp_path: Path
) -> None:
    pipeline = build_guard_pipeline(
        "C3", guard_settings(DEFAULT_GUARD_MODEL, l1_model_path=tmp_path / "clf.joblib")
    )
    layers = live_layers(pipeline)
    expected = f"OpenAIProvider:{DEFAULT_GUARD_MODEL}"
    assert layers.preset == "C3"
    assert layers.active_layers == ("l1", "l2", "l3", "l3b", "l4", "l5")
    assert (layers.l1_judge, layers.l2_llm) == (expected, expected)
    assert (layers.l3b_llm, layers.l4_llm) == (None, None)
    assert pipeline.l1.judge._base_url == "https://example.invalid/v1"


def test_missing_classifier_fails_require_live(gemini_env: None, tmp_path: Path) -> None:
    pipeline = build_guard_pipeline(
        "C3", guard_settings(DEFAULT_GUARD_MODEL, l1_model_path=tmp_path / "absent.joblib")
    )
    layers = live_layers(pipeline)
    assert layers.l1_classifier is False
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(layers, model_name=DEFAULT_GUARD_MODEL)


def test_unknown_model_name_is_caught_not_silently_degraded(tmp_path: Path) -> None:
    pipeline = build_guard_pipeline(
        "C3", guard_settings("gemma-typo", l1_model_path=tmp_path / "clf.joblib")
    )
    layers = live_layers(pipeline)
    assert layers.l1_judge is None
    with pytest.raises(GuardEnvError, match="l1_judge is None"):
        require_live(layers, model_name="gemma-typo")


async def test_c0_runs_no_layer_and_c3_reaches_a_policy_decision(tmp_path: Path) -> None:
    clf = tmp_path / "clf.joblib"
    c0 = build_guard_pipeline("C0", guard_settings("fake", l1_model_path=clf))
    r0 = await c0.inspect_inbound(SAMPLE_EMAIL, category="support")
    assert r0.l1 is None and r0.inbound_decision is None
    c3 = build_guard_pipeline("C3", guard_settings("fake", l1_model_path=clf))
    r3 = await c3.inspect_inbound(SAMPLE_EMAIL, category="support")
    assert r3.l1 is not None and r3.inbound_decision is not None


def test_smoke_without_make_env_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("MAILGUARD_DIR", "MAILGUARD_COMMIT", "MAILGUARD_ARTIFACTS"):
        monkeypatch.delenv(name, raising=False)
    assert guard_smoke.main([]) == 1
    assert "FAIL MAILGUARD_DIR" in capsys.readouterr().err

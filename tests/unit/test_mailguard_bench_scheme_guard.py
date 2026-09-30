"""Guard wiring of the v2 config scheme, on fake providers (task 7.20; ADR-0012 decision 11).

Needs the AgentMailGuard worktree installed (`uv run --with-editable`); skipped in CI.
No network and no model call: the guard's model is the registry's built-in "fake" backend.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("mailguard")

from evaluation.mailguard_bench import scheme  # noqa: E402
from evaluation.mailguard_bench.guard_build import (  # noqa: E402
    GuardBuild,
    build_guard,
    live_guard_llm_stages,
)
from evaluation.mailguard_bench.guard_env import GuardEnvError  # noqa: E402
from evaluation.mailguard_bench.guard_factory import live_layers, require_live  # noqa: E402

V2_GUARDED = tuple(c for c in scheme.V2_CONFIGS if c != "C0")
MODEL = "gpt-4o-mini"


def _build(config: str, tmp_path: Path, *, scheme_name: str = "v2", llm: bool = True) -> GuardBuild:
    l3b, l4 = scheme.v2_llm_stages(config) if llm else (False, False)
    return build_guard(
        config,
        model_name="fake",
        audit_log_path=tmp_path / f"{config}.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
        l3b_llm=l3b,
        l4_llm=l4,
        scheme=scheme_name,
    )


@pytest.mark.parametrize("config", V2_GUARDED)
def test_a_v2_config_is_built_from_explicit_layer_flags(config: str, tmp_path: Path) -> None:
    guard = _build(config, tmp_path)
    flags = guard.pipeline.config
    for layer in scheme.FULL_LAYERS:
        assert getattr(flags, layer) is (layer in scheme.V2_LAYERS[config]), (config, layer)
    assert tuple(flags.active_layers) == scheme.V2_LAYERS[config]
    assert flags.name == scheme.v2_guard_name(config)
    described = guard.describe()
    assert described["scheme"] == "v2"
    assert described["preset"] == scheme.v2_guard_name(config)
    assert described["config"] == config
    assert described["active_layers"] == list(scheme.V2_LAYERS[config])


def test_no_v2_config_uses_a_preset_of_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailguard.pipeline import GuardConfig

    def _forbidden(name: str) -> GuardConfig:
        raise AssertionError(f"v2 asked the guard for preset {name}")

    monkeypatch.setattr(GuardConfig, "preset", staticmethod(_forbidden))
    for config in V2_GUARDED:
        _build(config, tmp_path)


def test_a_v1_build_is_the_guards_own_preset_as_before(tmp_path: Path) -> None:
    for config, layers in (
        ("C0T", ()),
        ("C1", ("l1", "l5")),
        ("C2", ("l1", "l2", "l3", "l5")),
        ("C3", scheme.FULL_LAYERS),
        ("C3-L4", ("l1", "l2", "l3", "l3b", "l5")),
    ):
        guard = build_guard(
            config,
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
        )
        assert tuple(guard.pipeline.config.active_layers) == layers, config
        assert guard.describe()["scheme"] == "v1"
    with pytest.raises(ValueError, match="C4"):
        build_guard(
            "C4",
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
        )


def test_the_ablation_configs_exist_in_v1_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scheme v2"):
        build_guard(
            "C3-L1",
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
            scheme="v2",
        )
    with pytest.raises(ValueError, match="native"):
        build_guard(
            "C0",
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
            scheme="v2",
        )


def test_the_live_llm_stage_rule_of_each_scheme(tmp_path: Path) -> None:
    # v1 (unchanged): only the full guard C3 turns L3b's and L4's LLM stages on.
    assert live_guard_llm_stages("C3") == (True, True)
    assert live_guard_llm_stages("C3", "v1") == (True, True)
    assert live_guard_llm_stages("C2", "v1") == (False, False)
    # v2: a config runs exactly the AI stages the owner listed.
    assert {c: live_guard_llm_stages(c, "v2") for c in V2_GUARDED} == {
        c: scheme.v2_llm_stages(c) for c in V2_GUARDED
    }
    assert live_guard_llm_stages("C3", "v2") == (False, False)  # C3 is channel isolation in v2
    assert live_guard_llm_stages("C4", "v2") == (True, False)
    assert live_guard_llm_stages("C7", "v2") == (True, True)


def _made_live(guard: GuardBuild) -> None:
    """The classifier counts as loaded (none is trained here); every model is the counted one."""
    pipeline = guard.pipeline
    pipeline.l1.classifier.pipeline = guard.guard_llm  # any non-None: classifier "available"


@pytest.mark.parametrize("config", V2_GUARDED)
def test_a_fully_live_v2_config_has_no_missing_stage(config: str, tmp_path: Path) -> None:
    guard = _build(config, tmp_path)
    _made_live(guard)
    assert guard.missing_live_stages() == []


@pytest.mark.parametrize("config", V2_GUARDED)
def test_the_live_stage_check_expects_exactly_the_configs_stages(
    config: str, tmp_path: Path
) -> None:
    expected = scheme.v2_required_stages(config)
    for stage in ("l1.classifier", "l1.judge", "l2.llm", "l3b.llm", "l4.llm"):
        guard = _build(config, tmp_path)
        _made_live(guard)
        pipeline = guard.pipeline
        if stage == "l1.classifier":
            pipeline.l1.classifier.pipeline = None
        elif stage == "l1.judge":
            pipeline.l1.judge = None
        else:
            getattr(pipeline, stage.split(".")[0]).llm = None
        assert guard.missing_live_stages() == ([stage] if stage in expected else []), (
            config,
            stage,
        )


@pytest.mark.parametrize("config", V2_GUARDED)
def test_a_v2_stage_that_is_not_on_is_missing_even_if_the_layer_is_active(
    config: str, tmp_path: Path
) -> None:
    # A guard built without the L3b/L4 LLM stage that the config lists must not pass as live.
    guard = _build(config, tmp_path, llm=False)
    _made_live(guard)
    want = [s for s in scheme.v2_required_stages(config) if s in {"l3b.llm", "l4.llm"}]
    assert guard.missing_live_stages() == want


@pytest.mark.parametrize("config", V2_GUARDED)
def test_require_live_accepts_a_v2_guard_with_the_configs_flags(
    config: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    from mailguard.pipeline import GuardConfig

    from evaluation.mailguard_bench.guard_factory import build_guard_pipeline, guard_settings

    l3b, l4 = scheme.v2_llm_stages(config)
    settings = guard_settings(MODEL, l1_model_path=tmp_path / "clf.joblib", l3b_llm=l3b, l4_llm=l4)
    pipeline = build_guard_pipeline("C0", settings)
    pipeline.config = GuardConfig(
        scheme.v2_guard_name(config),
        **{layer: layer in scheme.V2_LAYERS[config] for layer in scheme.FULL_LAYERS},
    )
    layers = dataclasses.replace(live_layers(pipeline), l1_classifier=True)
    require_live(layers, model_name=MODEL, l3b_llm=l3b, l4_llm=l4)
    if scheme.V2_AI_STAGES[config]:
        stage = scheme.V2_AI_STAGES[config][0]
        field = {
            "l1.judge": "l1_judge",
            "l2.llm": "l2_llm",
            "l3b.llm": "l3b_llm",
            "l4.llm": "l4_llm",
        }[stage]
        broken = dataclasses.replace(layers, **{field: None})  # type: ignore[arg-type]
        with pytest.raises(GuardEnvError, match=f"{field} is None"):
            require_live(broken, model_name=MODEL, l3b_llm=l3b, l4_llm=l4)

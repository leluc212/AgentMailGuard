"""Guard wiring of the v3 config scheme, on fake providers (plan_v3 Task 12; ADR-0015).

Needs the AgentMailGuard worktree installed (`uv run --with-editable`); skipped in CI.
No network and no model call: the guard's model is the registry's built-in "fake" backend.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("mailguard")

from mailguard.pipeline import GuardConfig  # noqa: E402

from evaluation.mailguard_bench import scheme  # noqa: E402
from evaluation.mailguard_bench.guard_build import (  # noqa: E402
    GuardBuild,
    build_guard,
    live_guard_llm_stages,
)
from evaluation.mailguard_bench.guard_env import (  # noqa: E402
    V1_MAILGUARD_COMMIT,
    V2_MAILGUARD_COMMIT,
    GuardEnvError,
)
from evaluation.mailguard_bench.guard_factory import (  # noqa: E402
    active_switches,
    build_guard_pipeline,
    guard_settings,
    live_layers,
    require_live,
)

V3_GUARDED = tuple(c for c in scheme.V3_CONFIGS if c != "C0")
MODEL = "gpt-4o-mini"
STAGE_FIELD = {
    "inbound_judge": "inbound_judge",
    "inbound_extractor": "inbound_extractor",
    "doc_judge": "doc_judge",
    "doc_set_judge": "doc_set_judge",
    "output_judge": "output_judge",
}


def _build(config: str, tmp_path: Path, *, llm: bool = True, **kwargs: object) -> GuardBuild:
    doc, output = scheme.v3_llm_stages(config) if llm else (False, False)
    return build_guard(
        config,
        model_name="fake",
        audit_log_path=tmp_path / f"{config}.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
        doc_llm=doc,
        output_llm=output,
        **kwargs,  # type: ignore[arg-type]
    )


def _off(guard: GuardBuild, stage: str) -> None:
    """Switch one v3 stage of ``guard`` off, as a missing model or artifact would."""
    pipeline = guard.pipeline
    if stage == "classifier":
        pipeline.inbound_guard.classifier.pipeline = None
    elif stage == "inbound_judge":
        pipeline.inbound_guard.judge = None
    elif stage == "inbound_extractor":
        pipeline.inbound_guard.extractor_llm = None
    elif stage == "doc_judge":
        pipeline.doc_scanner.llm = None
    elif stage == "doc_set_judge":
        pipeline.doc_scanner.set_judge = None
    elif stage == "output_judge":
        pipeline.output_guard.llm = None
    else:
        raise AssertionError(stage)


def _made_live(guard: GuardBuild) -> None:
    """The classifier counts as loaded (none is trained here)."""
    guard.pipeline.inbound_guard.classifier.pipeline = guard.guard_llm  # any non-None


@pytest.mark.parametrize("config", V3_GUARDED)
def test_a_v3_config_is_the_guards_own_preset(config: str, tmp_path: Path) -> None:
    guard = _build(config, tmp_path)
    flags = guard.pipeline.config
    preset = scheme.v3_guard_preset(config)
    assert dataclasses.replace(flags, name=preset) == GuardConfig.preset(preset)
    assert flags.name == config and guard.preset == preset
    assert active_switches(flags) == scheme.V3_LAYERS[config]
    described = guard.describe()
    assert described["scheme"] == "v3"
    assert (described["config"], described["preset"]) == (config, preset)
    assert described["active_layers"] == list(scheme.V3_LAYERS[config])
    assert described["judge_fail_closed"] is True
    assert described["live_layers"]["preset"] == config


def test_the_scheme_table_is_the_guards_preset_table() -> None:
    for config in V3_GUARDED:
        preset = GuardConfig.preset(scheme.v3_guard_preset(config))
        assert active_switches(preset) == scheme.V3_LAYERS[config], config
    assert dict(GuardConfig.ABLATIONS) == scheme.V3_ABLATIONS
    assert tuple(GuardConfig.LAYERS) + ("channel_isolation",) == scheme.V3_SWITCHES


def test_retired_guard_commits_are_the_pinned_ones() -> None:
    assert scheme.RETIRED_SCHEME_GUARDS == {"v1": V1_MAILGUARD_COMMIT, "v2": V2_MAILGUARD_COMMIT}


@pytest.mark.parametrize("retired", ["v1", "v2"])
def test_a_retired_scheme_is_never_built(retired: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=scheme.RETIRED_SCHEME_GUARDS[retired]):
        build_guard(
            "C3",
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
            scheme=retired,
        )
    with pytest.raises(ValueError, match="checkout of that commit"):
        live_guard_llm_stages("C3", retired)


def test_v2_and_v1_names_do_not_exist_in_v3(tmp_path: Path) -> None:
    for name in ("C4", "C7", "C3-L3B", "C3-L5"):
        with pytest.raises(ValueError, match="scheme v3"):
            build_guard(
                name,
                model_name="fake",
                audit_log_path=tmp_path / "a.jsonl",
                l1_model_path=tmp_path / "clf.joblib",
            )
    with pytest.raises(ValueError, match="native"):
        build_guard(
            "C0",
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
        )


def test_the_live_judge_rule_of_v3() -> None:
    assert {c: live_guard_llm_stages(c) for c in V3_GUARDED} == {
        c: scheme.v3_llm_stages(c) for c in V3_GUARDED
    }
    assert live_guard_llm_stages("C0T") == (False, False)
    assert live_guard_llm_stages("C1") == (False, False)  # the Inbound Guard's judges are built
    assert live_guard_llm_stages("C2") == (True, False)
    assert live_guard_llm_stages("C3") == (True, True)
    assert live_guard_llm_stages("C3-L2") == (False, True)
    assert live_guard_llm_stages("C3-L4") == (True, False)


@pytest.mark.parametrize("config", V3_GUARDED)
def test_every_judge_of_a_layer_the_config_runs_is_wired(config: str, tmp_path: Path) -> None:
    guard = _build(config, tmp_path)
    _made_live(guard)
    live = guard.live_stages()
    for judge in scheme.V3_AI_STAGES[config]:
        assert live[judge], (config, judge)
    pipeline = guard.pipeline
    doc, output = scheme.v3_llm_stages(config)
    assert (pipeline.doc_scanner.llm is guard.guard_llm) is doc
    assert (pipeline.doc_scanner.set_judge is not None) is doc
    assert (pipeline.output_guard.llm is guard.guard_llm) is output
    assert guard.missing_live_stages() == []


@pytest.mark.parametrize("config", V3_GUARDED)
def test_the_live_stage_check_expects_exactly_the_configs_stages(
    config: str, tmp_path: Path
) -> None:
    expected = scheme.v3_required_stages(config)
    for stage in scheme.V3_STAGES:
        guard = _build(config, tmp_path)
        _made_live(guard)
        _off(guard, stage)
        assert guard.missing_live_stages() == ([stage] if stage in expected else []), (
            config,
            stage,
        )


@pytest.mark.parametrize("config", V3_GUARDED)
def test_a_v3_judge_that_is_not_on_is_missing(config: str, tmp_path: Path) -> None:
    # A guard built without the opt-in judges the config lists must not pass as live.
    guard = _build(config, tmp_path, llm=False)
    _made_live(guard)
    opt_in = {"doc_judge", "doc_set_judge", "output_judge"}
    assert guard.missing_live_stages() == [
        s for s in scheme.v3_required_stages(config) if s in opt_in
    ]


def test_the_judge_fallback_mode_is_the_v2_fallback(tmp_path: Path) -> None:
    closed = _build("C3", tmp_path)
    fallback = _build("C3", tmp_path, judge_fail_closed=False)
    assert closed.pipeline.settings.mailguard.fail_closed_on_judge_error is True
    assert fallback.pipeline.settings.mailguard.fail_closed_on_judge_error is False
    assert (closed.describe()["judge_fail_closed"], fallback.describe()["judge_fail_closed"]) == (
        True,
        False,
    )


@pytest.mark.parametrize("config", V3_GUARDED)
def test_require_live_accepts_a_v3_guard_with_the_configs_judges(
    config: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    doc, output = scheme.v3_llm_stages(config)
    settings = guard_settings(
        MODEL, l1_model_path=tmp_path / "clf.joblib", doc_llm=doc, output_llm=output
    )
    pipeline = build_guard_pipeline(scheme.v3_guard_preset(config), settings)
    layers = dataclasses.replace(live_layers(pipeline), classifier=True)
    require_live(layers, model_name=MODEL, doc_llm=doc, output_llm=output)
    for judge in scheme.V3_AI_STAGES[config]:
        broken = dataclasses.replace(layers, **{STAGE_FIELD[judge]: None})  # type: ignore[arg-type]
        with pytest.raises(GuardEnvError, match=f"{STAGE_FIELD[judge]} is None"):
            require_live(broken, model_name=MODEL, doc_llm=doc, output_llm=output)

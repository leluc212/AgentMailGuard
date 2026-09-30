"""Guard wiring of the layer-ablation presets on fake providers (task 7.22; R22.12).

Needs the AgentMailGuard worktree installed (`uv run --with-editable`); skipped in CI.
No network: the guard's model is the registry's built-in "fake" backend or a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mailguard")

from evaluation.mailguard_bench.guard_build import ABLATION_CONFIGS, build_guard  # noqa: E402
from evaluation.mailguard_bench.guard_env import GuardEnvError  # noqa: E402
from evaluation.mailguard_bench.guard_factory import LiveLayers, require_live  # noqa: E402
from evaluation.mailguard_bench.guarded_reply import GuardedCaseExecutor  # noqa: E402
from evaluation.mailguard_bench.runner import prepared_case_executor  # noqa: E402
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator  # noqa: E402
from tests.unit.test_mailguard_bench_guarded_reply import (  # noqa: E402
    BENIGN,
    INJECTION,
    _executor,
    _prepared,
)

FULL = ("l1", "l2", "l3", "l3b", "l4", "l5")
REMOVED = {
    "C3-L1": "l1",
    "C3-L2": "l2",
    "C3-L3": "l3",
    "C3-L3B": "l3b",
    "C3-L4": "l4",
    "C3-L5": "l5",
}
MODEL = "gpt-4o-mini"
LIVE = f"OpenAIProvider:{MODEL}"


@pytest.mark.parametrize("config", ABLATION_CONFIGS)
def test_each_config_builds_the_guards_own_preset_of_the_same_name(
    config: str, tmp_path: Path
) -> None:
    guard = build_guard(
        config,
        model_name="fake",
        audit_log_path=tmp_path / "audit.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
    )
    assert (guard.config, guard.preset) == (config, config)
    assert guard.pipeline.config.name == config
    assert tuple(guard.pipeline.config.active_layers) == tuple(
        layer for layer in FULL if layer != REMOVED[config]
    )
    assert guard.describe()["preset"] == config
    assert guard.describe()["live_layers"]["preset"] == config


def test_an_unknown_ablation_preset_still_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preset"):
        build_guard(
            "C3-L6",
            model_name="fake",
            audit_log_path=tmp_path / "a.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
        )


# --- GuardBuild.missing_live_stages expects exactly the stages the preset runs


def _stripped(config: str, tmp_path: Path, *, off: tuple[str, ...]) -> list[str]:
    """Missing stages of ``config`` once the named guard parts are switched off."""
    guard = build_guard(
        config,
        model_name="fake",
        audit_log_path=tmp_path / f"{config}.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
    )
    pipeline = guard.pipeline
    pipeline.settings.l3b.llm_enabled = True  # so an absent L3b/L4 model would count
    pipeline.settings.l4.llm_enabled = True
    pipeline.l3b.llm = pipeline.l4.llm = guard.guard_llm
    pipeline.l1.classifier.pipeline = guard.guard_llm  # any non-None: classifier "available"
    for part in off:
        if part == "l1.classifier":
            pipeline.l1.classifier.pipeline = None
        elif part == "l1.judge":
            pipeline.l1.judge = None
        else:
            layer, _, _ = part.partition(".")
            getattr(pipeline, layer).llm = None
    return guard.missing_live_stages()


@pytest.mark.parametrize(
    ("config", "stage", "expected"),
    [
        ("C3", "l1.judge", ["l1.judge"]),
        ("C3-L1", "l1.judge", []),  # no L1, so no L1 judge
        ("C3-L2", "l1.judge", ["l1.judge"]),
        ("C3", "l2.llm", ["l2.llm"]),
        ("C3-L2", "l2.llm", []),  # no L2 LLM step
        ("C3-L1", "l2.llm", ["l2.llm"]),
        ("C3", "l3b.llm", ["l3b.llm"]),
        ("C3-L3B", "l3b.llm", []),
        ("C3-L4", "l3b.llm", ["l3b.llm"]),
        ("C3", "l4.llm", ["l4.llm"]),
        ("C3-L4", "l4.llm", []),
        ("C3-L3B", "l4.llm", ["l4.llm"]),
        ("C3-L3", "l4.llm", ["l4.llm"]),  # L3 and L5 have no LLM stage of their own
        ("C3-L5", "l2.llm", ["l2.llm"]),
    ],
)
def test_only_the_llm_stages_the_preset_runs_are_required_live(
    config: str, stage: str, expected: list[str], tmp_path: Path
) -> None:
    assert _stripped(config, tmp_path, off=(stage,)) == expected


@pytest.mark.parametrize("config", ABLATION_CONFIGS)
def test_a_fully_live_ablation_preset_has_no_missing_stage(config: str, tmp_path: Path) -> None:
    assert _stripped(config, tmp_path, off=()) == []


@pytest.mark.parametrize("config", ABLATION_CONFIGS)
def test_a_missing_classifier_fails_wherever_a_layer_uses_it(config: str, tmp_path: Path) -> None:
    # L1 uses the trained classifier; so do L2 and L3b (the pipeline hands them L1's). Every
    # ablation preset keeps at least one of them, so a skipped `make mailguard-prep` must
    # never yield a silently weaker guard.
    assert _stripped(config, tmp_path, off=("l1.classifier",)) == ["l1.classifier"]


# --- require_live, the guard-smoke check, follows the preset too


def _layers(preset: str, **over: object) -> LiveLayers:
    active = tuple(layer for layer in FULL if layer != REMOVED.get(preset, ""))
    base: dict[str, object] = {
        "preset": preset,
        "active_layers": active,
        "l1_classifier": True,
        "l1_judge": LIVE,
        "l2_llm": LIVE,
        "l3b_llm": None,
        "l4_llm": None,
    }
    return LiveLayers(**{**base, **over})  # type: ignore[arg-type]


def test_require_live_still_demands_everything_for_the_full_guard() -> None:
    require_live(_layers("C3"), model_name=MODEL)
    for field in ("l1_judge", "l2_llm"):
        with pytest.raises(GuardEnvError, match=f"{field} is None"):
            require_live(_layers("C3", **{field: None}), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(_layers("C3", l1_classifier=False), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="l3b_llm is None"):
        require_live(_layers("C3"), model_name=MODEL, l3b_llm=True)
    with pytest.raises(GuardEnvError, match="l4_llm is .*expected off"):
        require_live(_layers("C3", l4_llm=LIVE), model_name=MODEL)


def test_require_live_without_l1_does_not_ask_for_an_l1_judge() -> None:
    require_live(_layers("C3-L1", l1_judge=None), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="l2_llm is None"):
        require_live(_layers("C3-L1", l1_judge=None, l2_llm=None), model_name=MODEL)


def test_require_live_without_l2_does_not_ask_for_an_l2_llm_step() -> None:
    require_live(_layers("C3-L2", l2_llm=None), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="l1_judge is None"):
        require_live(_layers("C3-L2", l1_judge=None, l2_llm=None), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(_layers("C3-L2", l2_llm=None, l1_classifier=False), model_name=MODEL)


def test_require_live_without_l3b_or_l4_ignores_their_wanted_flags() -> None:
    require_live(_layers("C3-L3B"), model_name=MODEL, l3b_llm=True)
    require_live(_layers("C3-L4"), model_name=MODEL, l4_llm=True)
    with pytest.raises(GuardEnvError, match="l4_llm is None"):
        require_live(_layers("C3"), model_name=MODEL, l4_llm=True)


def test_require_live_without_l1_still_needs_the_classifier_for_l2_and_l3b() -> None:
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(_layers("C3-L1", l1_judge=None, l1_classifier=False), model_name=MODEL)


# --- each preset runs through the real executor on stub models


@pytest.mark.parametrize("config", ABLATION_CONFIGS)
async def test_every_ablation_preset_makes_one_generation_call_for_a_benign_email(
    config: str, tmp_path: Path
) -> None:
    executor, fake, _ = _executor(config, tmp_path)

    execution = await executor.execute(_prepared(BENIGN))

    assert len(fake.recorded_calls) == 1
    assert execution.guard_errors == ()
    assert execution.record["blocked"] is False
    assert execution.record["final_draft"] is not None


@pytest.mark.parametrize("config", ABLATION_CONFIGS)
async def test_the_removed_layer_never_speaks_in_the_report(config: str, tmp_path: Path) -> None:
    executor, _, _ = _executor(config, tmp_path)

    execution = await executor.execute(_prepared(INJECTION))

    report = execution.record["report"]
    removed = REMOVED[config]
    if removed in ("l1", "l2", "l3", "l4"):
        assert report[removed] is None
    if removed == "l3b":
        assert report["l3b"] == []
    if removed == "l5":
        assert report["decision"] is None and execution.record["blocked"] is False
    assert execution.guard_errors == ()


async def test_without_l1_the_injection_is_still_seen_by_the_other_layers(
    tmp_path: Path,
) -> None:
    executor, _, _ = _executor("C3-L1", tmp_path)

    execution = await executor.execute(_prepared(INJECTION))

    assert "l1_injection_scanner" not in execution.record["detected_layers"]
    assert execution.record["report"]["l1"] is None
    assert execution.record["report"]["l2"] is not None


def test_ablation_configs_need_a_guard_like_c3(tmp_path: Path) -> None:
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response={}),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    for config in ABLATION_CONFIGS:
        with pytest.raises(ValueError, match="needs a guard"):
            prepared_case_executor(config, generator=generator, guard=None)
    guard = build_guard(
        "C3-L5",
        model_name="fake",
        audit_log_path=tmp_path / "a.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
    )
    executor = prepared_case_executor("C3-L5", generator=generator, guard=guard)
    assert isinstance(executor, GuardedCaseExecutor)
    assert executor.pipeline.config.name == "C3-L5"

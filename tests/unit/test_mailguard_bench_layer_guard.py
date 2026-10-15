"""Guard wiring of the v3 ablation configs on fake providers (plan_v3 Task 12; R22.12).

Each ablation is the guard's own "C3 minus one switch" preset (``GuardConfig.ABLATIONS``):
C3-L1 without the Inbound Guard, C3-L2 without the Doc Scanner, C3-L3 without the Inbound Gate,
C3-L4 without the Output Guard, C3-ISO without channel isolation. Needs the AgentMailGuard
worktree installed (`uv run --with-editable`); skipped in CI. No network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mailguard")

from evaluation.mailguard_bench import scheme  # noqa: E402
from evaluation.mailguard_bench.guard_build import build_guard  # noqa: E402
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

ABLATIONS = tuple(scheme.V3_ABLATIONS)
FULL = scheme.V3_SWITCHES
MODEL = "gpt-4o-mini"
LIVE = f"OpenAIProvider:{MODEL}"


@pytest.mark.parametrize("config", ABLATIONS)
def test_each_ablation_is_the_guards_own_preset_of_the_same_name(
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
    removed = scheme.V3_ABLATIONS[config]
    assert guard.describe()["active_layers"] == [s for s in FULL if s != removed]
    assert guard.describe()["live_layers"]["preset"] == config


def test_an_unknown_ablation_fails_loudly(tmp_path: Path) -> None:
    for name in ("C3-L6", "C3-L5", "C3-L3B"):
        with pytest.raises(ValueError, match="scheme v3"):
            build_guard(
                name,
                model_name="fake",
                audit_log_path=tmp_path / "a.jsonl",
                l1_model_path=tmp_path / "clf.joblib",
            )


# --- GuardBuild.missing_live_stages expects exactly the judges the ablation keeps


@pytest.mark.parametrize(
    ("config", "stage", "expected"),
    [
        ("C3-L1", "inbound_judge", []),  # no Inbound Guard, so none of its judges
        ("C3-L1", "inbound_extractor", []),
        ("C3-L1", "doc_judge", ["doc_judge"]),
        ("C3-L2", "doc_judge", []),  # no Doc Scanner
        ("C3-L2", "doc_set_judge", []),
        ("C3-L2", "inbound_judge", ["inbound_judge"]),
        ("C3-L3", "output_judge", ["output_judge"]),  # the gate has no judge of its own
        ("C3-L4", "output_judge", []),  # no Output Guard
        ("C3-L4", "doc_set_judge", ["doc_set_judge"]),
        ("C3-ISO", "inbound_extractor", ["inbound_extractor"]),
    ],
)
def test_only_the_judges_the_ablation_runs_are_required_live(
    config: str, stage: str, expected: list[str], tmp_path: Path
) -> None:
    from tests.unit.test_mailguard_bench_scheme_guard import _build, _made_live, _off

    guard = _build(config, tmp_path)
    _made_live(guard)
    _off(guard, stage)
    assert guard.missing_live_stages() == expected


@pytest.mark.parametrize("config", ABLATIONS)
def test_a_missing_classifier_fails_wherever_a_layer_uses_it(config: str, tmp_path: Path) -> None:
    # The Inbound Guard uses the trained classifier and hands it to the Doc Scanner. Every
    # ablation keeps at least one of them, so a skipped `make mailguard-prep` must never yield a
    # silently weaker guard.
    from tests.unit.test_mailguard_bench_scheme_guard import _build, _made_live, _off

    guard = _build(config, tmp_path)
    _made_live(guard)
    _off(guard, "classifier")
    assert guard.missing_live_stages() == ["classifier"]


# --- require_live follows the preset too


def _layers(preset: str, **over: object) -> LiveLayers:
    removed = scheme.V3_ABLATIONS.get(preset, "")
    base: dict[str, object] = {
        "preset": preset,
        "active_layers": tuple(s for s in FULL if s != removed),
        "classifier": True,
        "inbound_judge": LIVE,
        "inbound_extractor": LIVE,
        "doc_judge": None,
        "doc_set_judge": None,
        "output_judge": None,
    }
    return LiveLayers(**{**base, **over})  # type: ignore[arg-type]


def test_require_live_demands_everything_for_the_full_guard() -> None:
    require_live(_layers("C3"), model_name=MODEL)
    for field in ("inbound_judge", "inbound_extractor"):
        with pytest.raises(GuardEnvError, match=f"{field} is None"):
            require_live(_layers("C3", **{field: None}), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(_layers("C3", classifier=False), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="doc_set_judge is None"):
        require_live(_layers("C3", doc_judge=LIVE), model_name=MODEL, doc_llm=True)
    with pytest.raises(GuardEnvError, match="output_judge is .*expected off"):
        require_live(_layers("C3", output_judge=LIVE), model_name=MODEL)


def test_require_live_without_a_layer_ignores_its_judges() -> None:
    require_live(_layers("C3-L1", inbound_judge=None, inbound_extractor=None), model_name=MODEL)
    require_live(_layers("C3-L2"), model_name=MODEL, doc_llm=True)
    require_live(_layers("C3-L4"), model_name=MODEL, output_llm=True)
    with pytest.raises(GuardEnvError, match="output_judge is None"):
        require_live(_layers("C3"), model_name=MODEL, output_llm=True)


def test_require_live_without_the_inbound_guard_still_needs_the_classifier() -> None:
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(
            _layers("C3-L1", inbound_judge=None, inbound_extractor=None, classifier=False),
            model_name=MODEL,
        )


# --- each ablation runs through the real executor on stub models


@pytest.mark.parametrize("config", ABLATIONS)
async def test_every_ablation_makes_one_generation_call_for_a_benign_email(
    config: str, tmp_path: Path
) -> None:
    executor, fake, _ = _executor(config, tmp_path)

    execution = await executor.execute(_prepared(BENIGN))

    assert len(fake.recorded_calls) == 1
    assert execution.guard_errors == ()
    assert execution.record["blocked"] is False
    assert execution.record["final_draft"] is not None


def _part(report: dict[str, Any], removed: str) -> Any:
    """The part of a schema-3 report dump that the switch ``removed`` fills."""
    return {
        "inbound_guard": report["inbound_guard"],
        "doc_scanner": report["doc_scanner"],
        "inbound_gate": report["inbound_decision"],
        "output_guard": report["output_guard"],
        "channel_isolation": report["isolation"],
    }[removed]


@pytest.mark.parametrize("config", ABLATIONS)
async def test_the_removed_switch_never_speaks_in_the_report(config: str, tmp_path: Path) -> None:
    executor, _, _ = _executor(config, tmp_path)

    execution = await executor.execute(_prepared(INJECTION))

    report = execution.record["report"]
    assert report["schema_version"] == 3
    assert _part(report, scheme.V3_ABLATIONS[config]) is None
    assert execution.guard_errors == ()


async def test_without_the_inbound_guard_the_output_guard_still_decides(tmp_path: Path) -> None:
    executor, _, _ = _executor("C3-L1", tmp_path)

    execution = await executor.execute(_prepared(INJECTION))

    report = execution.record["report"]
    assert report["inbound_guard"] is None
    assert report["output_guard"] is not None and report["decision"] is not None
    assert "l1_injection_scanner" not in execution.record["detected_layers"]


async def test_without_the_inbound_gate_the_output_guard_makes_the_only_decision(
    tmp_path: Path,
) -> None:
    executor, fake, _ = _executor("C3-L3", tmp_path)

    execution = await executor.execute(_prepared(INJECTION))

    report = execution.record["report"]
    assert report["inbound_decision"] is None
    assert report["decision"] is not None  # the Output Guard decides after generation (R-e)
    assert len(fake.recorded_calls) == 1  # nothing stopped the job before generation
    assert execution.record["blocked_inbound"] is False


def test_ablation_configs_need_a_guard_like_c3(tmp_path: Path) -> None:
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response={}),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    for config in ABLATIONS:
        with pytest.raises(ValueError, match="needs a guard"):
            prepared_case_executor(config, generator=generator, guard=None)
    guard = build_guard(
        "C3-ISO",
        model_name="fake",
        audit_log_path=tmp_path / "a.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
    )
    executor = prepared_case_executor("C3-ISO", generator=generator, guard=guard)
    assert isinstance(executor, GuardedCaseExecutor)
    assert executor.pipeline.config.name == "C3-ISO"

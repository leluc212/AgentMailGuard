"""Guard wiring around the layers of a v3 config, on fake providers (plan_v3 Task 12; R22.12).

Since ADR-0017 scheme v3 has no ablation config: the guard keeps its "C3 minus one switch"
presets (``GuardConfig.ABLATIONS``), but the benchmark never builds them, and every name of them
is refused. What is left here is that refusal, the rule ``require_live`` applies per active
layer (it reads the layers a guard reports, not a config name), and C3's need for a guard. The
tests that built the v3 ablations are in the history (``scheme.V3_FULL_CAMPAIGN_COMMIT``).
Needs the AgentMailGuard worktree installed (`uv run --with-editable`); skipped in CI. No
network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mailguard")

from evaluation.mailguard_bench import scheme  # noqa: E402
from evaluation.mailguard_bench.guard_build import build_guard  # noqa: E402
from evaluation.mailguard_bench.guard_env import GuardEnvError  # noqa: E402
from evaluation.mailguard_bench.guard_factory import LiveLayers, require_live  # noqa: E402
from evaluation.mailguard_bench.guarded_reply import GuardedCaseExecutor  # noqa: E402
from evaluation.mailguard_bench.runner import prepared_case_executor  # noqa: E402
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator  # noqa: E402

FULL = scheme.V3_SWITCHES
MODEL = "gpt-4o-mini"
LIVE = f"OpenAIProvider:{MODEL}"


def test_an_ablation_name_fails_loudly(tmp_path: Path) -> None:
    # v1's C3-L3B / C3-L5 and the v3 ablations ADR-0017 removed are no v3 config.
    for name in ("C3-L6", "C3-L5", "C3-L3B", "C3-L1", "C3-L2", "C3-L3", "C3-L4", "C3-ISO"):
        with pytest.raises(ValueError, match="scheme v3"):
            build_guard(
                name,
                model_name="fake",
                audit_log_path=tmp_path / "a.jsonl",
                l1_model_path=tmp_path / "clf.joblib",
            )


# --- require_live follows the layers the guard reports


def _layers(removed: str = "", /, **over: object) -> LiveLayers:
    """What a guard reports: C3, or (``removed``) a guard without that switch."""
    base: dict[str, object] = {
        "preset": "C3" if not removed else f"without-{removed}",
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
    require_live(_layers(), model_name=MODEL)
    for field in ("inbound_judge", "inbound_extractor"):
        with pytest.raises(GuardEnvError, match=f"{field} is None"):
            require_live(_layers(**{field: None}), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(_layers(classifier=False), model_name=MODEL)
    with pytest.raises(GuardEnvError, match="doc_set_judge is None"):
        require_live(_layers(doc_judge=LIVE), model_name=MODEL, doc_llm=True)
    with pytest.raises(GuardEnvError, match="output_judge is .*expected off"):
        require_live(_layers(output_judge=LIVE), model_name=MODEL)


def test_require_live_without_a_layer_ignores_its_judges() -> None:
    # The rule is per active layer, so it still holds for a guard built without one.
    require_live(
        _layers("inbound_guard", inbound_judge=None, inbound_extractor=None),
        model_name=MODEL,
    )
    require_live(_layers("doc_scanner"), model_name=MODEL, doc_llm=True)
    require_live(_layers("output_guard"), model_name=MODEL, output_llm=True)
    with pytest.raises(GuardEnvError, match="output_judge is None"):
        require_live(_layers(), model_name=MODEL, output_llm=True)


def test_require_live_without_the_inbound_guard_still_needs_the_classifier() -> None:
    with pytest.raises(GuardEnvError, match="L1 classifier not loaded"):
        require_live(
            _layers(
                "inbound_guard",
                inbound_judge=None,
                inbound_extractor=None,
                classifier=False,
            ),
            model_name=MODEL,
        )


def test_the_full_guard_needs_a_guard(tmp_path: Path) -> None:
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response={}),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    with pytest.raises(ValueError, match="needs a guard"):
        prepared_case_executor("C3", generator=generator, guard=None)
    guard = build_guard(
        "C3",
        model_name="fake",
        audit_log_path=tmp_path / "a.jsonl",
        l1_model_path=tmp_path / "clf.joblib",
    )
    executor = prepared_case_executor("C3", generator=generator, guard=guard)
    assert isinstance(executor, GuardedCaseExecutor)
    assert executor.pipeline.config.name == "C3"

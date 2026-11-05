"""Layer ablation: remove one part at a time from the full guard (task 7.22; plan_v3 Task 13).

Pre-registration: docs/superpowers/specs/2026-09-30-mailguard-layer-ablation-design.md.
Since ADR-0017 scheme v3 runs C0 and C3 only, so no v3 run has an ablation config: what is left
here is the v1 ablation names the legacy reader keeps, the fingerprint keys that may differ
between configs, the report without an ablation section, and the benign-draft rule. The tests
that built v3 ablation runs are in the history (``scheme.V3_FULL_CAMPAIGN_COMMIT``). Tests that
need the AgentMailGuard scorer skip where mailguard is not installed (CI). No network, no model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.scheme import V3_ABLATIONS, V3_REMOVED_CONFIGS
from tests.unit.test_mailguard_bench_report import (
    C3_META,
    PRICES,
    _amg_or_skip,
    _raw,
    _run_folder,
)

V1_ABLATIONS = ("C3-L1", "C3-L2", "C3-L3", "C3-L3B", "C3-L4", "C3-L5")


def test_the_v1_ablation_presets_are_retired_and_v3_has_no_ablation() -> None:
    from evaluation.mailguard_bench.guard_build import ABLATION_CONFIGS
    from evaluation.mailguard_bench.runner import BENCH_CONFIGS, parse_args

    assert ABLATION_CONFIGS == V1_ABLATIONS  # the retired v1 presets, kept for the legacy reader
    assert V3_ABLATIONS == {}  # ADR-0017: the v3 ablations C3-L1 .. C3-L4, C3-ISO are gone
    assert BENCH_CONFIGS == ("C0", "C0T", "C1", "C2", "C3")
    # the v1 ablation is read from its committed folders only: scheme v1 runs are refused, and
    # no ablation name (v1's C3-L3B / C3-L5, or the removed v3 ones) is a v3 config
    for config in (*BENCH_CONFIGS, *V1_ABLATIONS):
        with pytest.raises(SystemExit):
            parse_args(["--config", config, "--run", "r", "--scheme", "v1"])
    for config in ("C3-L3B", "C3-L5", "C3-L6", *V3_REMOVED_CONFIGS):
        with pytest.raises(SystemExit):
            parse_args(["--config", config, "--run", "r"])
    assert parse_args(["--config", "C3", "--run", "r"]).scheme == "v3"


def test_only_the_per_config_keys_may_differ_between_configs_of_one_run() -> None:
    from evaluation.mailguard_bench.report import SHARED_SETTINGS, consistency_problems
    from evaluation.mailguard_bench.runner import FINGERPRINT_KEYS, settings_fingerprint

    per_config = set(FINGERPRINT_KEYS) - set(SHARED_SETTINGS)
    assert per_config == {"preset", "live_layers", "guard_models", "degraded_allowed"}
    assert "judge_fail_closed" in SHARED_SETTINGS  # one judge mode per run (plan_v3 Task 12)

    # Two configs of one run that differ only in a per-config key are consistent; a shared
    # setting that differs is refused. The names are only labels here.
    c3 = {**C3_META, "live_layers": {"preset": "C3", "l1_judge": "OpenAIProvider:m"}}
    other = {**c3, "preset": "other", "live_layers": {"preset": "other"}}
    a, b = settings_fingerprint(c3), settings_fingerprint(other)
    assert {key for key in a if a[key] != b[key]} == {"preset", "live_layers"}
    assert consistency_problems({"C3": c3, "other": other}, {}) == []
    for key, value in (("l1_model_sha256", "e" * 64), ("generation_model", "gpt-4o-mini")):
        (problem,) = consistency_problems({"C3": c3, "other": {**other, key: value}}, {})
        assert problem.startswith(f"configs ran with different {key}")


def _report(run: Path) -> str:
    harness, metrics, mailguard_dir = _amg_or_skip()
    from evaluation.mailguard_bench.report import build_report

    return build_report(
        run, harness=harness, metrics=metrics, prices=PRICES, mailguard_dir=mailguard_dir
    ).read_text("utf-8")


def test_a_run_without_ablation_configs_has_no_layer_ablation_section(tmp_path: Path) -> None:
    text = _report(_run_folder(tmp_path, with_c0t=False))  # C0 and C3, every v3 config

    assert "Layer ablation" not in text
    assert "## Where each job stopped, and why" in text
    summary = json.loads((tmp_path / "run1" / "summary.json").read_text("utf-8"))
    assert "layer_ablation" not in summary
    assert "layer_ablation_llmail" not in summary["tables"]


def test_real_benign_drafts_need_an_unblocked_draft_of_at_least_40_characters() -> None:
    from evaluation.mailguard_bench.scoring import RawRecord, real_benign_drafts

    def rec(case_id: str, body: str | None, **kw: Any) -> RawRecord:
        return RawRecord.from_dict(_raw(case_id, "C3", body, **kw))

    records = [
        rec("b0", "x" * 40),  # exactly 40: real
        rec("b1", "x" * 39),  # one short
        rec("b2", "  " + "x" * 38 + "  "),  # padding does not count as content
        rec("b3", "x" * 80, blocked=True),  # a blocked case never counts, whatever its body
        rec("b4", None, blocked=True),
        rec("b5", "x" * 80, status="error"),  # error rows are not scored
        rec("a0", "x" * 80),  # an attack case is not a benign draft
    ]
    assert real_benign_drafts(records, {"b0", "b1", "b2", "b3", "b4", "b5"}) == (1, 5)
    assert real_benign_drafts([], {"b0"}) == (0, 0)

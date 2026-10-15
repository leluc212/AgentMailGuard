"""The config scheme in the three benchmark processes: the in-process runner, the live runner and
the guard-worker (task 7.20; ADR-0012 decision 11; plan_v3 Task 12).

Every run records its scheme and its judge mode (fail-closed or the v2 fallback) in its meta and
its settings fingerprint. Runs at HEAD are scheme v3; v1 and v2 ran on other guard commits and are
refused, naming the commit. A run folder never mixes the schemes, and a folder from before the
schemes (a meta without the key) keeps its v1 meaning. No network, no model, no docker; the
live-runner and guard-worker cases reuse those modules' own test rigs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evaluation.mailguard_bench import runner, scheme
from evaluation.mailguard_bench.live import guard_worker
from evaluation.mailguard_bench.live.run import (
    GuardDescription,
    GuardWorker,
    build_live_meta,
    guard_worker_expectations,
    guard_worker_mismatches,
    parse_args,
    run,
)
from evaluation.mailguard_bench.runner import (
    FINGERPRINT_KEYS,
    RunSettingsMismatchError,
    check_resume,
    config_case_ids,
    settings_fingerprint,
)
from packages.core.settings import AppSettings
from tests.unit.test_mailguard_bench_live_run import (
    IMAGES,
    OLLAMA,
    TRIAGE,
    RunPool,
    SimWorld,
    _guard_facts,
    _live_deps,
    _run_args,
    _worker_meta,
    live_env,  # noqa: F401  (a fixture)
)
from tests.unit.test_mailguard_bench_runner import _case_set
from tests.unit.test_mailguard_live_guard_worker import (
    Rig,
    _start,
    rig,  # noqa: F401  (a fixture)
)
from tests.unit.test_mailguard_live_guard_worker import (
    _meta as worker_meta_of,
)

RETIRED = tuple(scheme.RETIRED_SCHEME_GUARDS)
GUARDED = tuple(c for c in scheme.V3_CONFIGS if c != "C0")

# --- the in-process runner -----------------------------------------------------------------


def test_a_new_run_of_the_in_process_runner_defaults_to_v3() -> None:
    args = runner.parse_args(["--config", "C3-ISO", "--run", "r"])
    assert args.scheme == "v3" and args.config == "C3-ISO" and args.judge_fallback is False


@pytest.mark.parametrize("config", scheme.V3_CONFIGS)
def test_the_in_process_runner_takes_every_v3_config(config: str) -> None:
    assert runner.parse_args(["--config", config, "--run", "r"]).config == config


@pytest.mark.parametrize("retired", RETIRED)
def test_the_in_process_runner_refuses_a_retired_scheme_naming_its_guard(
    retired: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        runner.parse_args(["--config", "C3", "--run", "r", "--scheme", retired])
    err = capsys.readouterr().err
    assert scheme.RETIRED_SCHEME_GUARDS[retired] in err and "scheme v3" in err


@pytest.mark.parametrize("config", ["C4", "C7", "C3-L3B", "C3-L5", "C8"])
def test_the_in_process_runner_refuses_a_config_v3_does_not_have(config: str) -> None:
    with pytest.raises(SystemExit):
        runner.parse_args(["--config", config, "--run", "r"])


def test_the_scheme_and_the_judge_mode_reach_the_resume_fingerprint() -> None:
    assert {"scheme", scheme.JUDGE_FAIL_CLOSED_KEY} <= set(FINGERPRINT_KEYS)
    assert settings_fingerprint({"scheme": "v3"})["scheme"] == "v3"


def test_v3_runs_every_config_on_the_full_case_set_and_v1_keeps_its_case_selection() -> None:
    manifest = _case_set("s" * 64).manifest
    full = ["a1", "a2", "b1", "r1"]
    for config in scheme.V3_CONFIGS:
        assert config_case_ids(manifest, config, "v3") == full, config
    # the readers of the committed v1 folders keep v1's selection: C1 and C2 the subset
    assert config_case_ids(manifest, "C1", "v1") == ["a2", "b1"]
    assert config_case_ids(manifest, "C3", "v1") == full


def _runner_meta(*argv: str, config: str = "C3-L2") -> dict[str, Any]:
    args = runner.parse_args(["--config", config, "--run", "r1", *argv])
    guard_facts = {
        "config": config,
        "scheme": args.scheme,
        "preset": scheme.v3_guard_preset(config),
        "active_layers": list(scheme.V3_LAYERS[config]),
        "mailguard_commit": "c" * 40,
        "l1_model_sha256": "d" * 64,
        "live_layers": {"preset": config},
    }
    settings = AppSettings()
    return runner.build_run_meta(
        args=args,
        cases_sha256="s" * 64,
        llm=settings.llm,
        settings=settings,
        guard_facts=guard_facts,
        missing=[],
        rag_email_commit="a" * 40,
    )


def test_a_v3_meta_records_its_scheme_the_guard_preset_and_the_full_case_sets() -> None:
    meta = _runner_meta()
    assert meta["scheme"] == "v3" and meta["fingerprint"]["scheme"] == "v3"
    assert meta["preset"] == "C3-L2" and meta["guard_preset"] == "C3-L2"
    assert meta["case_sets"] == ["llmail_attack", "llmail_benign", "rag_attack"]
    assert _runner_meta(config="C0T")["guard_preset"] == "C0"


def test_a_v3_meta_records_whether_the_judges_fail_closed() -> None:
    closed = _runner_meta()
    assert closed["judge_fail_closed"] is True and closed["fingerprint"]["judge_fail_closed"]
    fallback = _runner_meta("--judge-fallback")
    assert fallback["judge_fail_closed"] is False
    assert fallback["fingerprint"]["judge_fail_closed"] is False


def _stored(tmp_path: Path, fingerprint: dict[str, Any]) -> Path:
    path = tmp_path / "C3.meta.json"
    path.write_text(json.dumps({"fingerprint": fingerprint, "invocations": [{"n": 1}]}), "utf-8")
    return path


def test_a_meta_written_before_the_schemes_existed_is_v1_and_never_resumed_as_v3(
    tmp_path: Path,
) -> None:
    old = {"preset": "C3", "generation_model": "gpt-4o-mini"}  # no scheme key: v1
    path = _stored(tmp_path, old)
    assert check_resume(path, {**old, "scheme": "v1"}) == [{"n": 1}]
    with pytest.raises(RunSettingsMismatchError, match="scheme"):
        check_resume(path, {**old, "scheme": "v3"})


def test_a_resume_under_another_scheme_or_judge_mode_is_refused(tmp_path: Path) -> None:
    stored = {"preset": "C3", "scheme": "v3", "judge_fail_closed": True}
    path = _stored(tmp_path, stored)
    with pytest.raises(RunSettingsMismatchError, match="scheme"):
        check_resume(path, {**stored, "scheme": "v2"})
    with pytest.raises(RunSettingsMismatchError, match="judge_fail_closed"):
        check_resume(path, {**stored, "judge_fail_closed": False})
    assert check_resume(path, stored) == [{"n": 1}]


def test_the_in_process_runner_refuses_a_folder_of_another_scheme_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path)
    raw = tmp_path / "r1" / "raw"
    raw.mkdir(parents=True)
    (raw / "C0.meta.json").write_text(json.dumps({"preset": "C0"}), "utf-8")  # a v1 folder

    with pytest.raises(scheme.SchemeMixError, match=scheme.RETIRED_SCHEME_GUARDS["v1"]):
        asyncio.run(runner.run(runner.parse_args(["--config", "C3", "--run", "r1"])))

    (raw / "C0.meta.json").write_text(json.dumps({"scheme": "v2"}), "utf-8")
    with pytest.raises(scheme.SchemeMixError, match="never mixes"):
        asyncio.run(runner.run(runner.parse_args(["--config", "C3", "--run", "r1"])))


class _GuardBuiltError(Exception):
    """Raised by the stand-in ``build_guard`` once it has recorded its call."""


def _run_runner_to_build_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str
) -> dict[str, Any]:
    """Run ``runner.run`` up to its ``build_guard`` call and return that call's keywords."""
    from evaluation.mailguard_bench.guard_env import GuardPaths

    calls: list[dict[str, Any]] = []

    def stand_in(preset: str, **kwargs: Any) -> Any:
        calls.append({"preset": preset, **kwargs})
        raise _GuardBuiltError

    paths = GuardPaths(root=tmp_path, commit="c" * 40, artifacts=tmp_path)
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(runner, "apply_model_profile", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "guard_paths_from_env", lambda *_a, **_k: paths)
    monkeypatch.setattr(runner, "guard_provider_env", lambda *_a, **_k: {})  # no key, no os.environ
    monkeypatch.setattr(runner, "require_pinned_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "require_module_origins", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "load_case_set", lambda *_a, **_k: _case_set("s" * 64))
    case = type("_Case", (), {"from_dict": staticmethod(lambda raw: SimpleNamespace(**raw))})
    monkeypatch.setattr(runner, "EvalCase", case)
    monkeypatch.setattr(runner, "build_guard", stand_in)
    args = runner.parse_args(["--run", "r1", *argv])
    with pytest.raises(_GuardBuiltError):
        asyncio.run(runner.run(args))
    (call,) = calls
    return call


def test_the_in_process_runner_builds_a_v3_guard_with_its_configs_judges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for config in GUARDED:
        call = _run_runner_to_build_guard(monkeypatch, tmp_path, "--config", config)
        assert call["scheme"] == "v3" and call["preset"] == config
        assert (call["doc_llm"], call["output_llm"]) == scheme.v3_llm_stages(config), config
        assert call["judge_fail_closed"] is True


def test_the_in_process_runner_passes_the_judge_fallback_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    call = _run_runner_to_build_guard(monkeypatch, tmp_path, "--config", "C3", "--judge-fallback")
    assert call["judge_fail_closed"] is False


# --- the live runner -----------------------------------------------------------------------


def test_a_new_live_run_defaults_to_v3_and_takes_every_v3_config() -> None:
    for config in scheme.V3_CONFIGS:
        args = parse_args(["--config", config, "--run", "r", "--model-profile", "qwen2.5-7b"])
        assert (args.config, args.scheme) == (config, "v3")


def test_the_live_runner_refuses_a_retired_scheme_and_a_config_v3_does_not_have() -> None:
    profile = ["--model-profile", "qwen2.5-7b"]
    for argv in (
        *(["--config", "C3", "--run", "r", "--scheme", s, *profile] for s in RETIRED),
        ["--config", "C4", "--run", "r", *profile],
        ["--config", "C3-L5", "--run", "r", *profile],
        ["--config", "C8", "--run", "r", *profile],
    ):
        with pytest.raises(SystemExit):
            parse_args(argv)


def test_the_v3_ablations_run_live() -> None:
    # Unlike v1's in-process-only ablation, a v3 ablation keeps every judge of the layers it runs.
    profile = ["--model-profile", "qwen2.5-7b"]
    for config in scheme.V3_ABLATIONS:
        assert parse_args(["--config", config, "--run", "r", *profile]).config == config


def _live_meta(config: str, *argv: str) -> dict[str, Any]:
    args = parse_args(["--config", config, "--run", "r1", "--model-profile", "qwen2.5-7b", *argv])
    args.guard_model = "qwen2.5:7b-instruct"
    facts = _guard_facts(config, llm_stages=config in ("C2", "C3"))
    facts["preset"] = scheme.v3_guard_preset(config)
    guard = GuardDescription(facts, dict(facts["live_stages"]), [])
    return build_live_meta(
        args=args,
        settings=AppSettings(),
        cases_sha256="s" * 64,
        guard=guard,
        rag_email_commit="a" * 40,
        triage=TRIAGE,
        service_images=IMAGES,
        service_revisions=dict.fromkeys(IMAGES, "a" * 40),
        rag_email_dirty=False,
        ollama=OLLAMA,
    )


def test_a_live_v3_meta_records_the_scheme_and_the_judge_mode_in_the_fingerprint() -> None:
    meta = _live_meta("C3-L1")
    assert meta["scheme"] == "v3" and meta["fingerprint"]["scheme"] == "v3"
    assert meta["judge_fail_closed"] is True and meta["fingerprint"]["judge_fail_closed"] is True
    assert meta["case_sets"] == ["llmail_attack", "llmail_benign", "rag_attack"]
    assert _live_meta("C3", "--judge-fallback")["fingerprint"]["judge_fail_closed"] is False


def test_the_runner_expects_the_guard_worker_to_run_its_scheme_and_judge_mode(
    tmp_path: Path,
) -> None:
    from evaluation.mailguard_bench.guard_env import GuardPaths

    paths = GuardPaths(root=tmp_path, commit="c" * 40, artifacts=tmp_path)
    args = parse_args(["--config", "C3", "--run", "r1", "--model-profile", "qwen2.5-7b"])
    args.guard_model = "qwen2.5:7b-instruct"
    expected = guard_worker_expectations(args, AppSettings(), paths)
    assert expected["scheme"] == "v3" and expected["judge_fail_closed"] is True

    worker = GuardWorker(run="r1", config="C3", pid=4242, path=tmp_path / "w.pid")
    meta = _worker_meta("C3", pid=4242, run_dir=tmp_path)

    def problems_with(**recorded: Any) -> list[str]:
        problems = guard_worker_mismatches({**meta, **recorded}, expected=expected, worker=worker)
        return [p for p in problems if p.split(":")[0] in recorded]

    assert problems_with(scheme="v3", judge_fail_closed=True) == []
    (problem,) = problems_with(scheme="v2")
    assert "'v2'" in problem and "'v3'" in problem
    (problem,) = problems_with(judge_fail_closed=False)  # a worker started with --judge-fallback
    assert problem.startswith("judge_fail_closed")


async def test_the_live_runner_refuses_a_folder_of_another_scheme_before_any_case(
    live_env: Path,  # noqa: F811
) -> None:
    raw = live_env / "results" / "r1" / "raw"
    raw.mkdir(parents=True)
    (raw / "C0.meta.json").write_text(json.dumps({"preset": "C0"}), "utf-8")  # a v1 folder
    world, pool = SimWorld(), RunPool()
    deps = _live_deps(live_env, world, pool)

    with pytest.raises(scheme.SchemeMixError, match="never mixes"):
        await run(_run_args(live_env, "C0"), deps)

    assert not pool.opened and not (raw / "C0.jsonl").exists()  # nothing was fed or written


async def test_a_live_c0_run_writes_its_scheme_into_the_meta(live_env: Path) -> None:  # noqa: F811
    world, pool = SimWorld(), RunPool()
    world.scenarios = {"attack-a1": "ai", "attack-a2": "early_exit", "benign-b1": "template"}
    args = _run_args(live_env, "C0", "--limit", "1")
    assert await run(args, _live_deps(live_env, world, pool)) == 0
    meta = json.loads((live_env / "results" / "r1" / "raw" / "C0.meta.json").read_text("utf-8"))
    assert meta["scheme"] == "v3" and meta["fingerprint"]["scheme"] == "v3"


# --- the guard-worker ----------------------------------------------------------------------


def test_the_worker_serves_the_guarded_configs_of_v3() -> None:
    profile = ["--model-profile", "gpt-4o-mini"]
    for config in GUARDED:
        args = guard_worker.parse_args(["--config", config, "--run", "r", *profile])
        assert (args.config, args.scheme) == (config, "v3")
    for bad in ("C0", "C4", "C7", "C3-L3B", "C3-L5", "C8"):
        with pytest.raises(SystemExit):
            guard_worker.parse_args(["--config", bad, "--run", "r", *profile])
    for retired in RETIRED:
        with pytest.raises(SystemExit):
            guard_worker.parse_args(["--config", "C2", "--run", "r", "--scheme", retired, *profile])


def test_the_workers_fingerprint_carries_the_scheme_and_the_judge_mode() -> None:
    assert {"scheme", scheme.JUDGE_FAIL_CLOSED_KEY} <= set(guard_worker.FINGERPRINT_KEYS)


def test_a_v3_worker_builds_its_guard_with_the_scheme_and_the_configs_judges(
    rig: Rig,  # noqa: F811
) -> None:
    for config in ("C1", "C2", "C3-L2", "C3-ISO"):
        rig.built.clear()
        assert _start(config=config, run=f"run-{config}") == 0
        (call,) = rig.built
        stages = scheme.v3_llm_stages(config)
        assert call["scheme"] == "v3"
        assert (call["doc_llm"], call["output_llm"]) == stages, config
        meta = worker_meta_of(rig, config, f"run-{config}")
        assert meta["scheme"] == "v3" and meta["fingerprint"]["scheme"] == "v3"
        assert meta["guard_llm_stages"] == {"doc_llm": stages[0], "output_llm": stages[1]}


def test_the_worker_refuses_a_folder_of_another_scheme_before_it_writes_or_consumes(
    rig: Rig,  # noqa: F811
) -> None:
    raw = rig.run_dir() / "raw"
    raw.mkdir(parents=True)
    (raw / "C0.meta.json").write_text(json.dumps({"scheme": "v2"}), "utf-8")  # a v2 folder

    assert _start(config="C3") == 1  # main prints FAIL and exits 1

    assert not rig.pid_file("C3").exists()
    assert not guard_worker.meta_path(rig.run_dir(), "C3").exists()
    assert rig.built == []


# --- the report's consistency check -------------------------------------------------------


def test_configs_of_one_run_must_share_the_scheme_and_a_missing_key_is_v1() -> None:
    from evaluation.mailguard_bench.report import SHARED_SETTINGS, consistency_problems
    from tests.unit.test_mailguard_bench_report import C0_META, C3_META

    assert "scheme" in SHARED_SETTINGS
    # a meta that predates the schemes is v1: it agrees with one that says so, never with v3
    old = {k: v for k, v in C0_META.items() if k != "scheme"}
    marked = {**C3_META, "scheme": "v1"}
    assert consistency_problems({"C0": old, "C3": marked}, {}) == []
    (problem,) = consistency_problems({"C0": old, "C3": {**C3_META, "scheme": "v2"}}, {})
    assert problem.startswith("configs ran with different scheme")
    assert "'v1'" in problem and "'v2'" in problem
    (v3_vs_old,) = consistency_problems({"C0": old, "C3": C3_META}, {})
    assert "'v1'" in v3_vs_old and "'v3'" in v3_vs_old

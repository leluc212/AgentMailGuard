"""The config scheme in the three benchmark processes: the v1 runner, the live runner and the
guard-worker (task 7.20; ADR-0012 decision 11).

Every run records its scheme in its meta and its settings fingerprint, new runs default to v2,
a run folder never mixes the schemes, and a folder from before the schemes (a meta without the key)
keeps its v1 meaning. No network, no model, no docker; the live-runner and guard-worker cases reuse
those modules' own test rigs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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

V2_FLAGS = scheme.V2_LAYERS

# --- the v1 (in-process) runner ------------------------------------------------------------


def test_a_new_run_of_the_in_process_runner_defaults_to_v2() -> None:
    args = runner.parse_args(["--config", "C7", "--run", "r"])
    assert args.scheme == "v2" and args.config == "C7"


@pytest.mark.parametrize("config", scheme.V2_CONFIGS)
def test_the_in_process_runner_takes_every_v2_config(config: str) -> None:
    assert runner.parse_args(["--config", config, "--run", "r"]).config == config


@pytest.mark.parametrize("config", scheme.configs_for("v1"))
def test_the_in_process_runner_takes_every_v1_config_under_scheme_v1(config: str) -> None:
    args = runner.parse_args(["--config", config, "--run", "r", "--scheme", "v1"])
    assert (args.config, args.scheme) == (config, "v1")


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "C3-L1", "--run", "r"],  # the ablation is v1 only, and v2 is the default
        ["--config", "C3-L5", "--run", "r", "--scheme", "v2"],
        ["--config", "C4", "--run", "r", "--scheme", "v1"],  # C4..C7 do not exist in v1
        ["--config", "C7", "--run", "r", "--scheme", "v1"],
        ["--config", "C8", "--run", "r"],
        ["--config", "C3", "--run", "r", "--scheme", "v3"],
    ],
)
def test_the_in_process_runner_refuses_a_config_the_scheme_does_not_have(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        runner.parse_args(argv)


def test_the_scheme_reaches_the_resume_fingerprint() -> None:
    assert "scheme" in FINGERPRINT_KEYS
    assert settings_fingerprint({"scheme": "v2"})["scheme"] == "v2"


def test_v2_runs_every_config_on_the_full_case_set_and_v1_keeps_its_case_selection() -> None:
    manifest = _case_set("s" * 64).manifest
    full = ["a1", "a2", "b1", "r1"]
    for config in scheme.V2_CONFIGS:
        assert config_case_ids(manifest, config, "v2") == full, config
    # v1 (and a caller that names no scheme) selects exactly as before: C1 and C2 the subset
    assert config_case_ids(manifest, "C1", "v1") == ["a2", "b1"]
    assert config_case_ids(manifest, "C2") == ["a2", "b1"]
    assert config_case_ids(manifest, "C3", "v1") == full


def _runner_meta(*argv: str, config: str = "C4") -> dict[str, Any]:
    args = runner.parse_args(["--config", config, "--run", "r1", *argv])
    guard_facts = {
        "config": config,
        "scheme": args.scheme,
        "preset": scheme.v2_guard_name(config) if args.scheme == "v2" else config,
        "active_layers": list(V2_FLAGS.get(config, ())),
        "mailguard_commit": "c" * 40,
        "l1_model_sha256": "d" * 64,
        "live_layers": {"preset": "x"},
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


def test_a_v2_meta_records_its_scheme_the_guard_config_and_the_full_case_sets() -> None:
    meta = _runner_meta()
    assert meta["scheme"] == "v2"
    assert meta["fingerprint"]["scheme"] == "v2"
    assert meta["preset"] == "C4" and meta["guard_preset"] == "v2-C4"
    assert meta["case_sets"] == ["llmail_attack", "llmail_benign", "rag_attack"]


def test_a_v1_meta_records_v1_and_keeps_the_ablation_subset_of_c1() -> None:
    meta = _runner_meta("--scheme", "v1", config="C1")
    assert meta["scheme"] == "v1" and meta["fingerprint"]["scheme"] == "v1"
    assert meta["guard_preset"] == "C1"
    assert meta["case_sets"] == ["ablation_attack", "llmail_benign"]


def _stored(tmp_path: Path, fingerprint: dict[str, Any]) -> Path:
    path = tmp_path / "C3.meta.json"
    path.write_text(json.dumps({"fingerprint": fingerprint, "invocations": [{"n": 1}]}), "utf-8")
    return path


def test_a_resume_of_a_meta_written_before_the_schemes_existed_is_v1(tmp_path: Path) -> None:
    old = {"preset": "C3", "generation_model": "gpt-4o-mini"}  # no scheme key: v1
    path = _stored(tmp_path, old)
    assert check_resume(path, {**old, "scheme": "v1"}) == [{"n": 1}]
    with pytest.raises(RunSettingsMismatchError, match="scheme"):
        check_resume(path, {**old, "scheme": "v2"})


def test_a_resume_under_the_other_scheme_is_refused(tmp_path: Path) -> None:
    path = _stored(tmp_path, {"preset": "C3", "scheme": "v2"})
    with pytest.raises(RunSettingsMismatchError, match="scheme"):
        check_resume(path, {"preset": "C3", "scheme": "v1"})
    assert check_resume(path, {"preset": "C3", "scheme": "v2"}) == [{"n": 1}]


def test_the_in_process_runner_refuses_a_folder_of_the_other_scheme_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path)
    raw = tmp_path / "r1" / "raw"
    raw.mkdir(parents=True)
    (raw / "C0.meta.json").write_text(json.dumps({"preset": "C0"}), "utf-8")  # a v1 folder

    with pytest.raises(scheme.SchemeMixError, match="never mixes"):
        asyncio.run(runner.run(runner.parse_args(["--config", "C7", "--run", "r1"])))

    (raw / "C0.meta.json").write_text(json.dumps({"scheme": "v2"}), "utf-8")
    with pytest.raises(scheme.SchemeMixError):
        asyncio.run(
            runner.run(runner.parse_args(["--config", "C3", "--run", "r1", "--scheme", "v1"]))
        )


# --- the live runner -----------------------------------------------------------------------


def test_a_new_live_run_defaults_to_v2_and_takes_every_v2_config() -> None:
    for config in scheme.V2_CONFIGS:
        args = parse_args(["--config", config, "--run", "r", "--model-profile", "qwen2.5-7b"])
        assert (args.config, args.scheme) == (config, "v2")


def test_the_live_runner_refuses_a_config_the_scheme_does_not_have() -> None:
    profile = ["--model-profile", "qwen2.5-7b"]
    for argv in (
        ["--config", "C4", "--run", "r", "--scheme", "v1", *profile],
        ["--config", "C3-L1", "--run", "r", *profile],
        ["--config", "C8", "--run", "r", *profile],
    ):
        with pytest.raises(SystemExit):
            parse_args(argv)
    assert parse_args(["--config", "C2", "--run", "r", "--scheme", "v1", *profile]).scheme == "v1"


def _live_meta(config: str, *argv: str) -> dict[str, Any]:
    args = parse_args(["--config", config, "--run", "r1", "--model-profile", "qwen2.5-7b", *argv])
    args.guard_model = "qwen2.5:7b-instruct"
    facts = _guard_facts(config, llm_stages=config in ("C4", "C7"))
    facts["preset"] = scheme.v2_guard_name(config) if args.scheme == "v2" else config
    guard = GuardDescription(facts, dict(facts["live_stages"]), [])
    return build_live_meta(
        args=args,
        settings=AppSettings(),
        cases_sha256="s" * 64,
        guard=guard,
        rag_email_commit="a" * 40,
        triage=TRIAGE,
        service_images=IMAGES,
        ollama=OLLAMA,
    )


def test_a_live_v2_meta_records_the_scheme_in_the_meta_and_the_fingerprint() -> None:
    meta = _live_meta("C4")
    assert meta["scheme"] == "v2" and meta["fingerprint"]["scheme"] == "v2"
    assert meta["case_sets"] == ["llmail_attack", "llmail_benign", "rag_attack"]


def test_a_live_v1_meta_keeps_the_v1_case_selection() -> None:
    meta = _live_meta("C1", "--scheme", "v1")
    assert meta["scheme"] == "v1" and meta["fingerprint"]["scheme"] == "v1"
    assert meta["case_sets"] == ["ablation_attack", "llmail_benign"]
    assert _live_meta("C1")["case_sets"] == ["llmail_attack", "llmail_benign", "rag_attack"]


def test_the_runner_expects_the_guard_worker_to_run_its_scheme(tmp_path: Path) -> None:
    from evaluation.mailguard_bench.guard_env import GuardPaths

    paths = GuardPaths(root=tmp_path, commit="c" * 40, artifacts=tmp_path)
    args = parse_args(["--config", "C4", "--run", "r1", "--model-profile", "qwen2.5-7b"])
    args.guard_model = "qwen2.5:7b-instruct"
    expected = guard_worker_expectations(args, AppSettings(), paths)
    assert expected["scheme"] == "v2"

    worker = GuardWorker(run="r1", config="C4", pid=4242, path=tmp_path / "w.pid")
    meta = _worker_meta("C4", pid=4242, run_dir=tmp_path)

    def scheme_problems(recorded: str) -> list[str]:
        problems = guard_worker_mismatches(
            {**meta, "scheme": recorded}, expected=expected, worker=worker
        )
        return [p for p in problems if p.startswith("scheme")]

    assert scheme_problems("v2") == []
    (problem,) = scheme_problems("v1")
    assert "'v1'" in problem and "'v2'" in problem


async def test_the_live_runner_refuses_a_folder_of_the_other_scheme_before_any_case(
    live_env: Path,  # noqa: F811
) -> None:
    raw = live_env / "results" / "r1" / "raw"
    raw.mkdir(parents=True)
    (raw / "C0.meta.json").write_text(json.dumps({"preset": "C0"}), "utf-8")  # a v1 folder
    world, pool = SimWorld(), RunPool()
    deps = _live_deps(live_env, world, pool)

    with pytest.raises(scheme.SchemeMixError, match="never mixes"):
        await run(_run_args(live_env, "C0", "--scheme", "v2"), deps)

    assert not pool.opened and not (raw / "C0.jsonl").exists()  # nothing was fed or written


async def test_a_live_c0_run_writes_its_scheme_into_the_meta(live_env: Path) -> None:  # noqa: F811
    world, pool = SimWorld(), RunPool()
    world.scenarios = {"attack-a1": "ai", "attack-a2": "early_exit", "benign-b1": "template"}
    args = _run_args(live_env, "C0", "--limit", "1", "--scheme", "v2")
    assert await run(args, _live_deps(live_env, world, pool)) == 0
    meta = json.loads((live_env / "results" / "r1" / "raw" / "C0.meta.json").read_text("utf-8"))
    assert meta["scheme"] == "v2" and meta["fingerprint"]["scheme"] == "v2"

    # a second config of the same run under the other scheme is refused
    with pytest.raises(scheme.SchemeMixError):
        await run(
            _run_args(live_env, "C0", "--scheme", "v1"),
            _live_deps(live_env, world, RunPool()),
        )


# --- the guard-worker ----------------------------------------------------------------------


def test_the_worker_serves_the_guarded_configs_of_its_scheme() -> None:
    profile = ["--model-profile", "gpt-4o-mini"]
    for config in ("C0T", "C1", "C2", "C3", "C4", "C5", "C6", "C7"):
        args = guard_worker.parse_args(["--config", config, "--run", "r", *profile])
        assert (args.config, args.scheme) == (config, "v2")
    for bad in ("C0", "C3-L1", "C8"):
        with pytest.raises(SystemExit):
            guard_worker.parse_args(["--config", bad, "--run", "r", *profile])
    for bad in ("C4", "C7", "C0"):  # not v1 configs
        with pytest.raises(SystemExit):
            guard_worker.parse_args(["--config", bad, "--run", "r", "--scheme", "v1", *profile])
    assert (
        guard_worker.parse_args(["--config", "C2", "--run", "r", "--scheme", "v1", *profile]).scheme
        == "v1"
    )


def test_the_workers_fingerprint_carries_the_scheme() -> None:
    assert "scheme" in guard_worker.FINGERPRINT_KEYS


def test_a_v2_worker_builds_its_guard_with_the_scheme_and_the_configs_ai_stages(
    rig: Rig,  # noqa: F811
) -> None:
    for config, stages in (("C4", (True, False)), ("C5", (False, True)), ("C7", (True, True))):
        rig.built.clear()
        assert _start(config=config, run=f"run-{config}", scheme="v2") == 0
        (call,) = rig.built
        assert call["scheme"] == "v2"
        assert (call["l3b_llm"], call["l4_llm"]) == stages, config
        meta = worker_meta_of(rig, config, f"run-{config}")
        assert meta["scheme"] == "v2" and meta["fingerprint"]["scheme"] == "v2"
        assert meta["guard_llm_stages"] == {"l3b_llm": stages[0], "l4_llm": stages[1]}


def test_a_v1_worker_keeps_its_llm_stage_rule_and_records_v1(rig: Rig) -> None:  # noqa: F811
    assert _start(config="C3") == 0
    (call,) = rig.built
    assert call["scheme"] == "v1"
    assert (call["l3b_llm"], call["l4_llm"]) == (True, True)
    assert worker_meta_of(rig, "C3")["scheme"] == "v1"


def test_the_worker_refuses_a_folder_of_the_other_scheme_before_it_writes_or_consumes(
    rig: Rig,  # noqa: F811
) -> None:
    raw = rig.run_dir() / "raw"
    raw.mkdir(parents=True)
    (raw / "C0.meta.json").write_text(json.dumps({"preset": "C0"}), "utf-8")  # a v1 folder

    assert _start(config="C7", scheme="v2") == 1  # main prints FAIL and exits 1

    assert not rig.pid_file("C7").exists()
    assert not guard_worker.meta_path(rig.run_dir(), "C7").exists()
    assert rig.built == []


# --- the report's consistency check -------------------------------------------------------


def test_configs_of_one_run_must_share_the_scheme_and_a_missing_key_is_v1() -> None:
    from evaluation.mailguard_bench.report import SHARED_SETTINGS, consistency_problems
    from tests.unit.test_mailguard_bench_report import C0_META, C3_META

    assert "scheme" in SHARED_SETTINGS
    old, marked = C0_META, {**C3_META, "scheme": "v1"}  # C0_META predates the schemes
    assert consistency_problems({"C0": old, "C3": marked}, {}) == []
    (problem,) = consistency_problems({"C0": old, "C3": {**C3_META, "scheme": "v2"}}, {})
    assert problem.startswith("configs ran with different scheme")
    assert "'v1'" in problem and "'v2'" in problem

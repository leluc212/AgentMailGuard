"""The evaluation guard-worker process (task 7.20; ADR-0011; R22.12).

    python -m evaluation.mailguard_bench.live.guard_worker --config C3 --run RUN --model-profile M

It is the ai-worker with GuardedDraftingService in place of DraftingService. These tests run its
start-up and composition with the broker, the database and the guard replaced by stand-ins, and
its pid-file rules with real child processes. No mailguard import, no network, no model call, no
.env (every test that calls ``main`` works in an empty temporary directory).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.guard_build import GuardBuild
from evaluation.mailguard_bench.guard_env import GuardEnvError, GuardPaths, WorktreeInfo
from evaluation.mailguard_bench.live import guard_worker
from evaluation.mailguard_bench.live.guarded_drafting import GuardedDraftingService
from packages.core.settings import AIWorkerSettings
from services.ai_worker import main as ai_main
from tests.stubs.worker_resources import fake_worker_resources

MODEL = "gpt-4o-mini"
LINUX_PROC = Path("/proc/self/cmdline").exists()


class _Model:
    """The provider inside CountingProvider; only its shutdown is looked at."""

    model_name = "stub"

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class StubGuard(GuardBuild):
    """A GuardBuild that reports what a test says it is (no mailguard, no classifier)."""

    missing: list[str] = field(default_factory=list)

    def missing_live_stages(self) -> list[str]:
        return list(self.missing)

    def describe(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "preset": self.preset,
            "guard_model": self.model_name,
            "missing_live_stages": list(self.missing),
            "live_layers": {"preset": self.preset, "l3b_llm": "stub", "l4_llm": "stub"},
            "mailguard_commit": "c" * 40,
            "l1_model_sha256": None,
        }


def _stub_guard(config: str, *, missing: list[str] | None = None) -> StubGuard:
    return StubGuard(
        config=config,
        preset="C0" if config == "C0T" else config,
        model_name=MODEL,
        pipeline=object(),
        guard_llm=CountingProvider(_Model()),
        missing=missing or [],
    )


class FakeRuntime:
    """WorkerRuntime without a broker or a database: records its arguments, then 'serves'."""

    instances: list[FakeRuntime] = []
    while_running: Any = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeRuntime.instances.append(self)

    async def run(self) -> None:
        if FakeRuntime.while_running is not None:
            FakeRuntime.while_running()  # what a test wants to see while the worker is up


@dataclass
class Rig:
    results: Path
    paths: GuardPaths
    built: list[dict[str, Any]] = field(default_factory=list)  # build_guard's calls
    missing: list[str] = field(default_factory=list)  # the stages the built guard lacks

    def run_dir(self, run: str = "r1") -> Path:
        return self.results / run

    def pid_file(self, config: str, run: str = "r1") -> Path:
        return guard_worker.pid_path(self.run_dir(run), config)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    """A run of the worker with every outside dependency replaced; os.environ is restored."""
    monkeypatch.chdir(tmp_path)  # AppSettings and with_dot_env read ./.env; there is none here
    for name in list(os.environ):
        if name.startswith(("LLM__", "BENCH_")) or name in ("OPENAI_BASE_URL", "OPENAI_API_KEY"):
            monkeypatch.delenv(name)
    results = tmp_path / "results"
    paths = GuardPaths(root=tmp_path / "guard", commit="c" * 40, artifacts=tmp_path / "artifacts")
    harness = Rig(results=results, paths=paths)

    def build_guard(preset: str, **kwargs: Any) -> StubGuard:
        harness.built.append({"preset": preset, **kwargs})
        return _stub_guard(preset, missing=harness.missing)

    FakeRuntime.instances = []
    FakeRuntime.while_running = None
    monkeypatch.setattr(guard_worker, "RESULTS_ROOT", results)
    monkeypatch.setattr(guard_worker, "guard_paths_from_env", lambda _environ: paths)
    monkeypatch.setattr(
        guard_worker,
        "require_pinned_worktree",
        lambda root, commit: WorktreeInfo(path=root, commit=commit, clean=True),
    )
    monkeypatch.setattr(guard_worker, "require_module_origins", lambda *_a, **_k: {})
    monkeypatch.setattr(guard_worker, "build_guard", build_guard)
    monkeypatch.setattr(guard_worker, "WorkerRuntime", FakeRuntime)
    with patch.dict(os.environ):  # the run writes the profile and the guard's keys into it
        yield harness


def _main(*argv: str) -> int:
    return guard_worker.main(list(argv))


# ------------------------------------------------------------------ the command line


def test_the_worker_serves_one_guarded_config() -> None:
    profile = ["--model-profile", "gpt-4o-mini"]
    for config in ("C0T", "C1", "C2", "C3"):
        args = guard_worker.parse_args(["--config", config, "--run", "r", *profile])
        assert args.config == config
        assert (args.model_profile, args.port) == ("gpt-4o-mini", guard_worker.DEFAULT_PORT)
    for bad in ("C0", "C4", "c3"):  # C0 is the ai-worker container's job, not a guard's
        with pytest.raises(SystemExit):
            guard_worker.parse_args(["--config", bad, "--run", "r", *profile])
    with pytest.raises(SystemExit):
        guard_worker.parse_args(["--config", "C3", *profile])  # --run is required
    with pytest.raises(SystemExit):
        guard_worker.parse_args(["--config", "C3", "--run", "r", "--model-profile", "gpt-5"])


def test_the_worker_cannot_start_without_a_model_profile(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # One benchmarked model per run in every LLM role: without a profile the guard judges would
    # run DEFAULT_GUARD_MODEL while the generation call, the summarizer and the router use
    # whatever .env's LLM__* say, and the runner (which requires the profile) could not tell.
    with pytest.raises(SystemExit):
        guard_worker.parse_args(["--config", "C3", "--run", "r"])

    assert "--model-profile" in capsys.readouterr().err


def test_only_c3_turns_the_l3b_and_l4_llm_stages_on() -> None:
    assert guard_worker.guard_llm_stages("C3") == (True, True)
    for config in ("C0T", "C1", "C2"):
        assert guard_worker.guard_llm_stages(config) == (False, False)


def test_the_run_folder_files_have_the_names_the_feeder_looks_for() -> None:
    run_dir = Path("/runs/2026-09-29-x-live")
    assert guard_worker.pid_path(run_dir, "C3") == run_dir / "raw" / "guard_worker.C3.pid"
    assert guard_worker.audit_log_path(run_dir, "C3") == run_dir / "raw" / "audit__C3.jsonl"
    # the guard's own L5 log must not share the file: it holds one line per job
    l5 = guard_worker.l5_log_path(run_dir, "C3")
    assert l5.parent == run_dir / "raw" and l5 != guard_worker.audit_log_path(run_dir, "C3")
    assert guard_worker.meta_path(run_dir, "C3") == run_dir / "raw" / "guard_worker.C3.meta.json"


# ------------------------------------------------------------------ the pid file


def test_the_pid_file_holds_the_process_id_while_the_worker_lives(tmp_path: Path) -> None:
    path = tmp_path / "raw" / "guard_worker.C3.pid"

    with guard_worker.pid_file(path):
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())

    assert not path.exists()


def test_the_pid_file_goes_away_when_the_worker_fails(tmp_path: Path) -> None:
    path = tmp_path / "raw" / "guard_worker.C3.pid"

    with pytest.raises(RuntimeError, match="boom"), guard_worker.pid_file(path):
        raise RuntimeError("boom")

    assert not path.exists()


def _sleeper(*argv: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_a_live_guard_worker_of_another_config_or_run_blocks_a_second_one(
    tmp_path: Path,
) -> None:
    other = tmp_path / "results" / "run-a" / "raw" / "guard_worker.C3.pid"
    other.parent.mkdir(parents=True)
    sleeper = _sleeper("evaluation.mailguard_bench.live.guard_worker")  # what its cmdline holds
    try:
        other.write_text(f"{sleeper.pid}\n", encoding="utf-8")

        with pytest.raises(GuardEnvError, match=r"another guard-worker.*C3\.pid"):
            guard_worker.require_no_other_guard_worker(tmp_path / "results")
    finally:
        sleeper.kill()
        sleeper.wait()


def test_a_stale_pid_file_does_not_block_and_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "results" / "run-a" / "raw" / "guard_worker.C3.pid"
    path.parent.mkdir(parents=True)
    path.write_text(f"{_dead_pid()}\n", encoding="utf-8")

    guard_worker.require_no_other_guard_worker(tmp_path / "results")  # nothing is alive

    with guard_worker.pid_file(path):
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_an_unreadable_pid_file_counts_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "results" / "run-a" / "raw" / "guard_worker.C1.pid"
    path.parent.mkdir(parents=True)
    path.write_text("not a pid", encoding="utf-8")

    guard_worker.require_no_other_guard_worker(tmp_path / "results")


@pytest.mark.skipif(not LINUX_PROC, reason="reads /proc/<pid>/cmdline")
def test_a_recycled_pid_of_another_program_counts_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "results" / "run-a" / "raw" / "guard_worker.C2.pid"
    path.parent.mkdir(parents=True)
    other_program = _sleeper()  # alive, and its command line does not name a guard-worker
    try:
        path.write_text(f"{other_program.pid}\n", encoding="utf-8")

        guard_worker.require_no_other_guard_worker(tmp_path / "results")
    finally:
        other_program.kill()
        other_program.wait()


def test_the_worker_itself_is_not_another_worker(tmp_path: Path) -> None:
    path = tmp_path / "results" / "run-a" / "raw" / "guard_worker.C3.pid"
    with guard_worker.pid_file(path):
        guard_worker.require_no_other_guard_worker(tmp_path / "results")


# ------------------------------------------------------------------ start-up


def test_main_starts_the_worker_runtime_for_the_config_and_writes_its_files(rig: Rig) -> None:
    os.environ["BENCH_OPENAI_API_KEY"] = "sk-bench"
    observed: dict[str, Any] = {}

    def while_running() -> None:
        pid_file = rig.pid_file("C3")
        observed["pid"] = pid_file.read_text(encoding="utf-8").strip()
        observed["meta"] = json.loads(
            guard_worker.meta_path(rig.run_dir(), "C3").read_text(encoding="utf-8")
        )

    FakeRuntime.while_running = while_running

    code = _main(
        "--config", "C3", "--run", "r1", "--model-profile", "gpt-4o-mini", "--port", "8123"
    )

    assert code == 0
    (runtime,) = FakeRuntime.instances
    kwargs = runtime.kwargs
    assert kwargs["service_name"] == "guard_worker"
    assert (kwargs["port"], kwargs["host"]) == (8123, "127.0.0.1")
    assert "install_signal_handlers" not in kwargs  # SIGTERM drains the consumers, as the ai-worker
    settings = kwargs["settings"]
    assert isinstance(settings, AIWorkerSettings)
    assert settings.llm.fast_model == MODEL  # the profile was applied before the settings were read
    assert os.environ["OPENAI_API_KEY"] == "sk-bench"  # the guard's provider reads these two
    assert os.environ["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert observed["pid"] == str(os.getpid())
    assert not rig.pid_file("C3").exists()  # gone once the worker stops
    meta = observed["meta"]
    assert (meta["run_id"], meta["config"], meta["pid"]) == ("r1", "C3", os.getpid())
    assert meta["model_profile"] == "gpt-4o-mini" and meta["guard_model"] == MODEL
    assert meta["guard_llm_stages"] == {"l3b_llm": True, "l4_llm": True}
    assert meta["guard"]["mailguard_commit"] == "c" * 40
    assert meta["audit_log"] == str(guard_worker.audit_log_path(rig.run_dir(), "C3"))


def test_main_reads_a_profile_key_kept_only_in_dot_env(rig: Rig, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("BENCH_OPENAI_API_KEY=sk-from-dot-env\n", encoding="utf-8")

    assert _main("--config", "C3", "--run", "r1", "--model-profile", "gpt-4o-mini") == 0

    assert os.environ["OPENAI_API_KEY"] == "sk-from-dot-env"  # `uv run` does not load .env itself


def test_the_guard_is_built_for_the_config_on_the_runs_model(rig: Rig) -> None:
    os.environ["BENCH_OPENAI_API_KEY"] = "sk-bench"

    assert _main("--config", "C3", "--run", "r1", "--model-profile", "gpt-4o-mini") == 0

    (call,) = rig.built
    assert call["preset"] == "C3"
    assert call["model_name"] == MODEL  # one model in every role
    assert (call["l3b_llm"], call["l4_llm"]) == (True, True)
    assert call["l1_model_path"] == rig.paths.l1_model
    assert call["audit_log_path"] == guard_worker.l5_log_path(rig.run_dir(), "C3")


def test_c0t_and_the_ablation_configs_keep_l3b_and_l4_llm_off(rig: Rig) -> None:
    os.environ["BENCH_OPENAI_API_KEY"] = "sk-bench"
    for config in ("C0T", "C1", "C2"):
        rig.built.clear()
        assert _main("--config", config, "--run", "r1", "--model-profile", "gpt-4o-mini") == 0
        (call,) = rig.built
        assert (call["l3b_llm"], call["l4_llm"]) == (False, False)


def test_main_refuses_a_guard_that_is_not_at_full_strength(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.missing.extend(["l1.classifier", "l3b.llm"])
    os.environ["BENCH_OPENAI_API_KEY"] = "sk-bench"

    code = _main("--config", "C3", "--run", "r1", "--model-profile", "gpt-4o-mini")

    assert code == 1
    assert FakeRuntime.instances == []  # nothing was started
    err = capsys.readouterr().err
    assert "l1.classifier" in err and "l3b.llm" in err
    assert not rig.pid_file("C3").exists()


def test_main_refuses_while_another_guard_worker_is_alive(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    other = rig.pid_file("C1", run="older-run")
    other.parent.mkdir(parents=True)
    sleeper = _sleeper("evaluation.mailguard_bench.live.guard_worker")
    os.environ["BENCH_OPENAI_API_KEY"] = "sk-bench"
    try:
        other.write_text(f"{sleeper.pid}\n", encoding="utf-8")

        code = _main("--config", "C3", "--run", "r1", "--model-profile", "gpt-4o-mini")
    finally:
        sleeper.kill()
        sleeper.wait()

    assert code == 1
    assert "another guard-worker" in capsys.readouterr().err
    assert FakeRuntime.instances == [] and rig.built == []  # refused before building anything


def test_main_names_a_missing_profile_key_and_starts_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _main("--config", "C3", "--run", "r1", "--model-profile", "gpt-4o-mini")

    assert code == 1
    assert "BENCH_OPENAI_API_KEY" in capsys.readouterr().err
    assert FakeRuntime.instances == [] and rig.built == []


# ------------------------------------------------------------------ the composition


class _Consumer:
    """What build_guarded_components takes from an AIWorkerConsumer."""

    def __init__(self, provider: _Model) -> None:
        self.started = False
        self.drafting = type("D", (), {"generator": type("G", (), {"llm_provider": provider})()})()

    async def start(self) -> None:
        self.started = True


async def test_the_components_are_the_ai_workers_with_the_guarded_drafting_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)
    checked: list[int | None] = []
    warmed: list[bool] = []

    async def verify(_dsn: object, configured_dimension: int | None = None) -> None:
        checked.append(configured_dimension)

    monkeypatch.setattr(guard_worker, "verify_database_vector_dimension", verify)
    monkeypatch.setattr(guard_worker, "start_token_counter_warmup", lambda: warmed.append(True))
    monkeypatch.setattr(guard_worker, "TokenCounter", lambda: "the-counter")
    provider = _Model()
    consumers = [_Consumer(provider), _Consumer(provider)]
    seen: dict[str, Any] = {}

    def build_consumers(resources: Any, **kwargs: Any) -> list[_Consumer]:
        seen["res"], seen["kwargs"] = resources, kwargs
        return consumers

    monkeypatch.setattr(ai_main, "build_consumers", build_consumers)
    guard = _stub_guard("C3")
    audit = tmp_path / "raw" / "audit__C3.jsonl"

    start_fns = await guard_worker.build_guarded_components(res, guard=guard, audit_path=audit)

    assert checked == [settings.embedding.dimension] and warmed == [True]  # as build_components
    assert seen["res"] is res
    assert seen["kwargs"]["token_counter"] == "the-counter"
    assert seen["kwargs"]["embedder"] is not None
    factory = seen["kwargs"]["drafting_factory"]
    parts: dict[str, Any] = {
        "generator": object(),
        "job_store": object(),
        "persistence": object(),
        "price_table": {},
        "metrics": res.metrics,
    }
    service = factory(**parts)  # exactly the keywords DraftingService(...) receives
    assert isinstance(service, GuardedDraftingService)
    assert service.generator is parts["generator"] and service.metrics is res.metrics
    assert service.guard is guard and service.audit_path == audit
    for start in start_fns:
        await start()
    assert [c.started for c in consumers] == [True, True]

    await res.shutdown.trigger_shutdown("TEST")  # the HTTP clients close once the lanes drained
    assert provider.closed is True
    assert guard.guard_llm.inner.closed is True


async def test_a_worker_with_no_lane_has_nothing_to_start_and_closes_the_guards_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = fake_worker_resources(AIWorkerSettings())

    async def verify(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(guard_worker, "verify_database_vector_dimension", verify)
    monkeypatch.setattr(guard_worker, "start_token_counter_warmup", lambda: None)
    monkeypatch.setattr(guard_worker, "TokenCounter", lambda: "the-counter")
    monkeypatch.setattr(ai_main, "build_consumers", lambda *_a, **_k: [])
    guard = _stub_guard("C1")

    start_fns = await guard_worker.build_guarded_components(
        res, guard=guard, audit_path=tmp_path / "audit__C1.jsonl"
    )

    assert start_fns == []
    await res.shutdown.trigger_shutdown("TEST")
    assert guard.guard_llm.inner.closed is True

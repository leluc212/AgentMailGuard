"""The kit's orchestrator: one model, every config, the switch between drafting consumers
(task 7.23; ADR-0012 decisions 7 and 9; demo-runbook section 9.9 steps 3 to 7).

The machine is replaced by ``FakeHost``: docker, the guard-worker process, the runner and the
HTTP probe are scripted and recorded, and the clock is fake, so a 300 s readiness timeout takes
no time. Everything the orchestrator decides is real: the order of the commands, the readiness
rule (a pid file newer than the start AND /readyz answering), what is stopped when, the resume
and retry logic, the kit log, the refusals. The stack env is the real ``stack_env`` over a
temporary git repository and .env. No docker, no model call, no network, no live key.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.kit import campaign
from evaluation.mailguard_bench.kit.campaign import (
    DEFAULT_V2_CONFIGS,
    KitContext,
    RunOptions,
    run_campaign,
)
from tests.unit.mailguard_kit_fixtures import (  # noqa: F401  (bench_fixture is the `bench` fixture)
    GEMINI_KEY,
    HOST_ENV,
    OPENAI_KEY,
    REPORTS,
    RUN,
    STACK_UP,
    Bench,
    WorkerMode,
    bench_fixture,
    label,
    opts,
    pid_file,
    sequence,
    write_meta,
)

# --- the default list, the commands, the order ------------------------------------------------


def test_the_default_config_list_is_one_constant_with_friday_s_nine_configs() -> None:
    assert DEFAULT_V2_CONFIGS == ("C0", "C0T", "C1", "C2", "C3", "C4", "C5", "C6", "C7")


def test_the_exact_command_sequence_per_config(bench: Bench) -> None:
    bench.runner_outcomes({}, {})
    assert run_campaign(bench.ctx, opts(limit=5, concurrency=2)) == 0
    assert sequence(bench.host) == [
        STACK_UP,
        "docker compose start ai-worker",
        "live.run C0",
        "docker compose stop ai-worker",
        "spawn guard_worker C3",
        "live.run C3",
        "stop",
        *REPORTS,
    ]
    runs = {
        label(p): p for kind, p in bench.host.events if kind == "run" and "live.run" in " ".join(p)
    }
    assert runs["live.run C0"] == [
        "py",
        "-m",
        "evaluation.mailguard_bench.live.run",
        "--config",
        "C0",
        "--run",
        RUN,
        "--model-profile",
        "gpt-4o-mini",
        "--retry-errors",
        "--concurrency",
        "2",
        "--limit",
        "5",
    ]
    worker = next(p["command"] for k, p in bench.host.events if k == "spawn")
    assert worker == [
        "py",
        "-m",
        "evaluation.mailguard_bench.live.guard_worker",
        "--config",
        "C3",
        "--run",
        RUN,
        "--model-profile",
        "gpt-4o-mini",
    ]


def test_limit_is_left_out_of_the_runner_command_when_not_given(bench: Bench) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C0",)))
    cmd = next(p for k, p in bench.host.events if k == "run" and "live.run" in " ".join(p))
    assert "--limit" not in cmd


def test_the_stack_is_applied_with_both_env_files_and_no_deps_and_waited_on(bench: Bench) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C0",)))
    up = next(
        p
        for k, p in bench.host.events
        if k == "run" and p[:3] == ["docker", "compose", "--env-file"]
    )
    assert up == [
        "docker",
        "compose",
        "--env-file",
        str(bench.repo / ".env"),
        "--env-file",
        str(bench.repo / ".env.stack"),
        "up",
        "-d",
        "--no-deps",
        "api",
        "triage-worker",
        "knowledge-worker",
        "ai-worker",
    ]
    ps = [
        p for k, p in bench.host.events if k == "capture" and p[:3] == ["docker", "compose", "ps"]
    ]
    assert ps and ps[0][-4:] == ["api", "triage-worker", "knowledge-worker", "ai-worker"]
    assert (bench.repo / ".env.stack").exists()  # written by the real stack_env


def test_the_stack_is_not_healthy_in_time_so_nothing_runs(bench: Bench) -> None:
    bench.host.health = lambda ids: [f"{i}|running|starting|0" for i in ids]
    assert run_campaign(bench.ctx, opts(configs=("C0",), stack_wait_s=30)) == 1
    assert not [s for s in sequence(bench.host) if s.startswith("live.run")]
    assert any("not healthy after 30 s" in line for line in bench.err)


def test_a_stack_env_refusal_stops_before_docker(bench: Bench) -> None:
    (bench.repo / ".env").write_text(
        "".join(
            f"{k}={v}\n" for k, v in {**HOST_ENV, "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "500"}.items()
        ),
        encoding="utf-8",
    )
    assert run_campaign(bench.ctx, opts()) == 1
    assert bench.host.events == []
    assert any("RETRIEVAL__RETRIEVAL_TIMEOUT_MS must be 3000" in line for line in bench.err)


def test_a_failed_compose_up_stops_the_campaign(bench: Bench) -> None:
    bench.host.exits[STACK_UP] = 1
    assert run_campaign(bench.ctx, opts()) == 1
    assert sequence(bench.host) == [STACK_UP]


# --- readiness of the guard-worker ------------------------------------------------------------


def test_the_runner_waits_for_a_pid_file_newer_than_the_start_and_for_readyz(bench: Bench) -> None:
    stale = pid_file(bench, "C3")
    stale.parent.mkdir(parents=True)
    stale.write_text("1234\n", encoding="utf-8")
    os.utime(stale, (1, 1))  # left by a worker of long ago
    bench.host.modes["C3"] = WorkerMode(start="late", ready_after_s=7)
    bench.runner_outcomes({})
    assert run_campaign(bench.ctx, opts(configs=("C3",))) == 0
    spawn_at = 1000.0
    assert bench.host.now >= spawn_at + 7  # waited for the late worker, not for the stale file
    order = sequence(bench.host)
    assert order.index("spawn guard_worker C3") < order.index("live.run C3") < order.index("stop")
    probes = [url for kind, url in bench.host.events if kind == "http"]
    assert probes and set(probes) == {"http://127.0.0.1:8014/readyz"}


def test_readyz_without_a_fresh_pid_file_is_not_ready(bench: Bench) -> None:
    # A pid file left by an earlier worker must not pass for this start's, whatever answers.
    stale = pid_file(bench, "C3")
    stale.parent.mkdir(parents=True)
    stale.write_text("1234\n", encoding="utf-8")
    os.utime(stale, (1, 1))
    bench.host.modes["C3"] = WorkerMode(start="no-pid")
    assert run_campaign(bench.ctx, opts(configs=("C3",), gw_wait_s=10)) == 1
    assert not [s for s in sequence(bench.host) if s.startswith("live.run")]
    assert any("not ready after 10 s" in line for line in bench.err)


def test_a_pid_file_that_is_not_this_workers_is_left_alone(bench: Bench) -> None:
    stale = pid_file(bench, "C3")
    stale.parent.mkdir(parents=True)
    stale.write_text("1234\n", encoding="utf-8")
    os.utime(stale, (1, 1))
    bench.host.modes["C3"] = WorkerMode(start="crash")
    run_campaign(bench.ctx, opts(configs=("C3",)))
    assert stale.read_text(encoding="utf-8") == "1234\n"  # a live worker of another run, maybe


def test_a_pid_file_without_readyz_is_not_ready_and_times_out_with_the_runbook_message(
    bench: Bench,
) -> None:
    bench.host.modes["C3"] = WorkerMode(start="never-ready")
    assert run_campaign(bench.ctx, opts(configs=("C3",), gw_wait_s=20)) == 1
    assert not [s for s in sequence(bench.host) if s.startswith("live.run")]
    assert sequence(bench.host)[-1] == "stop"  # it was sent the stop signal, not left consuming
    log = "evaluation/results/mailguard_bench/r1/raw/guard-worker.C3.log"
    assert f"FAIL guard-worker C3 not ready after 20 s; see {Path(log)}" in bench.err
    assert not pid_file(bench, "C3").exists()
    assert bench.host.now >= 1000 + 20


def test_a_guard_worker_that_exits_before_it_is_ready_fails_the_config_at_once(
    bench: Bench,
) -> None:
    bench.host.modes["C3"] = WorkerMode(start="crash")
    assert run_campaign(bench.ctx, opts(configs=("C3",))) == 1
    assert not [s for s in sequence(bench.host) if s.startswith("live.run")]
    assert any(line.startswith("FAIL guard-worker C3 exited; see ") for line in bench.err)
    assert bench.host.now < 1000 + 10  # no waiting out the 300 s
    assert "stop" not in sequence(bench.host)  # nothing left to stop


def test_a_dead_guard_worker_aborts_the_campaign_so_the_next_config_is_not_tried(
    bench: Bench,
) -> None:
    bench.host.modes["C0T"] = WorkerMode(start="crash")
    assert run_campaign(bench.ctx, opts(configs=("C0T", "C3"))) == 1
    assert not [s for s in sequence(bench.host) if s.startswith("spawn guard_worker C3")]
    assert any("make bench-run" in line for line in bench.out + bench.err)  # how to resume


def test_the_guard_worker_log_is_under_the_run_folder(bench: Bench) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C3",)))
    log = next(p["log"] for kind, p in bench.host.events if kind == "spawn")
    assert log == bench.results_root / RUN / "raw" / "guard-worker.C3.log"


# --- stopping ---------------------------------------------------------------------------------


def test_the_stop_passes_the_pid_of_the_pid_file_and_then_removes_nothing_that_is_gone(
    bench: Bench,
) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C3",)))
    stop = next(p for kind, p in bench.host.events if kind == "stop")
    assert stop["worker_pid"] == stop["pid"]  # the fake worker wrote its own pid
    assert not pid_file(bench, "C3").exists()


def test_a_stale_pid_file_is_removed_only_after_the_process_is_confirmed_gone(
    bench: Bench,
) -> None:
    # Windows: CTRL_BREAK ends the worker before its finally block removes the file.
    bench.host.modes["C3"] = WorkerMode(stop="leaves-pid")
    bench.runner_outcomes({})
    assert run_campaign(bench.ctx, opts(configs=("C3",))) == 0
    assert not pid_file(bench, "C3").exists()


def test_a_worker_that_ignores_the_stop_is_killed_and_the_config_is_still_recorded(
    bench: Bench,
) -> None:
    bench.host.modes["C3"] = WorkerMode(stop="stuck")
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C3",)))
    seq = sequence(bench.host)
    assert seq[seq.index("live.run C3") :][:3] == ["live.run C3", "stop", "kill"]
    assert not pid_file(bench, "C3").exists()  # gone after the kill, so the file went too


def test_a_worker_that_cannot_be_stopped_keeps_its_pid_file_and_fails_loudly(
    bench: Bench,
) -> None:
    bench.host.modes["C3"] = WorkerMode(stop="unkillable")
    bench.runner_outcomes({})
    assert run_campaign(bench.ctx, opts(configs=("C3",))) == 1
    assert pid_file(bench, "C3").exists()  # never removed while the process may be alive
    assert any("did not exit" in line for line in bench.err)


def test_a_runner_that_raises_still_stops_the_guard_worker(bench: Bench) -> None:
    def boom(cmd: list[str]) -> int | None:
        if "live.run" in " ".join(cmd):
            raise RuntimeError("runner blew up")
        return None

    bench.host.run_hook = boom
    with pytest.raises(RuntimeError, match="blew up"):
        run_campaign(bench.ctx, opts(configs=("C3",)))
    assert sequence(bench.host)[-1] == "stop"
    assert not pid_file(bench, "C3").exists()


def test_ctrl_c_during_a_guarded_run_stops_the_worker_and_prints_the_resume_command(
    bench: Bench,
) -> None:
    def interrupt(cmd: list[str]) -> int | None:
        if "live.run" in " ".join(cmd) and "C3" in cmd:
            raise KeyboardInterrupt
        return None

    bench.host.run_hook = interrupt
    code = run_campaign(bench.ctx, opts(configs=("C3",), limit=5, concurrency=2))
    assert code == 130
    assert sequence(bench.host)[-1] == "stop"
    assert not pid_file(bench, "C3").exists()
    printed = "\n".join(bench.out + bench.err)
    assert "make bench-run MODEL=gpt-4o-mini RUN=r1 CONFIGS=C3 LIMIT=5 CONCURRENCY=2" in printed
    assert "-m evaluation.mailguard_bench.kit.campaign run --model-profile gpt-4o-mini" in printed
    # the module line needs the overlay (MAILGUARD_* and the editable guard): it says so
    module_line = next(line for line in printed.splitlines() if "kit.campaign run" in line)
    assert "overlay" in module_line
    assert [r["status"] for r in bench.kit_log() if r["step"] == "config"] == ["interrupted"]


def test_a_second_ctrl_c_during_the_drain_still_kills_the_worker_and_removes_its_pid_file(
    bench: Bench,
) -> None:
    # The worker runs in its own session: if the kit leaves without killing it, it keeps
    # consuming the lane queues with ai-worker stopped, and the next run refuses to start.
    bench.host.modes["C3"] = WorkerMode(stop="stuck", interrupt_wait=True)
    assert run_campaign(bench.ctx, opts(configs=("C3",))) == 130
    kinds = [kind for kind, _ in bench.host.events]
    assert "kill" in kinds
    assert bench.host.processes[0].exit_code is not None
    assert not pid_file(bench, "C3").exists()
    assert not list((bench.results_root / RUN / "raw").glob(".kit-stamp.*"))


def test_ctrl_c_while_waiting_for_readiness_stops_the_worker_too(bench: Bench) -> None:
    bench.host.modes["C3"] = WorkerMode(start="never-ready")
    original = bench.host.sleep

    def sleeping(seconds: float) -> None:
        original(seconds)
        raise KeyboardInterrupt

    bench.host.sleep = sleeping  # type: ignore[method-assign]
    assert run_campaign(bench.ctx, opts(configs=("C3",))) == 130
    assert sequence(bench.host)[-1] == "stop"


# --- C0 ---------------------------------------------------------------------------------------


def test_c0_starts_the_container_waits_for_healthy_and_starts_no_guard_worker(
    bench: Bench,
) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C0",)))
    assert "spawn" not in " ".join(sequence(bench.host))
    ps = [
        p
        for k, p in bench.host.events
        if k == "capture" and p[:3] == ["docker", "compose", "ps"] and p[-1] == "ai-worker"
    ]
    assert ps, "C0 waits for the ai-worker container to be healthy"


def test_c0_fails_without_running_the_runner_when_the_container_never_gets_healthy(
    bench: Bench,
) -> None:
    calls = {"n": 0}

    def health(ids: list[str]) -> list[str] | None:
        calls["n"] += 1
        if len(ids) == 1:  # the ai-worker alone, after `start`
            return [f"{ids[0]}|running|unhealthy|0"]
        return None

    bench.host.health = health
    assert run_campaign(bench.ctx, opts(configs=("C0",), stack_wait_s=10)) == 1
    assert "live.run C0" not in sequence(bench.host)


# --- reports ----------------------------------------------------------------------------------


def test_the_reports_are_report_analyses_report_with_the_run_folder(bench: Bench) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C0",)))
    report_cmds = [
        p for k, p in bench.host.events if k == "run" and label(p) in ("report", "analyses")
    ]
    run_dir = str(bench.results_root / RUN)
    assert [label(c) for c in report_cmds] == REPORTS
    for cmd in report_cmds:
        assert cmd[cmd.index("--run-dir") + 1] == run_dir
        assert cmd[cmd.index("--mailguard-dir") + 1] == "/guard"


def test_run_has_no_reader_because_the_shell_may_not_export_the_llm_settings_it_needs() -> None:
    # The meaning reader is served by LLM__* from the process settings, and the run refuses an
    # exported LLM__* (docker compose would let it win over the env file). So a reader on `run`
    # could only fail after the whole campaign: it is a step of `report` (make bench-report).
    assert "reader" not in {f.name for f in dataclasses.fields(RunOptions)}
    with pytest.raises(SystemExit):
        campaign.parse_args(
            ["run", "--model-profile", "gpt-4o-mini", "--run", "x", "--reader", "some-reader"]
        )
    assert campaign.parse_args(["report", "--run", "x", "--reader", "r"]).reader == "r"


def test_a_run_builds_the_reports_without_the_meaning_column(bench: Bench) -> None:
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C0",)))
    assert "meaning" not in sequence(bench.host)


def test_the_reports_are_skipped_when_a_config_failed_and_the_exit_code_says_so(
    bench: Bench,
) -> None:
    bench.host.exits["live.run C0"] = 1
    assert run_campaign(bench.ctx, opts(configs=("C0",))) == 1
    assert "report" not in sequence(bench.host)
    assert any("reports were not built" in line for line in bench.out + bench.err)


def test_a_failed_report_fails_the_campaign(bench: Bench) -> None:
    bench.runner_outcomes({})
    bench.host.exits["report"] = 1
    assert run_campaign(bench.ctx, opts(configs=("C0",))) == 1


# --- resume, retry, the kit log ---------------------------------------------------------------


def test_the_kit_log_has_one_line_per_step_with_timings_and_the_runner_s_counts(
    bench: Bench,
) -> None:
    bench.runner_outcomes({"selected": 6, "ok": 5, "error": 1}, {"selected": 6, "ok": 6})
    run_campaign(bench.ctx, opts(limit=6))
    config_steps = [r for r in bench.kit_log() if r["step"] == "config"]
    first = config_steps[0]
    assert (first["config"], first["pass"], first["status"]) == ("C0", 1, "ok")
    assert first["counts"] == {
        "selected": 6,
        "ok": 5,
        "error": 1,
        "skipped_already_recorded": 0,
    }
    assert first["limit"] == 6 and first["model_profile"] == "gpt-4o-mini"
    assert datetime.fromisoformat(first["start"]) <= datetime.fromisoformat(first["end"])
    assert first["seconds"] >= 0
    steps = [r["step"] for r in bench.kit_log()]
    assert steps.count("stack") == 1 and steps.count("reports") >= 1


def test_only_configs_with_error_rows_get_the_retry_pass(bench: Bench) -> None:
    # C0 finishes clean, C3 leaves two error rows: the retry pass runs C3 again and not C0.
    bench.runner_outcomes({}, {"error": 2, "ok": 2}, {"error": 0, "ok": 2, "skipped": 2})
    assert run_campaign(bench.ctx, opts()) == 0
    runs = [s for s in sequence(bench.host) if s.startswith("live.run")]
    assert runs == ["live.run C0", "live.run C3", "live.run C3"]
    passes = [(r["config"], r["pass"]) for r in bench.kit_log() if r["step"] == "config"]
    assert passes == [("C0", 1), ("C3", 1), ("C3", 2)]


def test_a_config_whose_runner_failed_is_tried_again_in_the_retry_pass(bench: Bench) -> None:
    bench.host.exits["live.run C0"] = 1
    assert run_campaign(bench.ctx, opts(configs=("C0",))) == 1
    assert sequence(bench.host).count("live.run C0") == 2
    assert [r["status"] for r in bench.kit_log() if r["step"] == "config"] == ["failed", "failed"]


def test_a_failed_config_does_not_stop_the_others(bench: Bench) -> None:
    bench.host.exits["live.run C0"] = 1
    bench.runner_outcomes({}, {})
    assert run_campaign(bench.ctx, opts(configs=("C0", "C3"))) == 1
    assert "live.run C3" in sequence(bench.host)


def test_counts_of_an_earlier_invocation_are_not_mistaken_for_a_failed_one(bench: Bench) -> None:
    write_meta(bench.results_root, RUN, "C0", selected=4, ok=4)  # an earlier, finished start
    bench.host.exits["live.run C0"] = 1  # this one is refused before it writes anything
    run_campaign(bench.ctx, opts(configs=("C0",)))
    first = [r for r in bench.kit_log() if r["step"] == "config"][0]
    assert first["counts"] is None


def test_rerunning_the_same_command_skips_the_finished_configs(bench: Bench) -> None:
    bench.runner_outcomes({"selected": 4, "ok": 4}, {"selected": 4, "ok": 4})
    assert run_campaign(bench.ctx, opts()) == 0
    before = len(bench.host.events)
    bench.host.events.clear()
    assert run_campaign(bench.ctx, opts()) == 0
    assert before > 0
    # nothing pending: no stack env, no docker, no runner; only the reports are rebuilt
    assert sequence(bench.host) == REPORTS
    assert any("already finished" in line for line in bench.out)


def test_a_rerun_picks_up_where_an_interrupted_run_stopped(bench: Bench) -> None:
    def interrupt_c3(cmd: list[str]) -> int | None:
        if "live.run" in " ".join(cmd):
            config = cmd[cmd.index("--config") + 1]
            if config == "C3":
                raise KeyboardInterrupt
            write_meta(bench.results_root, RUN, config)
        return None

    bench.host.run_hook = interrupt_c3
    assert run_campaign(bench.ctx, opts()) == 130
    bench.host.events.clear()
    bench.runner_outcomes({})
    assert run_campaign(bench.ctx, opts()) == 0
    seq = sequence(bench.host)
    assert "live.run C0" not in seq and "live.run C3" in seq
    assert "docker compose start ai-worker" not in seq  # C0 was done


def test_a_finished_config_is_run_again_when_the_limit_or_the_model_differs(
    bench: Bench,
) -> None:
    bench.runner_outcomes({"selected": 2, "ok": 2}, {"selected": 4, "ok": 4})
    run_campaign(bench.ctx, opts(configs=("C0",), limit=2))
    bench.host.events.clear()
    run_campaign(bench.ctx, opts(configs=("C0",), limit=None))
    assert "live.run C0" in sequence(bench.host)


# --- preflight --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["LLM__OPENAI_API_KEY", "EMBEDDING__MODEL_NAME", "RETRIEVAL__X", "SUMMARIZATION__Y",
     "ROUTING__CONFIGURED_CONSUMERS", "BENCH_SUMMARIZER_MODEL"],
)  # fmt: skip
def test_a_benchmark_setting_exported_in_the_shell_is_refused_by_name_only(
    bench: Bench, name: str
) -> None:
    bench.ctx = KitContext(
        **{**bench.ctx.__dict__, "environ": {**bench.ctx.environ, name: "secret-value-xyz"}}
    )
    assert run_campaign(bench.ctx, opts()) == 2
    message = "\n".join(bench.err)
    assert name in message and "secret-value-xyz" not in message
    assert bench.host.events == []


def test_an_unknown_model_profile_is_refused_before_anything_runs(bench: Bench) -> None:
    assert run_campaign(bench.ctx, opts(model_profile="no-such-model")) == 2
    assert bench.host.events == []


def test_an_empty_or_repeated_config_list_is_refused(bench: Bench) -> None:
    assert run_campaign(bench.ctx, opts(configs=())) == 2
    assert run_campaign(bench.ctx, opts(configs=("C0", "C0"))) == 2
    assert bench.host.events == []


def test_config_names_are_not_validated_by_the_kit(bench: Bench) -> None:
    # live.run and guard_worker validate them (scheme.configs_for("v2") is the integrator's);
    # the kit drafts with the ai-worker for C0 and the guard-worker for every other name.
    bench.runner_outcomes({})
    run_campaign(bench.ctx, opts(configs=("C4",)))
    assert "spawn guard_worker C4" in sequence(bench.host)


# --- dry run ----------------------------------------------------------------------------------


class ExplodingHost:
    os_name = "posix"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry run touched the machine: host.{name}")


def test_dry_run_prints_every_command_and_touches_nothing(bench: Bench) -> None:
    ctx = KitContext(**{**bench.ctx.__dict__, "host": ExplodingHost()})
    assert run_campaign(ctx, opts(dry_run=True)) == 0
    assert not (bench.repo / ".env.stack").exists()
    assert not (bench.results_root / RUN).exists()
    printed = "\n".join(bench.out)
    for needle in (
        "docker compose --env-file",
        "docker compose start ai-worker",
        "docker compose stop ai-worker",
        "evaluation.mailguard_bench.live.run --config C0",
        "evaluation.mailguard_bench.live.guard_worker --config C3",
        "evaluation.mailguard_bench.live.run --config C3",
        "evaluation.mailguard_bench.report",
        "evaluation.mailguard_bench.analyses",
        "http://127.0.0.1:8014/readyz",
    ):
        assert needle in printed, needle
    assert GEMINI_KEY not in printed and OPENAI_KEY not in printed
    assert "WARN" not in printed  # nothing ran, so there are no error rows to warn about


def test_dry_run_prints_the_commands_a_real_run_executes(bench: Bench) -> None:
    ctx = KitContext(**{**bench.ctx.__dict__, "host": ExplodingHost()})
    run_campaign(ctx, opts(dry_run=True, limit=3))
    dry = [line.removeprefix("would run: ") for line in bench.out if line.startswith("would run: ")]
    bench.out.clear()
    bench.runner_outcomes({}, {})
    assert run_campaign(bench.ctx, opts(limit=3)) == 0
    real = [
        " ".join(p["command"] if isinstance(p, dict) else p)
        for kind, p in bench.host.events
        if kind in ("run", "spawn")
    ]
    assert real == dry


def test_dry_run_lists_finished_configs_as_skipped(bench: Bench) -> None:
    bench.runner_outcomes({"selected": 4, "ok": 4})
    run_campaign(bench.ctx, opts(configs=("C0",)))
    bench.out.clear()
    ctx = KitContext(**{**bench.ctx.__dict__, "host": ExplodingHost()})
    run_campaign(ctx, opts(configs=("C0", "C3"), dry_run=True))
    printed = "\n".join(bench.out)
    assert "C0 already finished" in printed
    assert "live.run --config C0" not in printed.replace("evaluation.mailguard_bench.", "")


# --- the command line -------------------------------------------------------------------------


def test_the_command_line_defaults_to_the_v2_configs_and_one_worker() -> None:
    args = campaign.parse_args(["run", "--model-profile", "gpt-4o-mini", "--run", "x"])
    assert tuple(args.configs) == DEFAULT_V2_CONFIGS
    assert (args.concurrency, args.gw_wait_s, args.limit, args.dry_run) == (
        1,
        300.0,
        None,
        False,
    )


def test_configs_come_as_a_comma_list_and_are_not_checked_against_a_list() -> None:
    args = campaign.parse_args(
        ["run", "--model-profile", "qwen2.5-7b", "--run", "x", "--configs", "C0, C9 ,C0T"]
    )
    assert tuple(args.configs) == ("C0", "C9", "C0T")


def test_model_profiles_are_whatever_model_profiles_py_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OpenRouter route arrives as new profiles; the kit has no list of its own.
    from evaluation.mailguard_bench import model_profiles

    extra = model_profiles.ModelProfile(
        name="qwen2.5-7b-openrouter",
        model="qwen/qwen-2.5-7b-instruct",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="BENCH_OPENROUTER_API_KEY",
        input_per_m=0.0,
        output_per_m=0.0,
    )
    monkeypatch.setitem(model_profiles.PROFILES, extra.name, extra)
    args = campaign.parse_args(["run", "--model-profile", extra.name, "--run", "x"])
    assert args.model_profile == extra.name


def test_the_command_line_refuses_an_unknown_profile_and_a_third_worker() -> None:
    with pytest.raises(SystemExit):
        campaign.parse_args(["run", "--model-profile", "nope", "--run", "x"])
    with pytest.raises(SystemExit):
        campaign.parse_args(
            ["run", "--model-profile", "gpt-4o-mini", "--run", "x", "--concurrency", "3"]
        )


def test_the_run_name_model_and_configs_are_printed_first_so_a_defaulted_run_is_known(
    bench: Bench,
) -> None:
    bench.runner_outcomes({}, {})
    run_campaign(bench.ctx, opts())
    assert (
        bench.out[0]
        == f"run {RUN}: model gpt-4o-mini, configs C0, C3 (results in {bench.results_root / RUN})"
    )


def test_the_leak_scan_finds_a_key_at_the_very_end_of_a_large_file(
    bench: Bench,
) -> None:
    # A key is found wherever it sits in a file, even at the very end without a newline.
    run_dir = bench.results_root / RUN
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "raw" / "x.log").write_bytes(b"a" * 100_000 + GEMINI_KEY.encode())
    assert campaign.run_package(bench.ctx, RUN) == 1

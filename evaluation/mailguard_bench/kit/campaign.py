"""The teammate's benchmark kit: setup, run, report and package (task 7.23; ADR-0012 decision 9).

    python -m evaluation.mailguard_bench.kit.campaign setup
    python -m evaluation.mailguard_bench.kit.campaign run --model-profile M --run RUN [--configs ..]
    python -m evaluation.mailguard_bench.kit.campaign report --run RUN [--reader MODEL]
    python -m evaluation.mailguard_bench.kit.campaign package --run RUN

run the steps of docs/demo-runbook.md section 9.9 (steps 3 to 7) for ONE model, which the runbook
has the owner type by hand with the shell helpers ``mg`` and ``run_config``. Through the Make
targets (``make bench-setup``, ``bench-run``, ``bench-report``, ``bench-package``) they run under
the same overlay as the ``mailguard-*`` targets: the pinned AgentMailGuard worktree installed over
rag-email's environment, which every subprocess inherits (``sys.executable -m ...``).

    run:  stack env (live/stack_env.py, with its refusals) ─▶ docker compose up --no-deps ─▶ wait
          for each config  C0:      docker compose start ai-worker ─▶ wait healthy ─▶ live.run
                           guarded: docker compose stop ai-worker ─▶ start the guard-worker
                                    (host process, log in <run>/raw/guard-worker.<config>.log)
                                    ─▶ wait: pid file newer than the start AND /readyz answers
                                    ─▶ live.run ─▶ stop it and confirm it exited
          one retry pass over the configs that failed or left error rows
          reports: report, analyses, report; with --reader also the meaning column, then again

It is pure Python: no bash, so it runs natively on Windows as well (the stop signal of the
guard-worker is chosen by ``kit/system.py``). It never starts a model call of its own; the
runner and the guard-worker it starts do. Ctrl+C stops the guard-worker cleanly and prints the
command that resumes; rerunning the same command resumes (live.run skips the cases it recorded,
and a config the kit log says is finished is not started again). One JSON line per step goes to
``<run>/kit-log.jsonl``.

Config names are not checked here: ``live.run`` and ``guard_worker`` validate them, so the
scheme of the configs (``scheme.configs_for("v2")``, which the integrator wires in) stays in one
place. Model profiles are whatever ``model_profiles.PROFILES`` holds: the kit has no model list
and no route of its own.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import signal
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench import model_profiles
from evaluation.mailguard_bench.guard_build import NATIVE_CONFIG
from evaluation.mailguard_bench.guard_env import (
    REPO_ROOT,
    GuardEnvError,
    guard_paths_from_env,
    require_pinned_worktree,
)
from evaluation.mailguard_bench.kit.steplog import LOG_NAME, StepLog, finished_configs
from evaluation.mailguard_bench.kit.system import Host, ProcessHandle, SystemHost
from evaluation.mailguard_bench.live import stack_env
from evaluation.mailguard_bench.live.guard_worker import DEFAULT_PORT, HOST, pid_path
from evaluation.mailguard_bench.meaning import reader_model_problems
from evaluation.mailguard_bench.runner import RESULTS_ROOT, meta_path

# The configs of Friday's v2 run. One constant, in the kit only: the integrator replaces it by
# scheme.configs_for("v2") (work package R5) once that module is merged. The kit accepts any
# list (--configs); live.run and guard_worker validate the names.
DEFAULT_V2_CONFIGS = ("C0", "C0T", "C1", "C2", "C3", "C4", "C5", "C6", "C7")

DEFAULT_GW_WAIT_S = 300.0
"""Runbook's GW_WAIT_S: how long a started guard-worker may take to be ready."""
DEFAULT_STACK_WAIT_S = 600.0
DEFAULT_STOP_WAIT_S = 120.0
"""The guard-worker drains its consumers on SIGTERM (15 s by default); past this it is killed."""
GUARD_READY_URL = f"http://{HOST}:{DEFAULT_PORT}/readyz"
PROBE_TIMEOUT_S = 2.0
HEALTH_POLL_S = 2.0
EXPORTED_PREFIXES = ("LLM__", "EMBEDDING__", "RETRIEVAL__", "SUMMARIZATION__", "ROUTING__")
EXPORTED_NAMES = ("BENCH_SUMMARIZER_MODEL",)
INSPECT_FORMAT = (
    "{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
    "|{{.State.ExitCode}}"
)
TRACKED_OUTPUTS = (
    "manifest.json",
    "metrics.csv",
    "summary.json",
    "report.md",
    "analyses.md",
    "analysis",
    "case_manifest.json",
    LOG_NAME,
    "ollama-state.txt",
)
"""What demo-runbook section 9.7 commits, plus the kit log; ``raw/`` stays out of git."""
SECRET_NAME_SUFFIX = "API_KEY"
MIN_SECRET_LENGTH = 8


def _eprint(line: str) -> None:
    print(line, file=sys.stderr)


@dataclass(frozen=True)
class KitContext:
    """What the kit runs on: the machine, the repository, and where it reports."""

    host: Host
    repo_root: Path = REPO_ROOT
    results_root: Path = RESULTS_ROOT
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    python: str = sys.executable
    out: Callable[[str], None] = print
    err: Callable[[str], None] = _eprint
    ready_url: str = GUARD_READY_URL  # the guard-worker's /readyz (its default health port)


@dataclass(frozen=True)
class RunOptions:
    """One ``run``: one model, the configs to run, how long to wait for what."""

    model_profile: str
    run: str
    configs: tuple[str, ...]
    limit: int | None = None
    concurrency: int = 1
    gw_wait_s: float = DEFAULT_GW_WAIT_S
    stack_wait_s: float = DEFAULT_STACK_WAIT_S
    reader: str | None = None
    dry_run: bool = False
    stop_wait_s: float = DEFAULT_STOP_WAIT_S


class _AbortError(Exception):
    """Stop the campaign: the environment is broken, so the next config would fail the same way."""


def _join(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command)) if os.name == "nt" else shlex.join(command)


def _now() -> datetime:
    return datetime.now(UTC)


def module_command(ctx: KitContext, module: str, *args: str) -> list[str]:
    """``python -m module args``: the interpreter running the kit, so the overlay carries over."""
    return [ctx.python, "-m", module, *args]


def resume_commands(options: RunOptions) -> list[str]:
    """The commands that resume this run: the Make target, and the module it runs."""
    configs = ",".join(options.configs)
    make = (
        f"make bench-run MODEL={options.model_profile} RUN={options.run} CONFIGS={configs}"
        + (f" LIMIT={options.limit}" if options.limit is not None else "")
        + f" CONCURRENCY={options.concurrency}"
        + (f" READER={options.reader}" if options.reader else "")
    )
    module = (
        "python -m evaluation.mailguard_bench.kit.campaign run "
        f"--model-profile {options.model_profile} --run {options.run} --configs {configs}"
        + (f" --limit {options.limit}" if options.limit is not None else "")
        + f" --concurrency {options.concurrency}"
        + (f" --reader {options.reader}" if options.reader else "")
    )
    return [make, module]


# --- reports ----------------------------------------------------------------------------------


def report_commands(ctx: KitContext, run_dir: Path, reader: str | None) -> list[list[str]]:
    """Runbook step 7: report, analyses, report; with a reader the meaning column, then again."""
    guard_dir = ctx.environ.get("MAILGUARD_DIR")
    guard = ["--mailguard-dir", guard_dir] if guard_dir else []
    scored = [
        module_command(ctx, "evaluation.mailguard_bench.report", "--run-dir", str(run_dir), *guard),
        module_command(
            ctx, "evaluation.mailguard_bench.analyses", "--run-dir", str(run_dir), *guard
        ),
        module_command(ctx, "evaluation.mailguard_bench.report", "--run-dir", str(run_dir), *guard),
    ]
    if not reader:
        return scored
    meaning = module_command(
        ctx,
        "evaluation.mailguard_bench.meaning",
        "--run-dir",
        str(run_dir),
        "--reader-model",
        reader,
    )
    return [*scored, meaning, *scored]


def reader_problems(reader: str | None, generation_models: Sequence[str] = ()) -> list[str]:
    """Why ``reader`` may not read (ADR-0012 decision 7); empty when it may or is not given."""
    if not reader:
        return []
    meta = {f"run-{i}": {"generation_model": model} for i, model in enumerate(generation_models)}
    return reader_model_problems(reader, meta)


def run_reports(ctx: KitContext, run: str, reader: str | None = None) -> int:
    """``report``: the reports of a finished run. Returns 0, 1 (a step failed) or 2 (refused)."""
    problems = reader_problems(reader)
    if problems:
        for problem in problems:
            ctx.err(f"FAIL {problem}")
        return 2
    run_dir = ctx.results_root / run
    if not run_dir.is_dir():
        ctx.err(f"FAIL {run_dir} does not exist; nothing to report")
        return 2
    return _build_reports(ctx, run_dir, reader, StepLog(run_dir / LOG_NAME), dry=False)


def _build_reports(
    ctx: KitContext, run_dir: Path, reader: str | None, log: StepLog, *, dry: bool
) -> int:
    start = _now()
    status = 0
    for command in report_commands(ctx, run_dir, reader):
        if dry:
            ctx.out(f"would run: {_join(command)}")
            continue
        status = ctx.host.run(command, cwd=ctx.repo_root)
        if status != 0:
            ctx.err(f"FAIL {_join(command)} exited {status}")
            break
    log.append(
        {
            "step": "reports",
            "reader": reader,
            "start": start.isoformat(),
            "end": _now().isoformat(),
            "status": "ok" if status == 0 else "failed",
            "exit_code": status,
        }
    )
    return 1 if status != 0 else 0


# --- the run ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    config: str
    status: str
    exit_code: int | None
    counts: dict[str, int] | None

    @property
    def clean(self) -> bool:
        """Nothing for a retry pass to do: the runner finished and left no error row."""
        return self.status == "ok" and self.counts is not None and self.counts["error"] == 0


class _Campaign:
    def __init__(self, ctx: KitContext, options: RunOptions) -> None:
        self.ctx = ctx
        self.opts = options
        self.run_dir = ctx.results_root / options.run
        self.raw_dir = self.run_dir / "raw"
        self.dry = options.dry_run
        self.recorded = StepLog(self.run_dir / LOG_NAME)
        self.log = StepLog(None if self.dry else self.run_dir / LOG_NAME)

    # -- entry -------------------------------------------------------------------------------

    def execute(self) -> int:
        problems = self._refusals()
        if problems:
            for problem in problems:
                self.ctx.err(f"FAIL {problem}")
            return 2
        self.ctx.out(
            f"run {self.opts.run}: model {self.opts.model_profile}, "
            f"configs {', '.join(self.opts.configs)} (results in {self.run_dir})"
        )
        done = finished_configs(self.recorded.records(), self.opts.model_profile, self.opts.limit)
        pending = [c for c in self.opts.configs if c not in done]
        for config in self.opts.configs:
            if config in done:
                self.ctx.out(f"{config} already finished (see {LOG_NAME}); skipping it")
        try:
            outcomes = self._run_configs(pending) if pending else {}
            failed = [c for c, outcome in outcomes.items() if outcome.status != "ok"]
            if failed:
                self.ctx.err(
                    f"reports were not built: {', '.join(failed)} did not finish. "
                    "Fix the cause and rerun the same command; finished configs are skipped"
                )
                self._print_resume(stream=self.ctx.err)
                return 1
            with_errors = [] if self.dry else [c for c, o in outcomes.items() if not o.clean]
            if with_errors:
                self.ctx.out(
                    f"WARN {', '.join(with_errors)} still have error rows after the retry pass; "
                    "rerun the same command to retry them"
                )
            status = _build_reports(
                self.ctx, self.run_dir, self.opts.reader, self.log, dry=self.dry
            )
            if status == 0 and not self.dry:
                self.ctx.out(
                    f"ok reports in {self.run_dir}. Next model: its own `make bench-run`. "
                    "After the last model: delete .env.stack and run `make up` "
                    "(demo-runbook section 9.9 step 8)"
                )
            return status
        except _AbortError as stop:
            self.ctx.err(f"FAIL {stop}")
            self._print_resume(stream=self.ctx.err)
            return 1
        except KeyboardInterrupt:
            self.ctx.err(
                "interrupted: what the kit started (the guard-worker) was stopped. Resume with:"
            )
            self._print_resume(stream=self.ctx.err)
            return 130

    def _refusals(self) -> list[str]:
        opts, problems = self.opts, []
        try:
            profile = model_profiles.get_profile(opts.model_profile)
        except model_profiles.ModelProfileError as exc:
            return [str(exc)]
        if not opts.configs:
            problems.append("no configs given")
        if len(set(opts.configs)) != len(opts.configs):
            problems.append(f"a config is listed twice: {', '.join(opts.configs)}")
        if opts.concurrency not in (1, 2):
            problems.append("--concurrency must be 1 or 2")
        if opts.limit is not None and opts.limit < 1:
            problems.append("--limit must be at least 1")
        exported = sorted(
            name
            for name in self.ctx.environ
            if name.startswith(EXPORTED_PREFIXES) or name in EXPORTED_NAMES
        )
        if exported:
            problems.append(
                f"{', '.join(exported)} is exported in the shell; docker compose lets the shell "
                "win over every env file, so the containers would not get the settings of "
                "this run. `unset` them (demo-runbook section 9.9 step 1)"
            )
        problems.extend(reader_problems(opts.reader, [profile.model]))
        return problems

    def _print_resume(self, *, stream: Callable[[str], None]) -> None:
        make, module = resume_commands(self.opts)
        stream(f"  {make}")
        stream(f"  {module}")

    # -- the stack ---------------------------------------------------------------------------

    def _run_configs(self, pending: Sequence[str]) -> dict[str, _Outcome]:
        self._bring_up_stack()
        outcomes: dict[str, _Outcome] = {}
        for config in pending:
            outcomes[config] = self._config_step(config, 1)
        retry = [c for c in pending if not outcomes[c].clean]
        if self.dry:
            self.ctx.out(
                "would do: a retry pass over the configs that failed or left error rows "
                "(each one the same commands again)"
            )
        elif retry:
            self.ctx.out(f"retry pass: {', '.join(retry)}")
        for config in retry if not self.dry else []:
            outcomes[config] = self._config_step(config, 2)
        return outcomes

    def _bring_up_stack(self) -> None:
        env_file = self.ctx.repo_root / ".env"
        out = self.ctx.repo_root / ".env.stack"
        if self.dry:
            self.ctx.out(
                f"would do: render {out} for {self.opts.model_profile} "
                "(evaluation.mailguard_bench.live.stack_env, with its refusals; holds API keys)"
            )
            command = stack_env.compose_command([*([env_file] if env_file.is_file() else []), out])
            self.ctx.out(f"would run: {_join(command)}")
            self.ctx.out(
                f"would do: wait until {', '.join(stack_env.APP_SERVICES)} are healthy "
                f"(at most {self.opts.stack_wait_s:g} s)"
            )
            return
        start = _now()
        try:
            plan = stack_env.prepare_stack(
                self.opts.model_profile,
                env_file=env_file,
                out=out,
                process_env=self.ctx.environ,
            )
        except (stack_env.StackEnvError, model_profiles.ModelProfileError) as exc:
            raise _AbortError(str(exc)) from exc
        values = plan.values
        self.ctx.out(
            f"ok stack env for {plan.profile.name} written to {plan.out} "
            f"({len(values)} settings; git-ignored, owner-only, keys not shown)"
        )
        self.ctx.out(
            f"   llm       {plan.profile.model} at {values['LLM__OPENAI_BASE_URL']}"
            f" (timeout {values['LLM__TIMEOUT_S']} s)"
        )
        status = "ok"
        try:
            self._docker(plan.command)
            self._wait_healthy(stack_env.APP_SERVICES, self.opts.stack_wait_s)
            self.ctx.out(f"ok {', '.join(stack_env.APP_SERVICES)} are healthy")
        except BaseException:
            status = "failed"
            raise
        finally:
            self.log.append(
                {
                    "step": "stack",
                    "model_profile": self.opts.model_profile,
                    "start": start.isoformat(),
                    "end": _now().isoformat(),
                    "status": status,
                }
            )

    def _docker(self, command: Sequence[str]) -> None:
        if self.dry:
            self.ctx.out(f"would run: {_join(command)}")
            return
        code = self.ctx.host.run(command, cwd=self.ctx.repo_root)
        if code != 0:
            raise _AbortError(f"{_join(command)} exited {code}")

    def _wait_healthy(self, services: Sequence[str], timeout_s: float) -> None:
        host = self.ctx.host
        deadline = host.monotonic() + timeout_s
        while True:
            waiting = _container_problems(self.ctx, services)
            if not waiting:
                return
            if host.monotonic() >= deadline:
                raise _AbortError(
                    f"{', '.join(services)} not healthy after {timeout_s:g} s "
                    f"({'; '.join(waiting)}); see `docker compose ps` and `docker compose logs`"
                )
            host.sleep(HEALTH_POLL_S)

    # -- one config --------------------------------------------------------------------------

    def _config_step(self, config: str, pass_no: int) -> _Outcome:
        opts = self.opts
        start, began = _now(), self.ctx.host.monotonic() if not self.dry else 0.0
        status, exit_code, reason = "failed", None, None
        try:
            exit_code = self._drive(config)
            status = "ok" if exit_code == 0 else "failed"
            if exit_code != 0:
                reason = f"live.run exited {exit_code}"
                self.ctx.err(f"FAIL {config}: live.run exited {exit_code}")
        except KeyboardInterrupt:
            status, reason = "interrupted", "Ctrl+C"
            raise
        except BaseException as exc:
            reason = str(exc) or type(exc).__name__
            raise
        finally:
            counts = None if self.dry else _read_counts(self.ctx, opts.run, config, start)
            self.log.append(
                {
                    "step": "config",
                    "config": config,
                    "pass": pass_no,
                    "run": opts.run,
                    "model_profile": opts.model_profile,
                    "limit": opts.limit,
                    "concurrency": opts.concurrency,
                    "start": start.isoformat(),
                    "end": _now().isoformat(),
                    "seconds": round(self.ctx.host.monotonic() - began, 3) if not self.dry else 0,
                    "status": status,
                    "exit_code": exit_code,
                    "reason": reason,
                    "counts": counts,
                }
            )
        return _Outcome(config, status, exit_code, counts)

    def _drive(self, config: str) -> int:
        """The drafting-consumer switch and the run of one config (runbook ``run_config``)."""
        if config == NATIVE_CONFIG:
            self._docker(["docker", "compose", "start", "ai-worker"])
            if self.dry:
                self.ctx.out("would do: wait until the ai-worker container is healthy")
            else:
                self._wait_healthy(("ai-worker",), self.opts.stack_wait_s)
            return self._run_runner(config)
        self._docker(["docker", "compose", "stop", "ai-worker"])
        return self._run_guarded(config)

    def _runner_command(self, config: str) -> list[str]:
        opts = self.opts
        return module_command(
            self.ctx,
            "evaluation.mailguard_bench.live.run",
            "--config",
            config,
            "--run",
            opts.run,
            "--model-profile",
            opts.model_profile,
            "--retry-errors",
            "--concurrency",
            str(opts.concurrency),
            *(["--limit", str(opts.limit)] if opts.limit is not None else []),
        )

    def _worker_command(self, config: str) -> list[str]:
        return module_command(
            self.ctx,
            "evaluation.mailguard_bench.live.guard_worker",
            "--config",
            config,
            "--run",
            self.opts.run,
            "--model-profile",
            self.opts.model_profile,
        )

    def _run_runner(self, config: str) -> int:
        command = self._runner_command(config)
        if self.dry:
            self.ctx.out(f"would run: {_join(command)}")
            return 0
        return self.ctx.host.run(command, cwd=self.ctx.repo_root)

    def _shown(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.ctx.repo_root))
        except ValueError:
            return str(path)

    def _run_guarded(self, config: str) -> int:
        pid = pid_path(self.run_dir, config)
        log_path = self.raw_dir / f"guard-worker.{config}.log"
        worker = self._worker_command(config)
        if self.dry:
            self.ctx.out(f"would run: {_join(worker)}")
            self.ctx.out(f"would do: append its output to {self._shown(log_path)}")
            self.ctx.out(
                f"would do: wait until {self._shown(pid)} is newer than the start AND "
                f"{self.ctx.ready_url} answers (at most {self.opts.gw_wait_s:g} s)"
            )
            code = self._run_runner(config)
            self.ctx.out(
                "would do: stop the guard-worker (SIGTERM; CTRL_BREAK_EVENT on Windows), "
                "confirm it exited, then remove its pid file"
            )
            return code
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.raw_dir / f".kit-stamp.{config}"
        stamp.write_text("", encoding="utf-8")  # its mtime is "the start": a pid file must be newer
        proc = self.ctx.host.spawn(worker, cwd=self.ctx.repo_root, log_path=log_path)
        try:
            self._await_ready(config, proc, pid, stamp, log_path)
            code = self._run_runner(config)
        except BaseException:
            problem = self._stop_worker(config, proc, pid, stamp)
            if problem:
                self.ctx.err(f"FAIL {problem}")  # the error that got us here is re-raised
            raise
        problem = self._stop_worker(config, proc, pid, stamp)
        if problem:
            raise _AbortError(problem)
        return code

    def _await_ready(
        self, config: str, proc: ProcessHandle, pid: Path, stamp: Path, log_path: Path
    ) -> None:
        """Ready = a pid file newer than this start AND /readyz answering.

        The guard-worker writes the pid file first and starts its consumers afterwards, /readyz
        answers only once they run, and the runner fails a config at once when a lane queue has
        no consumer (runbook section 9.9 step 4).
        """
        host = self.ctx.host
        deadline = host.monotonic() + self.opts.gw_wait_s
        while True:
            if _fresh_pid(pid, stamp) is not None and host.http_ok(
                self.ctx.ready_url, PROBE_TIMEOUT_S
            ):
                self.ctx.out(f"ok guard-worker {config} is ready")
                return
            if proc.poll() is not None:
                raise _AbortError(f"guard-worker {config} exited; see {self._shown(log_path)}")
            if host.monotonic() >= deadline:
                raise _AbortError(
                    f"guard-worker {config} not ready after {self.opts.gw_wait_s:g} s; "
                    f"see {self._shown(log_path)}"
                )
            host.sleep(1.0)

    def _stop_worker(self, config: str, proc: ProcessHandle, pid: Path, stamp: Path) -> str | None:
        """Stop the guard-worker and confirm it exited; the problem text when it did not.

        The pid file is removed only after the process is confirmed gone, and only when it is
        this worker's (newer than the stamp). Windows ends the worker on CTRL_BREAK_EVENT before
        its own ``finally`` removes the file, so a stale file is expected there.
        """
        if proc.poll() is None:
            proc.request_stop(_fresh_pid(pid, stamp))
            try:
                proc.wait(self.opts.stop_wait_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    return (
                        f"guard-worker {config} (pid {proc.pid}) did not exit after a stop "
                        f"request and a kill; its pid file {pid} is left in place. Stop the "
                        "process by hand before any other run: two consumers split the lanes"
                    )
                self.ctx.err(
                    f"WARN guard-worker {config} did not stop within "
                    f"{self.opts.stop_wait_s:g} s and was killed"
                )
        if _fresh_pid(pid, stamp) is not None:
            pid.unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
        return None


def _fresh_pid(pid: Path, stamp: Path) -> int | None:
    """The pid in ``pid`` when the file is at least as new as ``stamp`` (written by this start)."""
    try:
        if pid.stat().st_mtime_ns < stamp.stat().st_mtime_ns:
            return None
        return int(pid.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _container_problems(ctx: KitContext, services: Sequence[str]) -> list[str]:
    """What keeps the containers from being ready (empty when they are).

    Ready is healthy; running when there is no health check; or exited with code 0 for a
    one-shot container (the compose file's ``init``). Asked of the named services, or of the
    whole project when none is named.
    """
    ids = ctx.host.capture(["docker", "compose", "ps", "-a", "-q", *services], cwd=ctx.repo_root)
    if ids.returncode != 0:
        return [f"docker compose ps exited {ids.returncode}"]
    found = [line.strip() for line in ids.stdout.splitlines() if line.strip()]
    if not found:
        return ["no container exists yet"]
    if services and len(found) < len(services):
        return [f"only {len(found)} of {len(services)} containers exist"]
    states = ctx.host.capture(
        ["docker", "inspect", "-f", INSPECT_FORMAT, *found], cwd=ctx.repo_root
    )
    if states.returncode != 0:
        return [f"docker inspect exited {states.returncode}"]
    waiting = []
    for line in states.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        name, status, health, exit_code = parts
        ready = (
            health == "healthy"
            or (health == "" and status == "running")
            or (health == "" and status == "exited" and exit_code == "0")
        )
        if not ready:
            waiting.append(f"{name.lstrip('/')} is {health or status}")
    return waiting


def _read_counts(ctx: KitContext, run: str, config: str, since: datetime) -> dict[str, int] | None:
    """What live.run wrote about its start at or after ``since``: the counts of that start.

    None when it wrote none (a refusal before its first write, a crash): an earlier start's
    summary is never taken for this one's.
    """
    try:
        meta = json.loads(meta_path(ctx.results_root / run, config).read_text(encoding="utf-8"))
        last = meta["invocations"][-1]
        if datetime.fromisoformat(last["started_at"]) < since:
            return None
        summary = last["summary"]
        return {
            "selected": int(summary["selected"]),
            "ok": int(summary["ok"]),
            "error": int(summary["error"]),
            "skipped_already_recorded": int(summary["skipped_already_recorded"]),
        }
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def run_campaign(ctx: KitContext, options: RunOptions) -> int:
    """``run``: one model through every config, the retry pass and the reports.

    Returns 0 (done), 1 (a config or a step failed), 2 (refused before anything ran) or 130
    (Ctrl+C: the guard-worker was stopped and the resume command printed).
    """
    return _Campaign(ctx, options).execute()


# --- setup ------------------------------------------------------------------------------------


def run_setup(ctx: KitContext) -> int:
    """``setup``: once per machine, after ``make bench-doctor`` passes.

    The cheap checks first (the guard worktree, the pinned inputs, the offline guard smoke), the
    slow one last: ``docker compose up -d --build`` and the wait for every container.
    """
    try:
        paths = guard_paths_from_env(ctx.environ)
        info = require_pinned_worktree(paths.root, paths.commit)
    except GuardEnvError as exc:
        ctx.err(f"FAIL {exc}")
        return 1
    ctx.out(f"ok guard worktree {info.path} @ {info.commit} (clean)")
    try:
        pinned = importlib.import_module("evaluation.mailguard_bench.kit.pinned")
    except ImportError:
        ctx.err("FAIL the pinned-inputs check (evaluation.mailguard_bench.kit.pinned) is missing")
        return 1
    problems = list(pinned.verify(ctx.repo_root))
    if problems:
        for problem in problems:
            ctx.err(f"FAIL {problem}")
        return 1
    ctx.out("ok pinned inputs (cases, L1 classifier) match their checksums")
    smoke = module_command(ctx, "evaluation.mailguard_bench.guard_smoke")
    if ctx.host.run(smoke, cwd=ctx.repo_root) != 0:
        ctx.err("FAIL the offline guard smoke failed (output above)")
        return 1
    up = ["docker", "compose", "up", "-d", "--build"]
    if ctx.host.run(up, cwd=ctx.repo_root) != 0:
        ctx.err(f"FAIL {_join(up)} failed (output above)")
        return 1
    deadline = ctx.host.monotonic() + DEFAULT_STACK_WAIT_S
    while True:
        waiting = _container_problems(ctx, ())
        if not waiting:
            break
        if ctx.host.monotonic() >= deadline:
            ctx.err(
                f"FAIL the stack is not healthy after {DEFAULT_STACK_WAIT_S:g} s "
                f"({'; '.join(waiting)}); see `docker compose ps`"
            )
            return 1
        ctx.host.sleep(HEALTH_POLL_S)
    ctx.out("ok the stack is up and healthy. Next: make bench-run MODEL=<profile>")
    return 0


# --- package ----------------------------------------------------------------------------------


def _is_env_file(path: Path) -> bool:
    name = path.name
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _secret_values(ctx: KitContext) -> dict[str, str]:
    environ = model_profiles.with_dot_env(ctx.environ, ctx.repo_root / ".env")
    return {
        name: value.strip()
        for name, value in environ.items()
        if name.upper().endswith(SECRET_NAME_SUFFIX) and len(value.strip()) >= MIN_SECRET_LENGTH
    }


def run_package(ctx: KitContext, run: str, out: Path | None = None) -> int:
    """``package``: ``bench-results-<run>.zip`` of the run folder, raw/ included, never a key.

    Refuses to write the zip when a key of .env or the environment appears in any file: the
    zip leaves this machine.
    """
    run_dir = ctx.results_root / run
    if not run_dir.is_dir():
        ctx.err(f"FAIL {run_dir} does not exist; nothing to package")
        return 1
    target = out or ctx.repo_root / f"bench-results-{run}.zip"
    files = [p for p in sorted(run_dir.rglob("*")) if p.is_file()]
    left_out = [p for p in files if _is_env_file(p) or p.name.startswith(".kit-stamp.")]
    keep = [p for p in files if p not in left_out and p != target]
    secrets = _secret_values(ctx)
    needles = [value.encode("utf-8") for value in secrets.values()]

    def leaks_a_key(path: Path) -> bool:
        data = path.read_bytes()  # once per file, not once per key
        return any(needle in data for needle in needles)

    leaks = sorted(str(p.relative_to(run_dir)) for p in keep if needles and leaks_a_key(p))
    if leaks:
        ctx.err(
            f"FAIL a key from .env or the environment appears in: {', '.join(leaks)}. "
            "Nothing was packaged; remove it from those files first"
        )
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in keep:
            archive.write(path, arcname=f"{run}/{path.relative_to(run_dir).as_posix()}")
    ctx.out(
        f"ok {target} ({len(keep)} files from {run_dir}, raw/ included"
        + (f"; {len(left_out)} env or stamp files left out" if left_out else "")
        + ")"
    )
    tracked = [
        (run_dir / name).relative_to(ctx.repo_root).as_posix()
        for name in TRACKED_OUTPUTS
        if (run_dir / name).exists()
    ]
    ctx.out("Send the zip to the owner. To commit the tracked outputs to a branch, as our runs do:")
    ctx.out(f"  git switch -c bench/{run}")
    ctx.out(f"  git add {' '.join(tracked)}")
    ctx.out(f'  git commit -m "docs(eval): benchmark results, run {run} [task 7.23] [R22.12]"')
    ctx.out(f"  git push -u origin bench/{run}")
    ctx.out("(raw/ holds the full attack emails and drafts; it stays out of git, in the zip only)")
    return 0


# --- the command line -------------------------------------------------------------------------


def _config_list(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in text.split(",") if item.strip())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="one model through every config, the retry pass, the reports")
    run.add_argument(
        "--model-profile",
        required=True,
        choices=sorted(model_profiles.PROFILES),
        help="the benchmarked model (model_profiles.py); the kit holds no model list",
    )
    run.add_argument("--run", required=True, help="results go to results/mailguard_bench/<run>")
    run.add_argument(
        "--configs",
        type=_config_list,
        default=DEFAULT_V2_CONFIGS,
        help="comma-separated, run in this order (default: " + ",".join(DEFAULT_V2_CONFIGS) + ")",
    )
    run.add_argument("--limit", type=int, default=None, help="first N cases of each config (smoke)")
    run.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    run.add_argument(
        "--gw-wait-s",
        type=float,
        default=DEFAULT_GW_WAIT_S,
        help="seconds a started guard-worker may take to be ready (default: %(default)g)",
    )
    run.add_argument(
        "--stack-wait-s",
        type=float,
        default=DEFAULT_STACK_WAIT_S,
        help="seconds the containers may take to be healthy (default: %(default)g)",
    )
    run.add_argument(
        "--reader",
        default=None,
        help="the meaning column's reader model; never a benchmarked model (ADR-0012 decision 7)",
    )
    run.add_argument(
        "--dry-run", action="store_true", help="print every command; run nothing, needs no docker"
    )

    sub.add_parser("setup", help="once per machine: guard, pinned inputs, guard smoke, the stack")

    report = sub.add_parser("report", help="the reports of a finished run")
    report.add_argument("--run", required=True)
    report.add_argument("--reader", default=None, help="add the meaning column with this reader")

    package = sub.add_parser("package", help="bench-results-<run>.zip, raw/ included, no keys")
    package.add_argument("--run", required=True)
    package.add_argument("--out", type=Path, default=None, help="where to write the zip")
    return parser.parse_args(argv)


@contextmanager
def _terminate_as_interrupt() -> Iterator[None]:
    """SIGTERM and SIGHUP (a closed terminal) stop the kit like Ctrl+C: the worker is stopped."""

    def raise_interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, raise_interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = KitContext(host=SystemHost())
    with _terminate_as_interrupt():
        try:
            if args.command == "run":
                return run_campaign(
                    ctx,
                    RunOptions(
                        model_profile=args.model_profile,
                        run=args.run,
                        configs=tuple(args.configs),
                        limit=args.limit,
                        concurrency=args.concurrency,
                        gw_wait_s=args.gw_wait_s,
                        stack_wait_s=args.stack_wait_s,
                        reader=args.reader,
                        dry_run=args.dry_run,
                    ),
                )
            if args.command == "setup":
                return run_setup(ctx)
            if args.command == "report":
                return run_reports(ctx, args.run, args.reader)
            return run_package(ctx, args.run, args.out)
        except KeyboardInterrupt:
            ctx.err("interrupted")
            return 130


if __name__ == "__main__":
    sys.exit(main())

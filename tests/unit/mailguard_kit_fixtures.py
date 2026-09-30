"""Shared fakes of the kit's tests: the machine, the repository, a finished runner (task 7.23).

``FakeHost`` replaces docker, the guard-worker process, the runner and the HTTP probe with
scripted, recorded stand-ins and a fake clock; everything the orchestrator decides stays real.
No docker, no model call, no network, no live key.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.kit.campaign import KitContext, RunOptions
from evaluation.mailguard_bench.kit.system import CommandResult

GEMINI_KEY = "gemini-key-000"
OPENAI_KEY = "sk-openai-000"
HOST_ENV = {
    "BENCH_OPENAI_API_KEY": OPENAI_KEY,
    "LLM__OPENAI_API_KEY": GEMINI_KEY,
    "EMBEDDING__MOCK": "false",
    "EMBEDDING__MODEL_NAME": "gemini-embedding-001",
    "EMBEDDING__DIMENSION": "1536",
    "EMBEDDING__BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
    "EMBEDDING__API_KEY": GEMINI_KEY,
    "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "3000",
    "LLM__TIMEOUT_S": "60",
}
RUN = "r1"


# --- the fake machine -------------------------------------------------------------------------


@dataclass
class WorkerMode:
    start: str = "ready"  # ready | crash | never-ready | late
    stop: str = "graceful"  # graceful | leaves-pid | stuck | unkillable
    ready_after_s: float = 5.0
    interrupt_wait: bool = False  # a second Ctrl+C / SIGTERM arrives while the drain is awaited


class FakeProcess:
    def __init__(self, host: FakeHost, command: list[str], pid_file: Path, mode: WorkerMode):
        self.host = host
        self.command = command
        self.pid_file = pid_file
        self.mode = mode
        self.pid = 7000 + len(host.processes)
        self.exit_code: int | None = None
        self.spawned_at = host.now
        self.pid_written = False
        if mode.start == "crash":
            self.exit_code = 1
        elif mode.start in ("ready", "never-ready"):  # "no-pid" writes none
            self._write_pid()

    def _write_pid(self) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(f"{self.pid}\n", encoding="utf-8")
        self.pid_written = True

    def tick(self) -> None:
        late = self.mode.start == "late"
        if late and not self.pid_written and self.host.now >= self.spawned_at + 2:
            self._write_pid()

    def ready(self) -> bool:
        if self.exit_code is not None or self.mode.start in ("crash", "never-ready"):
            return False  # "no-pid" answers /readyz without ever writing its pid file
        if self.mode.start == "late":
            return self.host.now >= self.spawned_at + self.mode.ready_after_s
        return True

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float) -> int:
        if self.exit_code is None and self.mode.interrupt_wait:
            self.mode.interrupt_wait = False
            raise KeyboardInterrupt
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("worker", timeout)
        return self.exit_code

    def request_stop(self, worker_pid: int | None = None) -> None:
        self.host.events.append(("stop", {"pid": self.pid, "worker_pid": worker_pid}))
        if self.mode.stop == "graceful":
            self.exit_code = 0
            self.pid_file.unlink(missing_ok=True)
        elif self.mode.stop == "leaves-pid":
            self.exit_code = 0  # Windows: CTRL_BREAK ends it without its finally block

    def kill(self) -> None:
        self.host.events.append(("kill", {"pid": self.pid}))
        if self.mode.stop != "unkillable":
            self.exit_code = -9


class FakeHost:
    os_name = "posix"

    def __init__(self, results_root: Path) -> None:
        self.results_root = results_root
        self.now = 1000.0
        self.events: list[tuple[str, Any]] = []
        self.processes: list[FakeProcess] = []
        self.modes: dict[str, WorkerMode] = {}
        self.run_hook: Callable[[list[str]], int | None] = lambda command: None
        self.health: Callable[[list[str]], list[str] | None] = lambda ids: None
        self.exits: dict[str, int] = {}  # label -> exit code

    # commands ------------------------------------------------------------------------------
    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        cmd = list(command)
        self.events.append(("run", cmd))
        hooked = self.run_hook(cmd)
        if hooked is not None:
            return hooked
        return self.exits.get(label(cmd), 0)

    def capture(self, command: Sequence[str], *, cwd: Path) -> CommandResult:
        cmd = list(command)
        self.events.append(("capture", cmd))
        if cmd[:3] == ["docker", "compose", "ps"]:
            services = [a for a in cmd[3:] if not a.startswith("-")] or [
                "api",
                "init",
                "postgres",
            ]
            return CommandResult(0, "".join(f"id-{s}\n" for s in services))
        if cmd[:2] == ["docker", "inspect"]:
            ids = cmd[4:]
            custom = self.health(ids)
            lines = custom if custom is not None else [f"{i}|running|healthy|0" for i in ids]
            return CommandResult(0, "\n".join(lines) + "\n")
        return CommandResult(0, "")

    def spawn(self, command: Sequence[str], *, cwd: Path, log_path: Path) -> FakeProcess:
        cmd = list(command)
        self.events.append(("spawn", {"command": cmd, "log": log_path}))
        config = cmd[cmd.index("--config") + 1]
        run = cmd[cmd.index("--run") + 1]
        pid_file = self.results_root / run / "raw" / f"guard_worker.{config}.pid"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake worker output\n", encoding="utf-8")
        proc = FakeProcess(self, cmd, pid_file, self.modes.get(config, WorkerMode()))
        self.processes.append(proc)
        return proc

    def http_ok(self, url: str, timeout_s: float) -> bool:
        self.events.append(("http", url))
        return any(p.ready() for p in self.processes[-1:])

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for proc in self.processes:
            proc.tick()

    def monotonic(self) -> float:
        return self.now


def label(cmd: list[str]) -> str:
    if cmd[0] == "docker":
        if "up" in cmd:  # after the --env-file pairs
            return "docker compose up"
        return " ".join(cmd[:4])
    if "-m" in cmd:
        module = cmd[cmd.index("-m") + 1].removeprefix("evaluation.mailguard_bench.")
        module = module.replace("live.guard_worker", "guard_worker")  # live.run stays live.run
        if "--config" in cmd:
            return f"{module} {cmd[cmd.index('--config') + 1]}"
        return module
    return " ".join(cmd)


def sequence(host: FakeHost) -> list[str]:
    """What was run, started and stopped, in order (captures and probes left out)."""
    out: list[str] = []
    for kind, payload in host.events:
        if kind == "run":
            out.append(label(payload))
        elif kind == "spawn":
            out.append("spawn " + label(payload["command"]))
        elif kind in ("stop", "kill"):
            out.append(kind)
    return out


def write_meta(
    results_root: Path,
    run: str,
    config: str,
    *,
    selected: int = 4,
    ok: int = 4,
    error: int = 0,
    skipped: int = 0,
) -> None:
    """What live.run leaves in raw/<config>.meta.json after a finished invocation."""
    path = results_root / run / "raw" / f"{config}.meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"invocations": []}
    meta.setdefault("scheme", "v2")  # what live.run records under its default scheme (a kit run)
    meta["invocations"].append(
        {
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "summary": {
                "selected": selected,
                "skipped_already_recorded": skipped,
                "ok": ok,
                "error": error,
                "torn_lines_skipped": 0,
                "cleanup_failures": 0,
            },
        }
    )
    path.write_text(json.dumps(meta), encoding="utf-8")


@dataclass
class Bench:
    ctx: KitContext
    host: FakeHost
    repo: Path
    results_root: Path
    out: list[str] = field(default_factory=list)
    err: list[str] = field(default_factory=list)

    def runner_outcomes(self, *outcomes: dict[str, int]) -> None:
        """Make the k-th live.run of the test write ``outcomes[k]`` as its result."""
        queue = list(outcomes)

        def hook(cmd: list[str]) -> int | None:
            if "live.run" in " ".join(cmd):
                config = cmd[cmd.index("--config") + 1]
                outcome = queue.pop(0) if queue else {}
                write_meta(self.results_root, cmd[cmd.index("--run") + 1], config, **outcome)
            return None

        self.host.run_hook = hook

    def kit_log(self, run: str = RUN) -> list[dict[str, Any]]:
        path = self.results_root / run / "kit-log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(name="bench")
def bench_fixture(tmp_path: Path) -> Bench:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / ".gitignore").write_text(".env.stack\n", encoding="utf-8")
    (repo / ".env").write_text("".join(f"{k}={v}\n" for k, v in HOST_ENV.items()), "utf-8")
    results_root = repo / "evaluation" / "results" / "mailguard_bench"
    host = FakeHost(results_root)
    out: list[str] = []
    err: list[str] = []
    ctx = KitContext(
        host=host,
        repo_root=repo,
        results_root=results_root,
        environ={"MAILGUARD_DIR": "/guard"},
        python="py",
        out=out.append,
        err=err.append,
    )
    return Bench(ctx=ctx, host=host, repo=repo, results_root=results_root, out=out, err=err)


def opts(**changes: Any) -> RunOptions:
    base: dict[str, Any] = {
        "model_profile": "gpt-4o-mini",
        "run": RUN,
        "configs": ("C0", "C3"),
        "limit": None,
        "concurrency": 1,
        "gw_wait_s": 300.0,
        "stack_wait_s": 600.0,
        "dry_run": False,
    }
    return RunOptions(**{**base, **changes})


def pid_file(bench: Bench, config: str, run: str = RUN) -> Path:
    return bench.results_root / run / "raw" / f"guard_worker.{config}.pid"


STACK_UP = "docker compose up"
# A kit run is scheme v2: the report alone, since the no-API analyses read C3 as the full guard
# (C7 in v2) and refuse a v2 folder until task 7.23. A v1 folder keeps report, analyses, report.
REPORTS = ["report"]
REPORTS_V1 = ["report", "analyses", "report"]

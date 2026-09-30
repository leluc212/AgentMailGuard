"""The kit over real processes: a stand-in `python`, a stand-in `docker`, real signals (task 7.23).

``FakeHost`` tests prove what the orchestrator decides; this proves the wiring: the real
``SystemHost`` starts a real guard-worker stand-in in its own session, the readiness rule reads a
real pid file's mtime against the kit's start stamp and probes a real HTTP server, the runner
stand-in fails unless it finds the worker ready when it starts, and the stop is a real SIGTERM
that the kit confirms before it removes the pid file. The stand-ins replace the services
(docker, the guard-worker, live.run); nothing else is replaced. POSIX only (the Windows stop is
driven through injection in test_mailguard_kit_system.py). No model call, no network beyond
127.0.0.1, no live key.
"""

from __future__ import annotations

import json
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evaluation.mailguard_bench.kit.campaign import KitContext, RunOptions, run_campaign
from evaluation.mailguard_bench.kit.system import SystemHost
from tests.unit.mailguard_kit_fixtures import HOST_ENV

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and shebangs")

FAKE_PYTHON = """#!{python}
import http.server, json, os, signal, sys, threading, time, urllib.request
from datetime import UTC, datetime
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "-m", args
module = args[1].removeprefix("evaluation.mailguard_bench.")
flags, i = {{}}, 2
while i < len(args):
    if args[i] in ("--retry-errors", "--dry-run"):
        flags[args[i]] = True
        i += 1
    else:
        flags[args[i]] = args[i + 1]
        i += 2
root = Path(os.environ["FAKE_RESULTS_ROOT"])
calls = Path(os.environ["FAKE_CALLS"])
port = int(os.environ["FAKE_READY_PORT"])
config = flags.get("--config", "")


def note(text):
    with calls.open("a") as handle:
        handle.write(text + "\\n")


if module == "live.guard_worker":
    raw = root / flags["--run"] / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    pid_file = raw / f"guard_worker.{{config}}.pid"
    stop = threading.Event()

    def on_term(*_):
        pid_file.unlink(missing_ok=True)  # what the real worker's finally block does
        note(f"worker {{config}} stopped")
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    time.sleep(0.3)
    pid_file.write_text(f"{{os.getpid()}}\\n")  # first the pid file...
    time.sleep(1.2)  # ...and only later the consumers and /readyz
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    note(f"worker {{config}} ready")
    stop.wait()
    server.shutdown()
elif module == "live.run":
    assert "--retry-errors" in flags, "the runner must be told to retry error rows"
    if config != "C0":
        pid_file = root / flags["--run"] / "raw" / f"guard_worker.{{config}}.pid"
        if not pid_file.exists():
            note(f"run {{config}} FAILED: no guard-worker")
            sys.exit(3)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{{port}}/readyz", timeout=2)
        except OSError:
            note(f"run {{config}} FAILED: not ready")
            sys.exit(4)
    meta_path = root / flags["--run"] / "raw" / f"{{config}}.meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {{"invocations": []}}
    meta["invocations"].append({{
        "started_at": datetime.now(UTC).isoformat(),
        "summary": {{"selected": 3, "skipped_already_recorded": 0, "ok": 3, "error": 0}},
    }})
    meta_path.write_text(json.dumps(meta))
    note(f"run {{config}} ok")
else:
    note(f"module {{module}}")
"""

FAKE_DOCKER = """#!{python}
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as handle:
    handle.write("docker " + " ".join(a for a in args if not a.startswith("/")) + "\\n")
if args[:2] == ["compose", "ps"]:
    print("\\n".join(f"id-{{s}}" for s in args[2:] if not s.startswith("-")))
elif args[0] == "inspect":
    print("\\n".join(f"/{{i}}|running|healthy|0" for i in args[3:]))
"""


def _executable(path: Path, text: str) -> Path:
    path.write_text(text.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_one_guarded_and_one_native_config_over_real_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / ".gitignore").write_text(".env.stack\n", encoding="utf-8")
    (repo / ".env").write_text("".join(f"{k}={v}\n" for k, v in HOST_ENV.items()), "utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "docker", FAKE_DOCKER)
    fake_python = _executable(tmp_path / "fakepy", FAKE_PYTHON)
    results_root = repo / "evaluation" / "results" / "mailguard_bench"
    calls = tmp_path / "calls.log"
    port = _free_port()
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_RESULTS_ROOT", str(results_root))
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    monkeypatch.setenv("FAKE_READY_PORT", str(port))
    out: list[str] = []
    err: list[str] = []
    ctx = KitContext(
        host=SystemHost(),
        repo_root=repo,
        results_root=results_root,
        environ={},
        python=str(fake_python),
        out=out.append,
        err=err.append,
        ready_url=f"http://127.0.0.1:{port}/readyz",
    )
    options = RunOptions(
        model_profile="gpt-4o-mini",
        run="real-1",
        configs=("C0", "C3"),
        gw_wait_s=30,
        stack_wait_s=30,
        stop_wait_s=30,
    )

    assert run_campaign(ctx, options) == 0, err

    lines = calls.read_text(encoding="utf-8").splitlines()
    waits = ("docker compose ps", "docker inspect")  # the health polls, however many there were
    assert [ln for ln in lines if not ln.startswith(waits)] == [
        next(ln for ln in lines if " up -d --no-deps " in ln),
        "docker compose start ai-worker",
        "run C0 ok",
        "docker compose stop ai-worker",
        "worker C3 ready",
        "run C3 ok",  # only after the worker was ready: the stand-in runner refuses otherwise
        "worker C3 stopped",  # a real SIGTERM, stopped before the reports
        "module report",
        "module analyses",
        "module report",
    ]
    assert not (results_root / "real-1" / "raw" / "guard_worker.C3.pid").exists()
    assert not list((results_root / "real-1" / "raw").glob(".kit-stamp.*"))
    assert (results_root / "real-1" / "raw" / "guard-worker.C3.log").is_file()
    steps = [
        json.loads(line)
        for line in (results_root / "real-1" / "kit-log.jsonl").read_text("utf-8").splitlines()
    ]
    configs = [s for s in steps if s["step"] == "config"]
    assert [(s["config"], s["status"], s["counts"]["ok"]) for s in configs] == [
        ("C0", "ok", 3),
        ("C3", "ok", 3),
    ]
    # Run again: everything is finished, so no docker, no worker, no runner; only the reports.
    calls.write_text("", encoding="utf-8")
    assert run_campaign(ctx, options) == 0, err
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "module report",
        "module analyses",
        "module report",
    ]

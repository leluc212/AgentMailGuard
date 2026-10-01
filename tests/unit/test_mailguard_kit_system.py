"""The kit's boundary to the machine: commands, the guard-worker process, HTTP (task 7.23).

Real child processes and a real local HTTP server where the behaviour is the point; the
Windows branches are driven through the injection points of ``SystemHost``.
"""

from __future__ import annotations

import http.server
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.kit.system import (
    CREATE_NEW_PROCESS_GROUP,
    CTRL_BREAK_EVENT,
    SystemHost,
)


class FakePopen:
    instances: list[FakePopen] = []

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 4000 + len(FakePopen.instances)
        self.killed = False
        self.terminated = False
        self.waits: list[float | None] = []
        self.wait_script: list[BaseException | int] = []
        FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        step = self.wait_script.pop(0) if self.wait_script else 0
        if isinstance(step, BaseException):
            raise step
        return step

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture(autouse=True)
def _reset_fake_popen() -> None:
    FakePopen.instances.clear()


def _kills() -> tuple[list[tuple[int, int]], Any]:
    seen: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        seen.append((pid, sig))

    return seen, kill


def test_the_guard_worker_starts_in_its_own_session_on_posix(tmp_path: Path) -> None:
    host = SystemHost(os_name="posix", popen=FakePopen)
    host.spawn(["py", "-m", "w"], cwd=tmp_path, log_path=tmp_path / "raw" / "w.log")
    kwargs = FakePopen.instances[0].kwargs
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert (tmp_path / "raw" / "w.log").is_file()  # the log's folder is made


def test_the_guard_worker_starts_in_its_own_process_group_on_windows(tmp_path: Path) -> None:
    host = SystemHost(os_name="nt", popen=FakePopen)
    host.spawn(["py", "-m", "w"], cwd=tmp_path, log_path=tmp_path / "w.log")
    kwargs = FakePopen.instances[0].kwargs
    assert kwargs["creationflags"] == CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in kwargs


def test_the_platform_is_read_from_os_name_so_windows_takes_the_windows_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(os, "name", "nt")
        host = SystemHost(popen=FakePopen)  # no os_name given: it is os.name at construction
    assert host.os_name == "nt"
    host.spawn(["py"], cwd=tmp_path, log_path=tmp_path / "w.log")
    assert FakePopen.instances[0].kwargs["creationflags"] == CREATE_NEW_PROCESS_GROUP
    assert SystemHost().os_name == os.name


def test_the_log_is_appended_with_a_header_so_a_retry_does_not_erase_the_first_pass(
    tmp_path: Path,
) -> None:
    log = tmp_path / "w.log"
    log.write_text("first pass output\n", encoding="utf-8")
    host = SystemHost(os_name="posix", popen=FakePopen)
    host.spawn(["py"], cwd=tmp_path, log_path=log)
    text = log.read_text(encoding="utf-8")
    assert text.startswith("first pass output\n")
    assert "kit: starting" in text


def test_the_extra_environment_is_merged_over_the_inherited_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIT_SPAWN_INHERITED", "from-the-shell")
    host = SystemHost(os_name="posix", popen=FakePopen)
    host.spawn(
        ["py", "-m", "w"],
        cwd=tmp_path,
        log_path=tmp_path / "w.log",
        env={"RETRIEVAL__RERANK_MODEL_DIR": "/models"},
    )
    env = FakePopen.instances[0].kwargs["env"]
    assert env["RETRIEVAL__RERANK_MODEL_DIR"] == "/models"
    assert env["KIT_SPAWN_INHERITED"] == "from-the-shell"  # merged over it, not replacing it


def test_the_extra_environment_is_that_child_s_alone_and_a_command_still_inherits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RETRIEVAL__RERANK_MODEL_DIR", raising=False)
    host = SystemHost(os_name="posix", popen=FakePopen)
    host.spawn(
        ["py"],
        cwd=tmp_path,
        log_path=tmp_path / "w.log",
        env={"RETRIEVAL__RERANK_MODEL_DIR": "/models"},
    )
    assert "RETRIEVAL__RERANK_MODEL_DIR" not in os.environ  # the kit's own environment is untouched
    host.run(["runner"], cwd=tmp_path)
    assert "env" not in FakePopen.instances[1].kwargs  # the next command inherits, as before


def test_a_real_child_gets_the_extra_environment_and_nobody_else_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIT_CHILD_ONLY", raising=False)
    monkeypatch.setenv("KIT_CHILD_INHERITED", "inherited")
    script = (
        "import os; print(os.environ.get('KIT_CHILD_ONLY'), os.environ.get('KIT_CHILD_INHERITED'))"
    )
    host = SystemHost()
    child = host.spawn(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        log_path=tmp_path / "w.log",
        env={"KIT_CHILD_ONLY": "child-value"},
    )
    assert child.wait(30) == 0
    assert "child-value inherited" in (tmp_path / "w.log").read_text(encoding="utf-8")
    assert "KIT_CHILD_ONLY" not in os.environ


def test_posix_stop_is_sigterm_to_the_pid_file_pid_and_the_child() -> None:
    seen, kill = _kills()
    host = SystemHost(os_name="posix", popen=FakePopen, kill=kill)
    proc = host.spawn(["py"], cwd=Path("."), log_path=Path("/dev/null"))
    proc.request_stop(worker_pid=9999)
    assert sorted(seen) == sorted([(9999, signal.SIGTERM), (proc.pid, signal.SIGTERM)])


def test_posix_stop_ignores_a_process_that_is_already_gone() -> None:
    def kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    host = SystemHost(os_name="posix", popen=FakePopen, kill=kill)
    proc = host.spawn(["py"], cwd=Path("."), log_path=Path("/dev/null"))
    proc.request_stop(worker_pid=5)  # no exception


def test_windows_stop_is_ctrl_break_to_the_process_group_and_never_a_terminate() -> None:
    seen, kill = _kills()
    host = SystemHost(os_name="nt", popen=FakePopen, kill=kill)
    proc = host.spawn(["py"], cwd=Path("."), log_path=Path("/dev/null"))
    proc.request_stop(worker_pid=9999)
    # The group id is the pid of the process created with CREATE_NEW_PROCESS_GROUP. The pid file
    # may name the worker behind a launcher, which is no group id: signalling it would be a
    # TerminateProcess with that exit code.
    assert seen == [(proc.pid, CTRL_BREAK_EVENT)]
    assert not FakePopen.instances[0].killed


def test_kill_is_the_last_resort_and_reaches_the_child() -> None:
    host = SystemHost(os_name="posix", popen=FakePopen)
    proc = host.spawn(["py"], cwd=Path("."), log_path=Path("/dev/null"))
    proc.kill()
    assert FakePopen.instances[0].killed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM")
def test_a_real_child_stops_on_request_stop_and_is_confirmed_gone(tmp_path: Path) -> None:
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "print('rea' + 'dy', flush=True)\n"
        "time.sleep(60)\n"
    )
    host = SystemHost()
    proc = host.spawn([sys.executable, "-c", script], cwd=tmp_path, log_path=tmp_path / "w.log")
    deadline = 0
    while "ready" not in (tmp_path / "w.log").read_text(encoding="utf-8") and deadline < 200:
        host.sleep(0.05)
        deadline += 1
    assert proc.poll() is None
    proc.request_stop()
    assert proc.wait(10) == 0
    assert proc.poll() == 0


def test_run_returns_the_exit_code_and_a_missing_program_is_127(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = SystemHost()
    assert host.run([sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path) == 3
    assert host.run(["no-such-program-xyz"], cwd=tmp_path) == 127
    assert "no-such-program-xyz" in capsys.readouterr().err


def test_capture_returns_stdout_and_the_exit_code(tmp_path: Path) -> None:
    host = SystemHost()
    result = host.capture([sys.executable, "-c", "print('hi')"], cwd=tmp_path)
    assert (result.returncode, result.stdout.strip()) == (0, "hi")
    assert host.capture(["no-such-program-xyz"], cwd=tmp_path).returncode == 127


def test_ctrl_c_during_a_command_waits_for_the_child_to_clean_up_then_reraises() -> None:
    # The child got the same Ctrl+C from the terminal; it needs its own time to purge what it
    # made (the runner's organizations), so the kit waits for it and does not kill it.
    host = SystemHost(popen=FakePopen)
    original = FakePopen.__init__

    def init(self: FakePopen, command: list[str], **kwargs: Any) -> None:
        original(self, command, **kwargs)
        self.wait_script = [KeyboardInterrupt(), 0]

    FakePopen.__init__ = init  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            host.run(["runner"], cwd=Path("."))
    finally:
        FakePopen.__init__ = original  # type: ignore[method-assign]
    child = FakePopen.instances[0]
    assert len(child.waits) == 2 and child.waits[1] is not None
    assert not child.terminated and not child.killed


def test_ctrl_c_terminates_a_child_that_does_not_stop() -> None:
    host = SystemHost(popen=FakePopen)
    original = FakePopen.__init__

    def init(self: FakePopen, command: list[str], **kwargs: Any) -> None:
        original(self, command, **kwargs)
        self.wait_script = [
            KeyboardInterrupt(),
            subprocess.TimeoutExpired("runner", 1),
            0,
        ]

    FakePopen.__init__ = init  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            host.run(["runner"], cwd=Path("."))
    finally:
        FakePopen.__init__ = original  # type: ignore[method-assign]
    assert FakePopen.instances[0].terminated


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[tuple[str, type[_Handler]]]:
    handler = type("H", (_Handler,), {"status": 200})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/readyz", handler
    server.shutdown()
    server.server_close()


def test_http_ok_is_true_only_for_a_2xx_answer(
    http_server: tuple[str, type[_Handler]],
) -> None:
    url, handler = http_server
    host = SystemHost()
    assert host.http_ok(url, 2.0)
    handler.status = 503
    assert not host.http_ok(url, 2.0)


def test_http_ok_is_false_when_nothing_listens() -> None:
    assert not SystemHost().http_ok("http://127.0.0.1:1/readyz", 0.5)

"""Stopping and probing the guard-worker where the OS is Windows (task 7.23; ADR-0012 decision 9).

On Windows ``os.kill(pid, 0)`` does not probe: any signal but CTRL_C_EVENT and CTRL_BREAK_EVENT
ends the process with TerminateProcess, so the pid-file liveness checks of the guard-worker and
the runner would kill the worker they look for. And a worker that Python does not handle
CTRL_BREAK_EVENT in dies at once, so its pid file and its drain never happen. No Windows here:
the Windows branches are driven through their injection points.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from evaluation.mailguard_bench.live import guard_worker, process
from evaluation.mailguard_bench.live import run as live_run


def _no_kill(pid: int, sig: int) -> None:
    raise AssertionError(f"os.kill({pid}, {sig}) must not be called on Windows")


def test_on_windows_liveness_never_sends_a_signal() -> None:
    probed: list[int] = []

    def probe(pid: int) -> bool:
        probed.append(pid)
        return True

    assert process.process_is_alive(4321, os_name="nt", kill=_no_kill, windows_probe=probe)
    assert probed == [4321]


def test_on_posix_liveness_probes_with_signal_zero() -> None:
    seen: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        seen.append((pid, sig))

    assert process.process_is_alive(77, os_name="posix", kill=kill)
    assert seen == [(77, 0)]


def test_on_posix_a_missing_process_is_not_alive_and_another_users_is() -> None:
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    def other_user(pid: int, sig: int) -> None:
        raise PermissionError

    assert not process.process_is_alive(1, os_name="posix", kill=gone)
    assert process.process_is_alive(1, os_name="posix", kill=other_user)


def test_a_real_child_is_alive_until_it_is_reaped() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process.process_is_alive(child.pid)
    finally:
        child.kill()
        child.wait()
    assert not process.process_is_alive(child.pid)


def test_both_pid_file_checks_go_through_the_same_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []

    def fake(pid: int, **_: object) -> bool:
        calls.append(pid)
        return False

    monkeypatch.setattr(process, "process_is_alive", fake)
    assert live_run.is_guard_worker_process(123) is False
    pid_file = tmp_path / "guard_worker.C3.pid"
    pid_file.write_text("456\n", encoding="utf-8")
    assert guard_worker._live_guard_worker_pid(pid_file) is None
    assert calls == [123, 456]


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX stand-in for SIGBREAK")
def test_the_break_handler_cancels_the_main_task_the_way_ctrl_c_does() -> None:
    async def main() -> str:
        task = asyncio.current_task()
        assert task is not None
        installed = process.install_break_handler(signal.SIGUSR1)
        assert installed
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return "cancelled"
        finally:
            signal.signal(signal.SIGUSR1, signal.SIG_DFL)
        return "slept"

    # The task is cancelled from the signal handler; the coroutine sees CancelledError, as
    # WorkerRuntime.run does on Ctrl+C, and drains before it lets go.
    assert asyncio.run(main()) == "cancelled"


def test_the_break_handler_is_a_no_op_where_there_is_no_sigbreak() -> None:
    if hasattr(signal, "SIGBREAK"):
        pytest.skip("Windows")

    async def main() -> bool:
        return process.install_break_handler()

    assert asyncio.run(main()) is False

"""Process liveness and the stop signal of the guard-worker, where the OS is Windows too.

Task 7.23 (teammate benchmark kit); ADR-0012 decision 9. On POSIX the guard-worker stops on
SIGTERM (``WorkerRuntime`` drains the consumers first) and a pid is probed with signal 0. Neither
works on Windows:

- ``os.kill(pid, 0)`` terminates the process there: every signal other than CTRL_C_EVENT and
  CTRL_BREAK_EVENT ends it with TerminateProcess, so a liveness check would kill the worker it
  looks for. ``process_is_alive`` asks the OS (``OpenProcess`` and ``GetExitCodeProcess``)
  instead.
- The kit stops a worker it started in its own process group with CTRL_BREAK_EVENT, which Python
  does not handle by default: the process would die on the spot, with no drain and its pid file
  left behind. ``install_break_handler`` turns it into the cancellation that Ctrl+C already is
  for ``asyncio.run`` (the drain in ``WorkerRuntime.run``).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable

KillFn = Callable[[int, int], None]


def _windows_alive(pid: int) -> bool:
    """Whether the OS knows a running process ``pid`` (Windows only)."""
    if sys.platform != "win32":  # pragma: no cover
        raise OSError("the Windows process probe runs on Windows only")
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied  # alive, only not ours to open
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return bool(code.value == still_active)
    finally:
        kernel32.CloseHandle(handle)


def process_is_alive(
    pid: int,
    *,
    os_name: str | None = None,
    kill: KillFn = os.kill,
    windows_probe: Callable[[int], bool] = _windows_alive,
) -> bool:
    """True when a process ``pid`` exists. Never signals it on Windows, where that kills it.

    On POSIX, ``PermissionError`` means it exists and is another user's.
    """
    if pid <= 0:
        return False
    if (os_name or os.name) == "nt":
        return windows_probe(pid)
    try:
        kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def install_break_handler(sig: int | None = None) -> bool:
    """Make ``sig`` (default: Windows' CTRL_BREAK_EVENT) cancel the running main task.

    Call it from inside the coroutine ``asyncio.run`` runs. Python's own Ctrl+C handling does
    the same thing for SIGINT, and ``WorkerRuntime.run`` answers the cancellation with its
    graceful stop. Returns False, and changes nothing, where the platform has no such signal
    (POSIX: SIGTERM is handled by the runtime itself).
    """
    signum = sig if sig is not None else getattr(signal, "SIGBREAK", None)
    if signum is None:
        return False
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        return False

    def handler(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(task.cancel)

    signal.signal(signum, handler)
    return True

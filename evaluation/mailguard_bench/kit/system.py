"""The kit's boundary to the machine: commands, the guard-worker process, HTTP and the clock.

Task 7.23 (teammate benchmark kit). The orchestrator (``campaign.py``) talks to a ``Host``, never
to ``subprocess`` or ``os`` itself, so its tests replace the machine with a recording fake while
everything it decides (the order of the commands, the readiness rule, what to stop when) is real.
``SystemHost`` is the one real implementation. It is pure Python, no shell, and runs on Windows:

- A command inherits the terminal and returns its exit code. Ctrl+C reaches the child from the
  terminal too, so the kit waits for the child to clean up before it re-raises, and terminates it
  only when it does not stop.
- The guard-worker is started detached from the terminal's Ctrl+C (its own session on POSIX, its
  own process group on Windows), so that the kit alone decides when it stops, and with its output
  appended to a log. It is stopped with SIGTERM on POSIX (``WorkerRuntime`` drains on it) and
  with CTRL_BREAK_EVENT on Windows, which ``live/process.py`` makes the worker treat like Ctrl+C.
  The Windows signal goes to the process group, whose id is the pid of the process the kit
  started: the pid in the worker's pid file may belong to a process behind a launcher, and any
  other signal sent with ``os.kill`` on Windows is a TerminateProcess.
- The guard-worker may be given extra environment variables (``spawn(..., env=...)``): they are
  merged over the environment the kit inherited and reach that child alone. The kit's own
  environment is never changed, so no later command (the runner, docker) inherits them.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

CTRL_BREAK_EVENT: int = getattr(signal, "CTRL_BREAK_EVENT", 1)
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
INTERRUPT_GRACE_S = 60.0
"""How long a child that got Ctrl+C from the terminal may take to clean up before it is stopped."""
COMMAND_NOT_FOUND = 127


@dataclass(frozen=True)
class CommandResult:
    """The outcome of a captured command."""

    returncode: int
    stdout: str = ""


class ProcessHandle(Protocol):
    """A process the kit started and will stop."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float) -> int:
        """Wait for the exit; ``subprocess.TimeoutExpired`` when it is still running."""

    def request_stop(self, worker_pid: int | None = None) -> None:
        """Ask the process to shut down gracefully (SIGTERM, or CTRL_BREAK_EVENT on Windows)."""

    def kill(self) -> None:
        """Stop it at once; only after a graceful stop was not enough."""


class Host(Protocol):
    """Everything the orchestrator needs from the machine."""

    @property
    def os_name(self) -> str: ...

    def run(self, command: Sequence[str], *, cwd: Path) -> int: ...

    def capture(self, command: Sequence[str], *, cwd: Path) -> CommandResult: ...

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        log_path: Path,
        env: Mapping[str, str] | None = None,
    ) -> ProcessHandle: ...

    def http_ok(self, url: str, timeout_s: float) -> bool: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


class SystemProcess:
    """A ``subprocess.Popen`` the kit started, with the platform's stop signal."""

    def __init__(self, popen: Any, *, os_name: str, kill: Callable[[int, int], None]) -> None:
        self._popen = popen
        self._os_name = os_name
        self._kill = kill

    @property
    def pid(self) -> int:
        return int(self._popen.pid)

    def poll(self) -> int | None:
        code: int | None = self._popen.poll()
        return code

    def wait(self, timeout: float) -> int:
        return int(self._popen.wait(timeout=timeout))

    def request_stop(self, worker_pid: int | None = None) -> None:
        if self._os_name == "nt":
            self._kill(self.pid, CTRL_BREAK_EVENT)
            return
        targets = [pid for pid in (worker_pid, self.pid) if pid is not None]
        for pid in dict.fromkeys(targets):  # the pid-file pid first, each one once
            with contextlib.suppress(ProcessLookupError):  # already gone
                self._kill(pid, signal.SIGTERM)

    def kill(self) -> None:
        self._popen.kill()


class SystemHost:
    """The real machine."""

    def __init__(
        self,
        *,
        os_name: str | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        kill: Callable[[int, int], None] = os.kill,
        interrupt_grace_s: float = INTERRUPT_GRACE_S,
    ) -> None:
        self._os_name = os_name or os.name
        self._popen = popen
        self._kill = kill
        self._interrupt_grace_s = interrupt_grace_s

    @property
    def os_name(self) -> str:
        return self._os_name

    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        try:
            child = self._popen(list(command), cwd=cwd)
        except FileNotFoundError:
            print(f"FAIL command not found: {command[0]}", file=sys.stderr)
            return COMMAND_NOT_FOUND
        try:
            return int(child.wait())
        except KeyboardInterrupt:
            try:
                child.wait(timeout=self._interrupt_grace_s)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            raise

    def capture(self, command: Sequence[str], *, cwd: Path) -> CommandResult:
        try:
            done = subprocess.run(
                list(command), cwd=cwd, capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            return CommandResult(COMMAND_NOT_FOUND)
        return CommandResult(done.returncode, done.stdout)

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        log_path: Path,
        env: Mapping[str, str] | None = None,
    ) -> ProcessHandle:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        detach: dict[str, Any] = (
            {"creationflags": CREATE_NEW_PROCESS_GROUP}
            if self._os_name == "nt"
            else {"start_new_session": True}
        )
        # A copy merged over the inherited environment: os.environ itself stays as it is, and
        # without ``env`` the child simply inherits.
        environment: dict[str, Any] = {"env": {**os.environ, **env}} if env else {}
        shown = (
            subprocess.list2cmdline(list(command)) if self._os_name == "nt" else shlex.join(command)
        )
        with log_path.open("ab") as log:
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            log.write(f"=== kit: starting {shown} at {stamp} ===\n".encode())
            log.flush()
            child = self._popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **detach,
                **environment,
            )
        return SystemProcess(child, os_name=self._os_name, kill=self._kill)

    def http_ok(self, url: str, timeout_s: float) -> bool:
        # No proxy: the address is this machine's own, and HTTP_PROXY would send it elsewhere.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=timeout_s) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

"""The teammate's environment doctor: is this machine ready to run the v2 benchmark?

    python -m evaluation.mailguard_bench.kit.doctor [--model-profile M] [--reader R]

One line per check, ``ok|WARN|FAIL <check>: <detail>``, and under every WARN or FAIL a fix hint.
The exit status is 1 when any check FAILs. Every check is a pure function over injected inputs
(a :class:`World`), so the tests feed it what a WSL2 laptop, a native Windows box or a half-set-up
machine would show; the CLI wires the real inputs.

The doctor changes nothing and is read-only toward docker: it runs only ``docker --version``,
``docker info``, ``docker compose version`` and ``docker compose ps``. It never prints the value
of a setting that may be a key, not even a prefix or a length: keys are reported by name as set or
not set, and a wrong value says what the setting must be, never what it is.

Task 7.23; R22.12 (reproducible runs), R24.5 (no live credentials in tests); ADR-0012 decisions 7
and 9; the procedure it guards is docs/demo-runbook.md section 9.9.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform as platform_module
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values

from evaluation.mailguard_bench.guard_env import (
    DEFAULT_MAILGUARD_COMMIT,
    REPO_ROOT,
    GuardEnvError,
    require_pinned_worktree,
)
from evaluation.mailguard_bench.kit import pinned
from evaluation.mailguard_bench.live.guard_worker import DEFAULT_PORT as GUARD_WORKER_PORT
from evaluation.mailguard_bench.live.stack_env import (
    EMBEDDING_KEY_ENV,
    HOST_MUST_NOT_SET,
    SUMMARIZER_MODEL_ENV,
    SUMMARIZER_MODEL_SETTING,
    StackEnvError,
    host_env_problems,
    render_stack_env,
)
from evaluation.mailguard_bench.meaning import reader_model_problems
from evaluation.mailguard_bench.model_profiles import (
    PROFILES,
    ModelProfile,
    ModelProfileError,
    get_profile,
    profile_env,
)

Platform = Literal["wsl2", "wsl1", "windows", "linux", "other"]

MIN_DOCKER_MEMORY_BYTES = 8 * 10**9  # decimal GB: a "8GB" WSL VM reports a little less than 8 GiB
MIN_FREE_DISK_BYTES = 25 * 10**9
MIN_PYTHON = (3, 12)
# The runbook's section 9.9 step 1: none of these may be exported in the shell, because Compose
# lets an exported variable win over every env file.
SHELL_PREFIXES = ("LLM__", "EMBEDDING__", "RETRIEVAL__", "SUMMARIZATION__", "ROUTING__")
SHELL_NAMES = (SUMMARIZER_MODEL_ENV,)
# The two lines an .env made before task 7.20 still has (runbook 9.9 step 1).
OLD_ENV_LINES = (SUMMARIZER_MODEL_SETTING, *HOST_MUST_NOT_SET)
GUIDE = "docs/BENCHMARK.md"
NATIVE_GUIDE = "docs/benchmark-windows-native.md"


class Status(StrEnum):
    """How a check came out. ``FAIL`` makes the doctor exit 1."""

    OK = "ok"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Result:
    """One check's outcome: a status, what was checked, what was found, and how to fix it."""

    status: Status
    check: str
    detail: str
    hint: str = ""

    def lines(self) -> list[str]:
        """The printed form: one line, plus a fix line when something is not ok."""
        out = [f"{self.status.value} {self.check}: {self.detail}"]
        if self.hint and self.status is not Status.OK:
            out.append(f"     fix: {self.hint}")
        return out


@dataclass(frozen=True)
class CommandResult:
    """What a finished subprocess printed."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HttpResult:
    """An HTTP answer: the status code and the body text."""

    status: int
    body: str


@dataclass(frozen=True)
class World:
    """Everything the checks read from the machine, injected so tests can fake it."""

    system: str
    proc_version: str | None
    repo_root: Path
    environ: Mapping[str, str]
    run: Callable[[Sequence[str]], CommandResult | None]
    which: Callable[[str], str | None]
    free_bytes: Callable[[str], int | None]
    port_in_use: Callable[[int], bool]
    http_get: Callable[[str], HttpResult | None]
    read_text: Callable[[Path], str | None]
    systemd_running: bool
    python_version: tuple[int, int, int]
    installed_sklearn: str | None
    require_guard: Callable[[Path, str], object]
    pinned_problems: Callable[[], list[str]]


# --- platform ----------------------------------------------------------------------------------


def detect_platform(system: str, proc_version: str | None) -> Platform:
    """WSL2, WSL1, native Windows, Linux or something else, from the OS and ``/proc/version``."""
    if system == "Windows":
        return "windows"
    if system == "Linux":
        lowered = (proc_version or "").lower()
        if "wsl2" in lowered or "microsoft-standard" in lowered:
            return "wsl2"
        if "microsoft" in lowered:
            return "wsl1"
        return "linux"
    return "other"


def check_platform(platform: Platform) -> Result:
    """WSL2 and Linux are the routes; native Windows works with the other guide; WSL1 cannot."""
    if platform == "wsl2":
        return Result(Status.OK, "platform", "WSL2 (the primary route)")
    if platform == "linux":
        return Result(Status.OK, "platform", "Linux")
    if platform == "wsl1":
        return Result(
            Status.FAIL,
            "platform",
            "WSL1: Docker Engine cannot run in it",
            "in PowerShell: `wsl --set-version <distro name> 2` (`wsl -l -v` lists the names)",
        )
    if platform == "windows":
        return Result(
            Status.WARN,
            "platform",
            "native Windows; the primary route is WSL2 (Docker Desktop's requirements list only "
            "Pro, Enterprise and Education)",
            f"follow {NATIVE_GUIDE}, or use the WSL2 route in {GUIDE}",
        )
    return Result(
        Status.WARN,
        "platform",
        "not a platform the kit was written for",
        f"the guides cover WSL2, Linux and Windows ({GUIDE})",
    )


def check_repo_location(platform: Platform, repo_path: str) -> Result:
    """In WSL a checkout on a Windows drive (/mnt/c/...) is slow and converts line endings."""
    if platform in ("wsl2", "wsl1") and re.match(r"^/mnt/[a-zA-Z]/", repo_path):
        return Result(
            Status.WARN,
            "repository location",
            f"{repo_path} is on a Windows drive",
            f"clone the repository inside Ubuntu, into ~/work (never /mnt/c): {GUIDE}, part B",
        )
    return Result(Status.OK, "repository location", repo_path)


def check_systemd(platform: Platform, *, systemd_running: bool) -> Result:
    """Ubuntu in WSL2 has systemd by default; Docker's and Ollama's services need it."""
    if platform not in ("wsl2", "wsl1"):
        return Result(Status.OK, "systemd", "not checked outside WSL")
    if systemd_running:
        return Result(Status.OK, "systemd", "running")
    return Result(
        Status.WARN,
        "systemd",
        "not running: `systemctl` cannot start Docker or Ollama",
        "put `[boot]` and `systemd=true` in /etc/wsl.conf, then run `wsl --shutdown` in "
        f"PowerShell and open Ubuntu again ({GUIDE}, part A)",
    )


# --- line endings ------------------------------------------------------------------------------

_EOL_LINE = re.compile(r"^\s*i/(\S+)\s+w/(\S+)\s+attr/(.*?)\s*\t(.*)$")


def check_line_endings(result: CommandResult | None) -> Result:
    """No tracked text file may be CRLF in the working tree (``git ls-files --eol``)."""
    if result is None or result.returncode != 0:
        return Result(
            Status.FAIL,
            "line endings",
            "cannot run `git ls-files --eol` here (git missing, or this is not a git checkout)",
            "install git, and run the doctor from the repository's root",
        )
    offenders: list[str] = []
    for line in result.stdout.splitlines():
        match = _EOL_LINE.match(line)
        if not match:
            continue
        _, worktree, attr, path = match.groups()
        if worktree in ("crlf", "mixed") and "-text" not in attr.split():
            offenders.append(path)
    if not offenders:
        return Result(Status.OK, "line endings", "no tracked text file has CRLF")
    shown = ", ".join(offenders[:5]) + (
        f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
    )
    return Result(
        Status.FAIL,
        "line endings",
        f"{len(offenders)} tracked text files have CRLF: {shown}",
        "clone the repository again inside Ubuntu (a clone made by Git for Windows converted "
        f"the files; {GUIDE}, part B), or run `git config core.autocrlf input` and re-checkout "
        "the files",
    )


# --- docker ------------------------------------------------------------------------------------


def _first_line(text: str, limit: int = 160) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[0] if lines else "")[:limit]


def check_docker_cli(result: CommandResult | None) -> Result:
    """The ``docker`` command exists."""
    if result is None or result.returncode != 0:
        return Result(
            Status.FAIL,
            "docker CLI",
            "`docker` not found",
            "install Docker Engine inside Ubuntu from Docker's apt repository: "
            f"https://docs.docker.com/engine/install/ubuntu/ ({GUIDE}, part A)",
        )
    return Result(Status.OK, "docker CLI", _first_line(result.stdout))


def parse_docker_info(stdout: str) -> dict[str, object] | None:
    """The JSON of ``docker info --format '{{json .}}'``, or None when it is not JSON."""
    try:
        value = json.loads(stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def check_docker_daemon(result: CommandResult | None) -> Result:
    """The daemon answers ``docker info``."""
    if result is None:
        return Result(Status.FAIL, "docker daemon", "no `docker` command to ask", "install Docker")
    if result.returncode == 0:
        info = parse_docker_info(result.stdout) or {}
        return Result(
            Status.OK, "docker daemon", f"reachable, server {info.get('ServerVersion', '?')}"
        )
    stderr = result.stderr
    if "permission denied" in stderr.lower():
        return Result(
            Status.FAIL,
            "docker daemon",
            "permission denied on the Docker socket",
            "add yourself to the docker group: `sudo usermod -aG docker $USER`, then close the "
            f"Ubuntu window and open a new one ({GUIDE}, part G)",
        )
    return Result(
        Status.FAIL,
        "docker daemon",
        f"not reachable: {_first_line(stderr) or 'no answer'}",
        "start it with `sudo systemctl start docker` (in WSL this needs systemd, part A of "
        f"{GUIDE}); with Docker Desktop, start the app",
    )


def check_compose(result: CommandResult | None) -> Result:
    """Docker Compose v2 (``docker compose``) is installed."""
    hint = "install the docker-compose-plugin package (docker's apt repository, part A)"
    if result is None or result.returncode != 0:
        return Result(Status.FAIL, "docker compose", "`docker compose` (v2) is not available", hint)
    version = result.stdout.strip().lstrip("v")
    major = version.split(".")[0]
    if not major.isdigit() or int(major) < 2:
        return Result(Status.FAIL, "docker compose", f"version {version or '?'} is not v2", hint)
    return Result(Status.OK, "docker compose", f"v{version}")


def check_docker_memory(info: Mapping[str, object] | None) -> Result:
    """Docker has at least 8 GB of memory (the stack, the reranker and the parser are heavy)."""
    hint = (
        "WSL2: write `[wsl2]` and `memory=16GB` in %UserProfile%\\.wslconfig, then "
        "`wsl --shutdown` "
        f"({GUIDE}, part A); Docker Desktop: Settings, Resources"
    )
    if info is None or not isinstance(info.get("MemTotal"), int):
        return Result(Status.FAIL, "docker memory", "unknown: the daemon did not report it", hint)
    total = int(info["MemTotal"])  # type: ignore[call-overload]
    detail = f"{total / 1e9:.1f} GB available to Docker (8 GB needed)"
    if total < MIN_DOCKER_MEMORY_BYTES:
        return Result(Status.FAIL, "docker memory", detail, hint)
    return Result(Status.OK, "docker memory", detail)


def parse_compose_ps(stdout: str) -> set[int]:
    """The host ports this project's running containers publish (``docker compose ps``).

    Compose prints either one JSON array or one JSON object per line, depending on its version.
    """
    text = stdout.strip()
    rows: list[object] = []
    try:
        if text.startswith("["):
            rows = list(json.loads(text))
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return set()
    held: set[int] = set()
    for row in rows:
        publishers = row.get("Publishers") if isinstance(row, dict) else None
        for publisher in publishers or []:
            port = publisher.get("PublishedPort") if isinstance(publisher, dict) else None
            if isinstance(port, int) and port > 0:
                held.add(port)
    return held


# --- disk, tools, python -----------------------------------------------------------------------


def check_disk(label: str, path: str, free: int | None) -> Result:
    """At least 25 GB free (the images, the models and the run folders)."""
    if free is None:
        return Result(Status.OK, label, f"cannot be measured from here ({path})")
    detail = f"{free / 1e9:.1f} GB free at {path} (25 GB wanted)"
    if free < MIN_FREE_DISK_BYTES:
        return Result(
            Status.WARN,
            label,
            detail,
            "free some space; the first build of the images and the Ollama models are the big "
            "downloads",
        )
    return Result(Status.OK, label, detail)


def check_tool(
    name: str, path: str | None, hint: str, *, missing_status: Status = Status.FAIL
) -> Result:
    """A command the kit needs is on the PATH."""
    if path is None:
        return Result(missing_status, name, "not found", hint)
    return Result(Status.OK, name, f"found at {path}")


def check_python(version: tuple[int, int, int]) -> Result:
    """Python 3.12 or newer runs the doctor and the runner."""
    text = ".".join(str(part) for part in version)
    if version[:2] < MIN_PYTHON:
        return Result(
            Status.FAIL,
            "python",
            f"{text}; 3.12 or newer is needed",
            "run through `uv run` (it installs a matching Python) or `uv python install 3.12`",
        )
    return Result(Status.OK, "python", text)


# --- .env and the shell ------------------------------------------------------------------------


def merge_env(env_text: str | None, process_env: Mapping[str, str]) -> dict[str, str]:
    """The shell over the .env file: the precedence the settings and Compose use."""
    from_file = dotenv_values(stream=io.StringIO(env_text or ""))
    return {**{k: v for k, v in from_file.items() if v is not None}, **process_env}


def check_env_file(exists: bool) -> Result:
    """``.env`` is there."""
    if exists:
        return Result(Status.OK, ".env", "present")
    return Result(
        Status.FAIL,
        ".env",
        "missing",
        f"`cp .env.example .env`, then set the keys ({GUIDE}, part C); never share or commit it",
    )


def check_env_keys(environ: Mapping[str, str], profile: ModelProfile | None) -> list[Result]:
    """Each key the run needs is set or not set, reported by name only.

    The embeddings always need the Gemini key (``LLM__OPENAI_API_KEY``, and the same value in
    ``EMBEDDING__API_KEY``). A model profile adds its own key, if it has one. Without a profile,
    every profile's key is listed, and a missing one is a warning naming its profile.
    """
    always = (EMBEDDING_KEY_ENV, "EMBEDDING__API_KEY")
    required: list[tuple[str, str | None]] = [(name, None) for name in always]
    optional: list[tuple[str, str | None]] = []
    if profile is not None:
        if profile.api_key_env and profile.api_key_env not in always:
            required.insert(0, (profile.api_key_env, None))
    else:
        for other in PROFILES.values():
            key = other.api_key_env
            if key and key not in always and all(key != name for name, _ in optional):
                optional.append((key, other.name))
    results: list[Result] = []
    for name, _ in required:
        is_set = bool((environ.get(name) or "").strip())
        results.append(
            Result(
                Status.OK if is_set else Status.FAIL,
                f"env {name}",
                "set" if is_set else "not set",
                "" if is_set else f"add `{name}=...` to .env ({GUIDE}, part C)",
            )
        )
    for name, profile_name in optional:
        is_set = bool((environ.get(name) or "").strip())
        results.append(
            Result(
                Status.OK if is_set else Status.WARN,
                f"env {name}",
                "set" if is_set else f"not set (the {profile_name} model profile needs it)",
                ""
                if is_set
                else f"add `{name}=...` to .env before running that model ({GUIDE}, C)",
            )
        )
    return results


def check_env_settings(environ: Mapping[str, str], profile: ModelProfile | None) -> Result:
    """The host processes read what the containers get (runbook 9.9 step 1).

    Reuses the stack env's own comparison. A wrong value is reported as what the setting must be;
    the found value is never printed.
    """
    if not (environ.get(EMBEDDING_KEY_ENV) or "").strip():
        return Result(
            Status.FAIL,
            ".env settings",
            f"cannot be compared with the containers' settings until {EMBEDDING_KEY_ENV} is set",
            f"set the Gemini key in .env ({GUIDE}, part C)",
        )
    stand_in = ModelProfile(
        name="any",
        model=(environ.get(SUMMARIZER_MODEL_SETTING) or "").strip() or "any",
        base_url="http://localhost/v1",
        api_key_env=None,
        input_per_m=0.0,
        output_per_m=0.0,
        fixed_api_key="unused",
    )
    try:
        values = render_stack_env(profile or stand_in, environ)
    except ModelProfileError:  # the profile's own key is missing; its key check reports that
        values = render_stack_env(stand_in, environ)
    except StackEnvError as exc:
        return Result(Status.FAIL, ".env settings", str(exc), f"fix .env ({GUIDE}, part C)")
    # The two old lines have their own check (the file) and the shell check (exports).
    problems = [p for p in host_env_problems(values, environ) if p.split()[0] not in OLD_ENV_LINES]
    if not problems:
        return Result(
            Status.OK, ".env settings", "the host processes read the settings the containers get"
        )
    return Result(
        Status.FAIL,
        ".env settings",
        "; ".join(problems),
        f"set them in .env as docs/demo-runbook.md section 9.9 step 1 says ({GUIDE}, part C)",
    )


def check_shell_exports(process_env: Mapping[str, str]) -> Result:
    """None of the runbook's setting prefixes is exported in the shell (names only)."""
    names = sorted(
        name for name in process_env if name.startswith(SHELL_PREFIXES) or name in SHELL_NAMES
    )
    if not names:
        return Result(Status.OK, "exported settings", "none of the benchmark settings is exported")
    return Result(
        Status.FAIL,
        "exported settings",
        f"exported in this shell: {', '.join(names)}",
        "Compose lets an exported variable win over every env file: run `unset NAME` for each, "
        "and look in ~/.bashrc and ~/.profile for the `export` that sets it",
    )


def check_old_env_lines(env_text: str | None) -> Result:
    """The two lines of an .env made before task 7.20 are not in it."""
    found = [
        name
        for name in OLD_ENV_LINES
        if re.search(rf"^\s*(export\s+)?{re.escape(name)}\s*=", env_text or "", re.MULTILINE)
    ]
    if not found:
        return Result(Status.OK, "old .env lines", "none of the two old lines is in .env")
    return Result(
        Status.FAIL,
        "old .env lines",
        f"still in .env: {', '.join(found)}",
        "remove those lines from .env (runbook section 9.9 step 1); the containers never read "
        "them, the guard-worker would",
    )


# --- the guard and the pinned inputs -----------------------------------------------------------


def default_guard_dir(repo_root: Path, environ: Mapping[str, str]) -> Path:
    """MAILGUARD_DIR, else ./agentmailguard when the repository holds the guard, else the worktree
    next to the repository (the Makefile's default until the single-repo layout)."""
    if environ.get("MAILGUARD_DIR"):
        return Path(environ["MAILGUARD_DIR"])
    inside = repo_root / "agentmailguard"
    if inside.is_dir():
        return inside
    return repo_root.parent / "AgentMailGuard-bench"


def check_guard(require: Callable[[Path, str], object], directory: Path, commit: str) -> Result:
    """The guard is at the pinned commit and clean (``guard_env.require_pinned_worktree``)."""
    try:
        require(directory, commit)
    except GuardEnvError as exc:
        return Result(
            Status.FAIL,
            "AgentMailGuard",
            str(exc),
            f"`make bench-setup` creates the worktree at the pinned commit ({GUIDE}, part D)",
        )
    return Result(Status.OK, "AgentMailGuard", f"{directory} at {commit[:8]}, clean")


def check_pinned(problems: Sequence[str]) -> Result:
    """The shipped cases and classifier match their pinned sha256."""
    if not problems:
        return Result(
            Status.OK,
            "pinned inputs",
            "cases.jsonl, manifest.json and the L1 classifier match their pinned sha256",
        )
    return Result(
        Status.FAIL,
        "pinned inputs",
        "; ".join(problems),
        "they ship in git: `git status` shows what changed; restore them with `git checkout -- "
        "evaluation/datasets/mailguard evaluation/mailguard_bench/pinned` or clone again",
    )


def check_sklearn(installed: str | None) -> Result:
    """scikit-learn is the version the pinned classifier was built with."""
    problem = pinned.check_scikit_learn(installed)
    if problem is None:
        return Result(Status.OK, "scikit-learn", f"{installed} (the classifier's version)")
    return Result(Status.FAIL, "scikit-learn", problem, "`uv sync` installs the locked version")


# --- ports -------------------------------------------------------------------------------------

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def published_ports(compose_text: str, environ: Mapping[str, str]) -> dict[int, str]:
    """Host ports the compose file publishes, with the service that publishes each.

    ``${NAME:-default}`` in a port is resolved through ``environ`` as Compose does.
    """
    ports: dict[int, str] = {}
    services = (yaml.safe_load(compose_text) or {}).get("services") or {}
    for service, spec in services.items():
        for entry in (spec or {}).get("ports") or []:
            if isinstance(entry, dict):
                host = str(entry.get("published", ""))
            else:
                text = _VAR.sub(lambda m: environ.get(m.group(1)) or (m.group(2) or ""), str(entry))
                parts = text.split("/")[0].split(":")
                host = parts[-2] if len(parts) >= 2 else ""
            if host.isdigit():
                ports.setdefault(int(host), str(service))
    return ports


def check_ports(
    ports: Mapping[int, str], *, owned: set[int], in_use: Callable[[int], bool]
) -> Result:
    """Each host port is free, or held by this project's own containers."""
    busy = {
        port: service
        for port, service in sorted(ports.items())
        if port not in owned and in_use(port)
    }
    if not busy:
        return Result(
            Status.OK,
            "ports",
            f"{len(ports)} host ports are free or held by this project's containers",
        )
    listed = ", ".join(f"{port} ({service})" for port, service in busy.items())
    first = next(iter(busy))
    return Result(
        Status.FAIL,
        "ports",
        f"in use by another program: {listed}",
        f"find it with `ss -ltnp | grep :{first}` (PowerShell: `netstat -ano | findstr :{first}`) "
        "and stop it, or move the port with the variable docker-compose.yml names for it",
    )


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True when something accepts a connection on the port."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


# --- local models (Ollama) ---------------------------------------------------------------------


def is_local_endpoint(url: str) -> bool:
    """A base URL that points at this machine or its private network (Ollama), not a hosted API."""
    host = urlsplit(url).hostname
    if not host:
        return False
    if host.lower() in ("localhost", "host.docker.internal"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def model_listed(model: str, names: Sequence[str]) -> bool:
    """Whether Ollama's model list has ``model`` (a name without a tag means ``:latest``)."""
    return model in names or (":" not in model and f"{model}:latest" in names)


def http_get(url: str, timeout_s: float = 3.0) -> HttpResult | None:
    """GET a URL without any proxy; None when nothing answers."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout_s) as response:
            return HttpResult(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _endpoint(profile: ModelProfile, environ: Mapping[str, str]) -> str:
    try:
        return profile_env(profile, environ)["LLM__OPENAI_BASE_URL"]
    except ModelProfileError:  # a hosted profile whose key is missing: its URL is the default
        return profile.base_url


def check_ollama(
    profile: ModelProfile,
    environ: Mapping[str, str],
    platform: Platform,
    fetch: Callable[[str], HttpResult | None],
) -> Result | None:
    """For a profile on a local server: it answers and lists the profile's model.

    None when the profile's endpoint is hosted (nothing local to check).
    """
    url = _endpoint(profile, environ)
    if not is_local_endpoint(url):
        return None
    origin = url.rstrip("/")
    origin = origin[: -len("/v1")] if origin.endswith("/v1") else origin
    version = fetch(f"{origin}/api/version")
    if version is None or version.status != 200:
        return Result(
            Status.FAIL,
            "ollama",
            f"no answer from {origin}/api/version",
            "start Ollama (`systemctl status ollama`), and set BENCH_OLLAMA_BASE_URL to the "
            f"address it listens on: {GUIDE}, part E (runbook 9.9 step 2)",
        )
    try:
        version_text = str(json.loads(version.body).get("version", "?"))
    except (ValueError, AttributeError):
        version_text = "?"
    tags = fetch(f"{origin}/api/tags")
    names: list[str] = []
    if tags is not None and tags.status == 200:
        try:
            names = [str(m.get("name")) for m in json.loads(tags.body).get("models", [])]
        except (ValueError, AttributeError):
            names = []
    if not model_listed(profile.model, names):
        return Result(
            Status.FAIL,
            "ollama",
            f"{origin} answers (version {version_text}) but has no model {profile.model}",
            f"`ollama pull {profile.model}` in the shell where OLLAMA_HOST is set as in part E",
        )
    host = urlsplit(url).hostname or ""
    loopback = host == "localhost" or (
        host.replace(".", "").isdigit() and ip_address(host).is_loopback
    )
    if loopback and platform in ("wsl2", "linux"):
        return Result(
            Status.WARN,
            "ollama",
            f"{profile.model} is at {origin} (version {version_text}); the containers reach "
            "the host "
            "through host.docker.internal, which on Docker Engine is the bridge address, "
            "not localhost",
            "make Ollama listen on the bridge address and set BENCH_OLLAMA_BASE_URL to it "
            f"({GUIDE}, part E; runbook 9.9 step 2)",
        )
    return Result(Status.OK, "ollama", f"{profile.model} is at {origin} (version {version_text})")


def check_gpu(nvidia_smi: str | None, *, local_model: bool | None) -> Result:
    """``nvidia-smi`` is information; a local model without a GPU is a warning."""
    if nvidia_smi:
        return Result(Status.OK, "nvidia-smi", f"found at {nvidia_smi}")
    if local_model:
        return Result(
            Status.WARN,
            "nvidia-smi",
            "not found: a local model would run on the CPU, much slower, and its latencies "
            "are not comparable",
            "install the current NVIDIA driver on Windows; WSL2 needs none inside Ubuntu "
            f"({GUIDE}, part E)",
        )
    return Result(Status.OK, "nvidia-smi", "not found (only local models need a GPU)")


# --- the reader --------------------------------------------------------------------------------


def check_reader(reader: str | None, profile: ModelProfile | None) -> Result:
    """The meaning column's reader is chosen and not a benchmarked model (ADR-0012 d. 7)."""
    if not reader:
        return Result(
            Status.WARN,
            "reader model",
            "none chosen yet",
            "choose the reader of the meaning-based column, write it down before the first run "
            "(ADR-0012 decision 7), and pass it as --reader <model>",
        )
    run_meta = {"this run": {"generation_model": profile.model}} if profile else {}
    problems = reader_model_problems(reader, run_meta)
    if problems:
        return Result(
            Status.FAIL,
            "reader model",
            "; ".join(problems),
            "pick a model that is not one of the benchmarked ones; the choice is yours",
        )
    return Result(Status.OK, "reader model", f"{reader} is not a benchmarked model")


# --- the run -----------------------------------------------------------------------------------


def run_checks(world: World, *, model_profile: str | None, reader: str | None) -> list[Result]:
    """Every check, in the order a person would fix them."""
    results: list[Result] = []
    profile: ModelProfile | None = None
    if model_profile:
        try:
            profile = get_profile(model_profile)
        except ModelProfileError as exc:
            results.append(
                Result(Status.FAIL, "model profile", str(exc), "pick one of the known names")
            )
    platform = detect_platform(world.system, world.proc_version)
    root = world.repo_root
    results.append(check_platform(platform))
    results.append(check_repo_location(platform, str(root)))
    results.append(check_systemd(platform, systemd_running=world.systemd_running))
    results.append(check_line_endings(world.run(["git", "ls-files", "--eol"])))

    results.append(check_docker_cli(world.run(["docker", "--version"])))
    info_run = world.run(["docker", "info", "--format", "{{json .}}"])
    results.append(check_docker_daemon(info_run))
    results.append(check_compose(world.run(["docker", "compose", "version", "--short"])))
    info = parse_docker_info(info_run.stdout) if info_run and info_run.returncode == 0 else None
    results.append(check_docker_memory(info))

    results.append(check_disk("repository disk", str(root), world.free_bytes(str(root))))
    docker_root = info.get("DockerRootDir") if info else None
    if isinstance(docker_root, str):
        results.append(check_disk("docker disk", docker_root, world.free_bytes(docker_root)))

    results.append(
        check_tool(
            "uv",
            world.which("uv"),
            "install uv: https://docs.astral.sh/uv/getting-started/installation/ "
            f"({GUIDE}, part B)",
        )
    )
    make_hint = (
        f"`sudo apt install make`; on native Windows use the `python -m` commands ({NATIVE_GUIDE})"
    )
    results.append(
        check_tool(
            "make",
            world.which("make"),
            make_hint,
            missing_status=Status.WARN if platform == "windows" else Status.FAIL,
        )
    )
    results.append(check_python(world.python_version))

    env_text = world.read_text(root / ".env")
    results.append(check_env_file(env_text is not None))
    merged = merge_env(env_text, world.environ)
    if env_text is not None:
        results.extend(check_env_keys(merged, profile))
        results.append(check_env_settings(merged, profile))
    results.append(check_shell_exports(world.environ))
    results.append(check_old_env_lines(env_text))

    guard_dir = default_guard_dir(root, world.environ)
    commit = world.environ.get("MAILGUARD_COMMIT") or DEFAULT_MAILGUARD_COMMIT
    results.append(check_guard(world.require_guard, guard_dir, commit))
    results.append(check_pinned(world.pinned_problems()))
    results.append(check_sklearn(world.installed_sklearn))

    compose_text = world.read_text(root / "docker-compose.yml")
    if compose_text is not None:
        ports = published_ports(compose_text, merged)
        ports.setdefault(GUARD_WORKER_PORT, "guard-worker health endpoint")
        ps = world.run(["docker", "compose", "ps", "--format", "json"])
        owned = parse_compose_ps(ps.stdout) if ps and ps.returncode == 0 else set()
        results.append(check_ports(ports, owned=owned, in_use=world.port_in_use))

    local_model: bool | None = None
    if profile is not None:
        ollama = check_ollama(profile, merged, platform, world.http_get)
        local_model = ollama is not None
        if ollama is not None:
            results.append(ollama)
    results.append(check_gpu(world.which("nvidia-smi"), local_model=local_model))
    results.append(check_reader(reader, profile))
    return results


# --- the real machine --------------------------------------------------------------------------


def _run(command: Sequence[str]) -> CommandResult | None:
    try:
        done = subprocess.run(
            list(command),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"`{' '.join(command[:3])}` did not answer within 30 s")
    return CommandResult(done.returncode, done.stdout, done.stderr)


def _free_bytes(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def real_world() -> World:
    """The machine the doctor is running on."""
    return World(
        system=platform_module.system(),
        proc_version=_read_text(Path("/proc/version")),
        repo_root=REPO_ROOT,
        environ=dict(os.environ),
        run=_run,
        which=shutil.which,
        free_bytes=_free_bytes,
        port_in_use=port_in_use,
        http_get=http_get,
        read_text=_read_text,
        systemd_running=Path("/run/systemd/system").is_dir(),
        python_version=(sys.version_info[0], sys.version_info[1], sys.version_info[2]),
        installed_sklearn=pinned.installed_scikit_learn(),
        require_guard=require_pinned_worktree,
        pinned_problems=pinned.verify,
    )


def main(argv: Sequence[str] | None = None, *, world: World | None = None) -> int:
    """Print every check; exit 1 when any FAILs."""
    parser = argparse.ArgumentParser(
        description="Check that this machine can run the v2 benchmark."
    )
    parser.add_argument(
        "--model-profile",
        help="the model you are about to run (model_profiles.py); adds that model's key, "
        "and for a local model the Ollama and GPU checks",
    )
    parser.add_argument(
        "--reader",
        help="the reader model of the meaning-based column; it may not be a benchmarked model",
    )
    args = parser.parse_args(argv)
    results = run_checks(
        world or real_world(), model_profile=args.model_profile, reader=args.reader
    )
    for result in results:
        for line in result.lines():
            print(line)
    counts = {status: sum(r.status is status for r in results) for status in Status}
    print(f"{counts[Status.OK]} ok, {counts[Status.WARN]} WARN, {counts[Status.FAIL]} FAIL")
    return 1 if counts[Status.FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())

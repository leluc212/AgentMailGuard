"""Per-model container environment for the live v2 benchmark (task 7.20; ADR-0011).

The v2 benchmark sends every case through rag-email's own services, so the containers that
call a model (api, triage-worker, knowledge-worker, ai-worker) must carry the benchmarked
model's settings. This module renders them:

    uv run python -m evaluation.mailguard_bench.live.stack_env --model-profile qwen2.5-7b

It writes them to a git-ignored file (.env.stack) and prints the ``docker compose ... up -d
--no-deps`` command that applies them to those four services. Postgres, RabbitMQ and MinIO are
never part of it. This module never runs docker: the owner runs the printed command
(docs/demo-runbook.md section 9.9).

What the file holds, the same for every run apart from the model:

- LLM__* from the model profile, with a loopback base URL pointed at the host (inside a
  container ``localhost`` is the container; ``host.docker.internal`` is the host, mapped by
  docker-compose.yml's ``extra_hosts``), and the summarizer model set to the same model: one
  benchmarked model in every LLM role. The summarizer model travels as BENCH_SUMMARIZER_MODEL,
  which docker-compose.yml maps to the containers' SUMMARIZATION__SUMMARIZER_MODEL. An .env
  made before task 7.20 still holds SUMMARIZATION__SUMMARIZER_MODEL=gpt-4o-mini (the example
  set it) and the ai-worker now honours that setting, so Compose must never read the setting's
  own name from .env.
- Gemini ``gemini-embedding-001`` at 1536 dimensions for every run. Its key is the Gemini key
  kept in .env as LLM__OPENAI_API_KEY, which is not the LLM key of a profile such as
  GPT-4o-mini; it is read at run time and never written to a tracked file.
- The retrieval budget for a hosted embedding call, and the reranker settings.

The file holds API keys, so it is written owner-only and only where git ignores it, and the
keys are never printed. ``uv run`` does not load .env, so like the runner this reads it with
the process environment on top (``with_dot_env``). Compose lets a variable exported in the
shell win over every env file; a shell value that differs from a rendered one is refused.

C0 drafts in the ai-worker container; C0T to C3 draft in the guard-worker, a host process. The
guard-worker and the live runner take the model profile's LLM__* keys from the profile and
every other setting from the shell and .env, never from .env.stack, so an .env that disagrees
with the containers would run the guarded configs on other settings than C0 with no error to
show it. `host_env_problems` finds those settings, and this refuses to write while any exists.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from evaluation.mailguard_bench.model_profiles import (
    PROFILES,
    ModelProfile,
    ModelProfileError,
    get_profile,
    profile_env,
    with_dot_env,
)
from packages.core.settings import GEMINI_OPENAI_BASE_URL

CONTAINER_HOST_ALIAS = "host.docker.internal"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 1536
EMBEDDING_KEY_ENV = "LLM__OPENAI_API_KEY"
# Not SUMMARIZATION__SUMMARIZER_MODEL: docker-compose.yml maps this name to it (see above).
SUMMARIZER_MODEL_ENV = "BENCH_SUMMARIZER_MODEL"
RETRIEVAL_TIMEOUT_MS = 3000
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_TIMEOUT_MS = 1000
DEFAULT_LLM_TIMEOUT_S = 60.0
MIN_LLM_TIMEOUT_S = 0.1  # LLMTiersSettings.timeout_s is ge=0.1
DEFAULT_ENV_FILE = ".env"
DEFAULT_OUT = ".env.stack"  # git-ignored (.gitignore) and never sent to a docker build
# The app services that call a model. Postgres, RabbitMQ and MinIO are never restarted.
APP_SERVICES = ("api", "triage-worker", "knowledge-worker", "ai-worker")

# What the host processes (the guard-worker, the live runner) must read like the containers do.
# The host must state these: its code default is not the container's value (a mock embedder,
# a 15 s timeout), or it must not be left to a default (the vector column's width, R5.10).
HOST_MUST_SET = (
    "EMBEDDING__MOCK",
    "EMBEDDING__MODEL_NAME",
    "EMBEDDING__DIMENSION",
    "EMBEDDING__BASE_URL",
    "EMBEDDING__API_KEY",
    "LLM__TIMEOUT_S",
    "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
)
# The code default is the container's value (design contract, packages A and B): unset is fine.
HOST_MAY_OMIT = (
    "RETRIEVAL__RERANK_ENABLED",
    "RETRIEVAL__RERANK_MODEL",
    "RETRIEVAL__RERANK_TIMEOUT_MS",
)
# Read by no container from a file (Compose forwards no ROUTING__*, and the summarizer model
# only under its stack-only name), so a line in .env would reach the host processes alone. An
# .env made before task 7.20 has both: gpt-4o-mini as the summarizer, a shorter lane list.
HOST_MUST_NOT_SET = ("ROUTING__CONFIGURED_CONSUMERS",)
SUMMARIZER_MODEL_SETTING = "SUMMARIZATION__SUMMARIZER_MODEL"
_SECRET_SETTINGS = frozenset({"EMBEDDING__API_KEY"})
_FLAG_SETTINGS = frozenset({"EMBEDDING__MOCK", "RETRIEVAL__RERANK_ENABLED"})
_NUMBER_SETTINGS = frozenset(
    {
        "EMBEDDING__DIMENSION",
        "LLM__TIMEOUT_S",
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
        "RETRIEVAL__RERANK_TIMEOUT_MS",
    }
)
_URL_SETTINGS = frozenset({"EMBEDDING__BASE_URL"})
_TRUE = frozenset({"1", "t", "true", "y", "yes", "on"})  # what the settings accept for a bool
_FALSE = frozenset({"0", "f", "false", "n", "no", "off"})


class StackEnvError(RuntimeError):
    """The stack environment cannot be rendered or written safely."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def container_url(url: str) -> str:
    """The URL as a container must reach it: a loopback host becomes the host alias.

    Inside a container ``localhost`` is the container itself, so an endpoint on the desktop
    (Ollama) is reached through ``host.docker.internal``, which docker-compose.yml maps to
    the host with ``extra_hosts``. Any other host is left as it is.
    """
    parts = urlsplit(url)
    if parts.hostname is None or not _is_loopback(parts.hostname):
        return url
    netloc = CONTAINER_HOST_ALIAS if parts.port is None else f"{CONTAINER_HOST_ALIAS}:{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def render_stack_env(
    profile: ModelProfile,
    environ: Mapping[str, str],
    *,
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
) -> dict[str, str]:
    """The settings the app containers need for one benchmark model.

    ``environ`` is the process environment over .env (`with_dot_env`). The embedding is the
    same for every model, and its key is the Gemini key kept in .env as LLM__OPENAI_API_KEY,
    which is not the LLM key of a profile such as GPT-4o-mini.

    Raises:
        ModelProfileError: If the profile needs a key that ``environ`` does not hold.
        StackEnvError: If the Gemini embedding key is missing or the timeout is too small.
    """
    if llm_timeout_s < MIN_LLM_TIMEOUT_S:
        raise StackEnvError(f"LLM__TIMEOUT_S must be at least {MIN_LLM_TIMEOUT_S} seconds")
    llm = profile_env(profile, environ)
    embedding_key = (environ.get(EMBEDDING_KEY_ENV) or "").strip()
    if not embedding_key:
        raise StackEnvError(
            f"{EMBEDDING_KEY_ENV} is empty; every run embeds with Gemini, and its key is read "
            "from that variable in .env"
        )
    return {
        **llm,
        "LLM__OPENAI_BASE_URL": container_url(llm["LLM__OPENAI_BASE_URL"]),
        "LLM__TIMEOUT_S": str(float(llm_timeout_s)),
        SUMMARIZER_MODEL_ENV: profile.model,
        "EMBEDDING__MOCK": "false",
        "EMBEDDING__MODEL_NAME": EMBEDDING_MODEL,
        "EMBEDDING__DIMENSION": str(EMBEDDING_DIMENSION),
        "EMBEDDING__BASE_URL": GEMINI_OPENAI_BASE_URL,
        "EMBEDDING__API_KEY": embedding_key,
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": str(RETRIEVAL_TIMEOUT_MS),
        "RETRIEVAL__RERANK_ENABLED": "true",
        "RETRIEVAL__RERANK_MODEL": RERANK_MODEL,
        "RETRIEVAL__RERANK_TIMEOUT_MS": str(RERANK_TIMEOUT_MS),
    }


def describe_route(routing: Mapping[str, Any]) -> str:
    """One line for the operator: which provider the LLM calls are pinned to, and how strictly."""
    order = ", ".join(str(slug) for slug in routing.get("order") or [])
    precision = "/".join(str(q) for q in routing.get("quantizations") or [])
    parts = [f"pinned to {order}"]
    if precision:
        parts.append(f"precision {precision}")
    parts.append("fallbacks off" if routing.get("allow_fallbacks") is False else "fallbacks ON")
    parts.append("every call records the provider that served it")
    return ", ".join(parts)


def format_env_file(values: Mapping[str, str], *, header: str = "") -> str:
    """The settings as the text of a Compose env file, one ``NAME='value'`` line each.

    Compose interpolates ``$`` in unquoted and double-quoted values and keeps single-quoted
    values literally, so every value is single-quoted. A value that cannot be written that way
    (a single quote, a backslash or a line break) is refused; the message names the setting,
    never the value, which may be a key.

    Raises:
        StackEnvError: If a value cannot be held literally by an env file.
    """
    lines = [f"# {line}" for line in header.splitlines()]
    for name, value in values.items():
        if any(char in value for char in "'\\\n\r\0"):
            raise StackEnvError(
                f"the value of {name} holds a quote, a backslash or a line break, "
                "which an env file cannot keep literally"
            )
        lines.append(f"{name}='{value}'")
    return "\n".join(lines) + "\n"


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(directory), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise StackEnvError("git is required to check that the file is git-ignored") from exc


def _nearest_existing(directory: Path) -> Path:
    while not directory.exists():
        directory = directory.parent
    return directory


def require_git_ignored(path: Path) -> None:
    """Refuse a path git could commit: the file holds API keys.

    Inside a git work tree the path must be ignored and untracked (`git check-ignore` reports a
    tracked file as not ignored, whatever the patterns say). A path outside any work tree is
    fine: no repository can commit it.

    Raises:
        StackEnvError: If git could commit the path, or git cannot answer.
    """
    target = path.resolve()
    directory = _nearest_existing(target.parent)
    inside = _git(directory, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    ignored = _git(directory, "check-ignore", "--quiet", "--", str(target))
    if ignored.returncode == 0:
        return
    if ignored.returncode == 1:
        raise StackEnvError(
            f"{path} is not git-ignored (or is tracked) and the file holds API keys; "
            "write it where .gitignore covers it, such as .env.stack"
        )
    raise StackEnvError(f"git check-ignore failed for {path}: {ignored.stderr.strip()}")


def write_env_file(path: Path, values: Mapping[str, str], *, header: str = "") -> None:
    """Write the settings to ``path``: owner-only, and only where git ignores the file.

    Raises:
        StackEnvError: If a value cannot be written literally or git could commit ``path``.
    """
    text = format_env_file(values, header=header)  # refuses a bad value before any file exists
    require_git_ignored(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        os.fchmod(handle.fileno(), 0o600)  # a file that already existed keeps its old mode
        handle.write(text)


def compose_command(env_files: Sequence[Path]) -> list[str]:
    """The command that applies the stack env to the app services, and only to them.

    ``--env-file`` replaces Compose's default .env, so every file is named, the later one
    winning. ``--no-deps`` keeps Compose from touching the services they depend on.
    """
    command = ["docker", "compose"]
    for env_file in env_files:
        command += ["--env-file", str(env_file)]
    return [*command, "up", "-d", "--no-deps", *APP_SERVICES]


def shell_conflicts(values: Mapping[str, str], process_env: Mapping[str, str]) -> list[str]:
    """Settings the shell sets to another value.

    Compose lets a variable exported in the shell win over every env file, so the container
    would silently get the shell's value instead of the rendered one.
    """
    return [name for name, value in values.items() if process_env.get(name, value) != value]


def _flag(text: str) -> bool | None:
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    return False if lowered in _FALSE else None


def _agree(name: str, host: str, container: str) -> bool:
    """Whether the host's spelling of a setting means what the containers get.

    The settings parse a number as a number (``60`` is ``60.0``), accept several spellings of
    a flag, and the embedder ignores a trailing slash on the base URL.
    """
    if name in _FLAG_SETTINGS:
        parsed = _flag(host)
        return parsed is not None and parsed == _flag(container)
    if name in _NUMBER_SETTINGS:
        try:
            return float(host) == float(container)
        except ValueError:
            return False
    if name in _URL_SETTINGS:
        return host.rstrip("/") == container.rstrip("/")
    return host == container


def host_env_problems(values: Mapping[str, str], environ: Mapping[str, str]) -> list[str]:
    """Settings on which the host processes would differ from the containers.

    ``values`` is the rendered stack env and ``environ`` what the host processes read: the
    shell over .env (`with_dot_env`). Blank counts as unset. Each problem is one line that
    starts with the setting's name and says what it must be. The host's own values are never
    repeated, because one may be a key; only the containers' values are, and never their key.
    """
    problems: list[str] = []
    for name in (*HOST_MUST_SET, *HOST_MAY_OMIT):
        host = (environ.get(name) or "").strip()
        if host and _agree(name, host, values[name]):
            continue
        if not host and name in HOST_MAY_OMIT:
            continue
        must = (
            f"the Gemini key (the value of {EMBEDDING_KEY_ENV})"
            if name in _SECRET_SETTINGS
            else values[name]
        )
        problems.append(
            f"{name} must be {must}" if name in HOST_MUST_SET else f"{name} must be unset or {must}"
        )
    # Unset is the same as the containers' model: the FAST tier, which the profile points at it.
    summarizer = (environ.get(SUMMARIZER_MODEL_SETTING) or "").strip()
    if summarizer and summarizer != values[SUMMARIZER_MODEL_ENV]:
        problems.append(
            f"{SUMMARIZER_MODEL_SETTING} must be unset or {values[SUMMARIZER_MODEL_ENV]}"
        )
    problems.extend(
        f"{name} must be unset" for name in HOST_MUST_NOT_SET if (environ.get(name) or "").strip()
    )
    return problems


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--model-profile",
        required=True,
        choices=sorted(PROFILES),
        help="the benchmarked model (model_profiles.py); one model per run, in every LLM role",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(DEFAULT_ENV_FILE),
        help="the .env that holds the keys, and that the guard-worker and the runner read "
        f"(default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUT),
        help=f"where to write the settings; git must ignore it (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--llm-timeout-s",
        type=float,
        default=DEFAULT_LLM_TIMEOUT_S,
        help=f"per-call LLM timeout of the containers (default: {DEFAULT_LLM_TIMEOUT_S:g}, "
        "what the v1 benchmark used)",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    profile = get_profile(args.model_profile)
    environ = with_dot_env(os.environ, args.env_file)
    values = render_stack_env(profile, environ, llm_timeout_s=args.llm_timeout_s)
    shadowed = shell_conflicts(values, os.environ)
    if shadowed:
        raise StackEnvError(
            f"the shell sets {', '.join(shadowed)} to other values; docker compose lets the "
            "shell win over the env files, so the containers would not get the rendered "
            "settings. Unset them (unset NAME) and run this again"
        )
    disagreements = host_env_problems(values, environ)
    if disagreements:
        raise StackEnvError(
            f"the guard-worker and the runner are host processes: they read {args.env_file} "
            "and the shell, never .env.stack, so they would run on other settings than the "
            f"containers: {'; '.join(disagreements)}. Fix {args.env_file} as "
            "docs/demo-runbook.md section 9.9 step 1 says and run this again"
        )
    write_env_file(
        args.out,
        values,
        header=(
            f"Container settings of the live v2 benchmark, model profile {profile.name}.\n"
            "Generated by evaluation.mailguard_bench.live.stack_env (task 7.20).\n"
            "Holds API keys: git-ignored, never commit it."
        ),
    )
    env_files = [args.env_file] if args.env_file.is_file() else []
    print(
        f"ok stack env for {profile.name} written to {args.out} "
        f"({len(values)} settings; git-ignored, owner-only, keys not shown)"
    )
    print(
        f"   llm       {profile.model} at {values['LLM__OPENAI_BASE_URL']}"
        f" (timeout {values['LLM__TIMEOUT_S']} s)"
    )
    if values.get("LLM__OPENAI_PROVIDER_ROUTING"):
        print(f"   route     {describe_route(json.loads(values['LLM__OPENAI_PROVIDER_ROUTING']))}")
    print(
        f"   embedding {values['EMBEDDING__MODEL_NAME']}, {values['EMBEDDING__DIMENSION']} "
        f"dimensions; retrieval budget {values['RETRIEVAL__RETRIEVAL_TIMEOUT_MS']} ms; "
        f"reranker {values['RETRIEVAL__RERANK_MODEL']}"
    )
    print(
        f"   host      {args.env_file} agrees, so the guard-worker and the runner read "
        "the same embedding, retrieval budget, LLM timeout, summarizer and lanes"
    )
    print(
        f"apply it to {', '.join(APP_SERVICES)} "
        "(Postgres, RabbitMQ and MinIO stay up; this command is not run for you):"
    )
    print(shlex.join(compose_command([*env_files, args.out])))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except (StackEnvError, ModelProfileError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

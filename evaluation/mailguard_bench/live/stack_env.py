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
  docker-compose.yml's ``extra_hosts``), and SUMMARIZATION__SUMMARIZER_MODEL set to the same
  model: one benchmarked model in every LLM role.
- Gemini ``gemini-embedding-001`` at 1536 dimensions for every run. Its key is the Gemini key
  kept in .env as LLM__OPENAI_API_KEY, which is not the LLM key of a profile such as
  GPT-4o-mini; it is read at run time and never written to a tracked file.
- The retrieval budget for a hosted embedding call, and the reranker settings.

The file holds API keys, so it is written owner-only and only where git ignores it, and the
keys are never printed. ``uv run`` does not load .env, so like the runner this reads it with
the process environment on top (``with_dot_env``). Compose lets a variable exported in the
shell win over every env file; a shell value that differs from a rendered one is refused.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
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
RETRIEVAL_TIMEOUT_MS = 3000
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_TIMEOUT_MS = 1000
DEFAULT_LLM_TIMEOUT_S = 60.0
MIN_LLM_TIMEOUT_S = 0.1  # LLMTiersSettings.timeout_s is ge=0.1
DEFAULT_ENV_FILE = ".env"
DEFAULT_OUT = ".env.stack"  # git-ignored (.gitignore) and never sent to a docker build
# The app services that call a model. Postgres, RabbitMQ and MinIO are never restarted.
APP_SERVICES = ("api", "triage-worker", "knowledge-worker", "ai-worker")


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
        "SUMMARIZATION__SUMMARIZER_MODEL": profile.model,
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
        help=f"the .env that holds the keys (default: {DEFAULT_ENV_FILE})",
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
    values = render_stack_env(
        profile, with_dot_env(os.environ, args.env_file), llm_timeout_s=args.llm_timeout_s
    )
    shadowed = shell_conflicts(values, os.environ)
    if shadowed:
        raise StackEnvError(
            f"the shell sets {', '.join(shadowed)} to other values; docker compose lets the "
            "shell win over the env files, so the containers would not get the rendered "
            "settings. Unset them (unset NAME) and run this again"
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
    print(
        f"   embedding {values['EMBEDDING__MODEL_NAME']}, {values['EMBEDDING__DIMENSION']} "
        f"dimensions; retrieval budget {values['RETRIEVAL__RETRIEVAL_TIMEOUT_MS']} ms; "
        f"reranker {values['RETRIEVAL__RERANK_MODEL']}"
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

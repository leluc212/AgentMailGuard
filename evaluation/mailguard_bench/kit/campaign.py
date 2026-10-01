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

    run:  stack env (live/stack_env.py, with its refusals) ─▶ one embedding call with the
          runner's EMBEDDING__* (kit/embedding_check.py: refused unless one 1536-wide vector comes
          back) ─▶ docker compose up --no-deps ─▶ wait
          a guarded config pending: copy the reranker model of the ai-worker image to
                   .cache/reranker, unless that already is this image's (see below)
          for each config  C0:      docker compose start ai-worker ─▶ wait healthy ─▶ live.run
                           guarded: docker compose stop ai-worker ─▶ start the guard-worker
                                    (host process, RETRIEVAL__RERANK_MODEL_DIR=<.cache/reranker>
                                    in its environment only, log in
                                    <run>/raw/guard-worker.<config>.log)
                                    ─▶ wait: pid file newer than the start AND /readyz answers
                                    ─▶ live.run ─▶ stop it and confirm it exited
          after each config: the triage-worker's and the ai-worker's log lines of that config
                   (``docker compose logs --since <its start>``) appended to
                   <run>/raw/services.<config>.log: their ``llm_inference`` lines record
                   which provider served every call of a pinned route (triage, the
                   summarizer and C0's generation reach no result row)
          a runner that exits ``route.ROUTE_STOP_EXIT`` (its breaker printed ``STOP``: a used-up
                   quota or credit of any provider, or a pinned route that stopped serving): the
                   campaign stops at once, no next config, no retry pass; the same command
                   resumes it later, also after another model's run in between
          one retry pass over the configs that failed or left error rows
          reports: the report (a v1 folder: report, analyses, report); the meaning column is
                   ``report --reader``: its LLM__* settings may not be exported during a run, so
                   it is not part of one. It passes ``--retry-errors``, so a rerun reads again
                   the drafts a failed read left unread

The reranker model (R11.1). C0 drafts in the ai-worker container, which reranks with the
cross-encoder baked into its image (``RETRIEVAL__RERANK_MODEL_DIR=/app/.cache/reranker``, read
with no network). The guard-worker is a host process with no such variable: left alone it would
download the model from the internet at its first rerank, a network dependency that may also
fetch another revision than C0 uses. So ``setup`` (once the stack is up) and ``run`` (before the
first config, when a guarded config is pending; C0 alone does not need it) copy the image's folder
with ``docker compose cp ai-worker:/app/.cache/reranker <repo>/.cache/reranker``, a git-ignored
folder, unless that folder already is this image's: ``.cache/reranker.image-id``, next to it, holds
the id of the image the ai-worker container was created from, and is written only after a complete
copy, so a rebuilt image or an interrupted copy gets a fresh one. ``docker compose cp`` works on a
stopped container too. The guard-worker is started with ``RETRIEVAL__RERANK_MODEL_DIR=<that
folder>`` in ITS environment only (``Host.spawn(env=...)``): the kit's own environment, the runner
and the containers never get it, and the refusal of a ``RETRIEVAL__*`` variable exported in the
shell stays. If the copy fails the run stops with a FAIL that names the command and the fix,
before any config runs. ``run --dry-run`` prints the command and touches nothing. By hand:
docs/demo-runbook.md section 9.9 step 4.

It is pure Python: no bash, so it runs natively on Windows as well (the stop signal of the
guard-worker is chosen by ``kit/system.py``). It never starts a model call of its own; the
runner and the guard-worker it starts do. Its one call of its own is the embedding check, before
the stack is touched. Ctrl+C stops the guard-worker cleanly and prints the
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
import shutil
import signal
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evaluation.mailguard_bench import model_profiles
from evaluation.mailguard_bench.guard_build import NATIVE_CONFIG
from evaluation.mailguard_bench.guard_env import (
    REPO_ROOT,
    GuardEnvError,
    guard_paths_from_env,
    require_pinned_worktree,
)
from evaluation.mailguard_bench.kit.embedding_check import EmbeddingCheckError, check_embedding
from evaluation.mailguard_bench.kit.steplog import LOG_NAME, StepLog, finished_configs
from evaluation.mailguard_bench.kit.system import Host, ProcessHandle, SystemHost
from evaluation.mailguard_bench.live import stack_env
from evaluation.mailguard_bench.live.guard_worker import DEFAULT_PORT, HOST, pid_path
from evaluation.mailguard_bench.meaning import reader_model_problems
from evaluation.mailguard_bench.route import ROUTE_STOP_EXIT
from evaluation.mailguard_bench.runner import RESULTS_ROOT, meta_path
from evaluation.mailguard_bench.scheme import (
    SCHEME_V1,
    SCHEME_V2,
    SchemeMixError,
    configs_for,
    folder_scheme,
)

# The configs of Friday's v2 run: the scheme's own list (ADR-0012 decision 11), never a copy. The
# kit accepts any list (--configs); live.run and guard_worker validate the names.
DEFAULT_V2_CONFIGS = configs_for(SCHEME_V2)

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
SECRET_NAME_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")
MIN_SECRET_LENGTH = 8
RERANK_SERVICE = "ai-worker"
"""The service whose image bakes the cross-encoder in; C0 drafts (and reranks) in its container."""
RERANK_IMAGE_DIR = "/app/.cache/reranker"
"""Where the image keeps the model: the Dockerfile's ``ENV RETRIEVAL__RERANK_MODEL_DIR``."""
RERANK_MODEL_DIR_ENV = "RETRIEVAL__RERANK_MODEL_DIR"
"""The setting that points a process at a folder holding the model, which is then read offline."""
RERANK_COPY_DIR = Path(".cache") / "reranker"
"""The host's copy of that folder, under the repo root: git-ignored and out of the build context."""
RERANK_COPY_MARKER = Path(".cache") / "reranker.image-id"
"""Next to the copy: the id of the image it came from, written only once the copy is complete."""
LOGGED_SERVICES = ("triage-worker", "ai-worker")
"""The model-calling containers whose log lines of a config are kept with the run."""
MEANING_MODULE = "evaluation.mailguard_bench.meaning"
"""The meaning step's module; it exits ``ROUTE_STOP_EXIT`` when the reader's quota is used up."""
RESTORE_ENV = (
    "put .env back: comment out the EMBEDDING__ lines, set LLM__TIMEOUT_S back to 15.0 and "
    "RETRIEVAL__CATEGORY_FILTER_ENABLED back to true; Compose forwards all three, so otherwise "
    "the normal stack keeps the paid embedding endpoint, a 60 s LLM timeout and no category filter"
)
"""What `make up` after the last model needs first (demo-runbook section 9.9 step 8)."""


def services_log_path(raw_dir: Path, config: str) -> Path:
    """Where a config's container log lines go (every pass appends)."""
    return raw_dir / f"services.{config}.log"


def services_log_command(since: datetime) -> list[str]:
    """The containers' log lines from ``since`` on: the served provider of every call."""
    stamp = since.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return [
        "docker",
        "compose",
        "logs",
        "--no-color",
        "--timestamps",
        "--since",
        stamp,
        *LOGGED_SERVICES,
    ]


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
    # The embedding check's transport: None is the network (the runner's machine); tests pass a
    # fake one, so no test ever reaches an embedding endpoint.
    embedding_transport: httpx.AsyncBaseTransport | None = None


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
    )
    module = (
        "python -m evaluation.mailguard_bench.kit.campaign run "
        f"--model-profile {options.model_profile} --run {options.run} --configs {configs}"
        + (f" --limit {options.limit}" if options.limit is not None else "")
        + f" --concurrency {options.concurrency}"
    )
    return [make, module]


# --- reports ----------------------------------------------------------------------------------


def report_commands(ctx: KitContext, run_dir: Path, reader: str | None) -> list[list[str]]:
    """Runbook step 7; with a reader the meaning column, then the reports again.

    The meaning step retries the reads an earlier ``report --reader`` left as error rows, so the
    same command, run again, completes a column a failed read left short.

    A scheme v2 folder (every kit run, and one with no meta yet) gets the report alone: the no-API
    analyses read C3 as the full guard, which is C7 in v2, and refuse a v2 folder until task 7.23,
    as `make mailguard-analyses` does. A scheme v1 folder keeps report, analyses, report. A folder
    that mixes schemes gets the report, which refuses it and says why.
    """
    guard_dir = ctx.environ.get("MAILGUARD_DIR")
    guard = ["--mailguard-dir", guard_dir] if guard_dir else []
    report = module_command(
        ctx, "evaluation.mailguard_bench.report", "--run-dir", str(run_dir), *guard
    )
    try:
        scheme = folder_scheme(run_dir) or SCHEME_V2
    except SchemeMixError:
        scheme = SCHEME_V2
    scored = [report]
    if scheme == SCHEME_V1:
        analyses = module_command(
            ctx, "evaluation.mailguard_bench.analyses", "--run-dir", str(run_dir), *guard
        )
        scored = [report, analyses, report]
    if not reader:
        return scored
    # --retry-errors: a read that failed (a timeout, a rate limit past its back-off, a used-up
    # quota) is read again by the next `make bench-report` (owner decision 2026-10-01, ADR-0014)
    meaning = module_command(
        ctx,
        MEANING_MODULE,
        "--run-dir",
        str(run_dir),
        "--reader-model",
        reader,
        "--retry-errors",
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
        if status == ROUTE_STOP_EXIT and MEANING_MODULE in command:
            ctx.err(
                "STOP the reader's quota, balance, spend limit or daily cap is used up (the "
                "meaning step's STOP line above says which). Restore it (a daily cap resets the "
                "next day), then run the same `make bench-report` command again: it reads the "
                "drafts not read yet and retries the reads that failed"
            )
            break
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


# --- the reranker model of the guarded configs ------------------------------------------------


class RerankModelError(Exception):
    """The reranker model could not be copied out of the image; the text says what to do."""


@dataclass(frozen=True)
class RerankCopy:
    """The host's copy of the image's reranker model, and whether this call had to make it."""

    folder: Path
    image: str
    copied: bool


def rerank_copy_paths(repo_root: Path) -> tuple[Path, Path]:
    """The folder the guard-worker reads its reranker model from, and the marker next to it."""
    return (repo_root / RERANK_COPY_DIR).absolute(), (repo_root / RERANK_COPY_MARKER).absolute()


def rerank_copy_command(repo_root: Path) -> list[str]:
    """``docker compose cp`` of the image's model folder to a destination that does not exist.

    Docker puts the contents of a source folder into a destination that does not exist yet (its
    parent must), and the folder itself, under its own name, inside one that does: the model
    would end up at ``reranker/reranker``. ``sync_rerank_model`` therefore clears the destination.
    """
    folder, _ = rerank_copy_paths(repo_root)
    return ["docker", "compose", "cp", f"{RERANK_SERVICE}:{RERANK_IMAGE_DIR}", str(folder)]


def _rerank_failure(problem: str) -> RerankModelError:
    return RerankModelError(
        f"{problem}. The guard-worker needs the reranker model of the {RERANK_SERVICE} image: "
        "without a copy it downloads the model from the internet at its first rerank, a network "
        "dependency that may also fetch another revision than C0 reranks with. Fix: make sure "
        f"Docker is running, the {RERANK_SERVICE} container exists (`make bench-setup` creates "
        f"it) and {RERANK_COPY_DIR.parent}/ is writable, then run the same command again "
        "(by hand: docs/demo-runbook.md section 9.9 step 4)"
    )


def _ai_worker_image(ctx: KitContext) -> str:
    """The id of the image the ai-worker container was created from: what ``docker cp`` copies.

    Only commands that answer for a stopped container are used: ``ps -a`` lists it, ``inspect``
    reads it in any state, and ``docker cp`` takes a running or a stopped container.
    """
    listing = ["docker", "compose", "ps", "-a", "-q", RERANK_SERVICE]
    found = ctx.host.capture(listing, cwd=ctx.repo_root)
    if found.returncode != 0:
        raise _rerank_failure(f"{_join(listing)} exited {found.returncode}")
    containers = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    if not containers:
        raise _rerank_failure(f"no {RERANK_SERVICE} container exists")
    inspect = ["docker", "inspect", "-f", "{{.Image}}", containers[0]]
    asked = ctx.host.capture(inspect, cwd=ctx.repo_root)
    if asked.returncode != 0:
        raise _rerank_failure(f"{_join(inspect)} exited {asked.returncode}")
    image = asked.stdout.strip()
    if not image:
        raise _rerank_failure(f"{_join(inspect)} printed no image id")
    return image


def _holds_files(folder: Path) -> bool:
    return folder.is_dir() and any(folder.iterdir())


def _copy_is_current(folder: Path, marker: Path, image: str) -> bool:
    """True when ``folder`` is a complete copy made from ``image`` (its marker is written last)."""
    try:
        return marker.read_text(encoding="utf-8").strip() == image and _holds_files(folder)
    except OSError:
        return False


def _remove(path: Path) -> None:
    """Delete a folder, or whatever file or link stands in its place; nothing when it is absent."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def sync_rerank_model(ctx: KitContext) -> RerankCopy:
    """Make the host's copy of the ai-worker image's reranker model match that image.

    The guard-worker reads its model from this copy, offline, instead of downloading one at its
    first rerank: the weights C0 reranks with in the container. Nothing is copied when the marker
    names the image the container was created from and the folder holds files; otherwise the old
    copy and its marker are removed first, the image's folder is copied out, and the marker is
    written last, so a failed or interrupted copy is never taken for a good one.

    Raises:
        RerankModelError: the image cannot be found, the copy fails or leaves nothing; the text
            names the command and the fix.
    """
    folder, marker = rerank_copy_paths(ctx.repo_root)
    image = _ai_worker_image(ctx)
    short = image.removeprefix("sha256:")[:12]
    if _copy_is_current(folder, marker, image):
        ctx.out(
            f"ok reranker model {RERANK_COPY_DIR} is already the {RERANK_SERVICE} image's "
            f"({short}); the guard-worker reads it offline"
        )
        return RerankCopy(folder, image, copied=False)
    command = rerank_copy_command(ctx.repo_root)
    try:
        marker.unlink(missing_ok=True)  # first: a copy without its marker is never trusted
        _remove(folder)  # docker would put the model inside a folder that is there
        folder.parent.mkdir(parents=True, exist_ok=True)  # and needs the parent to exist
    except OSError as exc:
        raise _rerank_failure(f"cannot prepare {folder}: {exc}") from exc
    ctx.out(
        f"copying the reranker model out of the {RERANK_SERVICE} image ({short}) "
        f"to {RERANK_COPY_DIR}"
    )
    try:
        code = ctx.host.run(command, cwd=ctx.repo_root)
        if code != 0:
            raise _rerank_failure(f"{_join(command)} exited {code}")
        if not _holds_files(folder):
            raise _rerank_failure(f"{_join(command)} copied nothing to {folder}")
        try:
            marker.write_text(f"{image}\n", encoding="utf-8")
        except OSError as exc:
            raise _rerank_failure(f"cannot write {marker}: {exc}") from exc
    except BaseException:  # a failure or Ctrl+C: leave neither half a copy nor a marker for it
        shutil.rmtree(folder, ignore_errors=True)
        with suppress(OSError):
            marker.unlink(missing_ok=True)
        raise
    ctx.out(
        f"ok reranker model copied from the {RERANK_SERVICE} image {short} to {RERANK_COPY_DIR}; "
        "the guard-worker reads it offline"
    )
    return RerankCopy(folder, image, copied=True)


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
            status = _build_reports(self.ctx, self.run_dir, None, self.log, dry=self.dry)
            if status == 0 and not self.dry:
                self.ctx.out(
                    f"ok reports in {self.run_dir}. Next model: its own `make bench-run`. "
                    f"After the last model: delete .env.stack; before any `make up`, {RESTORE_ENV} "
                    "(docs/BENCHMARK.md F1, demo-runbook section 9.9 step 8)"
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
            model_profiles.get_profile(opts.model_profile)
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
        return problems

    def _print_resume(self, *, stream: Callable[[str], None]) -> None:
        make, module = resume_commands(self.opts)
        stream(f"  {make}")
        stream(f"  native Windows, inside the guide's overlay: {module}")

    # -- the stack ---------------------------------------------------------------------------

    def _run_configs(self, pending: Sequence[str]) -> dict[str, _Outcome]:
        self._bring_up_stack()
        if any(config != NATIVE_CONFIG for config in pending):
            # Before any config, C0 included: a campaign whose guarded configs cannot run should
            # say so now and not after C0's hours. C0 alone reranks in the container, needs none.
            self._rerank_model()
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
            self.ctx.out(
                "would do: one embedding call with the EMBEDDING__* settings of .env (the "
                "services' embedder) and refuse unless it returns one "
                f"{stack_env.EMBEDDING_DIMENSION}-dimension vector"
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
        if values.get("LLM__OPENAI_PROVIDER_ROUTING"):
            route = json.loads(values["LLM__OPENAI_PROVIDER_ROUTING"])
            self.ctx.out(f"   route     {stack_env.describe_route(route)}")
        self.ctx.out(f"   {stack_env.describe_embedding(values)}")
        self._check_embedding(model_profiles.with_dot_env(self.ctx.environ, env_file))
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

    def _check_embedding(self, environ: Mapping[str, str]) -> None:
        """One embedding call before any model call (``kit/embedding_check.py``; ADR-0014).

        ``environ`` is what the host processes read (the shell over ``.env``). A refusal stops the
        campaign before the stack is touched; the kit log records the step either way.
        """
        start = _now()
        status, reason, found = "failed", None, None
        try:
            found = check_embedding(environ, transport=self.ctx.embedding_transport)
            status = "ok"
            self.ctx.out(
                f"ok embedding {found.model} at {found.host} returned one {found.dimension}-"
                "dimension vector (one call, before any model call)"
            )
        except EmbeddingCheckError as exc:
            reason = str(exc)
            raise _AbortError(f"the embedding check refused this run: {exc}") from exc
        finally:
            self.log.append(
                {
                    "step": "embedding_check",
                    "model_profile": self.opts.model_profile,
                    "embedding_model": found.model if found else None,
                    "embedding_host": found.host if found else None,
                    "dimension": found.dimension if found else None,
                    "start": start.isoformat(),
                    "end": _now().isoformat(),
                    "status": status,
                    "reason": reason,
                }
            )

    def _rerank_model(self) -> None:
        """The host's copy of the ai-worker image's reranker model, for the guarded configs."""
        if self.dry:
            folder, marker = rerank_copy_paths(self.ctx.repo_root)
            self.ctx.out(
                f"would do: copy the reranker model out of the {RERANK_SERVICE} image to "
                f"{self._shown(folder)}, unless {self._shown(marker)} already names this image"
            )
            self.ctx.out(f"would run: {_join(rerank_copy_command(self.ctx.repo_root))}")
            return
        start = _now()
        status = "failed"
        result: RerankCopy | None = None
        try:
            result = sync_rerank_model(self.ctx)
            status = "ok"
        except RerankModelError as exc:
            raise _AbortError(str(exc)) from exc
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            self.log.append(
                {
                    "step": "rerank_model",
                    "image": result.image if result else None,
                    "copied": result.copied if result else None,
                    "start": start.isoformat(),
                    "end": _now().isoformat(),
                    "status": status,
                }
            )

    def _worker_env(self) -> dict[str, str]:
        """What the guard-worker alone gets: the model folder it reranks from, with no network."""
        folder, _ = rerank_copy_paths(self.ctx.repo_root)
        return {RERANK_MODEL_DIR_ENV: str(folder)}

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
            if exit_code == ROUTE_STOP_EXIT:
                status, reason = "stopped", "the runner's breaker stopped it (STOP)"
            elif exit_code != 0:
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
            if not self.dry:
                self._save_service_logs(config, start)
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
        if status == "stopped":
            raise _AbortError(
                f"{config}: the run stopped (the runner's STOP line above says why: a used-up "
                "quota, balance, spend limit or daily cap of any provider, no credit, the pinned "
                "provider unavailable, or calls served by another provider). The campaign stops "
                "here, before the next config and the retry pass, which would hit the same limit "
                "or route. Fix the cause (restore the quota or credit, which for a daily cap is "
                "the next day, or wait until the provider serves again; never switch providers "
                "inside a RUN), then run the same command again: finished configs are skipped and "
                "the error rows retried. Another model's `make bench-run` (its own RUN) may run "
                "in between"
            )
        return _Outcome(config, status, exit_code, counts)

    def _save_service_logs(self, config: str, since: datetime) -> None:
        """Append the containers' log lines of this config to ``raw/services.<config>.log``.

        A pinned route's served provider is in each call's ``llm_inference`` line: for triage,
        the summarizer and C0's generation that is the only record (guarded rows carry their
        own). A failure here is a warning: the rows are what the report reads.
        """
        path = services_log_path(self.raw_dir, config)
        command = services_log_command(since)
        try:
            done = self.ctx.host.capture(command, cwd=self.ctx.repo_root)
        except OSError as exc:
            self.ctx.err(f"WARN could not save the container logs of {config}: {exc}")
            return
        if done.returncode != 0:
            self.ctx.err(
                f"WARN `{_join(command)}` exited {done.returncode}; the container logs of "
                f"{config} were not saved"
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"=== kit: {_join(command)} at {_now().isoformat()} ===\n")
            handle.write(done.stdout)

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
            self.ctx.out(
                f"would do: start it with {RERANK_MODEL_DIR_ENV}="
                f"{self._worker_env()[RERANK_MODEL_DIR_ENV]} in its own environment only"
            )
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
        proc = self.ctx.host.spawn(
            worker, cwd=self.ctx.repo_root, log_path=log_path, env=self._worker_env()
        )
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
        interrupted = False
        if proc.poll() is None:
            try:
                proc.request_stop(_fresh_pid(pid, stamp))
                proc.wait(self.opts.stop_wait_s)
            except subprocess.TimeoutExpired:
                self.ctx.err(
                    f"WARN guard-worker {config} did not stop within "
                    f"{self.opts.stop_wait_s:g} s and was killed"
                )
                problem = self._kill_worker(config, proc, pid)
                if problem:
                    return problem
            except KeyboardInterrupt:
                # A second Ctrl+C or SIGTERM while the drain is awaited. The worker has its own
                # session, so leaving now would leave it consuming the lane queues with ai-worker
                # stopped: finish the stop the hard way, then let the interrupt through.
                interrupted = True
                self.ctx.err(
                    f"WARN interrupted again: guard-worker {config} is killed, not drained"
                )
                problem = self._kill_worker(config, proc, pid)
                if problem:
                    self.ctx.err(f"FAIL {problem}")
                    raise
        if _fresh_pid(pid, stamp) is not None:
            pid.unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
        if interrupted:
            raise KeyboardInterrupt
        return None

    def _kill_worker(self, config: str, proc: ProcessHandle, pid: Path) -> str | None:
        """Kill the guard-worker; the problem text when it still did not exit."""
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            return (
                f"guard-worker {config} (pid {proc.pid}) did not exit after a stop request and "
                f"a kill; its pid file {pid} is left in place. Stop the process by hand before "
                "any other run: two consumers split the lanes"
            )
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
    slow one last: ``docker compose build`` (labelled with the checkout's commit), ``up -d`` and the
    wait for every container.
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
    # The images carry the commit they were built from (org.opencontainers.image.revision), and
    # `make bench-run` refuses containers that are not this checkout's commit, so the build
    # names it and `up` then starts what was just built.
    head = ctx.host.capture(["git", "rev-parse", "HEAD"], cwd=ctx.repo_root)
    commit = head.stdout.strip()
    if head.returncode != 0 or not commit:
        ctx.err(
            f"FAIL cannot read this checkout's commit (`git rev-parse HEAD` exited "
            f"{head.returncode}); the images must be built from a commit"
        )
        return 1
    build = ["docker", "compose", "build", "--build-arg", f"GIT_COMMIT={commit}"]
    up = ["docker", "compose", "up", "-d"]
    for command in (build, up):
        if ctx.host.run(command, cwd=ctx.repo_root) != 0:
            ctx.err(f"FAIL {_join(command)} failed (output above)")
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
    try:
        sync_rerank_model(ctx)  # the guarded configs' guard-worker reads it, offline
    except RerankModelError as exc:
        ctx.err(f"FAIL {exc}")
        return 1
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
        if name.upper().endswith(SECRET_NAME_SUFFIXES) and len(value.strip()) >= MIN_SECRET_LENGTH
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
    ctx.out(f'  git commit -m "docs(eval): benchmark results, run {run} [task 7.20] [R22.12]"')
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

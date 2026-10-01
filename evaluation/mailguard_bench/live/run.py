"""Live benchmark runner: every rag-email service runs for real (task 7.20; ADR-0011; R22.12).

    python -m evaluation.mailguard_bench.live.run --config C0|C0T|C1|C2|C3|C4|C5|C6|C7 --run RUN \\
        --model-profile M [--scheme v2|v1] [--limit n] [--concurrency 1|2] [--case-timeout-s 300]

    per case:  live_organization ─▶ feeder.feed ─▶ (the running services) ─▶ collector.collect
                 org + MinIO cleanup    KB via API,      triage · lane · ai-worker (C0)     row v3
                 in a finally           e-mail via the   or guard-worker (every guarded config)
                                        mail-connector's hand-off

The runner calls no model. The services do, one benchmarked model per run in every LLM role
(the stack env from ``stack_env.py`` and the guard-worker profile point them at it); the
runner feeds a case, waits for its job and records what the services persisted. Case
selection, the RUN/CONFIG lock, resume with its settings fingerprint, error retry and the
result store are v1's (``runner.py``), so a live run resumes and pairs exactly as a v1 run
does. Rows are ``mailguard-bench-result.v3``, and live runs use their own RUN names and the
fingerprint key ``transport: services-v2``, so they never mix with v1 rows.

The config names have two meanings (``scheme.py``, ADR-0012 decision 11). A run records its scheme
(``--scheme``, default v2) in its meta and fingerprint, refuses a folder that holds the other one,
and in scheme v2 runs every config on all pinned cases; scheme v1 keeps the published meanings and
case selection.

Exactly one drafting consumer must be active, and the runner refuses to start otherwise:
C0 is drafted by the ai-worker container, every guarded config by the guard-worker host process,
which the ai-worker container must then not compete with. The guard a row was drafted under
is described by that guard-worker's own meta (``raw/guard_worker.<config>.meta.json``), which
the runner checks against its own view of the run and refuses on any difference: it never
builds a guard of its own to describe.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import aio_pika
import httpx
from aio_pika.exceptions import ChannelNotFoundEntity

from evaluation.mailguard_bench.case_adapter import EvalCase
from evaluation.mailguard_bench.cases import DEFAULT_CASE_DIR, load_case_set
from evaluation.mailguard_bench.guard_build import NATIVE_CONFIG
from evaluation.mailguard_bench.guard_env import (
    DEFAULT_GUARD_MODEL,
    REPO_ROOT,
    GuardEnvError,
    GuardPaths,
    guard_paths_from_env,
    guard_provider_env,
    require_module_origins,
    require_pinned_worktree,
    sha256_file,
)
from evaluation.mailguard_bench.guarded_reply import GUARDED_PROMPT_VERSION
from evaluation.mailguard_bench.live import process
from evaluation.mailguard_bench.live.cleanup import (
    CleanupOutcome,
    MinioObjectAdmin,
    ObjectStoreAdmin,
    OrganizationPool,
    live_organization,
    organization_buckets,
    purge_stale_live_orgs,
)
from evaluation.mailguard_bench.live.collect import (
    AUDIT_GRACE_S,
    TRANSPORT,
    UNCONSUMED_GRACE_S,
    LiveCollector,
    PipelineJobError,
    PipelineStores,
)
from evaluation.mailguard_bench.live.feeder import (
    ApiFactory,
    CaseTimeoutError,
    CreateMailbox,
    Deadline,
    FedCase,
    HandOff,
    LiveFeeder,
    OrchestratorHandOff,
    http_api_factory,
    mailbox_creator,
)
from evaluation.mailguard_bench.model_profiles import (
    PROFILES,
    ModelProfile,
    get_profile,
    with_dot_env,
)
from evaluation.mailguard_bench.resilience import BackoffPolicy, is_rate_limited
from evaluation.mailguard_bench.results import RESULT_SCHEMA_V3, ResultStore
from evaluation.mailguard_bench.runmeta import keep_recorded_scoring_meta, scoring_meta
from evaluation.mailguard_bench.runner import (
    FINGERPRINT_KEYS,
    RESULTS_ROOT,
    RUN_META_SCHEMA,
    apply_model_profile,
    case_set_names,
    check_resume,
    config_case_ids,
    filter_cases,
    generation_meta,
    meta_path,
    native_guard_facts,
    result_path,
    run_cases,
    settings_fingerprint,
    snapshot_case_set,
    try_acquire_run_lock,
    write_json,
)
from evaluation.mailguard_bench.scheme import (
    DEFAULT_SCHEME,
    SCHEME_KEY,
    SCHEMES,
    require_folder_scheme,
    require_live_config,
)
from packages.broker.publisher import MessagePublisher
from packages.broker.routing import load_categories_from_yaml
from packages.core.settings import (
    AppSettings,
    BrokerSettings,
    DatabaseSettings,
    EmbeddingSettings,
    RetrievalSettings,
    TriageSettings,
)
from packages.core.storage import get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.classification import PostgresClassificationStore
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.db.mailbox import PostgresMailboxStore
from services.mail_connector.orchestrator import SyncOrchestrator

DEFAULT_CASE_TIMEOUT_S = 300.0
CONSUMER_WAIT_S = 60.0
"""How long a guard-worker that has announced itself may take to attach its consumers."""
GUARD_WORKER_MARKER = "guard_worker"
"""What a guard-worker's command line contains (``-m ...live.guard_worker``)."""
GUARD_WORKER_PID_GLOB = "*/raw/guard_worker.*.pid"
"""Where guard-workers announce themselves: ``<results root>/<run>/raw/guard_worker.<cfg>.pid``."""
GUARD_WORKER_META_SCHEMA = "mailguard-guard-worker.v1"
"""The ``schema`` of ``raw/guard_worker.<cfg>.meta.json``, which the guard-worker writes."""

APP_SERVICES = (
    "ai-worker",
    "api",
    "dispatch-worker",
    "email-worker",
    "knowledge-worker",
    "mail-connector",
    "triage-worker",
)
"""The compose services built from the app image; the fingerprint records their image ids."""
NEEDED_SERVICES = ("api", "email-worker", "knowledge-worker", "triage-worker")
"""What every config needs running: the API for the KB upload, and the three pipeline workers
that are not the drafting consumer. The drafting consumer differs by config."""
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMMAND_TIMEOUT_S = 30.0
OLLAMA_TIMEOUT_S = 5.0
KEEP_ALIVE_VARIABLE = "OLLAMA_KEEP_ALIVE"
SYSTEMD_OLLAMA_ENVIRONMENT = ("systemctl", "show", "ollama", "-p", "Environment")
"""The command demo-runbook 9.8 already uses to record the server's state."""

LIVE_ONLY_KEYS = (
    "transport",
    "reranker",
    "triage",
    "guard_llm_stages",
    "service_images",
    "service_revisions",
    "rag_email_dirty",
    "ollama",
)
"""The fingerprint keys a live run adds to v1's; none of them may be None (R22.12)."""
LIVE_FINGERPRINT_KEYS = (*FINGERPRINT_KEYS, *LIVE_ONLY_KEYS)

Connect = Callable[[str], Awaitable[Any]]
CommandRunner = Callable[[Sequence[str]], str]
JsonGetter = Callable[[str], Mapping[str, Any]]


class LiveRunError(RuntimeError):
    """The live run cannot start or continue as configured (printed as ``FAIL ...``)."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--config", required=True, help="a guarded or native config of the scheme")
    parser.add_argument(
        "--scheme",
        choices=SCHEMES,
        default=DEFAULT_SCHEME,
        help="what the config names mean (ADR-0012 decision 11); new runs are v2. A run folder "
        "never mixes the two",
    )
    parser.add_argument("--run", required=True, help="results go to results/mailguard_bench/<run>")
    parser.add_argument(
        "--model-profile",
        required=True,
        choices=sorted(PROFILES),
        help="the one live model of the run, in every LLM role (model_profiles.py)",
    )
    parser.add_argument("--case-dir", type=Path, default=DEFAULT_CASE_DIR)
    parser.add_argument("--limit", type=int, default=None, help="first N selected cases (smoke)")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--case-timeout-s",
        type=float,
        default=DEFAULT_CASE_TIMEOUT_S,
        help="one case's budget for its KB ingestion and its job; an overrun is an error row",
    )
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="run even when a guard stage the preset needs is not live (the report refuses it)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run although a tracked file of this checkout is modified (the meta records it)",
    )
    parser.add_argument(
        "--api-url", default=None, help="the running API (default: FRONTEND__API_BASE_URL)"
    )
    # runner.apply_model_profile points the guard judges at the profile's model through this
    parser.set_defaults(guard_model=DEFAULT_GUARD_MODEL)
    args = parser.parse_args(argv)
    try:
        require_live_config(args.scheme, args.config)
    except ValueError as exc:
        parser.error(str(exc))
    return args


@dataclass(frozen=True)
class GuardWorker:
    """A live guard-worker, as its pid file announces it."""

    run: str
    config: str
    pid: int
    path: Path

    def describe(self) -> str:
        return f"guard-worker {self.config} of run {self.run} (pid {self.pid})"


def is_guard_worker_process(pid: int) -> bool:
    """True when ``pid`` is alive and, where /proc says so, is a guard-worker.

    A pid file outlives a crashed worker, and its pid may since belong to another program;
    the command line tells them apart. Without /proc the liveness check is all there is.
    """
    if not process.process_is_alive(pid):  # never os.kill(pid, 0): that ends a process on Windows
        return False
    try:
        return GUARD_WORKER_MARKER.encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True


def live_guard_workers(results_root: Path) -> list[GuardWorker]:
    """Every live guard-worker of every run under ``results_root``.

    Every run, not only this one: a guard-worker of another RUN consumes the same lane
    queues and would draft this run's e-mails too.
    """
    workers: list[GuardWorker] = []
    for path in sorted(results_root.glob(GUARD_WORKER_PID_GLOB)):
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if not is_guard_worker_process(pid):
            continue
        config = path.name.removeprefix("guard_worker.").removesuffix(".pid")
        workers.append(GuardWorker(run=path.parent.parent.name, config=config, pid=pid, path=path))
    return workers


def drafting_consumer_problems(
    *,
    config: str,
    run_id: str,
    workers: Sequence[GuardWorker],
    consumers: Mapping[str, int | None],
    lane_queues: Sequence[str],
    scheme: str = DEFAULT_SCHEME,
) -> list[str]:
    """Why the stack is not in the state ``config`` needs; empty when it is.

    C0 is drafted by the ai-worker container: every lane queue needs a consumer (a scaled
    ai-worker is still one code path) and no guard-worker may be alive. A guarded config is
    drafted by its own guard-worker alone: one live worker, this run's and this config's,
    and exactly one consumer per lane queue, which rules out the ai-worker container
    competing for the same messages. All problems are returned, so one run of the preflight
    lists everything to fix.
    """
    guarded = config != NATIVE_CONFIG
    ours = [w for w in workers if w.run == run_id and w.config == config]
    problems: list[str] = []
    if guarded and not ours:
        problems.append(
            f"no live guard-worker for {config} in run {run_id}: start `python -m "
            f"evaluation.mailguard_bench.live.guard_worker --config {config} --scheme {scheme} "
            f"--run {run_id} --model-profile ...` and wait until it consumes"
        )
    problems.extend(
        f"{worker.describe()} is alive and would draft {config}'s e-mails; stop it ({worker.path})"
        for worker in workers
        if worker not in ours or not guarded
    )
    for queue in lane_queues:
        count = consumers.get(queue)
        if count is None:
            problems.append(
                f"lane queue {queue} does not exist on the broker (are the services up?)"
            )
        elif count == 0:
            who = (
                "the guard-worker is not consuming it yet"
                if guarded
                else "the ai-worker container is not consuming it"
            )
            problems.append(f"lane queue {queue} has no consumer: {who}")
        elif guarded and count > 1:
            problems.append(
                f"lane queue {queue} has {count} consumers, but only the guard-worker may draft "
                f"{config}: the ai-worker container is still consuming "
                "(docker compose stop ai-worker)"
            )
    return problems


async def probe_consumer_counts(
    broker: BrokerSettings,
    queues: Sequence[str],
    *,
    connect: Connect = aio_pika.connect_robust,
) -> dict[str, int | None]:
    """Consumers per queue, from passive declares; None for a queue that does not exist.

    A passive declare neither creates nor changes a queue (the queue monitor samples the
    same way). The broker closes the channel on a missing queue, so each queue gets its own.
    """
    connection = await connect(broker.url)
    try:
        counts: dict[str, int | None] = {}
        for name in queues:
            channel = await connection.channel()
            try:
                queue = await channel.declare_queue(name, passive=True)
                counts[name] = queue.declaration_result.consumer_count or 0
            except ChannelNotFoundEntity:
                counts[name] = None
            finally:
                if not channel.is_closed:
                    await channel.close()
        return counts
    finally:
        await connection.close()


def run_command(args: Sequence[str]) -> str:
    """Run a command and return its stdout.

    Raises:
        LiveRunError: If the program is missing, times out or exits non-zero.
    """
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False
        )
    except FileNotFoundError as exc:
        raise LiveRunError(f"{args[0]} not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LiveRunError(f"{args[0]} did not answer within {COMMAND_TIMEOUT_S:g} s") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LiveRunError(f"`{' '.join(args[:3])}` failed: {detail}")
    return completed.stdout


def http_get_json(url: str) -> Mapping[str, Any]:
    """GET ``url`` and return its JSON object (raises ``httpx.HTTPError`` or ``ValueError``)."""
    response = httpx.get(url, timeout=OLLAMA_TIMEOUT_S)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{url} did not answer with a JSON object")
    return payload


def triage_facts(triage: TriageSettings, repo_root: Path) -> dict[str, Any]:
    """The live triage's identity: its mode and the hashes of its two artifacts.

    Raises:
        LiveRunError: If an artifact is missing; a fingerprint never records None for it.
    """
    hashes: dict[str, str] = {}
    for key, relative in (
        ("ml_sha256", triage.ml_model_path),
        ("rules_sha256", triage.rules_path),
    ):
        path = repo_root / relative
        if not path.is_file():
            raise LiveRunError(f"triage artifact missing: {path}")
        hashes[key] = sha256_file(path)
    return {"mode": "live", **hashes}


def embedding_facts(embedding: EmbeddingSettings) -> dict[str, Any]:
    """The embedding setup of the run; the host of the base URL, never a key."""
    return {
        "mock": embedding.mock,
        "model": embedding.model_name,
        "dimension": embedding.dimension,
        "base_url_host": urlsplit(embedding.base_url).hostname,
    }


def retrieval_facts(retrieval: RetrievalSettings) -> dict[str, Any]:
    """The retrieval settings of the run (the guard-worker's meta has the same block).

    ``category_filter`` is whether retrieval filters by the category triage assigned
    (``RETRIEVAL__CATEGORY_FILTER_ENABLED``); the benchmark runs with it off, and a run or a
    resume under the other value is another fingerprint.
    """
    return {
        "top_k": retrieval.top_k,
        "top_n": retrieval.top_n,
        "timeout_ms": retrieval.retrieval_timeout_ms,
        "category_filter": retrieval.category_filter_enabled,
    }


def reranker_facts(retrieval: RetrievalSettings) -> dict[str, Any]:
    """Whether the cross-encoder reranks and which model (None on a tree without the setting)."""
    return {
        "enabled": retrieval.rerank_enabled,
        "model": getattr(retrieval, "rerank_model", None),
    }


def service_images(run: CommandRunner, *, config: str) -> dict[str, str]:
    """Image ids of the running app containers, so a run names the exact code it ran.

    Containers are found by their compose service label, not by project or container name,
    so it does not matter which checkout the stack was brought up from.

    Raises:
        LiveRunError: If no compose container runs, a service the config needs is not
            running, or one service runs from two different images (two stacks side by side).
    """
    ids = run(["docker", "ps", "-q", "--filter", f"label={COMPOSE_SERVICE_LABEL}"]).split()
    if not ids:
        raise LiveRunError("no compose containers are running; bring the stack up first")
    label = f'{{{{index .Config.Labels "{COMPOSE_SERVICE_LABEL}"}}}} {{{{.Image}}}}'
    found: dict[str, set[str]] = {}
    for line in run(["docker", "inspect", "--format", label, *ids]).splitlines():
        service, _, image = line.strip().partition(" ")
        if service in APP_SERVICES and image:
            found.setdefault(service, set()).add(image)
    needed = (*NEEDED_SERVICES, *(("ai-worker",) if config == NATIVE_CONFIG else ()))
    missing = [service for service in needed if service not in found]
    if missing:
        raise LiveRunError(f"app container(s) not running: {', '.join(missing)}")
    for service, images in found.items():
        if len(images) > 1:
            raise LiveRunError(
                f"{service} runs from two different images ({', '.join(sorted(images))}): "
                "stop the other stack"
            )
    return {service: next(iter(found[service])) for service in sorted(found)}


IMAGE_REVISION_LABEL = "org.opencontainers.image.revision"
"""The image label the Dockerfile sets from the GIT_COMMIT build argument (``make bench-setup``)."""
NO_REVISION = ("", "unknown", "<no value>")
"""What the label reads for an image built without a commit (the build argument's default)."""


def image_revisions(run: CommandRunner, images: Mapping[str, str]) -> dict[str, str]:
    """The commit each service's image was built from, read from its revision label.

    ``images`` maps a service to its image id (``service_images``). An image with no label reads
    as an empty string.

    Raises:
        LiveRunError: If docker cannot inspect the images or answers for fewer than were asked.
    """
    ids = sorted(set(images.values()))
    template = f'{{{{.Id}}}} {{{{index .Config.Labels "{IMAGE_REVISION_LABEL}"}}}}'
    revision_of: dict[str, str] = {}
    for line in run(["docker", "image", "inspect", "--format", template, *ids]).splitlines():
        image, _, revision = line.strip().partition(" ")
        if image:
            revision_of[image] = revision.strip()
    unread = [service for service, image in images.items() if image not in revision_of]
    if unread:
        raise LiveRunError(f"no revision label could be read for: {', '.join(sorted(unread))}")
    return {service: revision_of[image] for service, image in sorted(images.items())}


def require_current_images(revisions: Mapping[str, str], *, head: str) -> None:
    """Refuse containers that were not built from the commit this checkout is at.

    The containers draft C0 and run triage, knowledge and the API; the runner and the
    guard-worker run this checkout. After a ``git pull`` without a rebuild they would be two
    different programs under the one commit the meta names, and C7 against C0 would compare them.

    Raises:
        LiveRunError: Naming each service whose image is from another commit, or from none.
    """
    stale = {service: rev for service, rev in revisions.items() if rev != head}
    if not stale:
        return
    described = ", ".join(
        f"{service} ({'built without a commit' if rev in NO_REVISION else rev[:12]})"
        for service, rev in sorted(stale.items())
    )
    raise LiveRunError(
        f"container image(s) not built from this checkout's commit {head[:12]}: {described}. "
        "Rebuild them with `make bench-setup` (after every `git pull`); a run under other code "
        "than its meta names would compare different programs"
    )


def checkout_is_dirty(run: CommandRunner, repo_root: Path) -> bool:
    """Whether a tracked file of the checkout is modified (untracked files, such as the run's own
    result folders, are not code and are not counted).

    Raises:
        LiveRunError: If git cannot be run.
    """
    status = run(["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"])
    return bool(status.strip())


def checkout_head(run: CommandRunner, repo_root: Path) -> str:
    """The commit the checkout is at.

    Raises:
        LiveRunError: If git cannot be run or the checkout has no commit.
    """
    head = run(["git", "-C", str(repo_root), "rev-parse", "HEAD"]).strip()
    if not head:
        raise LiveRunError(f"{repo_root} has no commit: a run must name the code it ran")
    return head


def systemd_environment(output: str) -> dict[str, str]:
    """The ``NAME=value`` pairs in the output of ``systemctl show -p Environment``; {} if none.

    The property is one line, ``Environment=A=1 "B=two words"``, in which systemd quotes what
    needs it; ``--value`` output has no ``Environment=`` prefix. A word that is not an
    assignment is skipped, and a line that does not parse holds no environment.
    """
    try:
        words = shlex.split(output.strip().removeprefix("Environment="))
    except ValueError:  # an unbalanced quote
        return {}
    environment: dict[str, str] = {}
    for word in words:
        name, equals, value = word.partition("=")
        if equals and name:
            environment[name] = value
    return environment


def is_local_host(host: str | None) -> bool:
    """Whether ``host`` is this machine: ``localhost`` or an address of one of its interfaces.

    A name is not looked up: a check that decides where a fact is read from must not hang on
    DNS, so a machine's own hostname counts as another machine. An address is local when a
    socket can bind it (which needs no privilege and sends nothing).
    """
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((str(address), 0))
    except OSError:
        return False
    return True


def ollama_keep_alive(*, base_url: str, environ: Mapping[str, str], run: CommandRunner) -> str:
    """The keep-alive the Ollama server at ``base_url`` runs with.

    Ollama has no endpoint for it, so it is read where it is configured. When the server is on
    this machine that is its systemd service, where demo-runbook 9.8 sets it in the unit's
    override: ``systemctl show ollama -p Environment`` gives what the server was started with,
    so the service wins over any declaration. A server that cannot be read that way (another
    machine, the Windows app, one started by hand, a service that keeps it in an
    ``EnvironmentFile``, which that property does not show) has only the operator's word for it:
    ``OLLAMA_KEEP_ALIVE`` in the environment or ``.env``. Nothing is guessed, not even Ollama's
    default, because a fingerprint that records None for it compares equal to nothing.

    Raises:
        LiveRunError: If neither the service nor a declaration states it.
    """
    if is_local_host(urlsplit(base_url).hostname):
        try:
            served = systemd_environment(run(SYSTEMD_OLLAMA_ENVIRONMENT)).get(KEEP_ALIVE_VARIABLE)
        except LiveRunError:
            served = None  # no systemd, or no such unit, on this machine
        if served and served.strip():
            return served.strip()
    declared = (environ.get(KEEP_ALIVE_VARIABLE) or "").strip()
    if declared:
        return declared
    raise LiveRunError(
        f"cannot tell the keep-alive of the Ollama server at {base_url}: this machine's `ollama` "
        f"systemd service does not set {KEEP_ALIVE_VARIABLE} (`systemctl show ollama -p "
        f"Environment`), and it is not declared in the environment or .env either. Declare what "
        f"the server runs with, for example {KEEP_ALIVE_VARIABLE}=30m in .env "
        "(docs/demo-runbook.md 9.8 step 2); the fingerprint records it for every local model"
    )


def ollama_facts(
    profile: ModelProfile,
    *,
    base_url: str,
    environ: Mapping[str, str],
    get_json: JsonGetter,
    run: CommandRunner,
) -> dict[str, Any]:
    """Version, context length and keep-alive of the Ollama serving ``profile``; None for an API.

    The version and the context length come from the server (``/api/version`` and, for the
    model that is loaded, ``/api/ps``): the context length is the one the loaded model really
    runs with, which is what the run needs, not the service's default for a load to come. The
    keep-alive is not exposed by the server, so ``ollama_keep_alive`` finds where it is set.

    Raises:
        LiveRunError: If the server cannot be reached, the model is not loaded (a context
            length exists only for a loaded model, and a fingerprint must not record None), or
            the keep-alive cannot be found.
    """
    if profile.fixed_api_key is None:
        return {"version": None, "context_length": None, "keep_alive": None}
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        version = get_json(f"{root}/api/version").get("version")
        models = get_json(f"{root}/api/ps").get("models") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise LiveRunError(f"cannot reach Ollama at {root}: {exc}") from exc
    loaded = next((m for m in models if profile.model in (m.get("model"), m.get("name"))), None)
    if loaded is None:
        raise LiveRunError(
            f"{profile.model} is not loaded on the Ollama at {root}: load it first, for "
            f'example `ollama run {profile.model} "Reply with OK"`, and keep it loaded '
            f"({KEEP_ALIVE_VARIABLE}); its context length is only known for a loaded model"
        )
    return {
        "version": version,
        "context_length": loaded.get("context_length"),
        "keep_alive": ollama_keep_alive(base_url=base_url, environ=environ, run=run),
    }


@dataclass(frozen=True)
class GuardDescription:
    """What the run records about its guard: the facts, which stages are live, which are not."""

    facts: dict[str, Any]
    live_stages: dict[str, bool]
    missing: list[str]


def guard_worker_meta_path(run_dir: Path, config: str) -> Path:
    """The guard-worker's account of itself: ``raw/guard_worker.<cfg>.meta.json``."""
    return run_dir / "raw" / f"guard_worker.{config}.meta.json"


def guard_worker_expectations(
    args: argparse.Namespace, settings: AppSettings, paths: GuardPaths
) -> dict[str, Any]:
    """What a guard-worker's meta must say for it to be the one that drafts this run's config.

    Its own blocks have the shape of the runner's (``embedding_facts``, ``reranker_facts``,
    ``retrieval_facts``), so the two compare key by key. The AgentMailGuard commit and the L1
    artifact are the pinned ones: one RUN is one environment.
    """
    l1_model = paths.l1_model
    return {
        "run_id": args.run,
        "config": args.config,
        SCHEME_KEY: args.scheme,
        "model_profile": args.model_profile,
        "guard_model": args.guard_model,
        "mailguard_commit": paths.commit,
        "guarded_prompt_version": GUARDED_PROMPT_VERSION,
        "l1_model_sha256": sha256_file(l1_model) if l1_model.exists() else None,
        "embedding": embedding_facts(settings.embedding),
        "reranker": reranker_facts(settings.retrieval),
        "retrieval": retrieval_facts(settings.retrieval),
    }


def read_guard_worker_meta(path: Path) -> dict[str, Any]:
    """The guard-worker's meta, checked for the parts the runner uses.

    Raises:
        LiveRunError: If the file is missing or unreadable, is not a guard-worker's meta, or
            has no guard description.
    """
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LiveRunError(
            f"{path} does not exist: the guard-worker has not written what it is yet"
        ) from exc
    except (OSError, ValueError) as exc:
        raise LiveRunError(f"cannot read {path}: {exc}") from exc
    schema = meta.get("schema") if isinstance(meta, dict) else None
    if schema != GUARD_WORKER_META_SCHEMA:
        raise LiveRunError(f"{path} is not a {GUARD_WORKER_META_SCHEMA} file (schema {schema!r})")
    guard = meta.get("guard")
    if (
        not isinstance(guard, dict)
        or not isinstance(guard.get("live_stages"), dict)
        or not isinstance(guard.get("missing_live_stages"), list)
    ):
        raise LiveRunError(
            f"{path} has no guard description (guard.live_stages and guard.missing_live_stages)"
        )
    return dict(meta)


def guard_worker_mismatches(
    meta: Mapping[str, Any], *, expected: Mapping[str, Any], worker: GuardWorker | None
) -> list[str]:
    """Every way ``meta`` is not the live ``worker``'s account of this run; empty when it is."""
    if worker is None:
        return ["no live guard-worker"]
    problems: list[str] = []
    if meta.get("pid") != worker.pid:
        problems.append(
            f"pid: the meta was written by pid {meta.get('pid')!r}, the live guard-worker is "
            f"pid {worker.pid} (every start rewrites it: is this the meta of an earlier one?)"
        )
    problems.extend(
        f"{key}: the guard-worker has {meta.get(key)!r}, this run needs {want!r}"
        for key, want in expected.items()
        if meta.get(key) != want
    )
    return problems


def describe_guard(
    config: str,
    *,
    meta_file: Path,
    worker: GuardWorker | None,
    expected: Mapping[str, Any],
    paths: GuardPaths,
) -> GuardDescription:
    """The guard of ``config`` as the run's meta describes it: the live guard-worker's own.

    The runner never runs the guard, and the meta must say what ran, because the report names
    "guard stages that did not run" from it. So the description is what the guard-worker
    recorded about itself (``guard_worker.<config>.meta.json``), never a guard the runner
    builds for itself: that one lacks the worker's own choices (C3 turns on L3b's and L4's LLM
    stages) and would describe a weaker guard than the one that drafted, and name the wrong L5
    log. The meta must also be this run's, so it is refused unless the live worker wrote it and
    ``expected`` (the runner's own view) agrees with every fact it states. C0 runs no
    AgentMailGuard code and gets the native facts, as in v1.

    Raises:
        LiveRunError: If there is no live worker, its meta is missing, unreadable, stale or of
            another setup than this runner's (every difference is named).
    """
    if config == NATIVE_CONFIG:
        return GuardDescription(native_guard_facts(paths), {}, [])
    if worker is None:
        raise LiveRunError(f"no live guard-worker for {config}: there is no guard to describe")
    meta = read_guard_worker_meta(meta_file)
    problems = guard_worker_mismatches(meta, expected=expected, worker=worker)
    if problems:
        raise LiveRunError(
            f"the {config} guard-worker (pid {worker.pid}) is not the guard this run needs: "
            + "; ".join(problems)
            + f" (stop it, and start it again with this run's --model-profile and the same "
            f".env; its meta is {meta_file})"
        )
    guard = meta["guard"]
    return GuardDescription(
        facts=dict(guard),
        live_stages={str(stage): bool(live) for stage, live in guard["live_stages"].items()},
        missing=[str(stage) for stage in guard["missing_live_stages"]],
    )


def require_complete_fingerprint(fingerprint: Mapping[str, Any]) -> None:
    """Refuse a fingerprint in which a live fact is None: it would compare equal to nothing.

    Raises:
        LiveRunError: Naming every live key that is None.
    """
    absent = [key for key in LIVE_ONLY_KEYS if fingerprint.get(key) is None]
    if absent:
        raise LiveRunError(f"the run fingerprint has no value for: {', '.join(absent)}")


def build_live_meta(
    *,
    args: argparse.Namespace,
    settings: AppSettings,
    cases_sha256: str,
    guard: GuardDescription,
    rag_email_commit: str | None,
    triage: Mapping[str, Any],
    service_images: Mapping[str, str],
    service_revisions: Mapping[str, str],
    rag_email_dirty: bool,
    ollama: Mapping[str, Any],
) -> dict[str, Any]:
    """The run meta of one invocation, with its settings fingerprint.

    v1's keys, so Task 5 reads a live meta as it reads a v1 one, plus the live facts. The
    fingerprint is what a resume must reproduce and what the report compares across configs.
    The generation, embedding, retrieval and reranker facts are this process's settings: the
    stack env of the containers is rendered from the same profile and must be loaded here too
    (docs/demo-runbook.md), because the runner cannot read a container's environment.
    """
    settings_retrieval = settings.retrieval
    meta: dict[str, Any] = {
        "schema": RUN_META_SCHEMA,
        "run_id": args.run,
        "config": args.config,
        SCHEME_KEY: args.scheme,  # what the config names mean; a folder never mixes the schemes
        "preset": args.config,
        "guard_preset": guard.facts["preset"],  # AgentMailGuard preset; None for native C0
        "case_dir": str(args.case_dir),
        "cases_sha256": cases_sha256,
        "case_sets": list(case_set_names(args.config, args.scheme)),
        "rag_email_commit": rag_email_commit,
        "rag_email_dirty": rag_email_dirty,  # a tracked file was modified: the commit is not it
        "mailguard_commit": guard.facts["mailguard_commit"],
        "guarded_prompt_version": GUARDED_PROMPT_VERSION,  # C0 too: see runner.run
        **generation_meta(settings.llm),
        **scoring_meta(settings.llm.price_table),  # ADR-0012 2(d), 2(e)
        "guard_models": None if args.config == NATIVE_CONFIG else args.guard_model,
        "live_layers": guard.facts["live_layers"],
        "l1_model_sha256": guard.facts["l1_model_sha256"],
        "embedding_mock": settings.embedding.mock,
        "embedding": embedding_facts(settings.embedding),
        "retrieval": retrieval_facts(settings_retrieval),
        "database": settings.database.name,
        "guard": guard.facts,
        "degraded_allowed": bool(guard.missing),
        "transport": TRANSPORT,
        "reranker": reranker_facts(settings_retrieval),
        "triage": dict(triage),
        "guard_llm_stages": dict(guard.live_stages),
        "service_images": dict(service_images),
        "service_revisions": dict(service_revisions),  # the commit each image was built from
        "ollama": dict(ollama),
    }
    meta["fingerprint"] = settings_fingerprint(meta, keys=LIVE_FINGERPRINT_KEYS)
    require_complete_fingerprint(meta["fingerprint"])
    return meta


class Feeder(Protocol):
    """Puts one case into the stack (``LiveFeeder``)."""

    async def feed(
        self, case: EvalCase, *, organization_id: UUID, deadline: Deadline
    ) -> FedCase: ...


class Collector(Protocol):
    """Waits for the case's job and returns the row's ``result`` (``LiveCollector``)."""

    async def collect(self, case: EvalCase, fed: FedCase, deadline: Deadline) -> dict[str, Any]: ...


class LiveCaseExecutor:
    """One case: a throwaway organization, the feed, the collected result, the cleanup.

    The signature is ``run_cases``'s ``CaseExecutor``, so the v1 loop drives it as it drives
    the in-process executors: resume, error retry and the rate-limit back-off are its own.
    """

    def __init__(
        self,
        *,
        pool: OrganizationPool,
        admin: ObjectStoreAdmin,
        buckets: Sequence[str],
        feeder: Feeder,
        collector: Collector,
        label: str,
        case_timeout_s: float,
        on_cleanup: Callable[[CleanupOutcome], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pool = pool
        self.admin = admin
        self.buckets = buckets
        self.feeder = feeder
        self.collector = collector
        self.label = label
        self.case_timeout_s = case_timeout_s
        self.on_cleanup = on_cleanup
        self.monotonic = monotonic

    async def __call__(self, case: EvalCase) -> dict[str, Any]:
        """Run ``case`` and return its ``result``; its MinIO objects and organization go after.

        One budget covers the knowledge base ingestion and the job, so a case never runs
        longer than ``case_timeout_s`` however its time is split.
        """
        deadline = Deadline(self.case_timeout_s, monotonic=self.monotonic)
        async with live_organization(
            self.pool,
            self.admin,
            label=f"{self.label} {case.case_id}",
            buckets=self.buckets,
            on_cleanup=self.on_cleanup,
        ) as organization_id:
            fed = await self.feeder.feed(case, organization_id=organization_id, deadline=deadline)
            return await self.collector.collect(case, fed, deadline)


def case_rate_limited(exc: BaseException) -> bool:
    """Whether ``exc`` is a 429 the runner itself met: the only kind a rerun of the case can help.

    The runner calls no model. A model's 429 reaches it only as text in the last error of a job
    that timed out or failed (``CaseTimeoutError``, ``PipelineJobError``), and the services'
    retry ladder (30 s, 5 m and 30 m tiers) is already retrying that call. Running the case
    again would make a new organization, embed its knowledge base once more and wait the whole
    ``--case-timeout-s`` again, up to six times, and the organization the earlier attempt
    deleted would take the job out from under the ladder. Such a case is one error row, and the
    retry pass (``--retry-errors``) runs it again once the limit has cleared.
    """
    if isinstance(exc, CaseTimeoutError | PipelineJobError):
        return False
    return is_rate_limited(exc)


def audit_log_path(run_dir: Path, config: str) -> Path:
    """Where the guard-worker appends its per-job audit line: ``raw/audit__<config>.jsonl``."""
    return run_dir / "raw" / f"audit__{config}.jsonl"


def resolve_lane_queues(settings: AppSettings) -> list[str]:
    """The lane queues the ai-worker consumes, by the ai-worker's own rule.

    Imported when called: the ai-worker module pulls in the whole generation stack, which a
    runner that calls no model has no other use for.
    """
    from services.ai_worker.main import resolve_lane_queues as resolve

    return resolve(settings)


def require_live_environment(paths: GuardPaths) -> None:
    """The AgentMailGuard worktree is the pinned, clean one, and ``python -m`` put ours first."""
    require_pinned_worktree(paths.root, paths.commit)
    require_module_origins(REPO_ROOT, paths.root)


@dataclass(frozen=True)
class LiveStack:
    """What one run needs from the running stack; opened once, closed at the end."""

    stores: PipelineStores
    hand_off: HandOff
    create_mailbox: CreateMailbox
    api_factory: ApiFactory
    admin: ObjectStoreAdmin
    close: Callable[[], Awaitable[None]]


async def open_live_stack(settings: AppSettings, pool: Any, api_url: str) -> LiveStack:
    """The real collaborators: the mail-connector's orchestrator over its stores and publisher.

    Composed as ``services/mail_connector/main.py`` composes them, minus the consumers: the
    runner only calls ``sync_mailbox`` for the messages it feeds.
    """
    publisher = MessagePublisher(broker_settings=settings.broker, retry_settings=settings.retry)
    await publisher.connect()
    mailboxes = PostgresMailboxStore(pool)
    jobs = PostgresJobStore(pool)
    orchestrator = SyncOrchestrator(
        checkpoint_store=PostgresCheckpointStore(pool),
        storage_client=get_storage_client(settings.object_storage),
        publisher=publisher,
        mailbox_store=mailboxes,
        job_store=jobs,
        settings=settings,
    )
    return LiveStack(
        stores=PipelineStores(
            jobs=jobs,
            classifications=PostgresClassificationStore(pool),
            drafts=PostgresDraftStore(pool),
        ),
        hand_off=OrchestratorHandOff(orchestrator, mailboxes),
        create_mailbox=mailbox_creator(pool),
        api_factory=http_api_factory(api_url),
        admin=MinioObjectAdmin(settings.object_storage),
        close=publisher.close,
    )


@dataclass(frozen=True)
class LiveDeps:
    """Everything ``run`` reaches outside the process through, so a test can stand in for it."""

    open_pool: Callable[[DatabaseSettings], Awaitable[Any]] = create_pool_from_settings
    open_stack: Callable[[AppSettings, Any, str], Awaitable[LiveStack]] = open_live_stack
    probe_consumers: Callable[[BrokerSettings, Sequence[str]], Awaitable[dict[str, int | None]]] = (
        probe_consumer_counts
    )
    resolve_lanes: Callable[[AppSettings], list[str]] = resolve_lane_queues
    describe_guard: Callable[..., GuardDescription] = describe_guard
    require_environment: Callable[[GuardPaths], None] = require_live_environment
    run_command: CommandRunner = run_command
    get_json: JsonGetter = http_get_json
    results_root: Path = RESULTS_ROOT
    repo_root: Path = REPO_ROOT
    poll_interval_s: float = 1.0
    audit_grace_s: float = AUDIT_GRACE_S
    unconsumed_grace_s: float = UNCONSUMED_GRACE_S
    consumer_wait_s: float = CONSUMER_WAIT_S
    backoff: BackoffPolicy = BackoffPolicy()


def guard_worker_is_attaching(
    *,
    config: str,
    run_id: str,
    workers: Sequence[GuardWorker],
    consumers: Mapping[str, int | None],
    lane_queues: Sequence[str],
) -> bool:
    """Whether the only thing missing is this config's guard-worker finishing its start.

    A guard-worker writes its pid file before ``WorkerRuntime`` attaches its consumers, so for a
    moment every lane shows none, and the runbook starts the runner as soon as the file
    appears. That is a start in progress when this run's worker for this config is the only one
    alive and no lane is missing or over-consumed: every lane has none or one consumer, and
    some lane has none. A second consumer (the ai-worker container still running), a queue
    that does not exist and a second worker are for the operator to fix, not to wait out, and
    the ai-worker container does not need this: it reports healthy only once it is consuming.
    """
    if config == NATIVE_CONFIG:
        return False
    ours = [w for w in workers if w.run == run_id and w.config == config]
    if len(ours) != 1 or len(workers) != 1:
        return False
    counts = [consumers.get(queue) for queue in lane_queues]
    return any(count == 0 for count in counts) and all(count in (0, 1) for count in counts)


async def check_drafting_consumers(
    *,
    live: LiveDeps,
    settings: AppSettings,
    config: str,
    run_id: str,
    lanes: Sequence[str],
    scheme: str = DEFAULT_SCHEME,
) -> list[str]:
    """``drafting_consumer_problems``, given the time a starting guard-worker needs.

    The check is repeated every ``poll_interval_s`` while ``guard_worker_is_attaching`` says the
    worker is on its way, for at most ``consumer_wait_s``; anything else is reported at once.
    Nothing is written while it waits.

    Returns:
        The problems of the last look; empty when the stack is in the state ``config`` needs.
    """
    give_up = time.monotonic() + live.consumer_wait_s
    while True:
        workers = live_guard_workers(live.results_root)
        consumers = await live.probe_consumers(settings.broker, lanes)
        problems = drafting_consumer_problems(
            config=config,
            run_id=run_id,
            workers=workers,
            consumers=consumers,
            lane_queues=lanes,
            scheme=scheme,
        )
        if (
            not problems
            or time.monotonic() >= give_up
            or not guard_worker_is_attaching(
                config=config,
                run_id=run_id,
                workers=workers,
                consumers=consumers,
                lane_queues=lanes,
            )
        ):
            return problems
        await asyncio.sleep(live.poll_interval_s)


async def run(args: argparse.Namespace, deps: LiveDeps | None = None) -> int:
    """Run one config of one RUN against the running stack; return the exit code.

    Every fact is read and every refusal made before anything is written or any case is fed:
    the drafting-consumer preflight first (a guard-worker's account of itself is only read once
    it consumes), then the guard's description, the fingerprint and the resume check. Then the
    case folder is pinned, the RUN/CONFIG lock taken, the stale organizations of an earlier
    killed run purged, and the cases run.
    """
    live = deps or LiveDeps()
    # Before any setting is read or file written: a folder holds one config scheme.
    require_folder_scheme(live.results_root / args.run, args.scheme)
    environ = with_dot_env(os.environ)  # the environment over `.env`, as AppSettings reads it
    os.environ.update(apply_model_profile(args, environ))  # before AppSettings
    paths = guard_paths_from_env(os.environ)
    live.require_environment(paths)
    settings = AppSettings()
    llm = settings.llm
    os.environ.update(guard_provider_env(llm.openai_base_url, llm.openai_api_key))
    load_categories_from_yaml(settings.routing.categories_config_path)  # canonical KB categories
    loaded = load_case_set(args.case_dir)
    cases = filter_cases(
        [EvalCase.from_dict(case) for case in loaded.cases.values()],
        case_ids=config_case_ids(loaded.manifest, args.config, args.scheme),
        limit=args.limit,
    )
    run_dir = live.results_root / args.run
    lanes = live.resolve_lanes(settings)
    problems = await check_drafting_consumers(
        live=live,
        settings=settings,
        config=args.config,
        run_id=args.run,
        lanes=lanes,
        scheme=args.scheme,
    )
    if problems:
        for problem in problems:
            print(f"FAIL {args.config}: {problem}", file=sys.stderr)
        return 1

    # Only now: a guard-worker has written its meta before it consumes, so what it says about
    # itself is read once its lanes have a consumer.
    guard = live.describe_guard(
        args.config,
        meta_file=guard_worker_meta_path(run_dir, args.config),
        worker=next(
            (
                w
                for w in live_guard_workers(live.results_root)
                if w.run == args.run and w.config == args.config
            ),
            None,
        ),
        expected={}
        if args.config == NATIVE_CONFIG
        else guard_worker_expectations(args, settings, paths),
        paths=paths,
    )
    if guard.missing and not args.allow_degraded:
        print(
            f"FAIL {args.config}: guard stages not live: {', '.join(guard.missing)} "
            "(run `make mailguard-prep` / check evaluation/mailguard_bench/guard_models.yaml)",
            file=sys.stderr,
        )
        return 1
    # The code that ran must be the code the meta names: the containers are built from a commit
    # and the runner and guard-worker run this checkout, so they must be one commit, unmodified.
    head = checkout_head(live.run_command, live.repo_root)
    dirty = checkout_is_dirty(live.run_command, live.repo_root)
    if dirty and not args.allow_dirty:
        raise LiveRunError(
            "uncommitted changes to tracked files: the meta would name a commit that is not the "
            "code that runs. Commit or stash them (or pass --allow-dirty for a smoke run)"
        )
    images = service_images(live.run_command, config=args.config)
    revisions = image_revisions(live.run_command, images)
    require_current_images(revisions, head=head)
    meta = build_live_meta(
        args=args,
        settings=settings,
        cases_sha256=loaded.manifest["cases_sha256"],
        guard=guard,
        rag_email_commit=head,
        triage=triage_facts(settings.triage, live.repo_root),
        service_images=images,
        service_revisions=revisions,
        rag_email_dirty=dirty,
        ollama=ollama_facts(
            get_profile(args.model_profile),
            base_url=llm.openai_base_url,
            environ=environ,
            get_json=live.get_json,
            run=live.run_command,
        ),
    )
    meta_file = meta_path(run_dir, args.config)
    invocations = check_resume(meta_file, meta["fingerprint"])  # before any write or call
    meta = keep_recorded_scoring_meta(meta, meta_file)  # a resume keeps its first prices and rule

    snapshot_case_set(loaded, run_dir)
    result_path(run_dir, args.config).parent.mkdir(parents=True, exist_ok=True)
    store = ResultStore(result_path(run_dir, args.config))
    pool = await live.open_pool(settings.database)
    # One runner per RUN/CONFIG (v1's lock, on one connection held for the whole run).
    lock_conn = await pool.acquire()
    try:
        if not await try_acquire_run_lock(lock_conn, args.run, args.config):
            print(f"FAIL {args.run}/{args.config} is already running in another process",
                  file=sys.stderr)  # fmt: skip
            return 1
        stack = await live.open_stack(
            settings, pool, args.api_url or settings.frontend.api_base_url
        )
        try:
            buckets = organization_buckets(settings.object_storage)
            stale = await purge_stale_live_orgs(
                pool, stack.admin, scope=f"{args.run}/{args.config}", buckets=buckets
            )
            cleanup_failures: list[str] = []

            def note_cleanup(outcome: CleanupOutcome) -> None:
                if outcome.errors:
                    cleanup_failures.append(
                        f"{outcome.organization_id}: {'; '.join(outcome.errors)}"
                    )

            async def lane_consumers(queue: str) -> int | None:
                return (await live.probe_consumers(settings.broker, [queue]))[queue]

            executor = LiveCaseExecutor(
                pool=pool,
                admin=stack.admin,
                buckets=buckets,
                feeder=LiveFeeder(
                    api_factory=stack.api_factory,
                    hand_off=stack.hand_off,
                    create_mailbox=stack.create_mailbox,
                    poll_interval_s=live.poll_interval_s,
                ),
                collector=LiveCollector(
                    stores=stack.stores,
                    config=args.config,
                    audit_path=(
                        None
                        if args.config == NATIVE_CONFIG
                        else audit_log_path(run_dir, args.config)
                    ),
                    poll_interval_s=live.poll_interval_s,
                    audit_grace_s=live.audit_grace_s,
                    lane_consumers=lane_consumers,
                    claimed_lanes=lanes,
                    unconsumed_grace_s=live.unconsumed_grace_s,
                ),
                label=f"{args.run}/{args.config}",
                case_timeout_s=args.case_timeout_s,
                on_cleanup=note_cleanup,
            )
            invocation: dict[str, Any] = {
                "started_at": datetime.now(UTC).isoformat(),
                "n_cases_selected": len(cases),
                "limit": args.limit,
                "retry_errors": args.retry_errors,
                "concurrency": args.concurrency,
                "case_timeout_s": args.case_timeout_s,
                "purged_stale_orgs": stale.organizations,
                "purged_stale_objects": stale.objects,
                "purge_errors": list(stale.errors),
            }
            invocations.append(invocation)
            meta["invocations"] = invocations  # history: every resume is kept, never overwritten
            write_json(meta_file, meta)

            def progress(record: dict[str, Any]) -> None:
                print(f"{record['status']:5} {record['case_id']} (attempts={record['attempts']})")

            summary = await run_cases(
                cases,
                executor,
                store,
                config_name=args.config,
                run_id=args.run,
                retry_errors=args.retry_errors,
                concurrency=args.concurrency,
                policy=live.backoff,
                rate_limited=case_rate_limited,
                secrets=[llm.openai_api_key],
                on_record=progress,
                schema=RESULT_SCHEMA_V3,
            )
            invocation["finished_at"] = datetime.now(UTC).isoformat()
            invocation["summary"] = {
                "selected": summary.selected,
                "skipped_already_recorded": summary.skipped,
                "ok": summary.ok,
                "error": summary.error,
                "torn_lines_skipped": store.skipped_lines,
                "cleanup_failures": len(cleanup_failures),
            }
            if cleanup_failures:
                invocation["cleanup_failure_details"] = cleanup_failures
            write_json(meta_file, meta)
        finally:
            await stack.close()
    finally:
        await pool.release(lock_conn)
        await pool.close()
    print(
        f"ok {args.config}: {summary.ok} ok, {summary.error} error, "
        f"{summary.skipped} already recorded -> {store.path}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except (ValueError, KeyError, FileNotFoundError, GuardEnvError, LiveRunError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

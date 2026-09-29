"""Evaluation guard-worker: the ai-worker with AgentMailGuard around its drafting step
(task 7.20; ADR-0011; R22.12).

    email.<category>.<priority> ─▶ AIWorkerConsumer            (services/ai_worker, unchanged)
        summary? · retrieval · rerank ─▶ ContextPackage ─▶ GuardedDraftingService
                                                              (live/guarded_drafting.py)
        ─▶ generated_draft, and one audit line per job in <run_dir>/raw/audit__<config>.jsonl

Host process, run like the v1 runner: from the repo root, as a module, so rag-email's `services`
and `evaluation` win over the guard's packages of the same name:

    MAILGUARD_DIR=... MAILGUARD_COMMIT=... MAILGUARD_ARTIFACTS=... \\
      uv run --with-editable ../AgentMailGuard-bench \\
      python -m evaluation.mailguard_bench.live.guard_worker --config C0T|C1|C2|C3 --run RUN \\
        --model-profile M

It applies the model profile (keys from the environment or ``.env``, as the runner does), builds
the guard for the config on the profile's model, and runs ``build_consumers(...,
drafting_factory=...)`` until SIGTERM (WorkerRuntime drains the consumers first). C3 turns
L3b's and L4's LLM stages on; C0T, C1 and C2 keep them off. Only one of these workers, or the
ai-worker container, may consume the lane queues at a time, so it refuses to start while another
guard-worker is alive (pid files under every run folder). Files, all in ``<run_dir>/raw``:

    guard_worker.<config>.pid        this process's pid, while it is alive (the feeder reads it)
    guard_worker.<config>.meta.json  the guard's facts: live layers, L1 hash, guard commit, stages
    audit__<config>.jsonl            one line per job (guarded_drafting.py describes it)
    guard_l5__<config>.jsonl         the guard's own L5 log; kept apart so the file above is
                                     exactly one line per job

The guard's audit is AgentMailGuard's own (ADR-0010); rag-email adds no defence logic here.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.guard_build import BENCH_PRESETS, GuardBuild, build_guard
from evaluation.mailguard_bench.guard_env import (
    DEFAULT_GUARD_MODEL,
    REPO_ROOT,
    GuardEnvError,
    guard_paths_from_env,
    guard_provider_env,
    require_module_origins,
    require_pinned_worktree,
)
from evaluation.mailguard_bench.live.guarded_drafting import GuardedDraftingService
from evaluation.mailguard_bench.model_profiles import PROFILES, resolve_profile, with_dot_env
from evaluation.mailguard_bench.runner import RESULTS_ROOT
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AIWorkerSettings
from packages.db.migrator import verify_database_vector_dimension
from packages.knowledge.embedder import get_embedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm.inference_metrics import start_token_counter_warmup
from services.ai_worker import main as ai_main
from services.ai_worker.consumer import AIWorkerConsumer

SERVICE_NAME = "guard_worker"
DEFAULT_PORT = 8014
"""Health and metrics port; the ai-worker container's 8004 is not published to the host."""
HOST = "127.0.0.1"
FULL_GUARD_CONFIG = "C3"
META_SCHEMA = "mailguard-guard-worker.v1"


def guard_llm_stages(config: str) -> tuple[bool, bool]:
    """``(l3b_llm, l4_llm)``: C3, the full guard, runs both LLM stages; the others keep them off."""
    full = config == FULL_GUARD_CONFIG
    return full, full


def pid_path(run_dir: Path, config: str) -> Path:
    return run_dir / "raw" / f"guard_worker.{config}.pid"


def meta_path(run_dir: Path, config: str) -> Path:
    return run_dir / "raw" / f"guard_worker.{config}.meta.json"


def audit_log_path(run_dir: Path, config: str) -> Path:
    """The per-job audit lines (GuardedDraftingService)."""
    return run_dir / "raw" / f"audit__{config}.jsonl"


def l5_log_path(run_dir: Path, config: str) -> Path:
    """AgentMailGuard's own L5 log, kept apart from the audit file.

    v1 wrote it to ``audit__<config>.jsonl``; here that file holds exactly one line per job,
    so the guard's L5 lines go to their own file.
    """
    return run_dir / "raw" / f"guard_l5__{config}.jsonl"


def _live_guard_worker_pid(path: Path) -> int | None:
    """The pid in a pid file when that process is alive and is a guard-worker, else None.

    A file left by a killed worker, an unreadable one, and one whose pid has since been given to
    another program (its command line lacks ``guard_worker``; zombies have none) are stale.
    Where /proc does not exist the check stops at "the process is alive".
    """
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return None
    except PermissionError:
        pass  # alive, only not ours to signal
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return pid
    return pid if b"guard_worker" in cmdline else None


def require_no_other_guard_worker(results_root: Path) -> None:
    """Fail while a guard-worker of any run is alive: two would split the lane queues.

    Raises:
        GuardEnvError: Naming the pid file of the live worker.
    """
    for path in sorted(results_root.glob("*/raw/guard_worker.*.pid")):
        pid = _live_guard_worker_pid(path)
        if pid is not None and pid != os.getpid():
            raise GuardEnvError(
                f"another guard-worker (pid {pid}, {path}) is alive; it and this one would "
                "split the lane queues. Stop it first (if the file is stale, delete it)"
            )


@contextmanager
def pid_file(path: Path) -> Iterator[None]:
    """``path`` holds this process's pid for as long as the block runs, however it ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def write_meta(path: Path, meta: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")


async def build_guarded_components(
    res: WorkerResources, *, guard: GuardBuild, audit_path: Path
) -> list[StartFn]:
    """``ai_worker.main.build_components`` with GuardedDraftingService as the drafting step.

    The same steps in the same order: refuse to start when EMBEDDING__DIMENSION disagrees with
    VECTOR(n) (R5.10), warm the tokenizer, build every lane consumer, register the HTTP clients'
    shutdown after every consumer's own. The guard's model client is closed with them.
    """
    settings = res.settings
    await verify_database_vector_dimension(
        settings.database.asyncpg_dsn, configured_dimension=settings.embedding.dimension
    )
    embedder = get_embedder(settings.embedding, metrics=res.metrics)
    start_token_counter_warmup()
    counter = await asyncio.to_thread(TokenCounter)
    # Looked up here, not imported, so a test can replace it; typed loosely because the
    # drafting_factory keyword belongs to build_consumers itself (design contract, work package A).
    build_consumers: Callable[..., list[AIWorkerConsumer]] = ai_main.build_consumers
    consumers = build_consumers(
        res,
        token_counter=counter,
        embedder=embedder,
        drafting_factory=functools.partial(
            GuardedDraftingService, guard=guard, audit_path=audit_path
        ),
    )
    provider = consumers[0].drafting.generator.llm_provider if consumers else None
    for aclose in (
        getattr(provider, "aclose", None),
        getattr(embedder, "aclose", None),
        getattr(guard.guard_llm, "aclose", None),
    ):
        if aclose is not None:
            res.shutdown.register_cleanup_callback(aclose)
    return [consumer.start for consumer in consumers]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--config",
        required=True,
        choices=BENCH_PRESETS,
        help="the guarded config this worker drafts for (C0 is the ai-worker container)",
    )
    parser.add_argument(
        "--run", required=True, help="files go to results/mailguard_bench/<run>/raw"
    )
    parser.add_argument(
        "--model-profile",
        choices=sorted(PROFILES),
        default=None,
        help="live model for the generation call, the summarizer and the guard judges",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"health/metrics port on {HOST}"
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    updates, guard_model = resolve_profile(
        args.model_profile, with_dot_env(os.environ), DEFAULT_GUARD_MODEL
    )
    os.environ.update(updates)  # before AIWorkerSettings reads the environment
    paths = guard_paths_from_env(os.environ)
    require_pinned_worktree(paths.root, paths.commit)
    require_module_origins(REPO_ROOT, paths.root)
    require_no_other_guard_worker(RESULTS_ROOT)
    settings = AIWorkerSettings()
    llm = settings.llm
    os.environ.update(guard_provider_env(llm.openai_base_url, llm.openai_api_key))

    run_dir = RESULTS_ROOT / args.run
    l3b_llm, l4_llm = guard_llm_stages(args.config)
    guard = build_guard(
        args.config,
        model_name=guard_model,
        audit_log_path=l5_log_path(run_dir, args.config),
        l1_model_path=paths.l1_model,
        l3b_llm=l3b_llm,
        l4_llm=l4_llm,
    )
    missing = guard.missing_live_stages()
    if missing:
        print(
            f"FAIL {args.config}: guard stages not live: {', '.join(missing)} "
            "(run `make mailguard-prep` / check evaluation/mailguard_bench/guard_models.yaml)",
            file=sys.stderr,
        )
        return 1

    audit_path = audit_log_path(run_dir, args.config)
    meta = {
        "schema": META_SCHEMA,
        "run_id": args.run,
        "config": args.config,
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "model_profile": args.model_profile,
        "guard_model": guard_model,
        "guard_llm_stages": {"l3b_llm": l3b_llm, "l4_llm": l4_llm},
        "guard": guard.describe(),
        "audit_log": str(audit_path),
        "l5_audit_log": str(l5_log_path(run_dir, args.config)),
        "port": args.port,
    }
    with pid_file(pid_path(run_dir, args.config)):
        write_meta(meta_path(run_dir, args.config), meta)
        print(f"ok {args.config} guard-worker pid {os.getpid()}, audit lines -> {audit_path}")
        runtime = WorkerRuntime(
            service_name=SERVICE_NAME,
            settings=settings,
            port=args.port,
            host=HOST,
            build=functools.partial(build_guarded_components, guard=guard, audit_path=audit_path),
        )
        await runtime.run()  # until SIGTERM/SIGINT drains the consumers
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except (ValueError, KeyError, FileNotFoundError, GuardEnvError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

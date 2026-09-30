"""AgentMailGuard benchmark runner hosted by rag-email (task 7.19; spec §4, §4b, §5; ADR-0010).

    make mailguard-bench RUN=<id> CONFIG=C0|C0T|C1|C2|C3|C4|C5|C6|C7 [LIMIT=n]      (scheme v2)
    make mailguard-bench RUN=<id> CONFIG=C0|C3|C0T|C1|C2 SCHEME=v1 [LIMIT=n]
    make mailguard-bench RUN=<id> CONFIG=C3-L1|C3-L2|C3-L3|C3-L3B|C3-L4|C3-L5 SCHEME=v1 [LIMIT=n]

A run has a config scheme (``scheme.py``, ADR-0012 decision 11), recorded in every meta and its
settings fingerprint. New runs are v2 (C0 native, C0T the guard template with no layer, C1 to C6
one layer each with L5, C7 every layer); the descriptions below are scheme v1, the published one,
which stays reproducible with ``--scheme v1`` and keeps its case selection and reports exactly.

Owner-run live evaluation (real Gemini calls). It is never part of ``make ci`` (R24.5).
For each case it opens a throwaway organization, ingests the case KB and builds the real
ContextPackage. Then (owner decision 2026-09-29, plan BINDING section):

- ``C0`` (required): rag-email as it runs, ``SinglePassGenerator.generate_draft`` with its
  own profile template; no AgentMailGuard code runs.
- ``C3`` (required): AgentMailGuard's MailGuardPipeline with every layer, around ONE
  ``generate_from_messages`` call.
- ``C0T`` (the guard template, ``preset("C0")``), ``C1``, ``C2``: the same guarded path with
  fewer layers; C1/C2 run the ablation subset.
- ``C3-L1`` ... ``C3-L5`` (layer ablation, task 7.22): the guard's own "C3 minus one layer"
  preset of the same name, on the full case set exactly like C3.

Rows are appended per case, recorded case ids are skipped on resume, HTTP 429 backs off,
and a failure is an ``error`` row, never a defence.

Run it from the repo root as a module (``python -m``) so rag-email's ``services`` and
``evaluation`` packages win over AgentMailGuard's same-named ones on sys.path.
.env keys used: LLM__PROVIDER, LLM__OPENAI_BASE_URL, LLM__OPENAI_API_KEY (never printed),
LLM__FAST_MODEL, DATABASE__*, EMBEDDING__*, RETRIEVAL__*.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from evaluation.mailguard_bench.case_adapter import (
    EvalCase,
    EvalHost,
    PreparedCase,
    eval_organization,
    purge_stale_eval_orgs,
)
from evaluation.mailguard_bench.cases import (
    DEFAULT_CASE_DIR,
    CaseManifestError,
    LoadedCaseSet,
    canonical_line,
    load_case_set,
)
from evaluation.mailguard_bench.guard_build import (
    ABLATION_CONFIGS,
    BENCH_PRESETS,
    NATIVE_CONFIG,
    GuardBuild,
    build_guard,
    git_head,
    live_guard_llm_stages,
)
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
from evaluation.mailguard_bench.guarded_reply import (
    GUARDED_PROMPT_VERSION,
    CaseExecution,
    GuardedCaseExecutor,
)
from evaluation.mailguard_bench.model_profiles import PROFILES, resolve_profile, with_dot_env
from evaluation.mailguard_bench.native_reply import NativeCaseExecutor
from evaluation.mailguard_bench.resilience import BackoffPolicy, is_rate_limited, redact
from evaluation.mailguard_bench.results import RESULT_SCHEMA, ResultStore
from evaluation.mailguard_bench.runmeta import keep_recorded_scoring_meta, scoring_meta
from evaluation.mailguard_bench.scheme import (
    DEFAULT_SCHEME,
    SCHEME_KEY,
    SCHEME_V1,
    SCHEME_V2,
    SCHEMES,
    require_config,
    require_folder_scheme,
)
from packages.core.settings import AppSettings, LLMTiersSettings
from packages.db.connection import create_pool_from_settings
from packages.knowledge.embedder import get_embedder
from packages.llm.factory import create_llm_provider
from packages.llm.generator import SinglePassGenerator
from packages.llm.profile import AgentProfileRegistry

CaseExecutor = Callable[[EvalCase], Awaitable[dict[str, Any]]]
RateLimitTest = Callable[[BaseException], bool]
Sleep = Callable[[float], Awaitable[None]]
MAX_CONCURRENCY = 2  # spec §5: concurrency 1-2


@dataclass
class RunSummary:
    selected: int
    skipped: int = 0
    ok: int = 0
    error: int = 0


def build_record(
    case: EvalCase,
    *,
    config_name: str,
    run_id: str,
    status: str,
    attempts: int,
    error: dict[str, str] | None = None,
    result: dict[str, Any] | None = None,
    schema: str = RESULT_SCHEMA,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "run_id": run_id,
        "config": config_name,
        "case_id": case.case_id,
        "kind": case.kind,
        "source": case.source,
        "technique": case.technique,
        "vector": case.vector,
        "scenario": case.scenario,
        "status": status,
        "attempts": attempts,
        "error": error,
        "finished_at": datetime.now(UTC).isoformat(),
        "result": result,
    }


def error_kind(exc: BaseException) -> str:
    """The ``error.kind`` of an error row: the exception's own ``error_kind``, else its class name.

    The live runner's fail-closed outcomes carry a kind the report reads by name
    (``fail_closed_validation``); no v1 exception sets one, so v1 rows are unchanged.
    """
    kind = getattr(exc, "error_kind", None)
    return kind if isinstance(kind, str) else type(exc).__name__


async def _run_one(
    case: EvalCase,
    execute: CaseExecutor,
    *,
    config_name: str,
    run_id: str,
    policy: BackoffPolicy,
    sleep: Sleep,
    secrets: Sequence[str | None],
    schema: str,
    rate_limited: RateLimitTest = is_rate_limited,
) -> dict[str, Any]:
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await execute(case)
        except Exception as exc:
            limited = rate_limited(exc)
            if limited and attempt < policy.max_attempts:
                await sleep(policy.delay(attempt))
                continue
            return build_record(
                case,
                config_name=config_name,
                run_id=run_id,
                status="error",
                attempts=attempt,
                error={
                    "kind": "rate_limited" if limited else error_kind(exc),
                    "message": redact(str(exc), secrets)[:2000],
                },
                schema=schema,
            )
        guard_errors = [str(e) for e in result.get("guard_errors") or []]
        if guard_errors:
            return build_record(
                case,
                config_name=config_name,
                run_id=run_id,
                status="error",
                attempts=attempt,
                error={
                    "kind": "guard_layer_error",
                    "message": redact("; ".join(guard_errors), secrets)[:2000],
                },
                result=result,
                schema=schema,
            )
        return build_record(
            case,
            config_name=config_name,
            run_id=run_id,
            status="ok",
            attempts=attempt,
            result=result,
            schema=schema,
        )


async def run_cases(
    cases: Sequence[EvalCase],
    execute: CaseExecutor,
    store: ResultStore,
    *,
    config_name: str,
    run_id: str,
    policy: BackoffPolicy | None = None,
    retry_errors: bool = False,
    concurrency: int = 1,
    sleep: Sleep = asyncio.sleep,
    secrets: Sequence[str | None] = (),
    on_record: Callable[[dict[str, Any]], None] | None = None,
    schema: str = RESULT_SCHEMA,
    rate_limited: RateLimitTest = is_rate_limited,
) -> RunSummary:
    """Run every case not yet recorded; append each row as soon as it exists.

    Args:
        retry_errors: Also re-run cases whose latest row is an ``error`` row.
        concurrency: 1 or 2 cases in flight (spec §5).
        schema: The ``schema`` value of every row (the live runner writes v3 rows).
        rate_limited: Whether an error is an HTTP 429 worth running the case again after a
            back-off. v1 counts any 429 in the error's cause chain; the live runner narrows it,
            because a model's 429 is the services' to retry, not the runner's.

    Raises:
        ValueError: If concurrency is outside 1..2.
    """
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1 or 2 (spec §5), got {concurrency}")
    backoff = policy or BackoffPolicy()
    latest = store.latest_records()
    todo = [
        case
        for case in cases
        if case.case_id not in latest
        or (retry_errors and latest[case.case_id].get("status") == "error")
    ]
    summary = RunSummary(selected=len(cases), skipped=len(cases) - len(todo))
    gate = asyncio.Semaphore(concurrency)

    async def one(case: EvalCase) -> None:
        async with gate:
            record = await _run_one(
                case,
                execute,
                config_name=config_name,
                run_id=run_id,
                policy=backoff,
                sleep=sleep,
                secrets=secrets,
                schema=schema,
                rate_limited=rate_limited,
            )
            store.append(record)
            if record["status"] == "ok":
                summary.ok += 1
            else:
                summary.error += 1
            if on_record is not None:
                on_record(record)

    await asyncio.gather(*(one(case) for case in todo))
    return summary


def filter_cases(
    cases: Sequence[EvalCase], *, case_ids: Sequence[str] | None, limit: int | None
) -> list[EvalCase]:
    """Restrict to an id list (in its order) and/or the first ``limit`` cases.

    Raises:
        ValueError: If an id is not in the case file.
    """
    selected = list(cases)
    if case_ids is not None:
        by_id = {case.case_id: case for case in cases}
        missing = [case_id for case_id in case_ids if case_id not in by_id]
        if missing:
            raise ValueError(f"{len(missing)} case ids not in the case file, first {missing[0]}")
        selected = [by_id[case_id] for case_id in case_ids]
    return selected[:limit] if limit is not None else selected


RESULTS_ROOT = REPO_ROOT / "evaluation" / "results" / "mailguard_bench"
# C0 = native rag-email; C0T/C1/C2/C3 = AgentMailGuard (guard_build.GUARDED_CONFIGS)
BENCH_CONFIGS = (NATIVE_CONFIG, *BENCH_PRESETS)
# Every config the CLI runs: the v1 configs plus the layer ablation (C3 minus one layer).
RUNNABLE_CONFIGS = (*BENCH_CONFIGS, *ABLATION_CONFIGS)  # scheme v1; scheme.configs_for has both
FULL_RUN_SETS = ("llmail_attack", "llmail_benign", "rag_attack")
ABLATION_SETS = ("ablation_attack", "llmail_benign")  # spec §4b D2: C1/C2 subset + same benign
RUN_CASES_SCHEMA = "mailguard-bench-run-cases/v1"


class PreparedCaseExecutor(Protocol):
    """One prepared case to one result: NativeCaseExecutor (C0) or GuardedCaseExecutor."""

    async def execute(self, prepared: PreparedCase) -> CaseExecution: ...


def prepared_case_executor(
    config_name: str, *, generator: SinglePassGenerator, guard: Any | None
) -> PreparedCaseExecutor:
    """C0 gets rag-email's native path; every other config its AgentMailGuard pipeline.

    Raises:
        ValueError: If C0 is given a guard, or a guarded config none.
    """
    if config_name == NATIVE_CONFIG:
        if guard is not None:
            raise ValueError("C0 is rag-email's native path and runs no guard")
        return NativeCaseExecutor(generator=generator)
    if guard is None:
        raise ValueError(f"{config_name} needs a guard (build_guard)")
    return GuardedCaseExecutor(
        pipeline=guard.pipeline, generator=generator, guard_llm=guard.guard_llm
    )


def native_guard_facts(paths: GuardPaths) -> dict[str, Any]:
    """The ``guard`` meta of a C0 run: no layer, but the same pinned environment facts.

    C0 imports no AgentMailGuard code. It still records the worktree commit and the L1
    artifact hash the guarded configs record, because the report requires every compared
    config to share them (one RUN = one environment).
    """
    l1_path = paths.l1_model
    return {
        "config": NATIVE_CONFIG,
        "preset": None,
        "active_layers": [],
        "guard_model": None,
        "live_stages": {},
        "missing_live_stages": [],
        "live_layers": None,
        "l1_model_path": str(l1_path),
        "l1_model_sha256": sha256_file(l1_path) if l1_path.exists() else None,
        "mailguard_root": str(paths.root),
        "mailguard_commit": git_head(paths.root),
        "audit_log_path": None,
    }


class HostCaseExecutor:
    """Throwaway org → KB ingestion → real ContextPackage → native or guarded generation."""

    def __init__(self, *, host: EvalHost, executor: PreparedCaseExecutor, label: str) -> None:
        self.host = host
        self.executor = executor
        self.label = label

    async def __call__(self, case: EvalCase) -> dict[str, Any]:
        async with eval_organization(self.host.pool, label=f"{self.label} {case.case_id}") as org:
            prepared = await self.host.prepare(case, organization_id=org)
            execution = await self.executor.execute(prepared)
        return {
            "host": prepared.diagnostics(),
            **execution.record,
            "guard_errors": list(execution.guard_errors),
        }


def case_set_names(config_name: str, scheme: str = SCHEME_V1) -> tuple[str, ...]:
    """The pinned case sets one config runs: v1 C1 and C2 the ablation subset, everything else all.

    Scheme v2 runs every config on every pinned case (550), so the configs pair on the same ids.
    """
    reduced = scheme == SCHEME_V1 and config_name in ("C1", "C2")
    return ABLATION_SETS if reduced else FULL_RUN_SETS


def config_case_ids(
    manifest: Mapping[str, Any], config_name: str, scheme: str = SCHEME_V1
) -> list[str]:
    """Case ids one config runs.

    Scheme v1: C0/C0T/C3 and C3-L* every pinned case, C1/C2 the subset. Scheme v2: every config
    every pinned case.
    """
    sets: Mapping[str, list[str]] = manifest["sets"]
    seen: set[str] = set()
    ids: list[str] = []
    for name in case_set_names(config_name, scheme):
        for case_id in sets[name]:
            if case_id not in seen:
                seen.add(case_id)
                ids.append(case_id)
    return ids


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def snapshot_case_set(loaded: LoadedCaseSet, run_dir: Path) -> Path:
    """Pin a run folder to one case set (for the report and for McNemar pairing).

    The first run into ``run_dir`` writes ``cases.jsonl`` and ``case_manifest.json`` with the
    keys Task 5 reads. Every later run into the same folder must bring the same case set.

    Raises:
        CaseManifestError: If ``run_dir`` was started on a different case set.
    """
    sets = loaded.manifest["sets"]
    wanted = {
        "schema": RUN_CASES_SCHEMA,
        "seed": loaded.manifest["seed"],
        "cases_sha256": loaded.manifest["cases_sha256"],
        "llmail_attack_ids": list(sets["llmail_attack"]),
        "benign_ids": list(sets["llmail_benign"]),
        "rag_attack_ids": list(sets["rag_attack"]),
        "ablation_attack_ids": list(sets["ablation_attack"]),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "case_manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != wanted:
            raise CaseManifestError(
                f"{run_dir} was started on another case set (cases_sha256 "
                f"{existing.get('cases_sha256')} != {wanted['cases_sha256']}); use a new RUN "
                "so that C0 and C3 are paired on the same cases"
            )
    else:
        write_json(path, wanted)
    body = "".join(canonical_line(loaded.cases[cid]) + "\n" for cid in sorted(loaded.cases))
    (run_dir / "cases.jsonl").write_text(body, encoding="utf-8")
    return path


def result_path(run_dir: Path, config_name: str) -> Path:
    """raw/<CONFIG>.jsonl, the per-case rows Task 5 scores."""
    return run_dir / "raw" / f"{config_name}.jsonl"


def meta_path(run_dir: Path, config_name: str) -> Path:
    """raw/<CONFIG>.meta.json, the run facts Task 5 checks and copies into manifest.json."""
    return run_dir / "raw" / f"{config_name}.meta.json"


RUN_META_SCHEMA = "mailguard-bench-run.v2"
# What must not change between the invocations that fill one raw/<CONFIG>.jsonl (a resume),
# and what Task 5 compares across C0/C3/C1/C2 (spec Q6: "same model, same settings").
FINGERPRINT_KEYS = (
    "preset",
    "scheme",
    "cases_sha256",
    "rag_email_commit",
    "mailguard_commit",
    "guarded_prompt_version",
    "generation_model",
    "generation",
    "guard_models",
    "live_layers",
    "l1_model_sha256",
    "embedding",
    "retrieval",
    "database",
    "degraded_allowed",
)


class RunSettingsMismatchError(ValueError):
    """A resume would mix rows produced under different settings into one config."""


class SupportsFetchval(Protocol):
    """The one asyncpg connection method the run lock needs."""

    async def fetchval(self, query: str, *args: Any) -> Any: ...


def run_lock_key(run_id: str, config_name: str) -> str:
    """The advisory-lock name of one RUN/CONFIG, the same for the v1 and the live runner."""
    return f"mailguard-bench {run_id}/{config_name}"


async def try_acquire_run_lock(conn: SupportsFetchval, run_id: str, config_name: str) -> bool:
    """Take the session lock of one RUN/CONFIG on ``conn``; False when another runner holds it.

    The lock lives as long as ``conn`` does, so the caller keeps one connection for the run.
    """
    return bool(
        await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", run_lock_key(run_id, config_name)
        )
    )


def settings_fingerprint(
    meta: Mapping[str, Any], keys: Sequence[str] = FINGERPRINT_KEYS
) -> dict[str, Any]:
    """The settings of one invocation that every row of a config must share.

    Args:
        keys: The meta keys that make up the fingerprint (the live runner adds its own).

    Returned in its JSON form (tuples become lists), the form the meta file stores, so a
    resume compares like with like.
    """
    fingerprint: dict[str, Any] = json.loads(
        json.dumps({key: meta.get(key) for key in keys}, default=str)
    )
    return fingerprint


def check_resume(meta_file: Path, fingerprint: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Earlier invocations of this RUN/CONFIG, after checking they used the same settings.

    Raises:
        RunSettingsMismatchError: If ``meta_file`` records other settings (a re-pin, a new
            rag-email commit, another model, timeout, tier mapping or guard model).
    """
    if not meta_file.exists():
        return []
    existing = json.loads(meta_file.read_text(encoding="utf-8"))
    old = existing.get("fingerprint")
    if not isinstance(old, Mapping):
        raise RunSettingsMismatchError(
            f"{meta_file} has no settings fingerprint (written by an older runner); use a new RUN"
        )
    recorded = {SCHEME_KEY: SCHEME_V1, **old}  # a meta written before the schemes existed is v1
    changed = sorted(
        key for key in set(recorded) | set(fingerprint) if recorded.get(key) != fingerprint.get(key)
    )
    if changed:
        raise RunSettingsMismatchError(
            f"{meta_file.name} was started with other settings ({', '.join(changed)} changed); "
            "resuming would mix rows from both settings. Restore the settings, or use a new RUN "
            "and run every config again"
        )
    return list(existing.get("invocations") or [])


def generation_meta(llm: LLMTiersSettings) -> dict[str, Any]:
    """The ``generation_model`` and ``generation`` blocks of the run meta.

    packages/llm/factory.py maps every tier to ``strong_model`` under ``force_single_tier``;
    the routine/fast tier is what the reply profile uses. Task 5 cross-checks the model
    against the one each row's generation call actually recorded.
    """
    model_map = (
        dict.fromkeys(("fast", "routine", "strong", "fallback"), llm.strong_model)
        if llm.force_single_tier
        else {
            "fast": llm.fast_model,
            "routine": llm.fast_model,
            "strong": llm.strong_model,
            "fallback": llm.fallback_model,
        }
    )
    return {
        "generation_model": model_map["routine"],
        "generation": {
            "provider": llm.provider,
            "base_url": llm.openai_base_url,
            "model": model_map["routine"],
            "model_map": model_map,
            "force_single_tier": llm.force_single_tier,
            "timeout_s": llm.timeout_s,
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--config", required=True, help="a config of the scheme (scheme.configs_for)"
    )
    parser.add_argument(
        "--scheme",
        choices=SCHEMES,
        default=DEFAULT_SCHEME,
        help="what the config names mean (ADR-0012 decision 11); new runs are v2, the published "
        "runs v1. A run folder never mixes the two",
    )
    parser.add_argument("--run", required=True, help="results go to results/mailguard_bench/<run>")
    parser.add_argument("--case-dir", type=Path, default=DEFAULT_CASE_DIR)
    parser.add_argument("--limit", type=int, default=None, help="first N selected cases (smoke)")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--llm-timeout-s", type=float, default=None)
    parser.add_argument("--guard-model", default=DEFAULT_GUARD_MODEL)
    parser.add_argument(
        "--model-profile",
        choices=sorted(PROFILES),
        default=None,
        help="live model for BOTH the generation call and the guard judges (model_profiles.py)",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="run even when a guard stage the preset needs is not live (the report refuses it)",
    )
    args = parser.parse_args(argv)
    try:
        require_config(args.scheme, args.config)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def apply_model_profile(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, str]:
    """Settings for the chosen model profile; also points the guard judges at that model."""
    updates, args.guard_model = resolve_profile(args.model_profile, environ, args.guard_model)
    return updates


def build_run_meta(
    *,
    args: argparse.Namespace,
    cases_sha256: str,
    llm: LLMTiersSettings,
    settings: AppSettings,
    guard_facts: Mapping[str, Any],
    missing: Sequence[str],
    rag_email_commit: str | None,
) -> dict[str, Any]:
    """The run meta of one invocation of the in-process runner, with its settings fingerprint."""
    meta: dict[str, Any] = {
        "schema": RUN_META_SCHEMA,
        "run_id": args.run,
        "config": args.config,
        SCHEME_KEY: args.scheme,  # what the config names mean; a folder never mixes the schemes
        "preset": args.config,
        "guard_preset": guard_facts["preset"],  # AgentMailGuard preset; None for native C0
        "case_dir": str(args.case_dir),
        "cases_sha256": cases_sha256,
        "case_sets": list(case_set_names(args.config, args.scheme)),
        "rag_email_commit": rag_email_commit,
        "mailguard_commit": guard_facts["mailguard_commit"],
        # every config of a run records it, C0 too (it runs no guard prompt): the configs of one
        # run share the harness that built them, and a guarded.v1 row never meets a v2 one
        "guarded_prompt_version": GUARDED_PROMPT_VERSION,
        **generation_meta(llm),
        **scoring_meta(llm.price_table),  # ADR-0012 2(d), 2(e): the report reads them back
        "guard_models": None if args.config == NATIVE_CONFIG else args.guard_model,
        "live_layers": guard_facts["live_layers"],
        "l1_model_sha256": guard_facts["l1_model_sha256"],
        "embedding_mock": settings.embedding.mock,
        "embedding": {"mock": settings.embedding.mock, "model": settings.embedding.model_name},
        "retrieval": {
            "top_k": settings.retrieval.top_k,
            "top_n": settings.retrieval.top_n,
            "timeout_ms": settings.retrieval.retrieval_timeout_ms,
        },
        "database": settings.database.name,
        "guard": dict(guard_facts),
        "degraded_allowed": bool(missing),
    }
    meta["fingerprint"] = settings_fingerprint(meta)
    return meta


async def run(args: argparse.Namespace) -> int:
    run_dir = RESULTS_ROOT / args.run
    # Before any setting is read or file written: a folder holds one config scheme.
    require_folder_scheme(run_dir, args.scheme)
    os.environ.update(apply_model_profile(args, with_dot_env(os.environ)))  # before AppSettings
    paths = guard_paths_from_env(os.environ)
    require_pinned_worktree(paths.root, paths.commit)
    require_module_origins(REPO_ROOT, paths.root)
    settings = AppSettings()
    llm = (
        settings.llm
        if args.llm_timeout_s is None
        else settings.llm.model_copy(update={"timeout_s": args.llm_timeout_s})
    )
    os.environ.update(guard_provider_env(llm.openai_base_url, llm.openai_api_key))
    loaded = load_case_set(args.case_dir)
    cases = filter_cases(
        [EvalCase.from_dict(case) for case in loaded.cases.values()],
        case_ids=config_case_ids(loaded.manifest, args.config, args.scheme),
        limit=args.limit,
    )
    guard: GuardBuild | None = None
    missing: list[str] = []
    if args.config == NATIVE_CONFIG:
        guard_facts = native_guard_facts(paths)
    else:
        # v1 keeps L3b's and L4's LLM stages off in-process (task 7.19); a v2 config runs the AI
        # stage of each layer it lists, in this runner as in the live one.
        l3b_llm, l4_llm = (
            live_guard_llm_stages(args.config, SCHEME_V2)
            if args.scheme == SCHEME_V2
            else (False, False)
        )
        guard = build_guard(
            args.config,
            model_name=args.guard_model,
            audit_log_path=run_dir / "raw" / f"audit__{args.config}.jsonl",
            l1_model_path=paths.l1_model,
            l3b_llm=l3b_llm,
            l4_llm=l4_llm,
            scheme=args.scheme,
        )
        missing = guard.missing_live_stages()
        guard_facts = guard.describe()
    if missing and not args.allow_degraded:
        print(
            f"FAIL {args.config}: guard stages not live: {', '.join(missing)} "
            "(run `make mailguard-prep` / check evaluation/mailguard_bench/guard_models.yaml)",
            file=sys.stderr,
        )
        return 1

    meta = build_run_meta(
        args=args,
        cases_sha256=loaded.manifest["cases_sha256"],
        llm=llm,
        settings=settings,
        guard_facts=guard_facts,
        missing=missing,
        rag_email_commit=git_head(REPO_ROOT),
    )
    meta_file = meta_path(run_dir, args.config)
    invocations = check_resume(meta_file, meta["fingerprint"])  # before any write or call
    meta = keep_recorded_scoring_meta(meta, meta_file)  # a resume keeps its first prices and rule

    snapshot_case_set(loaded, run_dir)
    result_path(run_dir, args.config).parent.mkdir(parents=True, exist_ok=True)
    store = ResultStore(result_path(run_dir, args.config))
    pool = await create_pool_from_settings(settings.database)
    lock_key = run_lock_key(args.run, args.config)
    # One runner per RUN/CONFIG: a second terminal on the same config would share its orgs
    # and rows. The session lock lives on a connection held for the whole run (an idle pool
    # connection could be recycled, silently dropping it); asyncpg's release resets the
    # connection with pg_advisory_unlock_all(). Another config's runner holds another key.
    lock_conn = await pool.acquire()
    try:
        if not await try_acquire_run_lock(lock_conn, args.run, args.config):
            print(f"FAIL {lock_key} is already running in another process", file=sys.stderr)
            return 1
        purged = await purge_stale_eval_orgs(pool, scope=f"{args.run}/{args.config}")
        registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
        generator = SinglePassGenerator(
            llm_provider=create_llm_provider(llm),
            profile_registry=registry,
            price_table=llm.price_table,
        )
        host = EvalHost.create(
            settings,
            pool=pool,
            embedder=get_embedder(settings.embedding),
            profile_registry=registry,
        )
        executor = HostCaseExecutor(
            host=host,
            executor=prepared_case_executor(args.config, generator=generator, guard=guard),
            label=f"{args.run}/{args.config}",
        )
        invocation: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "n_cases_selected": len(cases),
            "limit": args.limit,
            "retry_errors": args.retry_errors,
            "concurrency": args.concurrency,
            "purged_stale_orgs": purged,
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
            policy=BackoffPolicy(max_attempts=args.max_attempts),
            retry_errors=args.retry_errors,
            concurrency=args.concurrency,
            secrets=[llm.openai_api_key],
            on_record=progress,
        )
        invocation["finished_at"] = datetime.now(UTC).isoformat()
        invocation["summary"] = {
            "selected": summary.selected,
            "skipped_already_recorded": summary.skipped,
            "ok": summary.ok,
            "error": summary.error,
            "torn_lines_skipped": store.skipped_lines,
        }
        write_json(meta_file, meta)
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
    except (ValueError, KeyError, FileNotFoundError, GuardEnvError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
